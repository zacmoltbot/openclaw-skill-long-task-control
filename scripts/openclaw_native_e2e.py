#!/usr/bin/env python3
import json
import subprocess
import tempfile
from pathlib import Path

ROOT = Path(__file__).resolve().parent.parent
OPS = ROOT / "scripts" / "openclaw_ops.py"
LEDGER_TOOL = ROOT / "scripts" / "task_ledger.py"


def run(*args):
    return subprocess.run(args, check=True, text=True, capture_output=True)


def run_json(*args):
    return json.loads(run(*args).stdout)


def load(path: Path):
    return json.loads(path.read_text())


def task_from(ledger_path: Path, task_id: str):
    return next(task for task in load(ledger_path)["tasks"] if task["task_id"] == task_id)


def make_stale(ledger_path: Path, task_id: str, *, nudge_count: int):
    ledger = load(ledger_path)
    task = next(task for task in ledger["tasks"] if task["task_id"] == task_id)
    old = "2020-01-01T00:00:00+00:00"
    task["last_checkpoint_at"] = old
    task.setdefault("heartbeat", {})["last_progress_at"] = old
    task["heartbeat"]["last_heartbeat_at"] = old
    task.setdefault("monitoring", {})["nudge_count"] = nudge_count
    if nudge_count:
        task["monitoring"]["last_nudge_at"] = old
    ledger_path.write_text(json.dumps(ledger, ensure_ascii=False, indent=2) + "\n")


def main():
    with tempfile.TemporaryDirectory() as tmpdir:
        tmp = Path(tmpdir)
        ledger = tmp / "state" / "long-task-ledger.json"
        task_id = "openclaw-native-20260411-a"

        activation = run("python3", str(OPS), "activation", "--task-note", "OpenClaw-native E2E sample").stdout.strip()
        assert "ACTIVATED" in activation
        assert "long-task-control" in activation

        activated = run_json(
            "python3", str(OPS), "--ledger", str(ledger), "bootstrap-task", task_id,
            "--goal", "Prove OpenClaw-native long-task-control activation to cron cleanup",
            "--requester-channel", "1484432523781083197",
            "--workflow", "Inspect inputs",
            "--workflow", "Resume after stale",
            "--workflow", "Complete and cleanup",
            "--next-action", "Run checkpoint 1",
            "--message-ref", "discord:msg:e2e-activation",
            "--fact", "channel_id=1484432523781083197",
            "--every", "30m",
            "--disabled",
        )
        assert activated["task_id"] == task_id
        job_id = activated["job"]["id"]
        assert job_id
        assert "message.send" in activated["prompt_preview"]
        assert "remove-monitor" in activated["prompt_preview"]

        task = task_from(ledger, task_id)
        assert task["monitoring"]["openclaw_cron_job_id"] == job_id
        assert task["message"]["requester_channel"] == "1484432523781083197"

        make_stale(ledger, task_id, nudge_count=0)
        preview_1 = run_json("python3", str(OPS), "--ledger", str(ledger), "preview-tick", task_id)
        assert preview_1["state"] == "NUDGE_MAIN_AGENT"
        assert "先自救" in preview_1["notification"]
        assert preview_1["remove_monitor"] is False

        make_stale(ledger, task_id, nudge_count=2)
        preview_2 = run_json("python3", str(OPS), "--ledger", str(ledger), "preview-tick", task_id)
        assert preview_2["state"] == "OWNER_RECONCILE"
        assert "A_IN_PROGRESS_FORGOT_LEDGER" in preview_2["notification"]

        run(
            "python3", str(LEDGER_TOOL), "--ledger", str(ledger), "owner-reply", task_id,
            "--reply", "E",
            "--summary", "Owner admitted the task was forgotten; resume now",
            "--next-action", "Resume execution now and publish checkpoint 2",
            "--fact", "owner_statement=forgot_to_do_it",
        )
        task = task_from(ledger, task_id)
        assert task["status"] == "RUNNING"
        assert task["monitoring"]["owner_response_kind"] == "E_FORGOT_OR_NOT_DOING"

        run(
            "python3", str(LEDGER_TOOL), "--ledger", str(ledger), "owner-reply", task_id,
            "--reply", "C",
            "--summary", "Owner confirmed the task is complete",
            "--validation", "artifact_exists=true",
            "--artifact", str(tmp / "artifact.txt"),
        )
        preview_3 = run_json("python3", str(OPS), "--ledger", str(ledger), "preview-tick", task_id)
        assert preview_3["state"] == "STOP_AND_DELETE"
        assert preview_3["remove_monitor"] is True

        removed = run_json("python3", str(OPS), "--ledger", str(ledger), "remove-monitor", task_id)
        assert removed["removed"] is True
        final_task = task_from(ledger, task_id)
        assert final_task["monitoring"]["cron_state"] == "DELETED"

        print(json.dumps({
            "ok": True,
            "task_id": task_id,
            "activation": activation,
            "job_id": job_id,
            "states": {
                "first_stale": preview_1["state"],
                "reconcile": preview_2["state"],
                "terminal": preview_3["state"],
            },
            "cron_removed": removed["removed"],
            "ledger": str(ledger),
        }, ensure_ascii=False, indent=2))


if __name__ == "__main__":
    main()
