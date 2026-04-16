#!/usr/bin/env python3
from __future__ import annotations

import json
import subprocess
import tempfile
from pathlib import Path

ROOT = Path(__file__).resolve().parent.parent
OPS = ROOT / 'scripts' / 'openclaw_ops.py'
LEDGER_TOOL = ROOT / 'scripts' / 'task_ledger.py'
CRON = ROOT / 'scripts' / 'monitor_cron.py'


def run_json(*cmd: str):
    proc = subprocess.run(cmd, check=True, text=True, capture_output=True)
    return json.loads(proc.stdout)


def task_from(path: Path, task_id: str):
    data = json.loads(path.read_text())
    return next(t for t in data['tasks'] if t['task_id'] == task_id)

with tempfile.TemporaryDirectory() as td:
    d = Path(td)
    ledger = d / 'ledger.json'
    cron_dir = d / 'cron'
    cron_dir.mkdir()
    task_id = 'delivery-flush-demo'

    subprocess.run([
        'python3', str(LEDGER_TOOL), '--ledger', str(ledger), 'init', task_id,
        '--goal', 'prove pending updates flush before terminal cleanup', '--owner', 'main-agent', '--channel', 'discord',
        '--workflow', 'Step 1', '--next-action', 'Complete step 1', '--message-ref', 'demo:msg',
    ], check=True, text=True, capture_output=True)

    # Use a valid Discord target so this regression checks real delivery-flush behavior,
    # not invalid-target fail-closed behavior (covered separately by monitor_fail_closed_regression_e2e.py).
    task_data = json.loads(ledger.read_text())
    task_data['tasks'][0].setdefault('message', {})['requester_channel'] = 'discord:channel:1477237887136432162'
    task_data['tasks'][0]['message']['requester_channel_raw'] = 'discord:channel:1477237887136432162'
    task_data['tasks'][0]['message']['requester_channel_valid'] = True
    task_data['tasks'][0]['message']['requester_channel_source'] = 'test-fixture'
    ledger.write_text(json.dumps(task_data, ensure_ascii=False, indent=2), encoding='utf-8')

    run_json('python3', str(CRON), '--ledger', str(ledger), '--cron-dir', str(cron_dir), 'install', task_id)
    run_json('python3', str(OPS), '--ledger', str(ledger), 'record-update', 'STEP_COMPLETED', task_id,
             '--summary', 'Step 1 done', '--current-checkpoint', 'step-01', '--next-action', 'Mark task completed')
    run_json('python3', str(OPS), '--ledger', str(ledger), 'record-update', 'TASK_COMPLETED', task_id,
             '--summary', 'Task done', '--current-checkpoint', 'step-01')

    pre_flush = task_from(ledger, task_id)
    assert len(pre_flush['reporting']['pending_updates']) == 2, pre_flush['reporting']

    result = run_json('python3', str(CRON), '--ledger', str(ledger), '--cron-dir', str(cron_dir), 'run-once', '--task-id', task_id)
    task = task_from(ledger, task_id)
    assert result['delivery_push_count'] >= 2, result
    assert task['reporting']['pending_updates'] == [], task['reporting']
    assert len(task['reporting']['delivered_updates']) >= 2, task['reporting']
    print(json.dumps({'ok': True, 'delivery_push_count': result['delivery_push_count'], 'delivered_updates': len(task['reporting']['delivered_updates'])}, ensure_ascii=False))
