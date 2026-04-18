#!/usr/bin/env python3
from __future__ import annotations

import json
import os
import signal
import subprocess
import tempfile
import time
from pathlib import Path

ROOT = Path(__file__).resolve().parent.parent
OPS = ROOT / "scripts" / "openclaw_ops.py"
RUNNER = ROOT / "scripts" / "runner_engine.py"
EXECUTOR = ROOT / "scripts" / "executor_engine.py"


def run(*args: str, env=None, check=True) -> subprocess.CompletedProcess:
    return subprocess.run(args, text=True, capture_output=True, env=env, check=check)


def run_json(*args: str, env=None):
    return json.loads(run(*args, env=env).stdout)


def load(path: Path):
    return json.loads(path.read_text())


def task_from(path: Path, task_id: str):
    return next(task for task in load(path)["tasks"] if task["task_id"] == task_id)


def test_prompt_contract(tmp: Path):
    ledger = tmp / "prompt-ledger.json"
    jobs_root = tmp / "prompt-jobs"
    task_id = "prompt-contract"
    run_json(
        "python3", str(OPS), "--ledger", str(ledger), "bootstrap-task", task_id,
        "--goal", "verify executor prompt uses canonical contract",
        "--owner", "main-agent",
        "--channel", "discord",
        "--requester-channel", "discord:channel:1477237887136432162",
        "--workflow", "Step 1 :: shell=printf 'ok'",
        "--next-action", "Run",
        "--jobs-root", str(jobs_root),
        "--disabled",
        "--dry-run",
        "--no-auto-start-execution",
    )
    payload = run_json("python3", str(OPS), "--ledger", str(ledger), "run-executor", task_id, "--jobs-root", str(jobs_root), "--dry-run")
    prompt = payload["job"]["message"]
    assert f"--jobs-root {jobs_root}" in prompt, prompt
    assert f"run-next {task_id}-job" in prompt, prompt
    assert "executor_engine.py --ledger" not in prompt, prompt
    return {"prompt_contract": "ok"}


def test_interrupt_resume_delivery_and_monitor(tmp: Path):
    ledger = tmp / "resume-ledger.json"
    jobs_root = tmp / "resume-jobs"
    out = tmp / "out"
    out.mkdir()
    sink = tmp / "sink.json"
    env = os.environ.copy()
    env["LTC_DELIVERY_SINK_FILE"] = str(sink)
    marker = tmp / "started.txt"
    artifact1 = out / "artifact-01.txt"
    artifact2 = out / "artifact-02.txt"
    interrupt_script = tmp / "interruptible.py"
    interrupt_script.write_text(
        "from pathlib import Path\nimport sys, time\nPath(sys.argv[1]).write_text('started\\n')\nwhile True: time.sleep(0.2)\n"
    )
    task_id = "resume-gate"
    job_id = f"{task_id}-job"
    run_json(
        "python3", str(OPS), "--ledger", str(ledger), "bootstrap-task", task_id,
        "--goal", "reconcile interrupted step before advancing and deliver artifacts",
        "--owner", "main-agent",
        "--channel", "discord",
        "--requester-channel", "discord:channel:1477237887136432162",
        "--workflow", f"Step 1 :: shell=python3 {interrupt_script} {marker} :: artifact={artifact1}",
        "--workflow", f"Step 2 :: shell=printf 'two' > {artifact2} :: artifact={artifact2}",
        "--workflow", f"Deliver :: auto_action=deliver_artifacts :: deliver_artifacts={artifact1}|{artifact2}",
        "--next-action", "Run canonical execution",
        "--jobs-root", str(jobs_root),
        "--disabled",
        "--dry-run",
        "--no-auto-start-execution",
        env=env,
    )
    proc = subprocess.Popen(
        ["python3", str(RUNNER), "--jobs-root", str(jobs_root), "run-loop", job_id, "--max-steps", "1", "--execution-owner", "resume-test"],
        stdout=subprocess.PIPE,
        stderr=subprocess.PIPE,
        text=True,
        start_new_session=True,
        env=env,
    )
    deadline = time.time() + 10
    while time.time() < deadline and not marker.exists():
        time.sleep(0.1)
    assert marker.exists(), "step 1 never started"
    artifact1.write_text("reconciled")
    os.killpg(proc.pid, signal.SIGTERM)
    proc.communicate(timeout=10)

    preview = run_json("python3", str(EXECUTOR), "--jobs-root", str(jobs_root), "preview", job_id, env=env)
    assert preview["action"] == "execute_item", preview
    assert preview["item_id"] == "step-02", preview

    step2 = run_json("python3", str(EXECUTOR), "--jobs-root", str(jobs_root), "run-next", job_id, "--execution-owner", "resume-test", env=env)
    assert step2["item_id"] == "step-02" and step2["status"] == "DONE", step2
    delivery = run_json("python3", str(EXECUTOR), "--jobs-root", str(jobs_root), "run-next", job_id, "--execution-owner", "resume-test", env=env)
    assert delivery["item_id"] == "step-03" and delivery["job_status"] == "COMPLETED", delivery
    task = task_from(ledger, task_id)
    assert task["status"] == "COMPLETED", task
    delivered = load(sink)
    assert len(delivered) == 2, delivered

    ledger_payload = load(ledger)
    task_mut = next(item for item in ledger_payload["tasks"] if item["task_id"] == task_id)
    task_mut.setdefault("monitoring", {})["executor_state"] = "TIMEOUT"
    task_mut["monitoring"]["executor_consecutive_timeouts"] = 2
    ledger.write_text(json.dumps(ledger_payload, ensure_ascii=False, indent=2) + "\n")
    unhealthy = run_json("python3", str(OPS), "--ledger", str(ledger), "preview-tick", task_id, env=env)
    assert unhealthy["state"] == "EXECUTOR_UNHEALTHY", unhealthy
    return {"resume_gate": "ok", "delivery": len(delivered), "monitor_state": unhealthy["state"]}


def test_retry_budget(tmp: Path):
    ledger = tmp / "retry-ledger.json"
    jobs_root = tmp / "retry-jobs"
    task_id = "retry-budget"
    job_id = f"{task_id}-job"
    run_json(
        "python3", str(OPS), "--ledger", str(ledger), "bootstrap-task", task_id,
        "--goal", "retry exactly once then block",
        "--owner", "main-agent",
        "--channel", "discord",
        "--requester-channel", "discord:channel:1477237887136432162",
        "--workflow", "Failing step :: shell=python3 -c \"import sys; sys.exit(1)\" :: retry_budget=1",
        "--next-action", "Run",
        "--jobs-root", str(jobs_root),
        "--disabled",
        "--dry-run",
        "--no-auto-start-execution",
    )
    run_json("python3", str(OPS), "--ledger", str(ledger), "init-execution-job", task_id, "--jobs-root", str(jobs_root), "--job-id", job_id)
    first = run_json("python3", str(EXECUTOR), "--jobs-root", str(jobs_root), "run-next", job_id, "--execution-owner", "retry-test")
    payload = run_json("python3", str(EXECUTOR), "--jobs-root", str(jobs_root), "run-next", job_id, "--execution-owner", "retry-test")
    job = load(jobs_root / job_id / "job.json")
    assert first["status"] == "RETRY", first
    assert payload["status"] == "BLOCKED", payload
    assert job["items"][0]["attempts"] == 2, job
    assert job["items"][0]["status"] == "BLOCKED", job
    assert job["items"][0]["blocked_reason"] == "RETRY_BUDGET_EXHAUSTED", job
    return {"retry_attempts": job["items"][0]["attempts"], "retry_terminal": job["items"][0]["status"]}


def main() -> None:
    with tempfile.TemporaryDirectory() as td:
        tmp = Path(td)
        result = {
            **test_prompt_contract(tmp),
            **test_interrupt_resume_delivery_and_monitor(tmp),
            **test_retry_budget(tmp),
        }
        print(json.dumps({"ok": True, **result}, ensure_ascii=False, indent=2))


if __name__ == "__main__":
    main()
