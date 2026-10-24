#!/usr/bin/env python3
"""Azure TV: health-check candidate live HLS URLs, create a SAFE preview only.

Never modifies the live R2 playlist. All 431k+ original records, their metadata,
order, and movie/series URLs stay byte-for-byte identical.
No stream is guaranteed to be available after a successful health check.
"""
from collections import Counter, defaultdict
from concurrent.futures import ThreadPoolExecutor, as_completed
from hashlib import sha256
import ipaddress
import io
import json
import os
from pathlib import Path
import re
import tempfile
import time
import urllib.error
import urllib.parse
import urllib.request
import unicodedata

import azure_tv_preview as base

ALTERNATIVE = 'https://raw.githubusercontent.com/iptv-org/iptv/master/streams/br.m3u'
RELIABILITY_SCHEMA = 3
CHECK_INTERVAL = 12 * 3600
MAX_CANDIDATES_PER_CHANNEL = 5
CHECK_WORKERS = 12
PROBE_TIMEOUT = 6
QUALITY_END = re.compile(r'(?:\s+\b(?:HD|FHD|FULL HD|SD|UHD|4K|720P|1080P)\b|\s+\((?:\d{3,4}p|HD|FHD)\)|\s+\[(?:not 24/7|\d{3,4}p)\])\s*$', re.I)
ADULT_TERMS = re.compile(r'(?<!\w)(?:xxx|porno\w*|hentai|adult\w*|sex\w*|18\+|\+18|casino|cassino|poker|apostas?|betting|gambling|slots?)(?!\w)', re.I)


def canonical_title(title):
    result = base.key(title)
    for _ in range(4):
        reduced = QUALITY_END.sub('', result).strip(' -|')
        if reduced == result:
            break
        result = reduced
    return result


def allowed_link(url):
    if not base.clean_uri(url):
        return False
    p = urllib.parse.urlsplit(url)
    if not p.path.lower().endswith(('.m3u8', '.m3u')):
        return False
    host = p.hostname or ''
    if host.lower().endswith(('.local', '.internal', '.localhost')) or host.lower() == 'localhost':
        return False
    try:
        addr = ipaddress.ip_address(host)
    except ValueError:
        return True
    return addr.is_global


class RestrictedRedirect(urllib.request.HTTPRedirectHandler):
    def redirect_request(self, req, fp, code, msg, headers, newurl):
        if not allowed_link(newurl) and not base.clean_uri(newurl):
            raise urllib.error.URLError('Blocked URL redirection')
        parsed = urllib.parse.urlsplit(newurl)
        host = parsed.hostname or ''
        if host.lower() == 'localhost' or host.lower().endswith(('.local','.internal','.localhost')):
            raise urllib.error.URLError('Blocked local redirection')
        try:
            addr = ipaddress.ip_address(host)
            if not addr.is_global:
                raise urllib.error.URLError('Blocked private-address redirection')
        except ValueError:
            pass
        return super().redirect_request(req, fp, code, msg, headers, newurl)


OPEN = urllib.request.build_opener(RestrictedRedirect)


def fetch_small(url, limit, timeout=PROBE_TIMEOUT):
    if not base.clean_uri(url):
        return None
    parsed = urllib.parse.urlsplit(url)
    host = parsed.hostname or ''
    if host.lower() == 'localhost' or host.lower().endswith(('.internal','.localhost','.local')):
        return None
    try:
        if not ipaddress.ip_address(host).is_global:
            return None
    except ValueError:
        pass
    req = urllib.request.Request(url, headers={
        'User-Agent':'Mozilla/5.0 (compatible; AzureTV-Check/1.0)',
        'Accept':'*/*',
        'Range':'bytes=0-32767',
    })
    try:
        with OPEN.open(req, timeout=timeout) as res:
            if res.status not in (200,206):
                return None
            data = res.read(limit)
            if not data or data.lstrip().lower().startswith((b'<html',b'<!doctype',b'{ "error"',b'{"error"')):
                return None
            return data
    except (TimeoutError, OSError, urllib.error.URLError, ValueError):
        return None


def playlist_children(blob):
    text = blob.decode('utf-8-sig', errors='replace')
    if not text.lstrip().startswith('#EXTM3U'):
        return None, []
    lines = [line.strip() for line in text.splitlines()]
    is_master = any(line.startswith('#EXT-X-STREAM-INF:') for line in lines)
    if is_master:
        for i, line in enumerate(lines):
            if line.startswith('#EXT-X-STREAM-INF:'):
                for possible in lines[i+1:]:
                    if possible and not possible.startswith('#'):
                        return 'master',[possible]
        return None,[]
    if not any(x.startswith('#EXT-X-TARGETDURATION:') for x in lines):
        return None,[]
    if not any(x.startswith('#EXTINF:') for x in lines):
        return None,[]
    uris = [x for x in lines if x and not x.startswith('#')]
    return ('media',uris[:1]) if uris else (None,[])


def hls_healthy(url):
    if not allowed_link(url):
        return False
    content = fetch_small(url,32768)
    if not content:
        return False
    kind,uris = playlist_children(content)
    if not kind or not uris:
        return False
    if kind == 'master':
        child = urllib.parse.urljoin(url,uris[0])
        if not allowed_link(child):
            return False
        content = fetch_small(child,32768)
        if not content:
            return False
        subkind,suburis = playlist_children(content)
        if subkind!='media' or not suburis:
            return False
        url,uris = child,suburis
    segment = urllib.parse.urljoin(url,uris[0])
    data = fetch_small(segment,1024)
    return bool(data) and not data[:50].lstrip().startswith(b'#EXTM3U')


def collect_original(original):
    targets=[]
    pending=None
    with original.open('rb') as f:
        for raw in f:
            line=raw.rstrip(b'\r\n')
            if line.startswith(b'#EXTINF:'):
                pending=base.metadata(line)
            elif pending is not None and line and not line.startswith(b'#'):
                title,group=pending
                url=line.decode('utf-8',errors='replace')
                if base.is_tv(group) and base.clean_uri(url) and not ADULT_TERMS.search(title+' '+group):
                    cleaned=canonical_title(title)
                    if len(cleaned)>=4:
                        targets.append((title,group,url,cleaned))
                pending=None
    return targets


def snapshot_from_sources(original_titles):
    """Read upstream without caching a million unrelated series entries."""
    revision, sources = base.source_snapshot()
    candidates=defaultdict(dict)
    counted=Counter()
    for name in sources:
        url='https://raw.githubusercontent.com/'+base.REPO+'/'+revision+'/'+urllib.parse.quote(name)
        with urllib.request.urlopen(base.repo_request(url), timeout=base.TIMEOUT) as stream:
            reader=io.TextIOWrapper(stream,encoding='utf-8-sig',errors='replace',newline=None)
            pending=None
            for raw in reader:
                text=raw.rstrip('\r\n')
                if text.startswith('#EXTINF:'):
                    title,group=base.metadata(text.encode('utf-8'))
                    clean=canonical_title(title)
                    pending=(clean,group) if clean in original_titles and base.is_tv(group) and not ADULT_TERMS.search(clean+' '+group) else None
                elif pending is not None and text and not text.startswith('#'):
                    clean,group=pending
                    stream_url=text.strip()
                    if allowed_link(stream_url):
                        candidates[clean].setdefault(stream_url,'Ramys')
                        counted['matchingUpstream']+=1
                    pending=None
    return revision,sources,candidates,counted


def load_alternatives(candidates, original_titles):
    with urllib.request.urlopen(base.repo_request(ALTERNATIVE),timeout=30) as f:
        content=f.read(4*1024*1024)
    if not content.startswith(b'#EXTM3U'):
        raise RuntimeError('Alternative index does not start with #EXTM3U')
    digest=sha256(content).hexdigest()
    pending=None
    count=0
    for line in content.decode('utf-8-sig',errors='replace').splitlines():
        if line.startswith('#EXTINF:'):
            # iptv-org title includes quality markers and sometimes a 24/7 indicator.
            title=line.rsplit(',',1)[-1].strip()
            clean=canonical_title(title)
            pending=clean if clean in original_titles and not ADULT_TERMS.search(clean) else None
        elif pending is not None and line and not line.startswith('#'):
            uri=line.strip()
            if allowed_link(uri):
                candidates[pending][uri]='iptv-org'
                count+=1
            pending=None
    return digest,count


def select_replacements(targets,candidates):
    """Check only channels with alternatives. Preserve healthy original URLs."""
    to_probe=set()
    eligible=[]
    for title,group,old,cleaned in targets:
        alternatives=[u for u in candidates.get(cleaned,{}) if u!=old and allowed_link(u)]
        if not alternatives:
            continue
        # A title match alone is insufficient to overwrite the live playlist.
        alternatives=sorted(alternatives,key=lambda u:(0 if candidates[cleaned][u]=='iptv-org' else 1,0 if u.startswith('https://') else 1,u))[:MAX_CANDIDATES_PER_CHANNEL]
        eligible.append((title,group,old,cleaned,alternatives))
        if allowed_link(old):
            to_probe.add(old)
        to_probe.update(alternatives)
    print('Potentially replaceable records:',len(eligible),'unique HLS endpoints:',len(to_probe),flush=True)
    health={}
    with ThreadPoolExecutor(max_workers=CHECK_WORKERS) as pool:
        futures={pool.submit(hls_healthy,url):url for url in to_probe}
        for future in as_completed(futures):
            url=futures[future]
            try:
                health[url]=bool(future.result())
            except Exception:
                health[url]=False
    decisions={}
    counts=Counter()
    for title,group,old,cleaned,alternatives in eligible:
        if health.get(old):
            counts['originalHealthy']+=1
            continue
        counts['originalUnverified']+=1
        for alt in alternatives:
            if not health.get(alt):
                continue
            # Confirm again before staging a fallback. No production change.
            if not hls_healthy(alt):
                counts['altSecondCheckFailed']+=1
                continue
            decisions[(title,group,old)]=alt
            counts['healthyAlternativesStaged']+=1
            break
        else:
            counts['noVerifiedFallback']+=1
    counts['checkedEndpoints']=len(health)
    counts['healthyEndpoints']=sum(health.values())
    counts['eligibleRecords']=len(eligible)
    return decisions,counts


def build_preview(original,staged,decisions):
    pending=None
    changes=0
    with original.open('rb') as f, staged.open('wb') as out:
        for raw in f:
            line=raw.rstrip(b'\r\n')
            if line.startswith(b'#EXTINF:'):
                pending=base.metadata(line)
                out.write(raw)
                continue
            if pending is not None and line and not line.startswith(b'#'):
                title,group=pending
                old=line.decode('utf-8',errors='replace')
                replacement=decisions.get((title,group,old))
                if replacement and base.is_tv(group):
                    out.write(replacement.encode('utf-8')+raw[len(line):])
                    changes+=1
                else:
                    out.write(raw)
                pending=None
                continue
            out.write(raw)
    return changes


def main():
    gateway=base.PreviewGateway()
    orig_revision,source_hashes=base.source_snapshot()
    checked=(gateway.call('/state').get('checked') or {})
    # Read the public alternative playlist each run; skip large downloads if unchanged.
    with urllib.request.urlopen(base.repo_request(ALTERNATIVE),timeout=30) as r:
        alt_bytes=r.read(4*1024*1024)
    alt_digest=sha256(alt_bytes).hexdigest()
    recent=(time.time()-float(checked.get('verifiedEpoch') or 0)) < CHECK_INTERVAL
    if (checked.get('healthSchema')==RELIABILITY_SCHEMA and checked.get('sources')==source_hashes
            and checked.get('altDigest')==alt_digest and recent and os.environ.get('FORCE_PREVIEW')!='true'):
        print('Source fingerprints unchanged and health check recent. Main playlist preserved.',flush=True)
        return
    with tempfile.TemporaryDirectory(prefix='azure-tv-health-') as tmp:
        original=Path(tmp)/'original.m3u8'
        staged=Path(tmp)/'candidate.m3u8'
        size,etag=gateway.download_original(original)
        before,original_counts=base.structural_signature(original)
        targets=collect_original(original)
        wanted=set(row[3] for row in targets)
        revision,sources,candidates,counts=snapshot_from_sources(wanted)
        alt_digest,alternatives=load_alternatives(candidates,wanted)
        decisions,health=select_replacements(targets,candidates)
        changed=build_preview(original,staged,decisions)
        after,after_counts=base.structural_signature(staged)
        if before!=after or original_counts!=after_counts:
            raise RuntimeError('FAIL-CLOSED: series, movies, metadata, order or titles differ')
        if changed<len(decisions):
            raise RuntimeError('Fewer changed entries than selected replacement keys')
        if not size*0.8<=staged.stat().st_size<=size*1.2:
            raise RuntimeError('Unexpected file size change')
        report={
            'healthSchema':RELIABILITY_SCHEMA,
            'sourceCommit':revision,
            'sources':sources,
            'altDigest':alt_digest,
            'urlUpdatesFound':changed,
            'verifiedEpoch':int(time.time()),
            'healthSummary':dict(health),
            'liveEntriesIdentified':original_counts['tv'],
            'vodEntriesProtected':original_counts['nonTv'],
            'alternativeCandidates':alternatives,
        }
        print('REPORT:',json.dumps({k:v for k,v in report.items() if k not in ('sources','altDigest')},ensure_ascii=False),flush=True)
        if changed:
            meta={'originalBytes':size,'originalEtag':etag,'entries':original_counts['entries'],
                  'tvEntries':original_counts['tv'],'protectedVOD':original_counts['nonTv'],
                  'urlsChanged':changed,'sourceCommit':revision,'structureHash':before,
                  'healthSummary':dict(health),'source':'validated HLS fallback',
                  'previewBytes':staged.stat().st_size}
            gateway.upload(staged,meta)
        else:
            print('No verified alternatives to stage. Original remains unchanged.',flush=True)
        gateway.call('/checked',report)


if __name__ == '__main__':
    main()
