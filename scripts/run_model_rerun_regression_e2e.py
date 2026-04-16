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


with tempfile.TemporaryDirectory() as td:
    tmp = Path(td)
    ledger = tmp / "state" / "long-task-ledger.json"

    run(
        "python3", str(OPS), "--ledger", str(ledger), "init-task", "rerun-demo",
        "--goal", "prove reruns create fresh runs",
        "--requester-channel", "demo",
        "--workflow", "Do the thing",
        "--next-action", "Start step-01",
    )
    run(
        "python3", str(OPS), "--ledger", str(ledger), "record-update", "STEP_PROGRESS", "rerun-demo",
        "--summary", "attempted first approach",
        "--current-checkpoint", "step-01",
        "--fact", "method=broken-v1",
    )
    run(
        "python3", str(OPS), "--ledger", str(ledger), "record-update", "BLOCKED", "rerun-demo",
        "--summary", "bad input contract",
        "--current-checkpoint", "step-01",
        "--safe-next-step", "fix prompt and rerun",
        "--next-action", "fix prompt and rerun",
        "--fact", "failure_type=INVALID_INPUT",
    )

    rerun = run_json(
        "python3", str(OPS), "--ledger", str(ledger), "rerun-task", "rerun-demo",
        "--reason", "user corrected the prompt",
        "--summary", "fresh run with corrected prompt",
        "--current-checkpoint", "step-01",
        "--next-action", "execute corrected prompt",
        "--previous-status", "FAILED",
        "--fact", "correction=prompt-v2",
    )
    assert rerun["previous_run"] == "run-01", rerun
    assert rerun["active_run_id"] == "run-02", rerun

    data = load(ledger)
    task = next(t for t in data["tasks"] if t["task_id"] == "rerun-demo")
    assert task["active_run_id"] == "run-02", task
    assert task["status"] == "RUNNING", task
    assert task["current_checkpoint"] == "step-01", task
    assert task["blocker"] is None, task
    assert task["observed"]["block"] is None, task
    assert task["artifacts"] == [], task
    assert len(task["runs"]) == 1, task["runs"]
    first = task["runs"][0]
    assert first["run_id"] == "run-01", first
    assert first["status"] == "FAILED", first
    assert first["blocker"]["reason"] == "bad input contract", first
    assert first["current_checkpoint"] == "step-01", first
    assert any(obs["event_type"] == "BLOCKED_CONFIRMED" for obs in first["observations"]), first
    assert task["observations"][0]["event_type"] == "STARTED", task["observations"]
    assert task["observations"][0]["facts"]["rerun_from"] == "run-01", task["observations"]
    assert task["observations"][0]["facts"]["correction"] == "prompt-v2", task["observations"]

    run(
        "python3", str(LEDGER_TOOL), "--ledger", str(ledger), "owner-reply", "rerun-demo",
        "--reply", "E",
        "--summary", "owner admitted previous rerun never actually started; create another fresh run",
        "--current-checkpoint", "step-01",
        "--next-action", "actually do the work now",
    )

    data2 = load(ledger)
    task2 = next(t for t in data2["tasks"] if t["task_id"] == "rerun-demo")
    assert task2["active_run_id"] == "run-03", task2
    assert len(task2["runs"]) == 2, task2["runs"]
    assert task2["runs"][1]["run_id"] == "run-02", task2["runs"]
    assert task2["runs"][1]["status"] == "RUNNING", task2["runs"]
    assert task2["observations"][0]["event_type"] == "STARTED", task2["observations"]
    assert task2["observations"][0]["facts"]["rerun_from"] == "run-02", task2["observations"]

    print(json.dumps({
        "ok": True,
        "task_id": "rerun-demo",
        "archived_runs": [r["run_id"] for r in task2["runs"]],
        "active_run_id": task2["active_run_id"],
    }, ensure_ascii=False, indent=2))
