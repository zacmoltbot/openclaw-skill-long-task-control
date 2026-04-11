#!/usr/bin/env python3
import json
import subprocess
import tempfile
from pathlib import Path

ROOT = Path(__file__).resolve().parent.parent
TASK_LEDGER = ROOT / "scripts" / "task_ledger.py"
OPENCLAW_OPS = ROOT / "scripts" / "openclaw_ops.py"


ACTIVATION_BLOCK = """ACTIVATED
- skill: long-task-control
- announcement: 目前這個 task 會採用 long-task-control SKILL 執行
- reporting: 我會用 checkpoint / blocker / completed 這類可驗證狀態回報進度；有新事實才更新，不用模糊的「還在跑」敘述
- next: 接著建立 task record，開始第一個可驗證步驟
- task_note: sample=30s 洗髮精廣告
""".strip()


def run(*args):
    return subprocess.run(args, check=True, text=True, capture_output=True)


def run_json(*args):
    return json.loads(run(*args).stdout)


def load(path):
    return json.loads(Path(path).read_text())


def task_from(ledger_path, task_id):
    ledger = load(ledger_path)
    return next(task for task in ledger["tasks"] if task["task_id"] == task_id)


def make_stale(task, *, nudge_count=0):
    old = "2020-01-01T00:00:00+00:00"
    task["last_checkpoint_at"] = old
    task.setdefault("heartbeat", {})["last_progress_at"] = old
    task["heartbeat"]["last_heartbeat_at"] = old
    task.setdefault("monitoring", {})["nudge_count"] = nudge_count
    if nudge_count:
        task["monitoring"]["last_nudge_at"] = old


def main():
    with tempfile.TemporaryDirectory() as tmpdir:
        tmp = Path(tmpdir)
        ledger = tmp / "state" / "long-task-ledger.json"
        task_id = "shampoo-30s-20260411-a"
        output_dir = tmp / "artifacts"
        output_dir.mkdir(parents=True, exist_ok=True)
        final_video = output_dir / "shampoo-ad-30s.mp4"
        final_video.write_bytes(b"FAKE_MP4_DATA")

        activation_payload = run_json(
            "python3", str(OPENCLAW_OPS), "--ledger", str(ledger), "bootstrap-task", task_id,
            "--goal", "Deliver a 30-second shampoo ad sample with verified lifecycle control",
            "--owner", "main-agent",
            "--channel", "discord",
            "--requester-channel", "1484432523781083197",
            "--workflow", "Draft treatment",
            "--workflow", "Render sample",
            "--workflow", "Validate and handoff",
            "--message-ref", "discord:msg:shampoo-activation",
            "--summary", "Activation announced and shampoo sample task initialized",
            "--fact", "activation_message=emitted",
            "--fact", "sample_name=30s_shampoo_ad",
            "--next-action", "Prepare treatment outline and create first checkpoint",
            "--expected-interval-sec", "180",
            "--timeout-sec", "300",
            "--nudge-after-sec", "300",
            "--renotify-interval-sec", "300",
            "--escalate-after-nudges", "1",
            "--max-nudges", "2",
            "--blocked-escalate-after-sec", "300",
            "--every", "5m",
            "--disabled"
        )

        activation_task = task_from(ledger, task_id)
        assert activation_task["activation"]["announced"] is True
        assert activation_task["checkpoints"][0]["facts"]["sample_name"] == "30s_shampoo_ad"
        assert activation_task["monitoring"]["cron_state"] == "DISABLED"
        assert activation_task["monitoring"]["openclaw_cron_job_id"]
        assert activation_payload["task_id"] == task_id

        started_payload = run_json(
            "python3", str(OPENCLAW_OPS), "--ledger", str(ledger), "record-update", "STARTED", task_id,
            "--summary", "Activation already announced; owner re-emits STARTED block through execution wrapper",
            "--current-checkpoint", "step-01",
            "--next-action", "Render the 30-second sample",
            "--fact", "activation_message=emitted",
            "--fact", "monitor_cron=installed"
        )
        started_block = started_payload["status_block"]

        checkpoint_payload = run_json(
            "python3", str(OPENCLAW_OPS), "--ledger", str(ledger), "record-update", "CHECKPOINT", task_id,
            "--summary", "Treatment outline approved for shampoo sample",
            "--current-checkpoint", "step-02",
            "--next-action", "Render the 30-second sample",
            "--fact", "storyboard=v1_ready",
            "--fact", "duration_sec=30"
        )
        task = task_from(ledger, task_id)
        assert checkpoint_payload["task_status"] == "RUNNING"
        assert task["current_checkpoint"] == "step-02"
        assert task["checkpoints"][-1]["facts"]["duration_sec"] == "30"

        task = load(ledger)
        inner = next(item for item in task["tasks"] if item["task_id"] == task_id)
        inner.setdefault("heartbeat", {})["last_heartbeat_at"] = "2020-01-01T00:00:00+00:00"
        ledger.write_text(json.dumps(task, ensure_ascii=False, indent=2) + "\n")

        reminder_result = run_json(
            "python3", str(OPENCLAW_OPS), "--ledger", str(ledger), "preview-tick", task_id
        )
        assert reminder_result["state"] == "HEARTBEAT_DUE"
        assert "HEARTBEAT_DUE" in reminder_result["notification"]
        assert reminder_result["remove_monitor"] is False

        task = load(ledger)
        inner = next(item for item in task["tasks"] if item["task_id"] == task_id)
        make_stale(inner, nudge_count=0)
        ledger.write_text(json.dumps(task, ensure_ascii=False, indent=2) + "\n")

        nudge_result = run_json(
            "python3", str(OPENCLAW_OPS), "--ledger", str(ledger), "preview-tick", task_id
        )
        assert nudge_result["state"] == "NUDGE_MAIN_AGENT"
        assert "請 main agent 立刻回來續行" in nudge_result["notification"]
        assert nudge_result["remove_monitor"] is False

        task = load(ledger)
        inner = next(item for item in task["tasks"] if item["task_id"] == task_id)
        make_stale(inner, nudge_count=1)
        ledger.write_text(json.dumps(task, ensure_ascii=False, indent=2) + "\n")

        reconcile_result = run_json(
            "python3", str(OPENCLAW_OPS), "--ledger", str(ledger), "preview-tick", task_id
        )
        assert reconcile_result["state"] == "OWNER_RECONCILE"
        assert "A_IN_PROGRESS_FORGOT_LEDGER" in reconcile_result["notification"]

        run(
            "python3", str(TASK_LEDGER), "--ledger", str(ledger), "owner-reply", task_id,
            "--reply", "E",
            "--summary", "Owner admitted execution stalled; resume the shampoo sample now",
            "--current-checkpoint", "step-02",
            "--next-action", "Resume render and publish verifiable render checkpoint",
            "--fact", "owner_statement=forgot_to_continue",
        )
        task = task_from(ledger, task_id)
        assert task["status"] == "RUNNING"
        assert task["monitoring"]["owner_response_kind"] == "E_FORGOT_OR_NOT_DOING"
        assert task["checkpoints"][-1]["facts"]["resume_required"] == "true"

        completed_payload = run_json(
            "python3", str(OPENCLAW_OPS), "--ledger", str(ledger), "record-update", "COMPLETED", task_id,
            "--summary", "Rendered shampoo sample and validated output file",
            "--current-checkpoint", "step-03",
            "--next-action", "None",
            "--output", str(final_video),
            "--completed-checkpoint", "step-01",
            "--completed-checkpoint", "step-02",
            "--completed-checkpoint", "step-03",
            "--fact", f"output_file={final_video}",
            "--fact", f"size_bytes={final_video.stat().st_size}",
            "--fact", "duration_sec=30"
        )
        assert completed_payload["task_status"] == "COMPLETED"
        assert any(item.startswith("artifact_exists[") for item in completed_payload["validation"])

        cleanup_result = run_json(
            "python3", str(OPENCLAW_OPS), "--ledger", str(ledger), "preview-tick", task_id
        )
        assert cleanup_result["state"] == "STOP_AND_DELETE"
        assert cleanup_result["remove_monitor"] is True

        removed = run_json(
            "python3", str(OPENCLAW_OPS), "--ledger", str(ledger), "remove-monitor", task_id
        )
        assert removed["removed"] is True

        final_task = task_from(ledger, task_id)
        assert final_task["status"] == "COMPLETED"
        assert final_task["monitoring"]["cron_state"] == "DELETED"
        assert final_task["validation"]

        print(json.dumps({
            "ok": True,
            "sample": "30s 洗髮精廣告",
            "activation_message": ACTIVATION_BLOCK,
            "started_block": started_block,
            "ledger": str(ledger),
            "final_video": str(final_video),
            "openclaw_cron_job_id": final_task["monitoring"].get("openclaw_cron_job_id"),
            "states": {
                "after_3min_reminder": reminder_result["state"],
                "after_5min_stale": nudge_result["state"],
                "after_owner_reconcile": reconcile_result["state"],
                "after_terminal": cleanup_result["state"],
            },
            "owner_reply_route": final_task["monitoring"]["owner_response_kind"],
            "cron_removed": removed["removed"],
        }, ensure_ascii=False, indent=2))


if __name__ == "__main__":
    main()
