#!/usr/bin/env python3
"""Export ONLY validated live-channel changes to a small, private test playlist."""
from itertools import zip_longest
from pathlib import Path
import json
import os
import tempfile
import urllib.request

from azure_tv_preview import PreviewGateway, structural_signature, metadata, is_tv


def export_changed_channels(original,preview,out):
    old_entry=[]
    new_entry=[]
    is_live=False
    changes=0
    with original.open('rb') as before,preview.open('rb') as after,out.open('wb') as mini:
        mini.write(b'#EXTM3U\n')
        for old,new in zip_longest(before,after):
            if old is None or new is None:
                raise RuntimeError('Playlist files have a different number of lines')
            if old.startswith(b'#EXTINF:'):
                if old!=new:
                    raise RuntimeError('Title or metadata changed in preview')
                title,group=metadata(old)
                is_live=is_tv(group)
                old_entry=[old]
                new_entry=[new]
                continue
            if old_entry and old.startswith(b'#'):
                if old!=new:
                    raise RuntimeError('Channel options changed in preview')
                old_entry.append(old)
                new_entry.append(new)
                continue
            if old_entry and old and not old.startswith(b'#'):
                if old!=new:
                    if not is_live:
                        raise RuntimeError('Movie/series URL changed, unsafe preview')
                    mini.writelines(new_entry)
                    mini.write(new)
                    changes+=1
                old_entry=[]
                new_entry=[]
                continue
            if old!=new:
                raise RuntimeError('Unexpected difference outside a channel URL')
    return changes


def download_staging(gateway,path,expected_bytes):
    req=urllib.request.Request(
        'https://azure-tv-preview.giovannisilvaalves604.workers.dev/preview',
        headers={'Authorization':'Bearer '+gateway.bearer(),'User-Agent':'AzureTV-SafeMini/1.0'})
    with urllib.request.urlopen(req,timeout=180) as response,path.open('wb') as dst:
        while data:=response.read(8*1024*1024):
            dst.write(data)
    if path.stat().st_size!=expected_bytes:
        raise RuntimeError('Preview download size changed')


def main():
    gateway=PreviewGateway()
    state=gateway.call('/state')
    meta=state.get('metadata') or {}
    if not state.get('preview',{}).get('size') or not meta.get('urlsChanged'):
        print('No verified preview changes available. Nothing to export.')
        return
    if meta.get('originalEtag')!=state.get('original',{}).get('etag'):
        raise RuntimeError('Original playlist changed since the preview was produced')
    if meta.get('previewBytes')!=state.get('preview',{}).get('size'):
        raise RuntimeError('Preview size metadata is inconsistent')
    with tempfile.TemporaryDirectory(prefix='azure-tv-mini-') as d:
        root=Path(d)
        src=root/'original.m3u8'
        preview=root/'preview.m3u8'
        mini=root/'channels-test.m3u8'
        gateway.download_original(src)
        download_staging(gateway,preview,meta['previewBytes'])
        old_hash,old_counts=structural_signature(src)
        new_hash,new_counts=structural_signature(preview)
        if old_hash!=new_hash or old_counts!=new_counts:
            raise RuntimeError('Original and preview differ in structure or VOD')
        count=export_changed_channels(src,preview,mini)
        if count!=meta['urlsChanged'] or count<1:
            raise RuntimeError('Number of preview changes does not match metadata')
        size=mini.stat().st_size
        if size>500000 or size<200:
            raise RuntimeError('Mini playlist size is outside safe range')
        saved=gateway.call('/save-mini',mini.read_bytes(),binary=True)
        if not saved.get('ok') or saved.get('size')!=size:
            raise RuntimeError('Mini playlist upload failed validation')
        print('PRIVATE_TEST_PLAYLIST_READY:',json.dumps({'channels':count,'bytes':size,'preservedEntries':old_counts['entries']}))


if __name__=='__main__':
    main()
