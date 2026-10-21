#!/usr/bin/env python3
"""Gera uma playlist M3U menor, a partir da release publica v1.0."""
from collections import Counter
from pathlib import Path
import re
import unicodedata

SOURCE = Path("Lista_Unificada.m3u8")
DEST = Path("Lista_Unificada_Leve.m3u8")
LIMIT = 24_000_000
GROUP = re.compile(r'group-title="([^"]*)"')
BLOCK = re.compile(r"(?i)\b(?:adultos?|adult|xxx|porn[oô]?(?:grafia)?|18\+)\b")

def norm(value):
    value = unicodedata.normalize("NFKD", value.casefold())
    return " ".join("".join(c for c in value if not unicodedata.combining(c)).split())

def category(group):
    normalized = norm(group)
    if not group or normalized.startswith("canais"): return "TV"
    if normalized.startswith(("filmes", "filme")): return "Filmes"
    return "Séries"

def short_group(cat, original):
    label = original.strip() or "Canais ao vivo"
    if cat == "TV":
        label = re.sub(r"^Canais\s*[|:]\s*", "", label, flags=re.I)
    elif cat == "Filmes":
        label = re.sub(r"^Filmes?\s*[|/:]\s*", "", label, flags=re.I)
    else:
        label = re.sub(r"^S[eé]ries?\s*[|/:]\s*", "", label, flags=re.I)
    label = re.sub(r"[✨⚽⭐⚡✈️▶️❤️⚔️✅☢️☣️✉️☕♥️♻️⛔Ⓜ️]|\s+", " ", label)
    return cat + " | " + label.strip(" -|: ")[:54]

items = {"TV": [], "Filmes": [], "Séries": []}
seen = set()
skipped = Counter()
metadata = None
options = []
with SOURCE.open("r", encoding="utf-8-sig", errors="replace") as stream:
    for raw in stream:
        line = raw.rstrip("\r\n")
        if line.startswith("#EXTINF:"):
            metadata, options = line, []
        elif line.startswith("#EXTVLCOPT:"):
            options.append(line)
        elif metadata and "://" in line and not line.startswith("#"):
            match = GROUP.search(metadata)
            group = match.group(1) if match else ""
            title = metadata.split(",", 1)[-1].strip()
            kind = category(group)
            if BLOCK.search(group) or BLOCK.search(title):
                skipped["adult"] += 1
            else:
                key = (kind, norm(title)) if kind != "TV" else (kind, norm(title), line)
                if key not in seen:
                    seen.add(key)
                    value = (
                        f'#EXTINF:-1 group-title="{short_group(kind, group)}",{title}\n'
                        + "".join(opt + "\n" for opt in options)
                        + line + "\n"
                    )
                    items[kind].append(value)
                else:
                    skipped["duplicate"] += 1
            metadata, options = None, []

chosen = {"TV": [], "Filmes": [], "Séries": []}
size = len("#EXTM3U\n")
for kind in ("TV", "Filmes"):
    for entry in items[kind]:
        length = len(entry.encode("utf-8"))
        if size + length > LIMIT: break
        chosen[kind].append(entry)
        size += length

series = items["Séries"]
budget = LIMIT - size
series_size = sum(len(e.encode("utf-8")) for e in series)
fraction = min(1, budget / series_size) if series_size else 0
indices = [
    i for i in range(len(series))
    if ((i * 2654435761) % 4294967296) / 4294967296 < fraction
]
for index in indices:
    entry = series[index]
    if size + len(entry.encode("utf-8")) <= LIMIT:
        chosen["Séries"].append(entry)
        size += len(entry.encode("utf-8"))

selected = set(indices)
for i, entry in enumerate(series):
    if LIMIT - size < 90: break
    if i not in selected and size + len(entry.encode("utf-8")) <= LIMIT:
        chosen["Séries"].append(entry)
        size += len(entry.encode("utf-8"))

with DEST.open("w", encoding="utf-8", newline="\n") as output:
    output.write("#EXTM3U\n")
    for kind in ("TV", "Filmes", "Séries"):
        output.writelines(chosen[kind])

print("Arquivo:", DEST, DEST.stat().st_size, "bytes")
print("Itens:", {k: len(v) for k, v in chosen.items()})
print("Excluidos:", dict(skipped))
