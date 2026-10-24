#!/usr/bin/env python3
"""Regression tests: never lose Azure TV entries, metadata or favorites."""
import tempfile
from pathlib import Path
import unittest
from unittest.mock import patch

import azure_tv_preview as original
import azure_tv_reliable as reliable


class AzureTVSafetyTest(unittest.TestCase):
    def test_channel_titles_with_quality_markers(self):
        self.assertEqual(reliable.canonical_title('Canal Rural HD'),
                         reliable.canonical_title('Canal Rural (720p)'))
        self.assertEqual(reliable.canonical_title('Canal Rural (720p) [Not 24/7]'),'canal rural')

    def test_hls_master_and_media(self):
        master=b'#EXTM3U\n#EXT-X-STREAM-INF:BANDWIDTH=200000\nvariant/index.m3u8\n'
        media=b'#EXTM3U\n#EXT-X-TARGETDURATION:10\n#EXTINF:10.0,\nchunk001.ts\n'
        self.assertEqual(reliable.playlist_children(master),('master',['variant/index.m3u8']))
        self.assertEqual(reliable.playlist_children(media),('media',['chunk001.ts']))
        self.assertIsNone(reliable.playlist_children(b'<html>error</html>')[0])

    def test_public_urls_only(self):
        self.assertFalse(reliable.allowed_link('http://127.0.0.1/x.m3u8'))
        self.assertFalse(reliable.allowed_link('http://192.168.0.1/x.m3u8'))
        self.assertFalse(reliable.allowed_link('http://localhost/x.m3u8'))
        self.assertFalse(reliable.allowed_link('https://cdn.example.org/video.mp4'))
        self.assertTrue(reliable.allowed_link('https://cdn.example.org/live.m3u8?token=123'))

    def test_favorites_structure_remains_exact(self):
        original_content=(
            '#EXTM3U\n'
            '#EXTINF:-1 tvg-id="LiveTV.br" group-title="Canais | TV",Live TV HD\n'
            'https://cdn.example.org/old.m3u8\n'
            '#EXTINF:-1 tvg-id="SeriesName" group-title="Séries | Drama",Minha Série S01E01\n'
            'https://vod.example.org/season1.mp4\n'
            '#EXTINF:-1 group-title="Filmes | Animação",Filme Favorito\n'
            'https://vod.example.org/movie.mkv\n'
            '#EXTINF:-1 group-title="Canais | TV",Segundo Canal HD\n'
            'https://cdn.example.org/second.m3u8\n'
        ).encode()
        with tempfile.TemporaryDirectory() as d:
            src=Path(d)/'old.m3u8'
            dst=Path(d)/'preview.m3u8'
            src.write_bytes(original_content)
            decision={('live tv hd','canais | tv','https://cdn.example.org/old.m3u8'):
                      'https://cdn.example.org/good.m3u8'}
            changed=reliable.build_preview(src,dst,decision)
            self.assertEqual(changed,1)
            before,counts=original.structural_signature(src)
            after,counts_after=original.structural_signature(dst)
            self.assertEqual(before,after)
            self.assertEqual(counts,counts_after)
            self.assertEqual(counts['entries'],4)
            self.assertEqual(dst.read_bytes().replace(b'https://cdn.example.org/good.m3u8',
                                                      b'https://cdn.example.org/old.m3u8'),original_content)
            self.assertIn(b'Minha S\xc3\xa9rie S01E01',dst.read_bytes())
            self.assertIn(b'https://vod.example.org/movie.mkv',dst.read_bytes())

    def test_does_not_replace_healthy_stream(self):
        target=[('live tv hd','canais | tv','https://cdn.example.org/old.m3u8','live tv')]
        options={'live tv':{
            'https://cdn.example.org/old.m3u8':'Ramys',
            'https://cdn.example.org/good.m3u8':'iptv-org'
        }}
        with patch.object(reliable,'hls_healthy',side_effect=lambda u:True):
            decisions,summary=reliable.select_replacements(target,options)
        self.assertFalse(decisions)
        self.assertEqual(summary['originalHealthy'],1)

    def test_stages_only_verified_alternative(self):
        target=[('live tv hd','canais | tv','https://cdn.example.org/old.m3u8','live tv')]
        options={'live tv':{'https://cdn.example.org/good.m3u8':'iptv-org'}}
        with patch.object(reliable,'hls_healthy',side_effect=lambda u:u.endswith('good.m3u8')):
            decisions,summary=reliable.select_replacements(target,options)
        self.assertEqual(decisions[('live tv hd','canais | tv','https://cdn.example.org/old.m3u8')],
                         'https://cdn.example.org/good.m3u8')
        self.assertEqual(summary['healthyAlternativesStaged'],1)


if __name__=='__main__':
    unittest.main(verbosity=2)
