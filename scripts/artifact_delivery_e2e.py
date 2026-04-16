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
        img1 = out_dir / 'image-01.png'
        img2 = out_dir / 'image-02.jpg'
        sink = tmp / 'delivery-sink.json'
        task_id = 'artifact-delivery-demo'
        job_id = f'{task_id}-job'

        env = os.environ.copy()
        env['LTC_DELIVERY_SINK_FILE'] = str(sink)

        bootstrap = run_json(
            'python3', str(OPS), '--ledger', str(ledger), 'bootstrap-task', task_id,
            '--goal', 'Generate artifacts and explicitly deliver them to the requester before marking success',
            '--owner', 'main-agent',
            '--channel', 'discord',
            '--requester-channel', 'discord:channel:1477237887136432162',
            '--workflow', f"Generate image one :: shell=printf 'fakepng' > {img1} :: expect={img1}",
            '--workflow', f"Generate image two :: shell=printf 'fakejpg' > {img2} :: expect={img2}",
            '--workflow', f"Deliver review pack :: auto_action=deliver_artifacts :: deliver_artifacts={img1}|{img2} :: deliver_caption=Eko 測試交付圖 :: next_action=Mark task completed after delivery evidence",
            '--next-action', 'Start step-01 and continue through delivery',
            '--message-ref', 'discord:msg:artifact-delivery-demo',
            '--jobs-root', str(jobs_root),
            '--disabled',
            env=env,
        )

        assert bootstrap['task_id'] == task_id
        assert bootstrap['execution']['first_run']['status'] == 'COMPLETED'
        assert bootstrap['execution']['first_run']['steps_executed'] == 3

        job_status = run_json('python3', str(RUNNER), '--jobs-root', str(jobs_root), 'status', job_id)
        assert job_status['status'] == 'COMPLETED'

        task_state = task_from(ledger, task_id)
        assert task_state['status'] == 'COMPLETED'
        delivered = task_state.get('reporting', {}).get('delivered_updates', [])
        assert any(item['event_type'] == 'COMPLETED_HANDOFF' for item in delivered), delivered

        payloads = json.loads(sink.read_text())
        media_payloads = [item for item in payloads if item.get('media')]
        media = [item['media'] for item in media_payloads]
        assert str(img1) in media and str(img2) in media, payloads
        assert all('REPORTING HOOK' not in item.get('message', '') for item in payloads), payloads

        print(json.dumps({
            'ok': True,
            'task_id': task_id,
            'job_id': job_id,
            'delivered_media': media,
            'delivery_payload_count': len(payloads),
            'task_status': task_state['status'],
        }, ensure_ascii=False, indent=2))


if __name__ == '__main__':
    main()
