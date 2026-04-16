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
    task_id = 'terminal-cleanup-demo'

    subprocess.run([
        'python3', str(LEDGER_TOOL), '--ledger', str(ledger), 'init', task_id,
        '--goal', 'terminal cleanup should not regress to HEARTBEAT_DUE', '--owner', 'main-agent', '--channel', 'discord',
        '--workflow', 'Step 1', '--workflow', 'Step 2', '--next-action', 'Start step 1', '--message-ref', 'demo:msg',
    ], check=True, text=True, capture_output=True)
    run_json('python3', str(CRON), '--ledger', str(ledger), '--cron-dir', str(cron_dir), 'install', task_id)

    run_json('python3', str(OPS), '--ledger', str(ledger), 'record-update', 'STEP_COMPLETED', task_id,
             '--summary', 'Step 1 done', '--current-checkpoint', 'step-01', '--next-action', 'Complete step 2')
    run_json('python3', str(OPS), '--ledger', str(ledger), 'record-update', 'STEP_COMPLETED', task_id,
             '--summary', 'Step 2 done', '--current-checkpoint', 'step-02', '--next-action', 'Mark complete')
    run_json('python3', str(OPS), '--ledger', str(ledger), 'record-update', 'TASK_COMPLETED', task_id,
             '--summary', 'Task done', '--current-checkpoint', 'step-02')

    task_after_completion = task_from(ledger, task_id)
    assert task_after_completion['status'] == 'COMPLETED', task_after_completion
    assert task_after_completion['heartbeat']['watchdog_state'] == 'STOP_AND_DELETE', task_after_completion['heartbeat']
    assert task_after_completion['monitoring']['last_action_state'] == 'STOP_AND_DELETE', task_after_completion['monitoring']

    preview = run_json('python3', str(OPS), '--ledger', str(ledger), 'preview-tick', task_id)
    assert preview['state'] == 'STOP_AND_DELETE', preview
    assert preview['remove_monitor'] is True, preview

    tick = run_json('python3', str(CRON), '--ledger', str(ledger), '--cron-dir', str(cron_dir), 'run-once', '--task-id', task_id)
    task = task_from(ledger, task_id)
    assert tick['report']['state'] == 'STOP_AND_DELETE', tick
    assert tick['cron_removed'] is True, tick
    assert task['monitoring']['cron_state'] == 'DELETED', task['monitoring']
    assert task['heartbeat']['watchdog_state'] == 'STOP_AND_DELETE', task['heartbeat']
    assert task['status'] == 'COMPLETED', task
    print(json.dumps({'ok': True, 'state': tick['report']['state'], 'cron_removed': tick['cron_removed']}, ensure_ascii=False))
