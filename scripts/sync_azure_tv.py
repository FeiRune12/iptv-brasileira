#!/usr/bin/env python3
"""Safely rebuild Azure TV from the original GitHub playlist sources.

Runs on GitHub Actions. Uses short-lived GitHub OIDC tokens to upload to
an isolated Cloudflare Worker; never stores R2 credentials in the repository.
"""
import hashlib
import io
import json
import os
from pathlib import Path
import re
import tempfile
import time
import unicodedata
import urllib.error
import urllib.parse
import urllib.request
from collections import Counter

UPSTREAM = 'Ramys/Iptv-Brasil-2026'
API = f'https://api.github.com/repos/{UPSTREAM}'
WORKER = 'https://azure-tv-sync.giovannisilvaalves604.workers.dev'
UA = 'AzureTV-Sync/1.0 (GitHub Actions)'
SOURCE_NAME = re.compile(r'^(?:CanaisBR\d{2}|Filmes-Series)\.m3u8$', re.I)
GROUP = re.compile(r'group-title="([^"]*)"', re.I)
ADULT = re.compile(r'(?<![a-z0-9])(?:xxx|porn\w*|erotic\w*|adult(?:o|os|s)?|18\s*\+|\+18|🔞|sex(?:o|ual)?|hentai|onlyfans)(?![a-z0-9])', re.I)
BAD_TV = re.compile(r'(?<![a-z0-9])(?:sd|4k|uhd|2160p|480p|360p)(?![a-z0-9])', re.I)
GOOD_TV = re.compile(r'(?<![a-z0-9])(?:hd|fhd|full[ -]?hd|1080p|720p)(?![a-z0-9])', re.I)
CHUNK = 8 * 1024 * 1024
PIPELINE_VERSION = 2


def get_json(url, headers=None, timeout=30):
    req = urllib.request.Request(url, headers={'User-Agent': UA, 'Accept': 'application/json', **(headers or {})})
    with urllib.request.urlopen(req, timeout=timeout) as resp:
        return json.load(resp)


def canonical(s):
    s = unicodedata.normalize('NFKD', s.casefold())
    return ''.join(c for c in s if not unicodedata.combining(c))


def kind_for(group, source):
    g = canonical(group)
    if any(x in g for x in ('serie', 'anime', 'novela', 'desenho')):
        return 'series'
    if any(x in g for x in ('filme', 'movie', 'cinema')):
        return 'movies'
    if source.lower().startswith('filmes-'):
        return 'movies'
    return 'tv'


class Uploader:
    def __init__(self):
        self.token = None
        self.token_until = 0

    def get_token(self):
        now = time.time()
        if self.token and now < self.token_until:
            return self.token
        base = os.environ['ACTIONS_ID_TOKEN_REQUEST_URL']
        permission = os.environ['ACTIONS_ID_TOKEN_REQUEST_TOKEN']
        glue = '&' if '?' in base else '?'
        data = get_json(base + glue + 'audience=azure-tv-sync-v1', {'Authorization': 'Bearer ' + permission})
        self.token = data['value']
        self.token_until = now + 90
        return self.token

    def call(self, path, payload=None, raw=False, timeout=180):
        url = WORKER + path
        headers = {'Authorization': 'Bearer ' + self.get_token(), 'User-Agent': UA}
        if raw:
            data = payload
            headers['Content-Type'] = 'application/octet-stream'
        elif payload is None:
            data = None
        else:
            data = json.dumps(payload, separators=(',', ':')).encode('utf-8')
            headers['Content-Type'] = 'application/json'
        request = urllib.request.Request(url, data=data, headers=headers, method='POST' if data is not None else 'GET')
        try:
            with urllib.request.urlopen(request, timeout=timeout) as response:
                return json.load(response)
        except urllib.error.HTTPError as err:
            detail = err.read(350).decode('utf-8', 'replace')
            raise RuntimeError(f'Worker returned HTTP {err.code}: {detail}') from err


def sources_snapshot():
    # Read one commit revision, and fetch file bodies pinned to it.
    ref = get_json(API + '/git/ref/heads/master')
    revision = ref['object']['sha']
    tree = get_json(API + f'/git/trees/{revision}?recursive=1')
    if tree.get('truncated'):
        raise RuntimeError('Upstream Git tree was truncated')
    files = {e['path']: e['sha'] for e in tree['tree']
             if e['type'] == 'blob' and '/' not in e['path'] and SOURCE_NAME.match(e['path'])}
    if not {'CanaisBR01.m3u8', 'CanaisBR02.m3u8', 'CanaisBR03.m3u8', 'Filmes-Series.m3u8'} <= set(files):
        raise RuntimeError('Original core files missing from upstream')
    return revision, dict(sorted(files.items()))


def write_playlist(revision, files, path):
    counts = Counter()
    seen_urls = set()
    digest = hashlib.sha256()
    with path.open('wb') as output:
        output.write(b'#EXTM3U\n')
        for name in files:
            url = f'https://raw.githubusercontent.com/{UPSTREAM}/{revision}/{urllib.parse.quote(name)}'
            print('Processing source:', name, flush=True)
            req = urllib.request.Request(url, headers={'User-Agent':UA})
            with urllib.request.urlopen(req, timeout=180) as res:
                reader = io.TextIOWrapper(res, encoding='utf-8-sig', errors='replace', newline=None)
                metadata = None
                options = []
                for raw_line in reader:
                    line = raw_line.rstrip('\r\n')
                    if line.startswith('#EXTINF:'):
                        metadata = line
                        options = []
                    elif metadata and line.startswith(('#EXTVLCOPT:', '#KODIPROP:', '#EXTGRP:', '#EXTHTTP:')):
                        options.append(line)
                    elif metadata and line and not line.startswith('#'):
                        if len(line) > 8192 or len(metadata) > 16384:
                            counts['invalid'] += 1
                        else:
                            group_match = GROUP.search(metadata)
                            group = group_match.group(1) if group_match else ''
                            title = metadata.rsplit(',', 1)[-1].strip()
                            kind = kind_for(group, name)
                            combined = canonical(group + ' ' + title)
                            uri = urllib.parse.urlsplit(line.strip())
                            if uri.scheme.lower() not in ('http', 'https') or not uri.netloc:
                                counts['invalid'] += 1
                            elif ADULT.search(combined):
                                counts['adult'] += 1
                            elif kind == 'tv' and (BAD_TV.search(combined) or not GOOD_TV.search(combined)):
                                counts['non_hd'] += 1
                            elif line.strip() in seen_urls:
                                counts['duplicate_url'] += 1
                            else:
                                seen_urls.add(line.strip())
                                block = ('\n'.join([metadata, *options, line.strip()])+'\n').encode('utf-8')
                                output.write(block)
                                digest.update(block)
                                counts[kind] += 1
                        metadata = None
                        options = []
    total = counts['tv'] + counts['movies'] + counts['series']
    size = path.stat().st_size
    print('Validated output: entries=', total, 'bytes=', size, 'breakdown=', dict(counts), flush=True)
    if total < 10000 or counts['tv'] < 40 or size < 10000000 or size > 450000000:
        raise RuntimeError('Safety check failed: generated playlist is incomplete or too small')
    return {'revision':revision,'sources':files, 'tv':counts['tv'],'movies':counts['movies'],
            'series':counts['series'],'total':total,'adultExcluded':counts['adult'],
            'duplicateUrlsExcluded':counts['duplicate_url'],'nonHdExcluded':counts['non_hd'],
            'sha256':digest.hexdigest(),'pipelineVersion':PIPELINE_VERSION,'generatedAt':time.strftime('%Y-%m-%dT%H:%M:%SZ',time.gmtime()),'bytes':size}


def upload(uploader, path, meta):
    started = uploader.call('/start', {})
    upload_id = started['uploadId']
    parts = []
    try:
        with path.open('rb') as stream:
            idx = 0
            while data := stream.read(CHUNK):
                idx += 1
                qs = urllib.parse.urlencode({'uploadId': upload_id, 'number': idx})
                part = uploader.call('/part?' + qs, data, raw=True)
                parts.append({'partNumber':part['partNumber'],'etag':part['etag']})
                print(f'Uploaded part {idx}', flush=True)
        if len(parts) < 2:
            raise RuntimeError('Refusing a single-part update')
        result = uploader.call('/finish', {'uploadId':upload_id,'parts':parts,'meta':meta})
        if not result.get('ok') or result.get('bytes') != path.stat().st_size:
            raise RuntimeError('Upload finished but size verification failed')
        print('Azure TV updated:', result['bytes'], 'bytes', flush=True)
    except Exception:
        try:
            uploader.call('/abort', {'uploadId':upload_id})
        except Exception:
            pass
        raise


def main():
    revision, files = sources_snapshot()
    print('Upstream revision:', revision, 'playlist files:', list(files), flush=True)
    worker = Uploader()
    current = worker.call('/state')
    previous = current.get('state') or {}
    if (previous.get('sources') == files and previous.get('pipelineVersion') == PIPELINE_VERSION
            and os.environ.get('FORCE_SYNC') != 'true'):
        print('No playlist changes upstream, nothing to deploy.', flush=True)
        return
    with tempfile.TemporaryDirectory() as directory:
        path = Path(directory) / 'playlist.m3u8'
        meta = write_playlist(revision, files, path)
        old_total = previous.get('total') or 0
        old_bytes = current.get('liveBytes') or 0
        if old_total and meta['total'] < old_total * .65:
            raise RuntimeError('Unusual drop in number of entries; keeping current playlist')
        if old_bytes and meta['bytes'] < old_bytes * .30:
            raise RuntimeError('Unusual shrinkage in bytes; keeping current playlist')
        upload(worker, path, meta)


if __name__ == '__main__':
    main()
