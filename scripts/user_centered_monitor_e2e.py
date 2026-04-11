#!/usr/bin/env python3
import json
import subprocess
import tempfile
from pathlib import Path

ROOT = Path(__file__).resolve().parent.parent
OPS = ROOT / "scripts" / "openclaw_ops.py"
LEDGER_TOOL = ROOT / "scripts" / "task_ledger.py"
CRON_TOOL = ROOT / "scripts" / "monitor_cron.py"


def run(*args):
    return subprocess.run(args, check=True, text=True, capture_output=True)


def run_json(*args):
    return json.loads(run(*args).stdout)


def load(path: Path):
    return json.loads(path.read_text())


def task_from(ledger_path: Path, task_id: str):
    return next(task for task in load(ledger_path)["tasks"] if task["task_id"] == task_id)


def make_stale(ledger_path: Path, task_id: str, *, nudge_count: int = 0):
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
        cron_dir = tmp / "state" / "monitor-crons"

        # Scenario A: transient problem -> self-heal / resume, no user escalation.
        resume_task = "scenario-a-resume"
        run(
            "python3", str(OPS), "--ledger", str(ledger), "init-task", resume_task,
            "--goal", "Recover from a transient stall and continue execution",
            "--owner", "main-agent",
            "--channel", "discord",
            "--requester-channel", "1484432523781083197",
            "--workflow", "Inspect",
            "--workflow", "Resume stalled step",
            "--workflow", "Complete",
            "--message-ref", "discord:msg:scenario-a",
            "--next-action", "Resume the stalled step",
        )
        run("python3", str(CRON_TOOL), "--ledger", str(ledger), "--cron-dir", str(cron_dir), "install", resume_task)
        make_stale(ledger, resume_task, nudge_count=0)
        preview_a1 = run_json("python3", str(OPS), "--ledger", str(ledger), "preview-tick", resume_task)
        assert preview_a1["state"] == "NUDGE_MAIN_AGENT"
        assert "先自救" in preview_a1["notification"]
        first_tick = run_json("python3", str(CRON_TOOL), "--ledger", str(ledger), "--cron-dir", str(cron_dir), "run-once", "--task-id", resume_task)
        assert first_tick["report"]["state"] == "NUDGE_MAIN_AGENT"
        assert first_tick["cron_removed"] is False

        run(
            "python3", str(LEDGER_TOOL), "--ledger", str(ledger), "owner-reply", resume_task,
            "--reply", "E",
            "--summary", "Owner admitted the task was forgotten; resume now",
            "--next-action", "Rebuild the transiently failed step and publish a checkpoint",
            "--fact", "owner_statement=forgot_to_do_it",
        )
        run_json(
            "python3", str(OPS), "--ledger", str(ledger), "record-update", "CHECKPOINT", resume_task,
            "--summary", "Transient failure recovered and execution resumed",
            "--current-checkpoint", "step-02",
            "--next-action", "Finish the task",
            "--fact", "rebuild_result=success",
        )
        second_tick = run_json("python3", str(CRON_TOOL), "--ledger", str(ledger), "--cron-dir", str(cron_dir), "run-once", "--task-id", resume_task)
        assert second_tick["report"]["state"] == "OK"
        assert second_tick["cron_removed"] is False
        resume_ledger = task_from(ledger, resume_task)
        assert resume_ledger["status"] == "RUNNING"
        assert resume_ledger["monitoring"]["recovery_attempt_count"] >= 1
        assert resume_ledger["monitoring"]["cron_state"] == "ACTIVE"

        # Scenario B: truly unrecoverable blocker -> escalate once and clean up cron immediately.
        blocked_task = "scenario-b-blocked"
        run(
            "python3", str(OPS), "--ledger", str(ledger), "init-task", blocked_task,
            "--goal", "Stop only when missing credentials make the task unrecoverable",
            "--owner", "main-agent",
            "--channel", "discord",
            "--requester-channel", "1484432523781083197",
            "--workflow", "Inspect",
            "--workflow", "Attempt recovery",
            "--workflow", "Escalate blocker if unrecoverable",
            "--message-ref", "discord:msg:scenario-b",
            "--next-action", "Try to recover or confirm blocker truth",
        )
        run("python3", str(CRON_TOOL), "--ledger", str(ledger), "--cron-dir", str(cron_dir), "install", blocked_task)
        run(
            "python3", str(LEDGER_TOOL), "--ledger", str(ledger), "owner-reply", blocked_task,
            "--reply", "B",
            "--summary", "Owner confirmed recovery is impossible without credentials",
            "--reason", "Missing upstream credentials",
            "--need", "Owner provides valid credentials",
            "--safe-next-step", "Wait for credentials, then restart execution",
            "--next-action", "Wait for credentials",
        )
        preview_b = run_json("python3", str(OPS), "--ledger", str(ledger), "preview-tick", blocked_task)
        assert preview_b["state"] == "BLOCKED_ESCALATE"
        assert preview_b["remove_monitor"] is True
        blocked_tick = run_json("python3", str(CRON_TOOL), "--ledger", str(ledger), "--cron-dir", str(cron_dir), "run-once", "--task-id", blocked_task)
        assert blocked_tick["report"]["state"] in {"BLOCKED_ESCALATE", "STOP_AND_DELETE"}
        assert blocked_tick["cron_removed"] is True
        blocked_ledger = task_from(ledger, blocked_task)
        assert blocked_ledger["status"] == "BLOCKED"
        assert blocked_ledger["monitoring"]["cron_state"] == "DELETED"

        print(json.dumps({
            "ok": True,
            "ledger": str(ledger),
            "scenario_a": {
                "task_id": resume_task,
                "states": [preview_a1["state"], second_tick["report"]["state"]],
                "cron_state": resume_ledger["monitoring"]["cron_state"],
            },
            "scenario_b": {
                "task_id": blocked_task,
                "states": [preview_b["state"], blocked_tick["report"]["state"]],
                "cron_removed": blocked_tick["cron_removed"],
                "cron_state": blocked_ledger["monitoring"]["cron_state"],
            },
        }, ensure_ascii=False, indent=2))


if __name__ == "__main__":
    main()
