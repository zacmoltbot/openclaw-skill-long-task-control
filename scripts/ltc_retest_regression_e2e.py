#!/usr/bin/env python3
from __future__ import annotations

import json
import os
import subprocess
import tempfile
from pathlib import Path

ROOT = Path(__file__).resolve().parent.parent
OPS = ROOT / "scripts" / "openclaw_ops.py"
RUNNER = ROOT / "scripts" / "runner_engine.py"
EXECUTOR = ROOT / "scripts" / "executor_engine.py"


def run(*args: str, env=None) -> subprocess.CompletedProcess:
    return subprocess.run(args, text=True, capture_output=True, check=True, env=env)


def run_json(*args: str, env=None):
    return json.loads(run(*args, env=env).stdout)


def load(path: Path):
    return json.loads(path.read_text())


def task_from(path: Path, task_id: str):
    return next(task for task in load(path)["tasks"] if task["task_id"] == task_id)


def main() -> None:
    with tempfile.TemporaryDirectory() as td:
        tmp = Path(td)
        ledger = tmp / "ledger.json"
        jobs_root = tmp / "jobs"
        out_dir = tmp / "out"
        out_dir.mkdir(parents=True, exist_ok=True)
        artifact1 = out_dir / "artifact-01.txt"
        artifact2 = out_dir / "artifact-02.txt"
        sink = tmp / "delivery-sink.json"
        env = os.environ.copy()
        env["LTC_DELIVERY_SINK_FILE"] = str(sink)

        task_id = "ltc-retest-regression"
        job_id = f"{task_id}-job"
        run_json(
            "python3", str(OPS), "--ledger", str(ledger), "bootstrap-task", task_id,
            "--goal", "Reproduce projection/recipient/observability regressions deterministically",
            "--owner", "main-agent",
            "--channel", "discord",
            "--requester-channel", "discord:channel:1477237887136432162",
            "--workflow", f"Step 1 :: shell=printf 'one' > {artifact1} :: expect={artifact1}",
            "--workflow", f"Step 2 :: shell=printf 'two' > {artifact2} :: expect={artifact2}",
            "--next-action", "Run canonical execution",
            "--jobs-root", str(jobs_root),
            "--disabled",
            "--no-auto-start-execution",
            env=env,
        )

        run_json("python3", str(EXECUTOR), "--jobs-root", str(jobs_root), "run-next", job_id, "--execution-owner", "regression-test", env=env)
        task = task_from(ledger, task_id)
        pending = task.get("reporting", {}).get("pending_updates", [])
        delivered = task.get("reporting", {}).get("delivered_updates", [])
        assert task["current_checkpoint"] == "step-02", task
        assert task["status"] == "RUNNING", task
        assert task["workflow_projection"][0]["state"] == "DONE", task["workflow_projection"]
        assert task["workflow_projection"][1]["state"] == "PENDING", task["workflow_projection"]
        assert any(update["event_type"] == "STEP_COMPLETED" and update["checkpoint"] == "step-01" for update in pending), task["reporting"]
        assert task["derived"]["user_facing"]["current_step"] == "step-02", task["derived"]
        assert task["derived"]["user_facing"]["pending_update_ids"], task["derived"]
        assert delivered == [], task["reporting"]

        preview = run_json("python3", str(OPS), "--ledger", str(ledger), "preview-tick", task_id, env=env)
        observability = preview["executor_observability"]
        assert observability["last_event"]["phase"] == "item_completed", observability
        assert observability["last_event"]["checkpoint"] == "step-01", observability
        progress_kinds = [item.get("kind") for item in observability["progress_tail"]]
        assert "ITEM_STARTED" in progress_kinds, observability
        assert "ITEM_COMPLETED" in progress_kinds, observability

        fake_bin = tmp / "bin"
        fake_bin.mkdir(parents=True, exist_ok=True)
        capture = tmp / "openclaw-argv.jsonl"
        fake_openclaw = fake_bin / "openclaw"
        fake_openclaw.write_text(
            "#!/usr/bin/env python3\n"
            "import json, os, sys\n"
            "capture = os.environ['FAKE_OPENCLAW_CAPTURE']\n"
            "with open(capture, 'a', encoding='utf-8') as f:\n"
            "    f.write(json.dumps(sys.argv[1:], ensure_ascii=False) + '\\n')\n"
            "if sys.argv[1:3] == ['cron', 'add']:\n"
            "    print(json.dumps({'id': 'fake-cron-id', 'schedule': {'kind': 'every', 'every': '5m'}}))\n"
            "else:\n"
            "    print(json.dumps({'ok': True}))\n"
        )
        fake_openclaw.chmod(0o755)
        env_cli = env.copy()
        env_cli["FAKE_OPENCLAW_CAPTURE"] = str(capture)
        env_cli["PATH"] = f"{fake_bin}:{env_cli['PATH']}"

        run_json("python3", str(OPS), "--ledger", str(ledger), "run-executor", task_id, "--jobs-root", str(jobs_root), env=env_cli)
        calls = [json.loads(line) for line in capture.read_text().splitlines() if line.strip()]
        cron_add = next(call for call in calls if call[:2] == ["cron", "add"])
        assert "--no-deliver" in cron_add, cron_add

        print(json.dumps({
            "ok": True,
            "task_id": task_id,
            "job_id": job_id,
            "projection_current_checkpoint": task["current_checkpoint"],
            "pending_update_ids": [item["update_id"] for item in pending],
            "executor_last_event": observability["last_event"],
            "progress_tail_kinds": progress_kinds,
            "run_executor_cli": cron_add,
        }, ensure_ascii=False, indent=2))


if __name__ == "__main__":
    main()
