#!/usr/bin/env python3
"""One-time Azure TV 119-channel promotion, with independent structural diff checks.

Checks all bytes before requesting the R2 promotion worker to create a backup,
replace the live playlist and verify the result. Never rewrites VOD or metadata.
"""
import hashlib
import json
import os
from pathlib import Path
import tempfile
import time
import urllib.error
import urllib.request

from azure_tv_preview import structural_signature
from export_azure_tv_test_channels import export_changed_channels

PROMOTER='https://azure-tv-promote.giovannisilvaalves604.workers.dev'
REQUIRED_ORIGINAL_BYTES=111000682
REQUIRED_PREVIEW_BYTES=111000186
REQUIRED_CHANGE_COUNT=119
OLD_ETAG='ebd1587511849e575e093055fe56e5f0'
NEW_ETAG='bc7adc9ed3e33540d89c01849d1eee10-14'
ALLOWED_ORIGINAL_ENTRIES=431730
ALLOWED_PROTECTED_VOD=431062

class Client:
    def __init__(self):
        self.token=None
        self.token_ttl=0

    def bearer(self):
        if self.token and time.time()<self.token_ttl:
            return self.token
        url=os.environ['ACTIONS_ID_TOKEN_REQUEST_URL']
        sep='&' if '?' in url else '?'
        req=urllib.request.Request(url+sep+'audience=azure-tv-sync-v1',
            headers={'Authorization':'Bearer '+os.environ['ACTIONS_ID_TOKEN_REQUEST_TOKEN'],
                'User-Agent':'AzureTV-ApprovedPromotion/1.0'})
        with urllib.request.urlopen(req,timeout=25) as response:
            self.token=json.load(response)['value']
        self.token_ttl=time.time()+90
        return self.token

    def request(self,route,body=None):
        head={'Authorization':'Bearer '+self.bearer(),'User-Agent':'AzureTV-ApprovedPromotion/1.0'}
        if body is not None:
            data=json.dumps(body,separators=(',',':')).encode()
            head['Content-Type']='application/json'
        else:
            data=None
        req=urllib.request.Request(PROMOTER+route,data=data,headers=head,
            method='POST' if data is not None else 'GET')
        try:
            with urllib.request.urlopen(req,timeout=180) as response:
                return json.load(response)
        except urllib.error.HTTPError as exc:
            raise RuntimeError('Promotion worker HTTP '+str(exc.code)+': '+exc.read(500).decode(errors='replace')) from exc

    def download(self,route,dest,expected_bytes):
        req=urllib.request.Request(PROMOTER+route,headers={
            'Authorization':'Bearer '+self.bearer(),
            'User-Agent':'AzureTV-ApprovedPromotion/1.0'})
        sha=hashlib.sha256()
        size=0
        with urllib.request.urlopen(req,timeout=180) as response, dest.open('wb') as output:
            while part:=response.read(4*1024*1024):
                sha.update(part)
                size+=len(part)
                output.write(part)
        if size!=expected_bytes:
            raise RuntimeError('Unexpected download size from '+route+': '+str(size))
        return sha.hexdigest()


def main():
    client=Client()
    state=client.request('/state')
    before=state.get('original') or {}
    proposed=state.get('staged') or {}
    meta=state.get('metadata') or {}
    if state.get('alreadyPromoted'):
        raise RuntimeError('This promotion already happened: refusing a second update')
    if (before.get('size')!=REQUIRED_ORIGINAL_BYTES or before.get('etag')!=OLD_ETAG):
        raise RuntimeError('Original R2 playlist changed from the restored favorites copy; aborting')
    if (proposed.get('size')!=REQUIRED_PREVIEW_BYTES or proposed.get('etag')!=NEW_ETAG):
        raise RuntimeError('Validated preview changed; aborting')
    for name,expected in [('urlsChanged',REQUIRED_CHANGE_COUNT),('originalBytes',REQUIRED_ORIGINAL_BYTES),
                          ('originalEtag',OLD_ETAG),('previewBytes',REQUIRED_PREVIEW_BYTES),
                          ('entries',ALLOWED_ORIGINAL_ENTRIES),('protectedVOD',ALLOWED_PROTECTED_VOD)]:
        if meta.get(name)!=expected:
            raise RuntimeError('Staged report mismatch for '+name)
    with tempfile.TemporaryDirectory(prefix='azure-tv-promotion-') as folder:
        path=Path(folder)
        original=path/'favorites-original.m3u8'
        candidate=path/'candidate-119-channel-urls.m3u8'
        mini=path/'changed-channels-only.m3u8'
        old_sha=client.download('/original',original,REQUIRED_ORIGINAL_BYTES)
        new_sha=client.download('/candidate',candidate,REQUIRED_PREVIEW_BYTES)
        struct_old,counts_old=structural_signature(original)
        struct_new,counts_new=structural_signature(candidate)
        if struct_old!=struct_new or counts_old!=counts_new:
            raise RuntimeError('Playlist records/metadata/VOD not identical: refusing to overwrite')
        if counts_old['entries']!=ALLOWED_ORIGINAL_ENTRIES or counts_old['nonTv']!=ALLOWED_PROTECTED_VOD:
            raise RuntimeError('Unexpected number of protected series/movie entries')
        if struct_old!=meta.get('structureHash'):
            raise RuntimeError('Candidate does not match the structural fingerprint in the report')
        changes=export_changed_channels(original,candidate,mini)
        if changes!=REQUIRED_CHANGE_COUNT:
            raise RuntimeError('Expected exactly '+str(REQUIRED_CHANGE_COUNT)+' channel URLs, found '+str(changes))
        if old_sha==new_sha:
            raise RuntimeError('Promotion candidate is identical to live, aborting')
        again=client.request('/state')
        if again.get('original')!=before or again.get('staged')!=proposed:
            raise RuntimeError('R2 files changed during checks, refusing to promote')
        print('PREFLIGHT_PASSED:',json.dumps({
            'entriesPreserved':counts_old['entries'],
            'vodEntriesIntact':counts_old['nonTv'],
            'urlsChanged':changes,
            'oldBytes':REQUIRED_ORIGINAL_BYTES,
            'newBytes':REQUIRED_PREVIEW_BYTES,
            'oldSha256':old_sha,
            'newSha256':new_sha,
        }),flush=True)
        result=client.request('/promote',{
            'confirmation':'PROMOTE_119_URL_ONLY',
            'count':REQUIRED_CHANGE_COUNT,
            'originalSha256':old_sha,
            'candidateSha256':new_sha
        })
        if not result.get('ok') or result.get('changes')!=changes:
            raise RuntimeError('Promotion worker did not confirm success: '+str(result)[:300])
        print('BACKUP_SAVED:',result.get('backup'),flush=True)
        print('PROMOTION_REPORTED:',json.dumps(result),flush=True)
        published=path/'published.m3u8'
        final_sha=client.download('/published',published,REQUIRED_PREVIEW_BYTES)
        if final_sha!=new_sha:
            raise RuntimeError('Published hash mismatch: backup exists at '+str(result.get('backup')))
        final_structure,final_count=structural_signature(published)
        if final_structure!=struct_old or final_count!=counts_old:
            raise RuntimeError('Published structural hash mismatch; backup still available!')
        print('PUBLISHED_VERIFIED:',json.dumps({'sha256':final_sha,'protectedVOD':final_count['nonTv'],
          'preservedRecords':final_count['entries'],'changedUrls':changes,'backup':result['backup']}),flush=True)


if __name__=='__main__':
    main()
