#!/usr/bin/env python3
from __future__ import annotations

import json
import subprocess
import tempfile
from pathlib import Path

OPS = Path(__file__).resolve().parent / "openclaw_ops.py"


def run_json(*args: str) -> dict:
    proc = subprocess.run(args, check=True, capture_output=True, text=True)
    return json.loads(proc.stdout)


def main() -> None:
    with tempfile.TemporaryDirectory() as td:
        base = Path(td)
        ledger = base / "ledger.json"
        jobs = base / "jobs"
        task_id = "owner-driven-external-smoke"

        bootstrap = run_json(
            "python3", str(OPS), "--ledger", str(ledger), "bootstrap-task", task_id,
            "--goal", "Owner-driven external workflow should not auto-complete",
            "--requester-channel", "1477237887136432162",
            "--workflow", "Run external provider batch :: generic_manual_mode=external_observed",
            "--next-action", "Start external work and record truth as it progresses",
            "--jobs-root", str(jobs),
            "--dry-run",
        )
        assert bootstrap["execution"]["job"]["owner_driven_tracking"] is True, bootstrap
        assert bootstrap["execution"]["first_run"]["status"] == "SKIPPED_AUTO_START", bootstrap
        assert bootstrap["execution"]["first_run"]["reason"] == "owner_driven_tracking", bootstrap

        preview0 = run_json("python3", str(OPS), "--ledger", str(ledger), "preview-tick", task_id)
        assert preview0["user_facing"]["status"] == "RUNNING", preview0
        assert preview0["user_facing"]["outcome_status"] == "IN_PROGRESS", preview0
        assert preview0["state"] != "STOP_AND_DELETE", preview0

        progress = run_json(
            "python3", str(OPS), "--ledger", str(ledger), "record-update", "STEP_PROGRESS", task_id,
            "--summary", "Started real external workflow outside canonical executor",
            "--current-checkpoint", "step-01",
            "--next-action", "Wait for provider response",
            "--fact", "stage=external_submit",
        )
        assert progress["state"] == "STEP_PROGRESS", progress

        preview1 = run_json("python3", str(OPS), "--ledger", str(ledger), "preview-tick", task_id)
        assert preview1["user_facing"]["status"] == "RUNNING", preview1
        assert preview1["user_facing"]["outcome_status"] == "IN_PROGRESS", preview1
        assert preview1["truth_state"] == "CONSISTENT", preview1
        assert preview1["state"] != "STOP_AND_DELETE", preview1

        print(json.dumps({
            "ok": True,
            "task_id": task_id,
            "checks": [
                "owner_driven_tracking_detected",
                "auto_start_skipped",
                "preview_stays_running_before_progress",
                "preview_stays_running_after_progress",
            ],
        }, ensure_ascii=False, indent=2))


if __name__ == "__main__":
    main()
