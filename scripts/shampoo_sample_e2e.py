#!/usr/bin/env python3
import json
import subprocess
import tempfile
from pathlib import Path

ROOT = Path(__file__).resolve().parent.parent
TASK_LEDGER = ROOT / "scripts" / "task_ledger.py"
MONITOR_CRON = ROOT / "scripts" / "monitor_cron.py"
CHECKPOINT_REPORT = ROOT / "scripts" / "checkpoint_report.py"


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
        cron_dir = tmp / "state" / "monitor-crons"
        task_id = "shampoo-30s-20260411-a"
        output_dir = tmp / "artifacts"
        output_dir.mkdir(parents=True, exist_ok=True)
        final_video = output_dir / "shampoo-ad-30s.mp4"
        final_video.write_bytes(b"FAKE_MP4_DATA")

        run(
            "python3", str(TASK_LEDGER), "--ledger", str(ledger), "init", task_id,
            "--goal", "Deliver a 30-second shampoo ad sample with verified lifecycle control",
            "--owner", "main-agent",
            "--channel", "discord",
            "--workflow", "Draft treatment",
            "--workflow", "Render sample",
            "--workflow", "Validate and handoff",
            "--activation-announced",
            "--message-ref", "discord:msg:shampoo-activation",
            "--summary", "Activation announced and shampoo sample task initialized",
            "--fact", "activation_message=emitted",
            "--fact", "sample_name=30s_shampoo_ad",
            "--next-action", "Prepare treatment outline and create first checkpoint",
            "--expected-interval-sec", "30",
            "--timeout-sec", "60",
            "--nudge-after-sec", "60",
            "--escalate-after-nudges", "1",
            "--max-nudges", "2",
            "--blocked-escalate-after-sec", "30",
        )

        activation_task = task_from(ledger, task_id)
        assert activation_task["activation"]["announced"] is True
        assert activation_task["checkpoints"][0]["facts"]["sample_name"] == "30s_shampoo_ad"

        installed = run_json(
            "python3", str(MONITOR_CRON), "--ledger", str(ledger), "--cron-dir", str(cron_dir),
            "install", task_id, "--cron-spec", "*/5 * * * *"
        )
        assert Path(installed["cron_file"]).exists()

        started_block = run(
            "python3", str(CHECKPOINT_REPORT), "STARTED", task_id,
            "--goal", "Deliver a 30-second shampoo ad sample with verified lifecycle control",
            "--workflow-step", "Draft treatment",
            "--workflow-step", "Render sample",
            "--workflow-step", "Validate and handoff",
            "--fact", "activation_message=emitted",
            "--fact", "monitor_cron=installed",
        ).stdout.strip()

        run(
            "python3", str(TASK_LEDGER), "--ledger", str(ledger), "checkpoint", task_id,
            "--summary", "Treatment outline approved for shampoo sample",
            "--current-checkpoint", "step-02",
            "--next-action", "Render the 30-second sample",
            "--fact", "storyboard=v1_ready",
            "--fact", "duration_sec=30",
        )
        task = task_from(ledger, task_id)
        assert task["current_checkpoint"] == "step-02"
        assert task["checkpoints"][-1]["facts"]["duration_sec"] == "30"

        task = load(ledger)
        inner = next(item for item in task["tasks"] if item["task_id"] == task_id)
        make_stale(inner, nudge_count=0)
        ledger.write_text(json.dumps(task, ensure_ascii=False, indent=2) + "\n")

        nudge_result = run_json(
            "python3", str(MONITOR_CRON), "--ledger", str(ledger), "--cron-dir", str(cron_dir),
            "run-once", "--task-id", task_id
        )
        assert nudge_result["report"]["state"] == "NUDGE_MAIN_AGENT"
        assert nudge_result["report"]["action_payload"]["kind"] == "NUDGE_MAIN_AGENT"
        assert nudge_result["cron_removed"] is False

        task = load(ledger)
        inner = next(item for item in task["tasks"] if item["task_id"] == task_id)
        make_stale(inner, nudge_count=1)
        ledger.write_text(json.dumps(task, ensure_ascii=False, indent=2) + "\n")

        reconcile_result = run_json(
            "python3", str(MONITOR_CRON), "--ledger", str(ledger), "--cron-dir", str(cron_dir),
            "run-once", "--task-id", task_id
        )
        assert reconcile_result["report"]["state"] == "OWNER_RECONCILE"
        assert reconcile_result["report"]["action_payload"]["kind"] == "OWNER_RECONCILE"

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

        run(
            "python3", str(TASK_LEDGER), "--ledger", str(ledger), "checkpoint", task_id,
            "--summary", "Rendered shampoo sample and validated output file",
            "--kind", "COMPLETED",
            "--status", "COMPLETED",
            "--current-checkpoint", "step-03",
            "--next-action", "None",
            "--artifact", str(final_video),
            "--fact", f"output_file={final_video}",
            "--fact", "size_bytes=13",
            "--fact", "duration_sec=30",
        )
        run(
            "python3", str(TASK_LEDGER), "--ledger", str(ledger), "owner-reply", task_id,
            "--reply", "C",
            "--summary", "Owner confirmed the shampoo sample is complete and validated",
            "--validation", f"file_exists={final_video.exists()}",
            "--validation", f"size_bytes={final_video.stat().st_size}",
            "--artifact", str(final_video),
        )

        cleanup_result = run_json(
            "python3", str(MONITOR_CRON), "--ledger", str(ledger), "--cron-dir", str(cron_dir),
            "run-once", "--task-id", task_id
        )
        assert cleanup_result["report"]["state"] == "STOP_AND_DELETE"
        assert cleanup_result["cron_removed"] is True
        assert not Path(cleanup_result["cron_file"]).exists()

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
            "cron_dir": str(cron_dir),
            "final_video": str(final_video),
            "states": {
                "after_first_stale": nudge_result["report"]["state"],
                "after_second_stale": reconcile_result["report"]["state"],
                "after_terminal": cleanup_result["report"]["state"],
            },
            "owner_reply_route": final_task["monitoring"]["owner_response_kind"],
            "cron_removed": cleanup_result["cron_removed"],
        }, ensure_ascii=False, indent=2))


if __name__ == "__main__":
    main()
