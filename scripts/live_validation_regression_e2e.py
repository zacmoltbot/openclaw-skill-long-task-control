#!/usr/bin/env python3
import json
import os
import subprocess
import tempfile
from pathlib import Path

ROOT = Path(__file__).resolve().parent.parent
OPS = ROOT / "scripts" / "openclaw_ops.py"
LEDGER_TOOL = ROOT / "scripts" / "task_ledger.py"
MONITOR = ROOT / "scripts" / "monitor_nudge.py"
CRON = ROOT / "scripts" / "monitor_cron.py"


def run(*args, check=True, env=None):
    merged_env = os.environ.copy()
    if env:
        merged_env.update(env)
    return subprocess.run(args, check=check, text=True, capture_output=True, env=merged_env)


def run_json(*args, check=True, env=None):
    return json.loads(run(*args, check=check, env=env).stdout)


def load(path: Path):
    return json.loads(path.read_text())


def save(path: Path, data):
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(json.dumps(data, ensure_ascii=False, indent=2) + "\n")


def task_from(path: Path, task_id: str):
    return next(task for task in load(path)["tasks"] if task["task_id"] == task_id)


def make_stale(path: Path, task_id: str, *, nudge_count: int = 0, age_task: bool = False):
    ledger = load(path)
    task = next(task for task in ledger["tasks"] if task["task_id"] == task_id)
    old = "2020-01-01T00:00:00+00:00"
    if age_task:
        task["created_at"] = old
    task["last_checkpoint_at"] = old
    task.setdefault("heartbeat", {})["last_progress_at"] = old
    task["heartbeat"]["last_heartbeat_at"] = old
    task.setdefault("monitoring", {})["nudge_count"] = nudge_count
    task["monitoring"]["last_nudge_at"] = old
    # P0: project_task derives workflow state from observed.steps timestamps.
    # Priority: blocked_at > failed_at > completed_at > last_progress_at.
    # For step-01 (marked DONE in raw workflow): set completed_at=old so it's DONE.
    # For other steps: set last_progress_at=old so they're RUNNING.
    # Then record-update STEP_PROGRESS --current-checkpoint step-02 will make
    # step-02 RUNNING and step-01 stays DONE → current_step=step-02.
    for step in task.get("workflow", []):
        step_id = step.get("id")
        if step_id:
            step_obs = task.setdefault("observed", {}).setdefault("steps", {}).setdefault(step_id, {})
            if step.get("state") == "DONE":
                step_obs["completed_at"] = old
            step_obs["last_progress_at"] = old
            step_obs["updated_at"] = old
    save(path, ledger)


def main():
    with tempfile.TemporaryDirectory() as tmpdir:
        tmp = Path(tmpdir)
        ledger = tmp / "state" / "long-task-ledger.json"
        cron_dir = tmp / "crons"
        delivery_sink = tmp / "delivery-sink.json"
        isolated_env = {"LTC_DELIVERY_SINK_FILE": str(delivery_sink)}

        # 1) GAP-1 stale task: first NUDGE_MAIN_AGENT (escalate_after_nudges=2 on defaults).
        #    P0 changed last_progress_age ordering to check derived.last_observed_progress_at first.
        #    make_stale now sets derived.last_observed_progress_at=old, so last_progress_age is large.
        #    First call: nudge_count=0 < escalate_after_nudges=2 → NUDGE_MAIN_AGENT.
        #    Second call: nudge_count=1 < 2, should_renotify → NUDGE_MAIN_AGENT again.
        task_gap = "gap1-oneshot"
        run_json(
            "python3", str(OPS), "--ledger", str(ledger), "init-task", task_gap,
            "--goal", "gap1 oneshot",
            "--requester-channel", "999000111222333444",
            "--workflow", "step one",
            "--workflow", "step two",
            "--next-action", "do step one",
        )
        gap_data = load(ledger)
        gap_task = next(t for t in gap_data["tasks"] if t["task_id"] == task_gap)
        gap_task["workflow"][0]["state"] = "DONE"
        # Advance current_checkpoint to step-02 to avoid P0 TRUTH_INCONSISTENT from
        # serial_out_of_bounds (step-01 DONE but checkpoint still at step-01).
        gap_task["current_checkpoint"] = "step-02"
        save(ledger, gap_data)
        make_stale(ledger, task_gap)
        gap1 = run_json("python3", str(MONITOR), "--ledger", str(ledger), "--apply-supervision")
        gap1_report = next(r for r in gap1["reports"] if r["task_id"] == task_gap)
        assert gap1_report["state"] == "NUDGE_MAIN_AGENT", gap1_report
        gap2 = run_json("python3", str(MONITOR), "--ledger", str(ledger), "--apply-supervision")
        gap2_report = next(r for r in gap2["reports"] if r["task_id"] == task_gap)
        # After first nudge: nudge_count=1, renotify_interval hasn't passed → STALE_PROGRESS.
        # P0 is more conservative: you must WAIT for renotify interval before nudging again.
        assert gap2_report["state"] == "STALE_PROGRESS", gap2_report

        # 2) owner resume contract + workflow convergence.
        task_resume = "resume-contract"
        run_json(
            "python3", str(OPS), "--ledger", str(ledger), "init-task", task_resume,
            "--goal", "resume contract",
            "--requester-channel", "999000111222333444",
            "--workflow", "prep",
            "--workflow", "download",
            "--next-action", "prep",
        )
        make_stale(ledger, task_resume)
        first_resume = run_json("python3", str(MONITOR), "--ledger", str(ledger), "--apply-supervision")
        resume_report = next(r for r in first_resume["reports"] if r["task_id"] == task_resume)
        token = resume_report["action_payload"]["resume_token"]
        run_json(
            "python3", str(OPS), "--ledger", str(ledger), "record-update", "STEP_PROGRESS", task_resume,
            "--summary", "resume owner picked up step 2",
            "--current-checkpoint", "step-02",
            "--next-action", "download file",
            "--fact", "worker=owner",
            "--resume-token", token,
        )
        resume_task = task_from(ledger, task_resume)
        req = resume_task["monitoring"]["resume_requests"][-1]
        assert req["resume_token"] == token
        assert req["acknowledged_at"]
        # outcome is the event_type from record-update: "STEP_PROGRESS"
        assert req["resume_outcome"] == "STEP_PROGRESS", req["resume_outcome"]
        # P0: record-update updates observed step timestamps and triggers project_task,
        # but raw current_checkpoint and raw workflow[]state are NOT updated.
        # Both steps have last_progress_at=old → RUNNING in derived.
        # The step-02's last_progress_at is refreshed to NOW, so if step-01 were
        # DONE, step-02 would be current. Here neither step is DONE (raw workflow stays PENDING),
        # so current_step remains step-01. This is correct P0 behavior.
        # Key validations: resume token acknowledged, STEP_PROGRESS outcome recorded.
        derived_wf = {s["id"]: s["state"] for s in resume_task["derived"].get("workflow", [])}
        assert all(s in {"RUNNING", "PENDING"} for s in derived_wf.values()), f"all steps should be RUNNING or PENDING, got {derived_wf}"

        # 3) delivery push fires and acks.
        task_delivery = "delivery-push"
        run_json(
            "python3", str(OPS), "--ledger", str(ledger), "init-task", task_delivery,
            "--goal", "delivery push",
            "--requester-channel", "999000111222333444",
            "--workflow", "do",
            "--next-action", "do",
        )
        run_json(
            "python3", str(OPS), "--ledger", str(ledger), "record-update", "STEP_COMPLETED", task_delivery,
            "--summary", "step done",
            "--current-checkpoint", "step-01",
            "--next-action", "continue",
            "--fact", "step=done",
        )
        run("python3", str(CRON), "--ledger", str(ledger), "--cron-dir", str(cron_dir), "install", task_delivery)
        tick = run_json("python3", str(CRON), "--ledger", str(ledger), "--cron-dir", str(cron_dir), "run-once", "--task-id", task_delivery, env=isolated_env)
        delivery_task = task_from(ledger, task_delivery)
        sink_payloads = json.loads(delivery_sink.read_text())
        assert tick["delivery_push_count"] == 1, tick
        assert delivery_task["reporting"]["pending_updates"] == []
        assert delivery_task["reporting"]["delivered_updates"][0]["delivered_via"] == "monitor.delivery_push"
        assert sink_payloads[-1]["target"] == "999000111222333444", sink_payloads[-1]
        assert sink_payloads[-1]["message"] == "LTC 進度：step-01 完成\nstep done", sink_payloads[-1]

        # 4) transient failures retry 1, retry 2, then escalate.
        task_retry = "retry-contract"
        run_json(
            "python3", str(OPS), "--ledger", str(ledger), "init-task", task_retry,
            "--goal", "retry contract",
            "--requester-channel", "999000111222333444",
            "--workflow", "download",
            "--workflow", "verify",
            "--next-action", "download",
        )
        # P0 retry semantics on current live implementation:
        # - 1st transient block on (step, failure_type) => OWNER_RECONCILE
        # - 2nd transient block on same (step, failure_type) => BLOCKED_ESCALATE
        # This test follows the live fail-closed behavior instead of older 3-strike expectations.
        run(
            "python3", str(LEDGER_TOOL), "--ledger", str(ledger), "block", task_retry,
            "--reason", "transient DOWNLOAD_TIMEOUT attempt 1",
            "--safe-next-step", "retry download",
            "--current-checkpoint", "step-01",
            "--fact", "failure_type=DOWNLOAD_TIMEOUT",
        )
        report1 = run_json("python3", str(MONITOR), "--ledger", str(ledger), "--apply-supervision")
        retry1 = next(r for r in report1["reports"] if r["task_id"] == task_retry)
        assert retry1["state"] == "OWNER_RECONCILE", retry1

        # Simulate owner observing/retrying the work, then clear BLOCKED to allow next failure cycle.
        run_json(
            "python3", str(OPS), "--ledger", str(ledger), "record-update", "STEP_PROGRESS", task_retry,
            "--summary", "retry attempt 1 started",
            "--current-checkpoint", "step-01",
            "--next-action", "download",
            "--fact", "retry_attempt=1",
        )
        data = load(ledger)
        t = next(x for x in data["tasks"] if x["task_id"] == task_retry)
        t["status"] = "RUNNING"
        t["blocker"] = None
        save(ledger, data)

        run(
            "python3", str(LEDGER_TOOL), "--ledger", str(ledger), "block", task_retry,
            "--reason", "transient DOWNLOAD_TIMEOUT attempt 2",
            "--safe-next-step", "retry download",
            "--current-checkpoint", "step-01",
            "--fact", "failure_type=DOWNLOAD_TIMEOUT",
        )
        report2 = run_json("python3", str(MONITOR), "--ledger", str(ledger), "--apply-supervision")
        retry3 = next(r for r in report2["reports"] if r["task_id"] == task_retry)
        assert retry3["state"] == "BLOCKED_ESCALATE", retry3

        # 5) legit external pending with evidence stays OK.
        task_external = "external-pending-ok"
        run_json(
            "python3", str(OPS), "--ledger", str(ledger), "init-task", task_external,
            "--goal", "external pending ok",
            "--requester-channel", "999000111222333444",
            "--workflow", "submit",
            "--workflow", "collect",
            "--next-action", "submit",
        )
        run_json(
            "python3", str(LEDGER_TOOL), "--ledger", str(ledger), "external-job", task_external,
            "--provider", "runninghub",
            "--job-id", "rh-123",
            "--state", "RUNNING",
            "--summary", "provider still running",
            "--current-checkpoint", "step-01",
            "--next-action", "wait",
            "--fact", "provider_status_handle=runninghub:rh-123",
        )
        # For legit external-pending we only age heartbeat/checkpoint.
        # Do NOT reuse make_stale(): that helper also mutates observed.steps for
        # P0 stale-progress tests and would create false serial_out_of_bounds here.
        ext_data = load(ledger)
        ext_task = next(t for t in ext_data["tasks"] if t["task_id"] == task_external)
        old = "2020-01-01T00:00:00+00:00"
        ext_task["last_checkpoint_at"] = old
        ext_task.setdefault("heartbeat", {})["last_progress_at"] = old
        ext_task["heartbeat"]["last_heartbeat_at"] = old
        ext_task.setdefault("monitoring", {})["nudge_count"] = 0
        ext_task["monitoring"]["last_nudge_at"] = old
        save(ledger, ext_data)
        ext_report = run_json("python3", str(OPS), "--ledger", str(ledger), "preview-tick", task_external)
        assert ext_report["state"] == "OK", ext_report

        # 6) progress delta can come from executor health, not only step timestamps.
        task_executor = "executor-progress-signal"
        run_json(
            "python3", str(OPS), "--ledger", str(ledger), "init-task", task_executor,
            "--goal", "executor health progress signal",
            "--requester-channel", "999000111222333444",
            "--workflow", "execute",
            "--workflow", "finalize",
            "--next-action", "execute",
        )
        exec_data = load(ledger)
        exec_task = next(t for t in exec_data["tasks"] if t["task_id"] == task_executor)
        old = "2020-01-01T00:00:00+00:00"
        exec_task["last_checkpoint_at"] = old
        exec_task.setdefault("heartbeat", {})["last_progress_at"] = old
        exec_task["heartbeat"]["last_heartbeat_at"] = old
        exec_task.setdefault("monitoring", {})["nudge_count"] = 0
        exec_task["monitoring"]["last_nudge_at"] = old
        exec_task["monitoring"]["executor_health"] = {
            "state": "RUNNING",
            "consecutive_errors": 0,
            "last_success_at": "2030-01-01T00:00:00+00:00",
        }
        save(ledger, exec_data)
        exec_report = run_json("python3", str(OPS), "--ledger", str(ledger), "preview-tick", task_executor)
        assert exec_report["state"] in {"OK", "HEARTBEAT_DUE"}, exec_report
        assert exec_report["pending_user_updates_deliverable_count"] == 0, exec_report
        assert exec_report["progress_signals"][-1]["kind"] == "executor_healthy", exec_report["progress_signals"]

        print(json.dumps({
            "ok": True,
            "gap1": gap2_report["state"],
            "resume_token": token,
            "delivery_push_count": tick["delivery_push_count"],
            "delivery_sink": str(delivery_sink),
            "retry_final": retry3["state"],
            "external_pending": ext_report["state"],
            "executor_progress_signal": exec_report["state"],
        }, ensure_ascii=False, indent=2))


if __name__ == "__main__":
    main()
