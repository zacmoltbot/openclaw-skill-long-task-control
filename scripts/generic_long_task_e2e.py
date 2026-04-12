#!/usr/bin/env python3
import json
import subprocess
import tempfile
from pathlib import Path

ROOT = Path(__file__).resolve().parent.parent
OPENCLAW_OPS = ROOT / 'scripts' / 'openclaw_ops.py'
TASK_LEDGER = ROOT / 'scripts' / 'task_ledger.py'


def run(*args):
    return subprocess.run(args, check=True, text=True, capture_output=True)


def run_json(*args):
    return json.loads(run(*args).stdout)


def load(path: Path):
    return json.loads(path.read_text())


def task_from(ledger_path: Path, task_id: str):
    return next(task for task in load(ledger_path)['tasks'] if task['task_id'] == task_id)


def make_stale(ledger_path: Path, task_id: str, *, nudge_count: int = 0):
    ledger = load(ledger_path)
    task = next(task for task in ledger['tasks'] if task['task_id'] == task_id)
    old = '2020-01-01T00:00:00+00:00'
    task['last_checkpoint_at'] = old
    task.setdefault('heartbeat', {})['last_progress_at'] = old
    task['heartbeat']['last_heartbeat_at'] = old
    task.setdefault('monitoring', {})['nudge_count'] = nudge_count
    if nudge_count:
        task['monitoring']['last_nudge_at'] = old
    ledger_path.write_text(json.dumps(ledger, ensure_ascii=False, indent=2) + '\n')


def main():
    with tempfile.TemporaryDirectory() as tmpdir:
        tmp = Path(tmpdir)
        ledger = tmp / 'state' / 'long-task-ledger.json'
        task_id = 'generic-long-task-20260411-a'
        artifact = tmp / 'reports' / 'final-summary.txt'
        artifact.parent.mkdir(parents=True, exist_ok=True)
        artifact.write_text('ok\n')

        bootstrap = run_json(
            'python3', str(OPENCLAW_OPS), '--ledger', str(ledger), 'bootstrap-task', task_id,
            '--goal', 'Collect inputs, build artifact, validate, and hand off',
            '--owner', 'main-agent',
            '--channel', 'discord',
            '--requester-channel', '1484432523781083197',
            '--workflow', 'Inspect input set',
            '--workflow', 'Build artifact',
            '--workflow', 'Validate output and handoff',
            '--artifact', str(artifact),
            '--message-ref', 'discord:msg:generic-activation',
            '--fact', 'scenario=generic_long_task',
            '--next-action', 'Inspect inputs and publish STARTED block',
            '--every', '7m',
            '--disabled',
        )
        assert 'ACTIVATED' in bootstrap['activation']
        assert 'TASK START' in bootstrap['task_start']
        assert 'record-update STARTED' in bootstrap['suggested_owner_updates']['started']

        task = task_from(ledger, task_id)
        assert task['activation']['announced'] is True
        assert task['monitoring']['openclaw_cron_job_id']
        assert task['monitoring']['cron_state'] == 'DISABLED'

        started = run_json(
            'python3', str(OPENCLAW_OPS), '--ledger', str(ledger), 'record-update', 'STEP_PROGRESS', task_id,
            '--summary', 'Input inspection started with source manifest loaded',
            '--current-checkpoint', 'step-01',
            '--next-action', 'Build the artifact',
            '--fact', 'input_manifest=loaded',
        )
        assert started['task_status'] == 'RUNNING'
        assert 'STEP_PROGRESS' in started['status_block']

        checkpoint = run_json(
            'python3', str(OPENCLAW_OPS), '--ledger', str(ledger), 'record-update', 'STEP_COMPLETED', task_id,
            '--summary', 'Artifact build finished',
            '--current-checkpoint', 'step-02',
            '--next-action', 'Validate output and handoff',
            '--fact', 'artifact_build=done',
            '--fact', f'artifact_path={artifact}',
        )
        assert checkpoint['task_status'] == 'RUNNING'

        make_stale(ledger, task_id, nudge_count=0)
        nudge = run_json('python3', str(OPENCLAW_OPS), '--ledger', str(ledger), 'preview-tick', task_id)
        assert nudge['state'] == 'NUDGE_MAIN_AGENT'

        run(
            'python3', str(TASK_LEDGER), '--ledger', str(ledger), 'owner-reply', task_id,
            '--reply', 'A',
            '--summary', 'Owner confirms work continued and ledger is now backfilled',
            '--current-checkpoint', 'step-03',
            '--next-action', 'Publish completion with validation evidence',
            '--fact', 'backfill=true',
        )
        task = task_from(ledger, task_id)
        assert task['monitoring']['owner_response_kind'] == 'A_IN_PROGRESS_FORGOT_LEDGER'
        assert task['status'] == 'RUNNING'

        completed = run_json(
            'python3', str(OPENCLAW_OPS), '--ledger', str(ledger), 'record-update', 'TASK_COMPLETED', task_id,
            '--summary', 'Validated final artifact and ready for handoff',
            '--current-checkpoint', 'step-03',
            '--next-action', 'None',
            '--output', str(artifact),
            '--completed-checkpoint', 'step-01',
            '--completed-checkpoint', 'step-02',
            '--completed-checkpoint', 'step-03',
            '--fact', 'validation=passed',
        )
        assert completed['task_status'] == 'COMPLETED'
        assert any(item.startswith('artifact_exists[') for item in completed['validation'])

        cleanup = run_json('python3', str(OPENCLAW_OPS), '--ledger', str(ledger), 'preview-tick', task_id)
        assert cleanup['state'] == 'STOP_AND_DELETE'
        removed = run_json('python3', str(OPENCLAW_OPS), '--ledger', str(ledger), 'remove-monitor', task_id)
        assert removed['removed'] is True

        print(json.dumps({
            'ok': True,
            'task_id': task_id,
            'bootstrap_fields': sorted(list(bootstrap.keys())),
            'states': {
                'stale': nudge['state'],
                'terminal': cleanup['state'],
            },
            'cron_removed': removed['removed'],
            'artifact': str(artifact),
            'ledger': str(ledger),
        }, ensure_ascii=False, indent=2))


if __name__ == '__main__':
    main()
