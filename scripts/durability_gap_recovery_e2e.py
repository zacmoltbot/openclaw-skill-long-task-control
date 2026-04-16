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
    task_id = 'durability-gap-demo'

    subprocess.run([
        'python3', str(LEDGER_TOOL), '--ledger', str(ledger), 'init', task_id,
        '--goal', 'recover external success after interrupted owner/subagent', '--owner', 'main-agent', '--channel', 'discord',
        '--workflow', 'Produce remote video', '--workflow', 'Review result', '--next-action', 'Wait for remote output', '--message-ref', 'demo:msg',
    ], check=True, text=True, capture_output=True)

    # Simulate owner/subagent interrupted after remote success: output exists, ledger still stale/at step-01
    (outdir / 'matrix_case_1.flac').write_bytes(b'0' * 1000)
    (outdir / 'matrix_case_2.mp4').write_bytes(b'0' * 700_000)

    recovered = run_json(
        'python3', str(OPS), '--ledger', str(ledger), 'recover-external-success', task_id, str(outdir / 'matrix_case.mp4'),
        '--current-checkpoint', 'step-01', '--next-action', 'Proceed to review result',
    )
    task = task_from(ledger, task_id)
    obs = task['observations'][-1]
    assert recovered['state'] == 'STEP_COMPLETED', recovered
    assert task['status'] == 'RUNNING', task
    assert task['current_checkpoint'] == 'step-01', task
    assert obs['event_type'] == 'STEP_COMPLETED', obs
    assert obs['facts']['video_path'].endswith('matrix_case_2.mp4'), obs
    assert obs['facts']['recovered_from_external_truth'] == 'true', obs
    print(json.dumps({'ok': True, 'recovered_video': obs['facts']['video_path']}, ensure_ascii=False))
