#!/usr/bin/env python3
"""
Regression test: ISSUE-09 deliver terminal convergence + ISSUE-10 terminal blocked cleanup gap.

Both issues share the same root: normalize_resumable_item() did not emit ITEM_BLOCKED
progress + bridge.sync_item_blocked() when a RUNNING item exhausted its retry budget.
This caused the ledger task status to NOT reflect BLOCKED, making the monitor fall
through to HEARTBEAT_DUE/NUDGE_MAIN_AGENT instead of BLOCKED_ESCALATE/STOP_AND_DELETE.

FIX: Added _emit_item_blocked() helper + calls in normalize_resumable_item() for the
RUNNING/retry-exhausted and RUNNING/cannot-resume paths.

Tests:
1) ISSUE-10: RUNNING item exhausts retry budget → ITEM_BLOCKED in progress, preview=blocked, monitor≠HEARTBEAT_DUE
2) ISSUE-09: terminal step fails + exhausts retries → BLOCKED, no pseudo-running, monitor aware
"""
from __future__ import annotations

import json
import os
import subprocess
import tempfile
from pathlib import Path

ROOT = Path(__file__).resolve().parent.parent
LEDGER_TOOL = ROOT / "scripts" / "task_ledger.py"
RUNNER = ROOT / "scripts" / "runner_engine.py"
EXECUTOR = ROOT / "scripts" / "executor_engine.py"
MONITOR = ROOT / "scripts" / "monitor_nudge.py"


def run(*args, **kwargs):
    kwargs.setdefault("timeout", 10)
    return subprocess.run(args, check=True, text=True, capture_output=True, **kwargs)


def run_json(*args, **kwargs):
    r = run(*args, **kwargs)
    return json.loads(r.stdout.strip())


def load(path):
    return json.loads(Path(path).read_text())


def create_job(ledger_path, jobs_root, task_id, workflow_steps, goal="test goal"):
    """Create a task + execution job without bootstrap-task (no gateway needed)."""
    job_id = f"{task_id}-job"

    # 1. Init task in ledger
    workflow_args = []
    for step in workflow_steps:
        workflow_args.extend(["--workflow", step])

    run(
        "python3", str(LEDGER_TOOL), "--ledger", str(ledger_path),
        "init", task_id,
        "--goal", goal,
        "--owner", "main-agent",
        "--channel", "discord",
        "--next-action", "Run execution",
        *workflow_args,
    )

    # 2. Create job spec directly
    items = []
    for idx, step in enumerate(workflow_steps, start=1):
        step_id = f"step-{idx:02d}"
        parts = step.split("::")
        title = parts[0].strip()
        item = {"item_id": step_id, "title": title, "checkpoint": step_id}
        for chunk in parts[1:]:
            if "=" not in chunk:
                continue
            k, v = chunk.split("=", 1)
            k = k.strip().lower().replace("-", "_")
            v = v.strip()
            if k in {"shell", "cwd", "next_action", "generic_manual_mode"}:
                item[k] = v
            elif k in {"timeout", "timeout_sec", "retry_budget", "max_retries"}:
                field = "retry_budget" if k == "max_retries" else k
                try:
                    item[field] = int(v)
                except ValueError:
                    item[field] = v
            elif k in {"artifact", "output", "expect"}:
                item.setdefault("expect_artifacts", []).append(v)
        items.append(item)

    spec = {
        "job_id": job_id,
        "kind": "generic-long-task",
        "adapter": "generic_manual",
        "mode": "serial",
        "bridge": {"ledger": str(ledger_path), "task_id": task_id},
        "items": items,
    }

    spec_path = Path(jobs_root) / job_id / "job-spec.json"
    spec_path.parent.mkdir(parents=True, exist_ok=True)
    spec_path.write_text(json.dumps(spec, ensure_ascii=False, indent=2) + "\n")

    # 3. Init job via runner
    run_json(
        "python3", str(RUNNER), "--jobs-root", str(jobs_root),
        "init-job", str(spec_path),
        "--ledger", str(ledger_path),
        "--task-id", task_id,
    )

    return job_id


def progress_kinds(job_id, jobs_root):
    p = Path(jobs_root) / job_id / "progress.jsonl"
    if not p.exists():
        return []
    return [json.loads(l)["kind"] for l in p.read_text().splitlines() if l.strip()]


def main() -> None:
    failures = []

    # ── TEST 1: ISSUE-10 — RUNNING item exhausts retry budget ──
    with tempfile.TemporaryDirectory() as td:
        ledger = Path(td) / "ledger.json"
        jobs_root = Path(td) / "jobs"
        task_id = "issue10-terminal-blocked-cleanup"
        job_id = create_job(
            ledger, jobs_root, task_id,
            ["Step 1 :: shell=false :: retry_budget=1 :: title=failing-step"],
            goal="Verify terminal blocked cleanup: RUNNING item exhausts retry budget",
        )

        # Attempt 1 → RETRY
        r1 = run_json("python3", str(EXECUTOR), "--jobs-root", str(jobs_root),
                      "run-next", job_id, "--execution-owner", "regression-test")
        assert r1.get("status") == "RETRY", f"attempt 1 should RETRY, got {r1}"

        # Attempt 2 → retry exhausted → BLOCKED (normalize_resumable_item path)
        r2 = run_json("python3", str(EXECUTOR), "--jobs-root", str(jobs_root),
                      "run-next", job_id, "--execution-owner", "regression-test")
        assert r2.get("status") == "BLOCKED", f"attempt 2 should BLOCKED, got {r2}"

        job = load(jobs_root / job_id / "job.json")
        kinds = progress_kinds(job_id, jobs_root)

        # CORE: ITEM_BLOCKED must appear in progress
        if "ITEM_BLOCKED" not in kinds:
            failures.append(f"ISSUE-10: ITEM_BLOCKED missing from progress.jsonl: {kinds}")

        # Job must be terminal BLOCKED
        if job["status"] != "BLOCKED":
            failures.append(f"ISSUE-10: job status should be BLOCKED, got {job['status']}")

        blocked_item = next((i for i in job["items"] if i["status"] == "BLOCKED"), None)
        if not blocked_item:
            failures.append("ISSUE-10: no BLOCKED item found")
        elif blocked_item.get("blocked_reason") != "RETRY_BUDGET_EXHAUSTED":
            failures.append(f"ISSUE-10: blocked_reason should be RETRY_BUDGET_EXHAUSTED, got {blocked_item.get('blocked_reason')}")

        # Preview must say blocked (not infinite loop)
        preview = run_json("python3", str(EXECUTOR), "--jobs-root", str(jobs_root),
                           "preview", job_id)
        if preview.get("action") != "blocked":
            failures.append(f"ISSUE-10: preview action should be blocked, got {preview}")

        # Monitor must NOT pick HEARTBEAT_DUE/NUDGE_MAIN_AGENT (those indicate task status was not BLOCKED in ledger)
        mr = run_json("python3", str(MONITOR), "--ledger", str(ledger))
        report = next((r for r in mr["reports"] if r["task_id"] == task_id), None)
        if not report:
            failures.append(f"ISSUE-10: no monitor report for {task_id}")
        elif report["state"] in {"HEARTBEAT_DUE", "NUDGE_MAIN_AGENT"}:
            failures.append(f"ISSUE-10 REGRESSION: monitor fell through to {report['state']} (task status not reflected as BLOCKED in ledger)")
        elif report["state"] not in {"BLOCKED_ESCALATE", "STOP_AND_DELETE", "OWNER_RECONCILE", "EXECUTOR_UNHEALTHY"}:
            failures.append(f"ISSUE-10: unexpected monitor state {report['state']}")

        print(json.dumps({
            "test": "ISSUE-10 terminal blocked cleanup",
            "job_status": job["status"],
            "progress_kinds": kinds,
            "blocked_item": {"item_id": blocked_item["item_id"], "reason": blocked_item.get("blocked_reason")} if blocked_item else None,
            "preview_action": preview.get("action"),
            "monitor_state": report["state"] if report else None,
            "ok": True,
        }, ensure_ascii=False, indent=2))

    # ── TEST 2: ISSUE-09 — terminal step fails and exhausts retries ──
    with tempfile.TemporaryDirectory() as td:
        ledger = Path(td) / "ledger.json"
        jobs_root = Path(td) / "jobs"
        task_id = "issue09-deliver-terminal-convergence"
        # step-01 completes normally; step-02 (terminal) fails with retry exhaustion
        job_id = create_job(
            ledger, jobs_root, task_id,
            [
                "Step 1 :: shell=true :: title=completed-step",
                "Step 2 (Terminal) :: shell=exit 1 :: retry_budget=1 :: title=terminal-fail",
            ],
            goal="Verify deliver terminal convergence: last step fails and exhausts retries",
        )

        # Run 1: step-01 DONE, step-02 RUNNING
        r1 = run_json("python3", str(EXECUTOR), "--jobs-root", str(jobs_root),
                      "run-next", job_id, "--execution-owner", "regression-test")
        assert r1.get("job_status") == "RUNNING", f"run 1 should RUNNING (more items), got {r1}"

        # Run 2: step-02 fails → RETRY
        r2 = run_json("python3", str(EXECUTOR), "--jobs-root", str(jobs_root),
                      "run-next", job_id, "--execution-owner", "regression-test")
        assert r2.get("status") == "RETRY", f"run 2 should RETRY, got {r2}"

        # Run 3: step-02 fails again → retry exhausted → BLOCKED
        r3 = run_json("python3", str(EXECUTOR), "--jobs-root", str(jobs_root),
                      "run-next", job_id, "--execution-owner", "regression-test")
        assert r3.get("status") == "BLOCKED", f"run 3 should BLOCKED, got {r3}"

        job = load(jobs_root / job_id / "job.json")
        kinds = progress_kinds(job_id, jobs_root)

        # CORE: ITEM_BLOCKED must appear
        if "ITEM_BLOCKED" not in kinds:
            failures.append(f"ISSUE-09: ITEM_BLOCKED missing from progress.jsonl: {kinds}")

        # Job must be BLOCKED (not stuck in pseudo-running)
        if job["status"] != "BLOCKED":
            failures.append(f"ISSUE-09: job status should be BLOCKED, got {job['status']}")

        terminal_item = job["items"][1]  # step-02
        if terminal_item["status"] != "BLOCKED":
            failures.append(f"ISSUE-09: terminal item status should be BLOCKED, got {terminal_item['status']}")

        # Preview must be blocked
        preview = run_json("python3", str(EXECUTOR), "--jobs-root", str(jobs_root),
                           "preview", job_id)
        if preview.get("action") != "blocked":
            failures.append(f"ISSUE-09: preview should be blocked, got {preview}")

        # Monitor must be aware (not HEARTBEAT_DUE/NUDGE_MAIN_AGENT)
        mr = run_json("python3", str(MONITOR), "--ledger", str(ledger))
        report = next((r for r in mr["reports"] if r["task_id"] == task_id), None)
        if not report:
            failures.append(f"ISSUE-09: no monitor report for {task_id}")
        elif report["state"] in {"HEARTBEAT_DUE", "NUDGE_MAIN_AGENT"}:
            failures.append(f"ISSUE-09 REGRESSION: monitor fell through to {report['state']}")
        elif report["state"] not in {"BLOCKED_ESCALATE", "STOP_AND_DELETE", "OWNER_RECONCILE", "EXECUTOR_UNHEALTHY"}:
            failures.append(f"ISSUE-09: unexpected monitor state {report['state']}")

        print(json.dumps({
            "test": "ISSUE-09 deliver terminal convergence",
            "job_status": job["status"],
            "progress_kinds": kinds,
            "terminal_item": {"status": terminal_item["status"], "reason": terminal_item.get("blocked_reason")},
            "preview_action": preview.get("action"),
            "monitor_state": report["state"] if report else None,
            "ok": True,
        }, ensure_ascii=False, indent=2))

    if failures:
        for f in failures:
            print(f"FAIL: {f}", flush=True)
        raise SystemExit(1)

    print(json.dumps({
        "ok": True,
        "tests_passed": 2,
        "summary": "ISSUE-09 + ISSUE-10 regressions: terminal blocked truth converges correctly",
    }, ensure_ascii=False, indent=2))


if __name__ == "__main__":
    main()
