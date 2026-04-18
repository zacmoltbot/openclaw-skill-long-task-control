#!/usr/bin/env python3
import json
import subprocess
import tempfile
from pathlib import Path

ROOT = Path(__file__).resolve().parent.parent
OPS = ROOT / "scripts" / "openclaw_ops.py"
LEDGER_TOOL = ROOT / "scripts" / "task_ledger.py"


def run(*args, check=True):
    return subprocess.run(args, text=True, capture_output=True, check=check)


def run_json(*args):
    proc = run(*args)
    return json.loads(proc.stdout)


def load_task(ledger: Path, task_id: str):
    data = json.loads(ledger.read_text())
    return next(t for t in data["tasks"] if t["task_id"] == task_id)


def main():
    with tempfile.TemporaryDirectory() as td:
        ledger = Path(td) / "ledger.json"

        bad = run(
            "python3", str(OPS), "--ledger", str(ledger), "init-task", "bad-workflow",
            "--goal", "dangerous request",
            "--workflow", "Do everything end to end",
            "--next-action", "n/a",
            check=False,
        )
        assert bad.returncode != 0, bad.stdout + bad.stderr

        task_id = "serial-contract"
        run(
            "python3", str(OPS), "--ledger", str(ledger), "init-task", task_id,
            "--goal", "compile validated workflow",
            "--workflow", "Prepare inputs",
            "--workflow", "Render artifact",
            "--workflow", "Deliver artifact",
            "--next-action", "Prepare inputs",
        )
        task = load_task(ledger, task_id)
        assert task["workflow"][0]["retry_budget"] == 2
        assert task["workflow"][0]["kind"] == "generic"

        # Canonical serial path starts at step-01.
        tick_initial = run_json("python3", str(OPS), "--ledger", str(ledger), "preview-tick", task_id)
        assert tick_initial["current_step"] == "step-01", tick_initial
        assert tick_initial["truth_state"] == "CONSISTENT", tick_initial

        # Completing step-01 should advance derived.current_step to step-02.
        run(
            "python3", str(LEDGER_TOOL), "--ledger", str(ledger), "checkpoint", task_id,
            "--event-type", "STEP_COMPLETED",
            "--summary", "completed canonical step 01",
            "--current-checkpoint", "step-01",
        )
        task_after_first = load_task(ledger, task_id)
        assert task_after_first["derived"]["current_step"] == "step-02", task_after_first["derived"]

        # Manual skip to step-03 should violate serial truth and surface TRUTH_INCONSISTENT.
        run(
            "python3", str(LEDGER_TOOL), "--ledger", str(ledger), "checkpoint", task_id,
            "--event-type", "STEP_COMPLETED",
            "--summary", "wild manual skip to step 03",
            "--current-checkpoint", "step-03",
        )
        tick_oob = run_json("python3", str(OPS), "--ledger", str(ledger), "preview-tick", task_id)
        assert tick_oob["state"] == "TRUTH_INCONSISTENT", tick_oob
        assert tick_oob["truth_state"] == "INCONSISTENT", tick_oob
        assert any(str(x).startswith("serial_out_of_bounds:") for x in tick_oob.get("inconsistencies", [])), tick_oob

        unhealthy_id = "executor-unhealthy"
        run(
            "python3", str(OPS), "--ledger", str(ledger), "init-task", unhealthy_id,
            "--goal", "monitor executor health",
            "--workflow", "Do safe work",
            "--workflow", "Deliver result",
            "--next-action", "Do safe work",
        )
        data = json.loads(ledger.read_text())
        unhealthy_task = next(t for t in data["tasks"] if t["task_id"] == unhealthy_id)
        unhealthy_task.setdefault("monitoring", {})["executor_health"] = {
            "consecutive_errors": 3,
            "last_error_at": "2026-04-17T00:00:00+00:00",
            "last_success_at": "2026-04-17T00:01:00+00:00",
        }
        ledger.write_text(json.dumps(data, ensure_ascii=False, indent=2) + "\n")
        tick_unhealthy = run_json("python3", str(OPS), "--ledger", str(ledger), "preview-tick", unhealthy_id)
        assert tick_unhealthy["state"] == "EXECUTOR_UNHEALTHY", tick_unhealthy

        print(json.dumps({
            "ok": True,
            "checks": [
                "compile_gate_rejects_coarse_workflow",
                "checkpoint_completion_advances_only_canonical_next_step",
                "out_of_bounds_detected_on_manual_skip",
                "monitor_surfaces_executor_unhealthy",
            ],
        }, ensure_ascii=False, indent=2))


if __name__ == "__main__":
    main()
