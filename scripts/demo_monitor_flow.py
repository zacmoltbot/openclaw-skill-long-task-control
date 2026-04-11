#!/usr/bin/env python3
import json
import subprocess
import tempfile
from pathlib import Path

ROOT = Path(__file__).resolve().parent.parent
TASK_LEDGER = ROOT / "scripts" / "task_ledger.py"
MONITOR = ROOT / "scripts" / "monitor_nudge.py"


def run(*args):
    return subprocess.run(args, check=True, text=True, capture_output=True)


def load(path):
    return json.loads(Path(path).read_text())


def task_map(ledger):
    return {task["task_id"]: task for task in ledger["tasks"]}


def stale_task(task, *, nudge_count=0):
    old = "2020-01-01T00:00:00+00:00"
    task["last_checkpoint_at"] = old
    task.setdefault("heartbeat", {})["last_progress_at"] = old
    task.setdefault("heartbeat", {})["last_heartbeat_at"] = old
    task.setdefault("monitoring", {})["nudge_count"] = nudge_count
    if nudge_count:
        task["monitoring"]["last_nudge_at"] = old


def assert_report_state(payload, task_id, expected_state):
    report = next(item for item in payload["reports"] if item["task_id"] == task_id)
    assert report["state"] == expected_state, report
    return report


def main():
    with tempfile.TemporaryDirectory() as tmpdir:
        ledger_path = Path(tmpdir) / "ledger.json"

        for task_id, goal, next_action in [
            ("demo-resume", "Demonstrate reconcile -> resume execution", "Resume implementation"),
            ("demo-block-route", "Demonstrate reconcile -> blocked", "Ask owner why progress stopped"),
            ("demo-complete-route", "Demonstrate reconcile -> completed", "Ask owner whether task already finished"),
        ]:
            run(
                "python3", str(TASK_LEDGER), "--ledger", str(ledger_path), "init", task_id,
                "--goal", goal,
                "--owner", "main-agent",
                "--channel", "discord",
                "--workflow", "Inspect",
                "--workflow", "Implement",
                "--activation-announced",
                "--next-action", next_action,
                "--expected-interval-sec", "30",
                "--timeout-sec", "60",
                "--nudge-after-sec", "60",
                "--escalate-after-nudges", "1",
                "--max-nudges", "2",
                "--blocked-escalate-after-sec", "30",
            )

        ledger = load(ledger_path)
        tasks = task_map(ledger)
        stale_task(tasks["demo-resume"], nudge_count=1)
        stale_task(tasks["demo-block-route"], nudge_count=1)
        stale_task(tasks["demo-complete-route"], nudge_count=1)
        ledger_path.write_text(json.dumps(ledger, ensure_ascii=False, indent=2) + "\n")

        first_monitor = json.loads(run("python3", str(MONITOR), "--ledger", str(ledger_path), "--apply-supervision").stdout)
        for task_id in ["demo-resume", "demo-block-route", "demo-complete-route"]:
            report = assert_report_state(first_monitor, task_id, "OWNER_RECONCILE")
            assert report["action_payload"]["kind"] == "OWNER_RECONCILE"

        run(
            "python3", str(TASK_LEDGER), "--ledger", str(ledger_path), "owner-reply", "demo-resume",
            "--reply", "E",
            "--summary", "Owner admitted the task was forgotten; resume now",
            "--next-action", "Resume execution immediately and publish checkpoint 2",
            "--fact", "owner_statement=forgot_to_do_it",
        )
        run(
            "python3", str(TASK_LEDGER), "--ledger", str(ledger_path), "owner-reply", "demo-block-route",
            "--reply", "B",
            "--summary", "Owner confirmed dependency is missing",
            "--reason", "Need upstream credentials",
            "--need", "Credentials from owner",
            "--safe-next-step", "Wait for credentials, then resume implementation",
            "--fact", "owner_statement=blocked_on_credentials",
        )
        run(
            "python3", str(TASK_LEDGER), "--ledger", str(ledger_path), "owner-reply", "demo-complete-route",
            "--reply", "C",
            "--summary", "Owner confirmed task already completed",
            "--validation", "owner supplied validated artifact checksum",
            "--artifact", "/tmp/demo-complete-route/output.txt",
            "--fact", "owner_statement=already_done",
        )

        after_reply = task_map(load(ledger_path))
        resume_task = after_reply["demo-resume"]
        assert resume_task["status"] == "RUNNING"
        assert resume_task["next_action"] == "Resume execution immediately and publish checkpoint 2"
        assert resume_task["monitoring"]["owner_response_kind"] == "E_FORGOT_OR_NOT_DOING"
        assert resume_task["checkpoints"][-1]["facts"]["resume_required"] == "true"

        blocked_task = after_reply["demo-block-route"]
        assert blocked_task["status"] == "BLOCKED"
        assert blocked_task["blocker"]["reason"] == "Need upstream credentials"
        assert blocked_task["monitoring"]["owner_response_kind"] == "B_BLOCKED"

        completed_task = after_reply["demo-complete-route"]
        assert completed_task["status"] == "COMPLETED"
        assert completed_task["validation"]
        assert completed_task["monitoring"]["owner_response_kind"] == "C_COMPLETED"

        second_monitor = json.loads(run("python3", str(MONITOR), "--ledger", str(ledger_path), "--apply-supervision").stdout)
        resume_report = assert_report_state(second_monitor, "demo-resume", "OK")
        blocked_report = assert_report_state(second_monitor, "demo-block-route", "BLOCKED_ESCALATE")
        completed_report = assert_report_state(second_monitor, "demo-complete-route", "STOP_AND_DELETE")
        assert blocked_report["action_payload"]["kind"] == "BLOCKED_ESCALATE"
        assert completed_report["action_payload"]["kind"] == "STOP_AND_DELETE"

        final_tasks = task_map(load(ledger_path))
        assert final_tasks["demo-block-route"]["monitoring"]["cron_state"] == "DELETE_REQUESTED"
        assert final_tasks["demo-complete-route"]["monitoring"]["cron_state"] == "DELETE_REQUESTED"
        assert final_tasks["demo-resume"]["heartbeat"]["watchdog_state"] == "OK"

        print(json.dumps({
            "ok": True,
            "ledger": str(ledger_path),
            "first_monitor_states": {item["task_id"]: item["state"] for item in first_monitor["reports"]},
            "owner_reply_routes": {
                "demo-resume": final_tasks["demo-resume"]["monitoring"]["owner_response_kind"],
                "demo-block-route": final_tasks["demo-block-route"]["monitoring"]["owner_response_kind"],
                "demo-complete-route": final_tasks["demo-complete-route"]["monitoring"]["owner_response_kind"],
            },
            "second_monitor_states": {
                "demo-resume": resume_report["state"],
                "demo-block-route": blocked_report["state"],
                "demo-complete-route": completed_report["state"],
            },
        }, ensure_ascii=False, indent=2))


if __name__ == "__main__":
    main()
