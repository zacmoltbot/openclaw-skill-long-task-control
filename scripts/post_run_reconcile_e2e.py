#!/usr/bin/env python3
from __future__ import annotations

import json
import subprocess
import tempfile
from pathlib import Path

ROOT = Path(__file__).resolve().parent.parent
OPS = ROOT / 'scripts' / 'openclaw_ops.py'
LEDGER_TOOL = ROOT / 'scripts' / 'task_ledger.py'


def run_json(*cmd: str):
    proc = subprocess.run(cmd, check=True, text=True, capture_output=True)
    return json.loads(proc.stdout)


def task_from(path: Path, task_id: str):
    data = json.loads(path.read_text())
    return next(t for t in data['tasks'] if t['task_id'] == task_id)

with tempfile.TemporaryDirectory() as td:
    d = Path(td)
    ledger = d / 'ledger.json'
    outdir = d / 'out'
    outdir.mkdir()
    task_id = 'reconcile-before-block-demo'

    subprocess.run([
        'python3', str(LEDGER_TOOL), '--ledger', str(ledger), 'init', task_id,
        '--goal', 'prove post-run reconcile before blocked', '--owner', 'main-agent', '--channel', 'discord',
        '--workflow', 'Generate output', '--next-action', 'Wait for output', '--message-ref', 'demo:msg',
    ], check=True, text=True, capture_output=True)

    # simulate output naming mismatch: expected foo.mp4 but actual foo_2.mp4 appears with audio sidecar
    (outdir / 'foo_1.flac').write_bytes(b'0' * 1000)
    (outdir / 'foo_2.mp4').write_bytes(b'0' * 600_000)

    result = run_json(
        'python3', str(OPS), '--ledger', str(ledger), 'reconcile-before-block', task_id, str(outdir / 'foo.mp4'),
        '--current-checkpoint', 'step-01', '--next-action', 'Proceed to next step',
        '--summary-if-resolved', 'Recovered completed output from reconcile pass',
        '--summary-if-blocked', 'Output still missing after reconcile',
    )
    task = task_from(ledger, task_id)
    assert result['state'] == 'STEP_COMPLETED', result
    assert task['status'] == 'RUNNING', task
    assert task['current_checkpoint'] == 'step-01', task
    obs = task['observations'][-1]
    assert obs['event_type'] == 'STEP_COMPLETED', obs
    assert obs['facts']['video_path'].endswith('foo_2.mp4'), obs
    print(json.dumps({'ok': True, 'resolved_video': obs['facts']['video_path']}, ensure_ascii=False))
