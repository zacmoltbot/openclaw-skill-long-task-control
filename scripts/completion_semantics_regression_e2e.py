#!/usr/bin/env python3
from __future__ import annotations

import json
import os
import subprocess
import tempfile
from pathlib import Path

ROOT = Path(__file__).resolve().parent.parent
OPS = ROOT / 'scripts' / 'openclaw_ops.py'
RUNNER = ROOT / 'scripts' / 'runner_engine.py'


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
        bad = out_dir / 'missing.txt'
        ok2 = out_dir / 'ok2.txt'
        task_id = 'completion-semantics-demo'
        env = os.environ.copy()
        env['LTC_DELIVERY_SINK_FILE'] = str(tmp / 'sink.json')

        bootstrap = run_json(
            'python3', str(OPS), '--ledger', str(ledger), 'bootstrap-task', task_id,
            '--goal', 'Verify failed items do not become SUCCESS',
            '--owner', 'main-agent',
            '--channel', 'discord',
            '--requester-channel', 'discord:channel:1477237887136432162',
            '--workflow', f"Step 1 :: shell=printf 'ok1' > {ok1} :: expect={ok1}",
            '--workflow', f"Step 2 broken :: shell=printf 'nope' >/dev/null :: expect={bad}",
            '--workflow', f"Step 3 :: shell=printf 'ok2' > {ok2} :: expect={ok2}",
            '--next-action', 'Run through all steps',
            '--jobs-root', str(jobs_root),
            '--disabled',
            env=env,
        )

        first_run = bootstrap['execution']['first_run']
        assert first_run['status'] == 'BLOCKED', first_run

        job = run_json('python3', str(RUNNER), '--jobs-root', str(jobs_root), 'status', f'{task_id}-job')
        assert job['status'] == 'BLOCKED', job
        assert job['blocked_reason'] == 'MISSING_COMPLETION_EVIDENCE', job

        preview = run_json('python3', str(OPS), '--ledger', str(ledger), 'preview-tick', task_id, env=env)
        user_facing = preview['user_facing']
        assert user_facing['outcome_status'] == 'PARTIAL_SUCCESS', user_facing
        assert preview['state'] != 'OK', preview
        assert preview['truth_state'] == 'CONSISTENT', preview
        assert 'step-02' in user_facing['remaining_steps'], user_facing

        task_state = task_from(ledger, task_id)
        assert task_state['status'] == 'BLOCKED', task_state['status']
        print(json.dumps({
            'ok': True,
            'job_status': job['status'],
            'preview_state': preview['state'],
            'outcome_status': user_facing['outcome_status'],
            'remaining_steps': user_facing['remaining_steps'],
        }, ensure_ascii=False, indent=2))


if __name__ == '__main__':
    main()
