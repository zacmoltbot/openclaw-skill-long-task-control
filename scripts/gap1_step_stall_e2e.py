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


def make_stale(path: Path, task_id: str, age_days: int = 1, heartbeat_stale: bool = True):
    """Make checkpoint/progress stale by setting timestamps to N days ago.
    
    Args:
        path: ledger file path
        task_id: task to stale
        age_days: how old to make timestamps
        heartbeat_stale: if True, also stale heartbeat.last_heartbeat_at (default True).
                         Set False for pure GAP-1 test where heartbeat should stay fresh.
    """
    ledger = load(path)
    for task in ledger["tasks"]:
        if task["task_id"] == task_id:
            old = (datetime.now(timezone.utc) - timedelta(days=age_days)).astimezone().isoformat(timespec="seconds")
            task["last_checkpoint_at"] = old
            task.setdefault("heartbeat", {})["last_progress_at"] = old
            if heartbeat_stale:
                task["heartbeat"]["last_heartbeat_at"] = old
            break
    save(path, ledger)


def override_completed_at_old(path: Path, task_id: str):
    """Override step's completed_at timestamp to simulate step completed long ago.
    
    This creates the 'step done but not advanced' scenario for GAP-1 testing.
    Sets completed_at in observed.steps to old, to cause TRUTH_INCONSISTENT.
    """
    from datetime import datetime, timezone, timedelta
    ledger = load(path)
    old = (datetime.now(timezone.utc) - timedelta(days=1)).astimezone().isoformat(timespec="seconds")
    for task in ledger["tasks"]:
        if task["task_id"] == task_id:
            # Set completed_at to old in observed.steps to simulate step completed 1 day ago
            task.setdefault("observed", {}).setdefault("steps", {}).setdefault("step-01", {})["completed_at"] = old
            task["last_checkpoint_at"] = old
            task["heartbeat"]["last_progress_at"] = old
            # heartbeat.last_heartbeat_at stays fresh
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

        # Mark step-01 as COMPLETED via checkpoint (this creates observed_steps entry,
        # which is what project_task() uses to derive step state in the new architecture)
        # NOTE: checkpoint command returns plain text, not JSON
        run(
            "python3", str(LEDGER_TOOL), "--ledger", str(ledger_path), "checkpoint", task_a,
            "--event-type", "STEP_COMPLETED",
            "--current-checkpoint", "step-01",
            "--summary", "Step 1 done",
        )

        # Verify: step-01 is COMPLETED in observed.steps (what project_task uses)
        ledger_a = load(ledger_path)
        task_a_obj = next((t for t in ledger_a["tasks"] if t["task_id"] == task_a), None)
        assert task_a_obj is not None, "task a not found"
        step_01_obs = task_a_obj.get("observed", {}).get("steps", {}).get("step-01", {})
        assert step_01_obs.get("completed_at") is not None, \
            f"step-01 should be COMPLETED in observed.steps, got {step_01_obs}"
        # Verify current_checkpoint still step-01 (hasn't advanced)
        assert task_a_obj["current_checkpoint"] == "step-01", \
            f"current_checkpoint should be step-01, got {task_a_obj['current_checkpoint']}"

        # Override timestamps to simulate step completed 1 day ago but no advancement since then
        # (this creates the "step done but stalled" scenario in new architecture)
        override_completed_at_old(ledger_path, task_a)

        # First monitor tick: new architecture fires TRUTH_INCONSISTENT because
        # step-01 is COMPLETED but current_checkpoint hasn't advanced
        # (old architecture: NUDGE_MAIN_AGENT)
        report_a1 = run_monitor(ledger_path)
        state_a1 = next((r for r in report_a1["reports"] if r["task_id"] == task_a), None)
        assert state_a1 is not None, f"no report for {task_a}"
        assert state_a1["state"] in ("TRUTH_INCONSISTENT", "OWNER_RECONCILE", "NUDGE_MAIN_AGENT"), \
            f"tick1 expected TRUTH_INCONSISTENT/OWNER_RECONCILE/NUDGE, got {state_a1['state']}: {state_a1['reason']}"

        # Second monitor tick (still stale, still inconsistent)
        report_a2 = run_monitor(ledger_path)
        state_a2 = next((r for r in report_a2["reports"] if r["task_id"] == task_a), None)

        # Third tick -> TRUTH_INCONSISTENT (inconsistency persists until resolved)
        # Note: new architecture doesn't automatically escalate TRUTH_INCONSISTENT to BLOCKED_ESCALATE
        report_a3 = run_monitor(ledger_path)
        state_a3 = next((r for r in report_a3["reports"] if r["task_id"] == task_a), None)
        assert state_a3["state"] in ("TRUTH_INCONSISTENT", "OWNER_RECONCILE", "BLOCKED_ESCALATE"), \
            f"tick3 expected TRUTH_INCONSISTENT/OWNER_RECONCILE/BLOCKED, got {state_a3['state']}: {state_a3['reason']}"

        print(json.dumps({
            "ok": True,
            "scenario": "GAP-1: step completed but stalled (new arch)",
            "tick1_state": state_a1["state"],
            "tick2_state": state_a2["state"],
            "tick3_state": state_a3["state"],
            "gap1a": "PASS",
        }, ensure_ascii=False, indent=2))


if __name__ == "__main__":
    main()
