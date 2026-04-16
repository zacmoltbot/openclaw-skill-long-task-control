#!/usr/bin/env python3
from __future__ import annotations

import json
import os
import subprocess
import tempfile
from pathlib import Path

ROOT = Path(__file__).resolve().parent.parent
OPS = ROOT / 'scripts' / 'openclaw_ops.py'
JOB_MODELS = ROOT / 'scripts' / 'job_models.py'


def run(*args, env=None):
    return subprocess.run(args, check=True, text=True, capture_output=True, env=env)


def run_json(*args, env=None):
    return json.loads(run(*args, env=env).stdout)


def load(path: Path):
    return json.loads(path.read_text())


def task_from(ledger_path: Path, task_id: str):
    return next(task for task in load(ledger_path)['tasks'] if task['task_id'] == task_id)


def main():
    with tempfile.TemporaryDirectory() as tmpdir:
        tmp = Path(tmpdir)
        ledger = tmp / 'ledger.json'
        jobs_root = tmp / 'jobs'
        out_dir = tmp / 'out'
        out_dir.mkdir(parents=True, exist_ok=True)
        ok1 = out_dir / 'ok1.txt'
        ok2 = out_dir / 'ok2.txt'
        ok1.write_text('ok1')
        ok2.write_text('ok2')
        env = os.environ.copy()
        env['LTC_DELIVERY_SINK_FILE'] = str(tmp / 'sink.json')
        task_id = 'terminal-closeout-demo'
        job_id = f'{task_id}-job'

        run_json(
            'python3', str(OPS), '--ledger', str(ledger), 'bootstrap-task', task_id,
            '--goal', 'Verify terminal closeout auto-reconciles partial failure',
            '--owner', 'main-agent',
            '--channel', 'discord',
            '--requester-channel', 'discord:channel:1477237887136432162',
            '--workflow', f"Step 1 :: shell=printf 'ok1' > {ok1} :: expect={ok1}",
            '--workflow', f"Step 2 :: shell=printf 'ok2' > {ok2} :: expect={ok2}",
            '--next-action', 'Run through all steps',
            '--jobs-root', str(jobs_root),
            '--disabled',
            env=env,
        )

        job = load(jobs_root / job_id / 'job.json')
        job['status'] = 'BLOCKED'
        job['current_index'] = 1
        job['completed'] = ['step-01']
        job['failed'] = ['step-02']
        job['blocked_reason'] = 'Execution finished with failed items: step-02'
        job['artifacts'] = [
            {'path': str(ok1), 'kind': 'artifact', 'label': None, 'created_at': '2026-04-17T00:00:00+08:00', 'facts': {}},
            {'path': str(ok2), 'kind': 'artifact', 'label': None, 'created_at': '2026-04-17T00:00:01+08:00', 'facts': {}},
        ]
        (jobs_root / job_id / 'job.json').write_text(json.dumps(job, ensure_ascii=False, indent=2) + '\n')

        reconcile = run_json('python3', str(OPS), '--ledger', str(ledger), 'reconcile-execution-terminal', task_id, '--jobs-root', str(jobs_root), env=env)
        assert reconcile['reconciled_to'] == 'BLOCKED', reconcile

        preview = run_json('python3', str(OPS), '--ledger', str(ledger), 'preview-tick', task_id, env=env)
        uf = preview['user_facing']
        assert preview['truth_state'] == 'CONSISTENT', preview
        assert uf['outcome_status'] == 'PARTIAL_SUCCESS', uf
        assert preview['state'] == 'OWNER_RECONCILE', preview

        task = task_from(ledger, task_id)
        assert task['status'] == 'BLOCKED', task['status']
        blocker = task.get('blocker') or {}
        assert blocker.get('failure_type') == 'EXECUTION_PARTIAL_FAILURE', blocker

        print(json.dumps({
            'ok': True,
            'task_status': task['status'],
            'preview_state': preview['state'],
            'outcome_status': uf['outcome_status'],
            'failure_type': blocker.get('failure_type'),
            'remaining_steps': uf['remaining_steps'],
        }, ensure_ascii=False, indent=2))


if __name__ == '__main__':
    main()
