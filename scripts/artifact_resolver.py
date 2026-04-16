#!/usr/bin/env python3
from __future__ import annotations

import glob
import json
from pathlib import Path
from typing import Iterable

VIDEO_EXTS = {'.mp4', '.mov', '.m4v', '.webm'}
AUDIO_EXTS = {'.flac', '.wav', '.mp3', '.m4a', '.aac', '.ogg'}


def _existing(paths: Iterable[Path]) -> list[Path]:
    out = []
    seen = set()
    for p in paths:
        if p in seen:
            continue
        seen.add(p)
        if p.exists() and p.is_file():
            out.append(p)
    return out


def resolve_output_variants(expected_path: str | Path, *, min_video_bytes: int = 500_000) -> dict:
    expected = Path(expected_path)
    parent = expected.parent
    stem = expected.stem
    suffix = expected.suffix.lower()

    candidates = []
    candidates.append(expected)
    if suffix:
        candidates.append(parent / f"{stem}_2{suffix}")
        candidates.append(parent / f"{stem}_1{suffix}")
    # same basename, any extension / numeric suffix
    for pat in [
        str(parent / f"{stem}*"),
        str(parent / f"{stem.rsplit('_', 1)[0]}*") if '_' in stem else None,
    ]:
        if not pat:
            continue
        for item in glob.glob(pat):
            candidates.append(Path(item))

    existing = _existing(candidates)
    videos = []
    audios = []
    others = []
    for p in existing:
        ext = p.suffix.lower()
        size = p.stat().st_size
        row = {
            'path': str(p),
            'size': size,
            'ext': ext,
            'mtime': p.stat().st_mtime,
        }
        if ext in VIDEO_EXTS:
            videos.append(row)
        elif ext in AUDIO_EXTS:
            audios.append(row)
        else:
            others.append(row)

    videos = sorted(videos, key=lambda r: (r['size'], r['mtime']), reverse=True)
    chosen = None
    if videos:
        large = [v for v in videos if v['size'] >= min_video_bytes]
        chosen = (large or videos)[0]

    return {
        'expected_path': str(expected),
        'chosen_video': chosen,
        'video_candidates': videos,
        'audio_candidates': sorted(audios, key=lambda r: (r['size'], r['mtime']), reverse=True),
        'other_candidates': sorted(others, key=lambda r: (r['size'], r['mtime']), reverse=True),
        'resolved': bool(chosen),
    }


if __name__ == '__main__':
    import argparse
    ap = argparse.ArgumentParser(description='Resolve output-file naming variants for generated artifacts')
    ap.add_argument('expected_path')
    ap.add_argument('--min-video-bytes', type=int, default=500_000)
    args = ap.parse_args()
    print(json.dumps(resolve_output_variants(args.expected_path, min_video_bytes=args.min_video_bytes), ensure_ascii=False, indent=2))
