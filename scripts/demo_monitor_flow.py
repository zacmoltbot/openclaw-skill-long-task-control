#!/usr/bin/env python3
import json
import subprocess
import tempfile
from copy import deepcopy
from pathlib import Path

ROOT = Path(__file__).resolve().parent.parent
TASK_LEDGER = ROOT / "scripts" / "task_ledger.py"
MONITOR = ROOT / "scripts" / "monitor_nudge.py"


def run(*args):
    return subprocess.run(args, check=True, text=True, capture_output=True)


def load(path):
    return json.loads(Path(path).read_text())


def force_ages(ledger_path: Path):
    ledger = load(ledger_path)
    tasks = {task["task_id"]: task for task in ledger["tasks"]}

    running = tasks["demo-running"]
    running["last_checkpoint_at"] = "2020-01-01T00:00:00+00:00"
    running["heartbeat"]["last_progress_at"] = "2020-01-01T00:00:00+00:00"
    running["heartbeat"]["last_heartbeat_at"] = "2020-01-01T00:00:00+00:00"

    blocked = tasks["demo-blocked"]
    blocked["last_checkpoint_at"] = "2020-01-01T00:00:00+00:00"
    blocked["heartbeat"]["last_progress_at"] = "2020-01-01T00:00:00+00:00"
    blocked["heartbeat"]["last_heartbeat_at"] = "2020-01-01T00:00:00+00:00"

    completed = tasks["demo-complete"]
    completed["last_checkpoint_at"] = "2020-01-01T00:00:00+00:00"
    completed["heartbeat"]["last_progress_at"] = "2020-01-01T00:00:00+00:00"
    completed["heartbeat"]["last_heartbeat_at"] = "2020-01-01T00:00:00+00:00"

    ledger_path.write_text(json.dumps(ledger, ensure_ascii=False, indent=2) + "\n")


def main():
    with tempfile.TemporaryDirectory() as tmpdir:
        ledger_path = Path(tmpdir) / "ledger.json"

        run(
            "python3", str(TASK_LEDGER), "--ledger", str(ledger_path), "init", "demo-running",
            "--goal", "Demonstrate NUDGE_MAIN_AGENT",
            "--owner", "main-agent",
            "--channel", "discord",
            "--workflow", "Inspect",
            "--workflow", "Implement",
            "--activation-announced",
            "--next-action", "Resume implementation",
            "--expected-interval-sec", "30",
            "--timeout-sec", "60",
            "--nudge-after-sec", "60",
        )
        run(
            "python3", str(TASK_LEDGER), "--ledger", str(ledger_path), "init", "demo-blocked",
            "--goal", "Demonstrate BLOCKED_ESCALATE",
            "--owner", "main-agent",
            "--channel", "discord",
            "--workflow", "Submit",
            "--workflow", "Wait",
            "--activation-announced",
            "--next-action", "Wait for approval",
            "--expected-interval-sec", "30",
            "--timeout-sec", "60",
            "--blocked-escalate-after-sec", "30",
        )
        run(
            "python3", str(TASK_LEDGER), "--ledger", str(ledger_path), "block", "demo-blocked",
            "--reason", "Need user approval",
            "--need", "User approval",
            "--safe-next-step", "Wait until user approves",
        )
        run(
            "python3", str(TASK_LEDGER), "--ledger", str(ledger_path), "init", "demo-complete",
            "--goal", "Demonstrate STOP_AND_DELETE",
            "--owner", "main-agent",
            "--channel", "discord",
            "--workflow", "Build",
            "--workflow", "Validate",
            "--activation-announced",
            "--next-action", "None",
            "--expected-interval-sec", "30",
            "--timeout-sec", "60",
        )
        run(
            "python3", str(TASK_LEDGER), "--ledger", str(ledger_path), "checkpoint", "demo-complete",
            "--kind", "COMPLETED",
            "--summary", "Validated deliverable",
            "--status", "COMPLETED",
            "--fact", "validation=passed",
        )

        force_ages(ledger_path)
        before = load(ledger_path)
        before_truth = {task["task_id"]: deepcopy({k: v for k, v in task.items() if k not in {"heartbeat", "monitoring"}}) for task in before["tasks"]}

        result = run("python3", str(MONITOR), "--ledger", str(ledger_path), "--apply-supervision")
        payload = json.loads(result.stdout)
        reports = {item["task_id"]: item for item in payload["reports"]}

        assert reports["demo-running"]["state"] == "NUDGE_MAIN_AGENT", reports["demo-running"]
        assert reports["demo-running"]["action_payload"]["kind"] == "NUDGE_MAIN_AGENT"
        assert reports["demo-blocked"]["state"] == "BLOCKED_ESCALATE", reports["demo-blocked"]
        assert reports["demo-blocked"]["action_payload"]["kind"] == "BLOCKED_ESCALATE"
        assert reports["demo-complete"]["state"] == "STOP_AND_DELETE", reports["demo-complete"]
        assert reports["demo-complete"]["action_payload"]["kind"] == "STOP_AND_DELETE"

        after = load(ledger_path)
        for task in after["tasks"]:
            truth = {k: v for k, v in task.items() if k not in {"heartbeat", "monitoring"}}
            assert truth == before_truth[task["task_id"]], f"Task truth mutated by monitor: {task['task_id']}"

        running = next(task for task in after["tasks"] if task["task_id"] == "demo-running")
        assert running["monitoring"]["nudge_count"] == 1
        assert running["heartbeat"]["watchdog_state"] == "NUDGE_MAIN_AGENT"

        blocked = next(task for task in after["tasks"] if task["task_id"] == "demo-blocked")
        assert blocked["monitoring"]["last_escalated_at"]
        assert blocked["monitoring"]["cron_state"] == "DELETE_REQUESTED"

        completed = next(task for task in after["tasks"] if task["task_id"] == "demo-complete")
        assert completed["monitoring"]["cron_state"] == "DELETE_REQUESTED"

        print(json.dumps({
            "ok": True,
            "ledger": str(ledger_path),
            "states": {task_id: report["state"] for task_id, report in reports.items()},
        }, ensure_ascii=False, indent=2))


if __name__ == "__main__":
    main()
