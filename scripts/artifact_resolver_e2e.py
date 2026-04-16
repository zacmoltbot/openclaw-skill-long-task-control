#!/usr/bin/env python3
from __future__ import annotations

import json
import subprocess
import tempfile
from pathlib import Path

ROOT = Path(__file__).resolve().parent.parent
RESOLVER = ROOT / 'scripts' / 'artifact_resolver.py'

with tempfile.TemporaryDirectory() as td:
    d = Path(td)
    # simulate workflow that emits audio + suffixed video instead of expected base mp4
    (d / 'matrix_C2v2_img_lipsync_1.flac').write_bytes(b'0' * 1000)
    (d / 'matrix_C2v2_img_lipsync_2.mp4').write_bytes(b'0' * 600_000)
    out = subprocess.run(
        ['python3', str(RESOLVER), str(d / 'matrix_C2v2_img_lipsync.mp4')],
        check=True, text=True, capture_output=True,
    )
    payload = json.loads(out.stdout)
    assert payload['resolved'] is True, payload
    assert payload['chosen_video']['path'].endswith('matrix_C2v2_img_lipsync_2.mp4'), payload
    assert len(payload['audio_candidates']) == 1, payload
    print(json.dumps({'ok': True, 'chosen_video': payload['chosen_video']['path']}, ensure_ascii=False))
