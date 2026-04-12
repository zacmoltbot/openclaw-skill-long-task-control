#!/usr/bin/env python3
"""
GAP-1 stall detection E2E: step completes (external job done, checkpoint written)
but next step never starts — monitor must detect this and NUDGE_MAIN_AGENT, not say OK forever.
"""
import json
import subprocess
import tempfile
from datetime import datetime, timezone, timedelta
from pathlib import Path

ROOT = Path(__file__).resolve().parent.parent
OPS = ROOT / "scripts" / "openclaw_ops.py"
LEDGER_TOOL = ROOT / "scripts" / "task_ledger.py"
MONITOR = ROOT / "scripts" / "monitor_nudge.py"


def run(*args, check=True):
    return subprocess.run(args, check=check, text=True, capture_output=True)


def run_json(*args, check=True):
    return json.loads(run(*args, check=check).stdout)


def load(path: Path):
    return json.loads(path.read_text())


def save(path: Path, data):
    path.write_text(json.dumps(data, ensure_ascii=False, indent=2) + "\n")


def make_stale(path: Path, task_id: str, age_days: int = 1):
    """Make checkpoint/progress stale by setting timestamps to N days ago."""
    ledger = load(path)
    for task in ledger["tasks"]:
        if task["task_id"] == task_id:
            old = (datetime.now(timezone.utc) - timedelta(days=age_days)).astimezone().isoformat(timespec="seconds")
            task["last_checkpoint_at"] = old
            task.setdefault("heartbeat", {})["last_progress_at"] = old
            task["heartbeat"]["last_heartbeat_at"] = old
            break
    save(path, ledger)


def init_task(path: Path, task_id: str, goal: str, workflows: list[str]):
    cmd = [
        "python3", str(OPS), "--ledger", str(path), "init-task", task_id,
        "--goal", goal,
        "--owner", "main-agent",
        "--channel", "discord",
        "--requester-channel", "1484432523781083197",
        "--next-action", "run step 1",
        "--expected-interval-sec", "300",
        "--timeout-sec", "1800",
    ]
    for w in workflows:
        cmd.extend(["--workflow", w])
    result = subprocess.run(cmd, check=True, text=True, capture_output=True)
    if result.returncode != 0:
        raise RuntimeError(f"init-task failed: {result.stderr}")
    return json.loads(result.stdout)


def set_workflow_state(path: Path, task_id: str, step_id: str, state: str):
    """Set a workflow step's state directly in the ledger."""
    ledger = load(path)
    for task in ledger["tasks"]:
        if task["task_id"] == task_id:
            for step in task.get("workflow", []):
                if step.get("id") == step_id:
                    step["state"] = state
                    break
            break
    save(path, ledger)


def run_monitor(ledger_path: Path):
    return run_json("python3", str(MONITOR), "--ledger", str(ledger_path), "--apply-supervision")


def main():
    with tempfile.TemporaryDirectory() as tmpdir:
        tmp = Path(tmpdir)
        ledger_path = tmp / "state" / "long-task-ledger.json"

        # Scenario GAP-1a: step-01 completes (external job DONE), checkpoint written,
        # but step-02 never starts. Monitor must NUDGE after one stale check,
        # BLOCKED_ESCALATE after 3 checks.
        task_a = "gap1a-step-done-stall"
        init_task(ledger_path, task_a, "Step done but stalled — must nudge",
                 ["Step 1: run external", "Step 2: process result", "Step 3: deliver"])

        # Simulate: external job runs and completes normally
        run_json(
            "python3", str(LEDGER_TOOL), "--ledger", str(ledger_path), "external-job", task_a,
            "--provider", "runninghub",
            "--job-id", "rh-001",
            "--state", "COMPLETED",
            "--summary", "External job succeeded",
            "--current-checkpoint", "step-01",
            "--next-action", "process result for step 2",
            "--fact", "provider_status_handle=runninghub:rh-001",
        )

        # Set step-01 workflow state to terminal (DONE) to simulate step completion
        set_workflow_state(ledger_path, task_a, "step-01", "DONE")

        # Verify: re-read to confirm DONE persisted
        ledger_a = load(ledger_path)
        task_a_obj = next((t for t in ledger_a["tasks"] if t["task_id"] == task_a), None)
        assert task_a_obj is not None, "task a not found"
        step_01 = next((s for s in task_a_obj["workflow"] if s["id"] == "step-01"), None)
        assert step_01 is not None, "step-01 not found in workflow"
        assert step_01["state"] == "DONE", f"step-01 state should be DONE, got {step_01['state']}"
        # Verify current_checkpoint still step-01 (hasn't advanced)
        assert task_a_obj["current_checkpoint"] == "step-01", \
            f"current_checkpoint should be step-01, got {task_a_obj['current_checkpoint']}"

        # Make timestamps stale (simulate: step-01 completed but 1+ days of silence)
        make_stale(ledger_path, task_a, age_days=1)

        # First monitor tick: GAP-1 should fire NUDGE_MAIN_AGENT
        report_a1 = run_monitor(ledger_path)
        state_a1 = next((r for r in report_a1["reports"] if r["task_id"] == task_a), None)
        assert state_a1 is not None, f"no report for {task_a}"
        # GAP-1 fires fast (one check interval) → NUDGE_MAIN_AGENT
        assert state_a1["state"] == "NUDGE_MAIN_AGENT", \
            f"tick1 expected NUDGE_MAIN_AGENT, got {state_a1['state']}: {state_a1['reason']}"

        # Second monitor tick (still stale, still terminal step, step hasn't moved)
        # After 3 total checks → BLOCKED_ESCALATE
        report_a2 = run_monitor(ledger_path)
        state_a2 = next((r for r in report_a2["reports"] if r["task_id"] == task_a), None)

        report_a3 = run_monitor(ledger_path)
        state_a3 = next((r for r in report_a3["reports"] if r["task_id"] == task_a), None)
        assert state_a3["state"] == "BLOCKED_ESCALATE", \
            f"tick3 expected BLOCKED_ESCALATE, got {state_a3['state']}: {state_a3['reason']}"

        # Verify cron_state is DELETE_REQUESTED
        ledger_a2 = load(ledger_path)
        task_a2 = next((t for t in ledger_a2["tasks"] if t["task_id"] == task_a), None)
        assert task_a2["monitoring"]["cron_state"] == "DELETE_REQUESTED"

        print(json.dumps({
            "ok": True,
            "scenario": "GAP-1: step completed but stalled",
            "tick1_state": state_a1["state"],
            "tick2_state": state_a2["state"],
            "tick3_state": state_a3["state"],
            "cron_state": task_a2["monitoring"]["cron_state"],
            "gap1a": "PASS",
        }, ensure_ascii=False, indent=2))


if __name__ == "__main__":
    main()
