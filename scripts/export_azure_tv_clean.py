#!/usr/bin/env python3
"""Build a clean Azure TV M3U from a pinned upstream snapshot."""
from collections import Counter, defaultdict
from pathlib import Path
import re
import unicodedata
import urllib.request

UPSTREAM = "Ramys/Iptv-Brasil-2026"
REVISION = "5b01915e5b3a20e7c866c5750d009b64586abe89"
SOURCES = ["CanaisBR01.m3u8", "CanaisBR02.m3u8", "CanaisBR03.m3u8",
           "CanaisBR04.m3u8", "Filmes-Series.m3u8"]
OUT = Path("Azure_TV_Limpa_HD_FHD.m3u8")
GROUP = re.compile(r'group-title="([^"]*)"', re.I)
ADULT = re.compile(r'(?<![a-z0-9])(?:xxx|porn\\w*|erotic\\w*|adult(?:o|os|s)?|18\\s*\\+|\\+18|🔞|sex(?:o|ual)?|hentai|onlyfans)(?![a-z0-9])', re.I)
BAD_TV = re.compile(r'(?<![a-z0-9])(?:sd|4k|uhd|2160p|480p|360p)(?![a-z0-9])', re.I)
GOOD_TV = re.compile(r'(?<![a-z0-9])(?:hd|fhd|full[ -]?hd|1080p|720p)(?![a-z0-9])', re.I)
EPISODE = re.compile(r'(?i)(?:s(\\d{1,2})[ ._-]*e(\\d{1,3})|t(\\d{1,2})[ ._-]*e(\\d{1,3})|(\\d{1,2})x(\\d{1,3})|temporada[ ._-]*(\\d{1,2}).{0,12}?epis[oó]dio[ ._-]*(\\d{1,3}))')
LANG_DUB = re.compile(r'(?i)\\b(?:dublado|dublagem|dub|pt[ ._-]?br|portugu[eê]s)\\b')
LANG_SUB = re.compile(r'(?i)\\b(?:legendado|legendas?|sub(?:titled)?|original)\\b')
LANG_WORDS = re.compile(r'(?i)\\b(?:dublado|dublagem|dub|pt[ ._-]?br|portugu[eê]s|legendado|legendas?|sub(?:titled)?|original)\\b')
QUALITY = re.compile(r'(?i)\\b(?:sd|hd|fhd|4k|uhd|2160p|1080p|720p|480p|360p)\\b')

def norm(s):
    s = unicodedata.normalize("NFKD", s.casefold())
    s = "".join(c for c in s if not unicodedata.combining(c))
    return " ".join(s.split())

def classify(group, source):
    g = norm(group)
    if any(x in g for x in ("serie", "anime", "novela", "desenho")):
        return "series"
    if any(x in g for x in ("filme", "movie", "cinema")):
        return "movies"
    return "tv" if source.lower().startswith("canaisbr") else "movies"

def lang_for(text):
    if LANG_DUB.search(text): return "dub"
    if LANG_SUB.search(text): return "sub"
    return "other"

def clean_title(title):
    title = LANG_WORDS.sub(" ", title)
    title = QUALITY.sub(" ", title)
    title = re.sub(r"[\\[\\](){}]", " ", title)
    return " ".join(title.split()).strip(" -_|:.")

def series_parts(title):
    m = EPISODE.search(title)
    if not m:
        return clean_title(title), "full"
    if m.group(1) is not None:
        episode = f"S{int(m.group(1)):02d}E{int(m.group(2)):03d}"
    elif m.group(3) is not None:
        episode = f"S{int(m.group(3)):02d}E{int(m.group(4)):03d}"
    elif m.group(5) is not None:
        episode = f"S{int(m.group(5)):02d}E{int(m.group(6)):03d}"
    else:
        episode = f"S{int(m.group(7)):02d}E{int(m.group(8)):03d}"
    return clean_title(title[:m.start()]), episode

def fetch(name):
    url = f"https://raw.githubusercontent.com/{UPSTREAM}/{REVISION}/{name}"
    req = urllib.request.Request(url, headers={"User-Agent":"AzureTV-Export/1.0"})
    with urllib.request.urlopen(req, timeout=180) as response:
        return response

def main():
    counts = Counter()
    seen_urls = set()
    tv, movies = [], []
    series_candidates = []
    for source in SOURCES:
        print("Lendo", source, flush=True)
        with fetch(source) as response:
            metadata = None
            options = []
            for raw in response:
                line = raw.decode("utf-8", "replace").rstrip("\r\n")
                if line.startswith("#EXTINF:"):
                    metadata, options = line, []
                elif metadata and line.startswith(("#EXTVLCOPT:", "#KODIPROP:", "#EXTGRP:", "#EXTHTTP:")):
                    options.append(line)
                elif metadata and line and not line.startswith("#"):
                    group = (GROUP.search(metadata).group(1) if GROUP.search(metadata) else "")
                    title = metadata.rsplit(",", 1)[-1].strip()
                    combined = norm(group + " " + title)
                    kind = classify(group, source)
                    url = line.strip()
                    if ADULT.search(combined):
                        counts["adult_removed"] += 1
                    elif not url.startswith(("http://", "https://")) or len(url) > 8192:
                        counts["invalid_removed"] += 1
                    elif kind == "tv" and (BAD_TV.search(combined) or not GOOD_TV.search(combined)):
                        counts["tv_not_hd_fhd_removed"] += 1
                    elif url in seen_urls:
                        counts["duplicate_url_removed"] += 1
                    else:
                        seen_urls.add(url)
                        entry = (metadata, options[:], url, group, title)
                        if kind == "tv":
                            tv.append(entry)
                        elif kind == "movies":
                            movies.append(entry)
                        else:
                            base, episode = series_parts(title)
                            lang = lang_for(group + " " + title)
                            series_candidates.append((entry, norm(base), episode, lang, norm(group)))
                        counts[kind + "_seen"] += 1
                    metadata, options = None, []
    # One movie entry per title/language class (dubbed and subtitled may coexist).
    movie_selected = {}
    for entry in movies:
        _, _, _, group, title = entry
        lang = lang_for(group + " " + title)
        key = (norm(clean_title(title)), lang)
        movie_selected.setdefault(key, entry)
    # For each series/language, choose the group/source with the largest episode set.
    groups = defaultdict(lambda: defaultdict(set))
    for entry, base, episode, lang, source_group in series_candidates:
        groups[(base, lang)][source_group].add(episode)
    best_group = {}
    for key, by_group in groups.items():
        best_group[key] = max(by_group, key=lambda g: (len(by_group[g]), g))
    series_selected = {}
    for entry, base, episode, lang, source_group in series_candidates:
        if source_group != best_group[(base, lang)]:
            counts["series_smaller_source_removed"] += 1
            continue
        series_selected.setdefault((base, lang, episode), entry)
    with OUT.open("w", encoding="utf-8", newline="\n") as out:
        out.write("#EXTM3U\n")
        for entry in tv:
            meta, opts, url, _, _ = entry
            out.write(meta + "\n")
            for opt in opts: out.write(opt + "\n")
            out.write(url + "\n")
        for entry in movie_selected.values():
            meta, opts, url, _, _ = entry
            out.write(meta + "\n")
            for opt in opts: out.write(opt + "\n")
            out.write(url + "\n")
        for entry in series_selected.values():
            meta, opts, url, _, _ = entry
            out.write(meta + "\n")
            for opt in opts: out.write(opt + "\n")
            out.write(url + "\n")
    counts["tv_output"] = len(tv)
    counts["movies_output"] = len(movie_selected)
    counts["series_output"] = len(series_selected)
    counts["total_output"] = sum(counts[k] for k in ("tv_output","movies_output","series_output"))
    print("RELATORIO", dict(counts), flush=True)
    print("BYTES", OUT.stat().st_size, flush=True)
    if counts["tv_output"] < 20 or counts["total_output"] < 10000:
        raise SystemExit("Falha na validacao: resultado inesperadamente pequeno")

if __name__ == "__main__":
    main()
