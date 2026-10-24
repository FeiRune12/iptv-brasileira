#!/usr/bin/env python3
"""Generate an Azure TV preview changing ONLY URLs of unambiguously matched live channels.

The live R2 playlist stays completely untouched. No title, group, order, series URL,
movie URL, metadata, header, or line break is rewritten.
"""
from collections import Counter, defaultdict
from hashlib import sha256
import io
import json
import os
from pathlib import Path
import re
import tempfile
import time
import unicodedata
from urllib import request, parse, error

REPO = 'Ramys/Iptv-Brasil-2026'
API = 'https://api.github.com/repos/' + REPO
WORKER = 'https://azure-tv-preview.giovannisilvaalves604.workers.dev'
USER_AGENT = 'AzureTV-PreservedFavoritesPreview/1.0'
SOURCE = re.compile(r'^CanaisBR\d{2}\.m3u8$', re.I)
GROUP_RE = re.compile(r'group-title="([^"]*)"', re.I)
NO_VOD = re.compile(r'\b(film(?:e|es)?|series?|animes?|novelas?|desenhos?|movies?|temporadas?|episodes?|vod|cinema)\b', re.I)
YES_TV = re.compile(r'\b(canais?|tv|televisao|ao vivo|noticias?|esportes?|abertos?|fechados?|news|sports|internacionais?)\b', re.I)
CHUNK = 8 * 1024 * 1024
TIMEOUT = 180


def key(value):
    value = unicodedata.normalize('NFKD', value.casefold())
    value = ''.join(c for c in value if not unicodedata.combining(c))
    return ' '.join(value.split())


def metadata(raw):
    value = raw.decode('utf-8', errors='replace').rstrip('\r\n')
    match = GROUP_RE.search(value)
    group = match.group(1) if match else ''
    title = value.rsplit(',', 1)[-1].strip()
    return key(title), key(group)


def is_tv(group):
    if NO_VOD.search(group):
        return False
    return not group or bool(YES_TV.search(group))


def clean_uri(uri):
    try:
        parsed = parse.urlsplit(uri)
        return parsed.scheme in ('http', 'https') and bool(parsed.hostname) and len(uri) < 8192
    except ValueError:
        return False


def repo_request(url):
    return request.Request(url, headers={'User-Agent': USER_AGENT, 'Accept': 'application/vnd.github+json'})


def remote_json(url):
    with request.urlopen(repo_request(url), timeout=TIMEOUT) as r:
        return json.load(r)


class PreviewGateway:
    def __init__(self):
        self.token = ''
        self.expires = 0

    def bearer(self):
        if self.token and time.time() < self.expires:
            return self.token
        token_url = os.environ['ACTIONS_ID_TOKEN_REQUEST_URL']
        separator = '&' if '?' in token_url else '?'
        token_req = request.Request(token_url + separator + 'audience=azure-tv-sync-v1',
            headers={'Authorization': 'Bearer ' + os.environ['ACTIONS_ID_TOKEN_REQUEST_TOKEN'],
                     'User-Agent': USER_AGENT})
        with request.urlopen(token_req, timeout=30) as result:
            self.token = json.load(result)['value']
        self.expires = time.time() + 60
        return self.token

    def call(self, route, data=None, binary=False, timeout=TIMEOUT):
        headers = {'Authorization': 'Bearer ' + self.bearer(), 'User-Agent': USER_AGENT}
        if binary:
            data_bytes = data
            headers['Content-Type'] = 'application/octet-stream'
        elif data is None:
            data_bytes = None
        else:
            data_bytes = json.dumps(data, separators=(',', ':')).encode()
            headers['Content-Type'] = 'application/json'
        req = request.Request(WORKER + route, data=data_bytes, headers=headers,
            method='POST' if data_bytes is not None else 'GET')
        try:
            with request.urlopen(req, timeout=timeout) as res:
                return json.load(res)
        except error.HTTPError as exc:
            raise RuntimeError('Azure TV preview gateway HTTP ' + str(exc.code) + ': ' + exc.read(300).decode(errors='replace')) from exc

    def download_original(self, filename):
        req = request.Request(WORKER+'/original',headers={
            'Authorization': 'Bearer '+self.bearer(), 'User-Agent':USER_AGENT})
        with request.urlopen(req, timeout=TIMEOUT) as response:
            size = int(response.headers['X-Original-Size'])
            etag = response.headers['X-Original-Etag']
            with filename.open('wb') as file:
                while part := response.read(CHUNK):
                    file.write(part)
        if size < 10_000_000 or filename.stat().st_size != size:
            raise RuntimeError('Original download incomplete, not creating preview')
        print('Original safely downloaded:', size, 'bytes', flush=True)
        return size, etag

    def upload(self, filename, meta):
        state=self.call('/start', {})
        upload_id=state['uploadId']
        if state['originalBytes'] != meta['originalBytes'] or state['originalEtag'] != meta['originalEtag']:
            raise RuntimeError('Original changed between download and upload')
        parts=[]
        try:
            with filename.open('rb') as fp:
                while block := fp.read(CHUNK):
                    number=len(parts)+1
                    q='?'+parse.urlencode({'uploadId':upload_id,'number':number})
                    part=self.call('/part'+q,block,binary=True)
                    parts.append({'partNumber':part['partNumber'],'etag':part['etag']})
            if len(parts)<2:
                raise RuntimeError('Suspicious one-part preview')
            result=self.call('/finish',{'uploadId':upload_id,'parts':parts,'meta':meta})
            if not result.get('ok') or result.get('previewBytes')!=filename.stat().st_size:
                raise RuntimeError('Preview verification failed')
            print('PREVIEW READY:',json.dumps(result),flush=True)
        except Exception:
            try:
                self.call('/abort',{'uploadId':upload_id})
            except Exception:
                pass
            raise


def latest_candidates():
    ref=remote_json(API+'/git/ref/heads/master')['object']['sha']
    tree=remote_json(API+'/git/trees/'+ref+'?recursive=1')
    if tree.get('truncated'):
        raise RuntimeError('Upstream tree truncated')
    names=sorted(row['path'] for row in tree['tree'] if row['type']=='blob'
                 and '/' not in row['path'] and SOURCE.fullmatch(row['path']))
    if not {'CanaisBR01.m3u8','CanaisBR02.m3u8'}<=set(names):
        raise RuntimeError('Upstream core files missing')
    # Exact normalized channel title -> set of available source URLs.
    candidates=defaultdict(set)
    counts=Counter()
    for name in names:
        url='https://raw.githubusercontent.com/'+REPO+'/'+ref+'/'+parse.quote(name)
        print('Reading source:',name,flush=True)
        with request.urlopen(repo_request(url),timeout=TIMEOUT) as source:
            reader=io.TextIOWrapper(source,encoding='utf-8-sig',errors='replace',newline=None)
            pending=None
            for raw in reader:
                line=raw.rstrip('\r\n')
                if line.startswith('#EXTINF:'):
                    pending,group=metadata(line.encode('utf-8'))
                elif pending and line.startswith('#'):
                    continue
                elif pending and line.strip():
                    value=line.strip()
                    if clean_uri(value):
                        candidates[pending].add(value)
                        counts['sourceStreams']+=1
                    pending=None
    print('Upstream revision:',ref,'files:',len(names),'distinct titles:',len(candidates),
          'streams:',counts['sourceStreams'],flush=True)
    return ref,candidates


def choose(title, old_url, candidates):
    options=candidates.get(title)
    if not options:
        return None,'no_match'
    if old_url in options:
        return None,'unchanged'
    if len(options)==1:
        return next(iter(options)),'unique'
    # If providers conflict, permit an update only when exactly one option
    # has the same hostname as the old URL. Otherwise preserve the original.
    try:
        old_host=parse.urlsplit(old_url).hostname
        host_matches=[u for u in options if parse.urlsplit(u).hostname==old_host]
    except ValueError:
        return None,'ambiguous'
    if len(host_matches)==1:
        return host_matches[0],'same_host'
    return None,'ambiguous'


def structural_signature(filename):
    """Mask live URLs, hash everything else including VOD URLs and all metadata."""
    hasher=sha256()
    count=Counter()
    pending=None
    with filename.open('rb') as stream:
        for raw in stream:
            line=raw.rstrip(b'\r\n')
            if line.startswith(b'#EXTINF:'):
                pending=metadata(line)
                count['entries']+=1
                hasher.update(raw)
            elif pending is not None and line and not line.startswith(b'#'):
                title,group=pending
                if is_tv(group) and clean_uri(line.decode('utf-8',errors='replace')):
                    hasher.update(b'__AZURE_TV_URL_PLACEHOLDER__\n')
                    count['tv']+=1
                else:
                    hasher.update(raw)
                    count['nonTv']+=1
                pending=None
            else:
                hasher.update(raw)
    return hasher.hexdigest(),count


def transform(original,preview,candidates):
    summary=Counter()
    pending=None
    with original.open('rb') as source, preview.open('wb') as output:
        for raw in source:
            line=raw.rstrip(b'\r\n')
            if line.startswith(b'#EXTINF:'):
                pending=metadata(line)
                output.write(raw)
                continue
            if pending is not None and line and not line.startswith(b'#'):
                title,group=pending
                old=line.decode('utf-8',errors='replace')
                if is_tv(group) and clean_uri(old):
                    new,reason=choose(title,old,candidates)
                    summary[reason]+=1
                    if new and new!=old:
                        ending=raw[len(line):]  # Preserve CRLF vs LF verbatim.
                        output.write(new.encode('utf-8')+ending)
                        summary['urlsChanged']+=1
                    else:
                        output.write(raw)
                    summary['tvEntries']+=1
                else:
                    summary['protectedVOD']+=1
                    output.write(raw)
                pending=None
                continue
            output.write(raw)
    return summary


def main():
    gateway=PreviewGateway()
    with tempfile.TemporaryDirectory(prefix='azure-tv-preview-') as directory:
        original=Path(directory)/'original.m3u8'
        preview=Path(directory)/'preview.m3u8'
        original_size,etag=gateway.download_original(original)
        commit,candidates=latest_candidates()
        before,original_counts=structural_signature(original)
        changes=transform(original,preview,candidates)
        after,preview_counts=structural_signature(preview)
        if before!=after or original_counts!=preview_counts:
            raise RuntimeError('CRITICAL: Metadata, order or VOD changed; refusing upload')
        preview_size=preview.stat().st_size
        if not original_size*0.8 < preview_size < original_size*1.2:
            raise RuntimeError('Preview changed size too much')
        print('No entries added or deleted:', original_counts['entries'],flush=True)
        print('Live URL changes:',dict(changes),flush=True)
        print('Structural checksum identical:',before,flush=True)
        if changes['urlsChanged']==0:
            raise RuntimeError('No unambiguous live URL updates were found: keeping original untouched')
        meta={'originalBytes':original_size,'originalEtag':etag,
              'entries':original_counts['entries'],'tvEntries':original_counts['tv'],
              'protectedVOD':original_counts['nonTv'],'urlsChanged':changes['urlsChanged'],
              'changesByType':dict(changes),'sourceCommit':commit,
              'structureHash':before,'previewBytes':preview_size,
              'generatedAt':time.strftime('%Y-%m-%dT%H:%M:%SZ',time.gmtime())}
        gateway.upload(preview,meta)


if __name__=='__main__':
    main()
