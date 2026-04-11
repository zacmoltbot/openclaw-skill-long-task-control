#!/usr/bin/env python3
import argparse
import json
from copy import deepcopy
from datetime import datetime, timezone
from pathlib import Path

TERMINAL_STATUSES = {"COMPLETED", "FAILED", "ABANDONED"}
ACTIVE_STATUSES = {"PENDING", "RUNNING"}
STATE_PRIORITY = [
    "STOP_AND_DELETE",
    "BLOCKED_ESCALATE",
    "OWNER_RECONCILE",
    "NUDGE_MAIN_AGENT",
    "STALE_PROGRESS",
    "HEARTBEAT_DUE",
    "OK",
]
# Per-step, per-failure-type retry tracking
FAILURE_TYPES = {
    "TIMEOUT": "TIMEOUT",          # no checkpoint within timeout_sec
    "EXECUTION_ERROR": "EXECUTION_ERROR",  # command/API returned error
    "EXTERNAL_WAIT": "EXTERNAL_WAIT",      # external system (RunningHub etc.) returned failure/wait_exceeded
}
MAX_RETRY_COUNT = 3  # same failure type on same step 3x → BLOCKED_ESCALATE
DEFAULTS = {
    "nudge_after_sec": None,
    "renotify_interval_sec": None,
    "max_nudges": 3,
    "escalate_after_nudges": 2,
    "blocked_escalate_after_sec": None,
}
SUPERVISION_ALLOWED_PATHS = {
    "heartbeat",
    "heartbeat.watchdog_state",
    "monitoring",
    "monitoring.nudge_count",
    "monitoring.last_nudge_at",
    "monitoring.last_escalated_at",
    "monitoring.last_action_at",
    "monitoring.last_action_state",
    "monitoring.last_action_reason",
    "monitoring.last_action_kind",
    "monitoring.last_action_payload",
    "monitoring.action_log",
    "monitoring.cron_state",
    "monitoring.owner_query_at",
    "monitoring.owner_response_at",
    "monitoring.owner_response_kind",
    "monitoring.reconcile_count",
    "monitoring.last_reconcile_at",
    "monitoring.last_resume_request_at",
    "monitoring.recovery_attempt_count",
    "monitoring.retry_count",
}


def now_iso():
    return datetime.now(timezone.utc).astimezone().isoformat(timespec="seconds")


def parse_ts(value):
    if not value:
        return None
    return datetime.fromisoformat(value)


def age_seconds(now, value):
    ts = parse_ts(value)
    if not ts:
        return None
    return int((now - ts).total_seconds())


def first_non_null(*values):
    for value in values:
        if value is not None:
            return value
    return None


# ─── Retry-count helpers ────────────────────────────────────────────────────

def retry_key(step_id, failure_type):
    return f"{step_id}:{failure_type}"


def get_retry_count(monitoring, step_id, failure_type):
    """Return current retry count for a step+type combo, 0 if absent."""
    counts = monitoring.get("retry_count", {})
    return int(counts.get(retry_key(step_id, failure_type), 0))


def increment_retry(monitoring, step_id, failure_type):
    """Increment and persist retry count for a step+type combo."""
    counts = monitoring.setdefault("retry_count", {})
    key = retry_key(step_id, failure_type)
    counts[key] = counts.get(key, 0) + 1
    return counts[key]


def reset_retry(monitoring, step_id, failure_type):
    """Clear retry count on successful forward progress."""
    counts = monitoring.get("retry_count", {})
    key = retry_key(step_id, failure_type)
    if key in counts:
        del counts[key]


def clear_all_retries_for_step(monitoring, step_id):
    """Clear all retry counters for a step when it advances successfully."""
    counts = monitoring.get("retry_count", {})
    for key in list(counts.keys()):
        if key.startswith(f"{step_id}:"):
            del counts[key]


# ─── Smart stale detection helpers ───────────────────────────────────────────

def has_pending_external_return(task):
    """
    Return True when the task is waiting on an external async return
    (RunningHub API queue/pending, remote job still running, etc.)
    and therefore should NOT be flagged STALE_PROGRESS.

    Detection strategy (any one sufficient):
    1. recent checkpoint facts contain a remote job id (runninghub_job_id,
       remote_job_id, rh_job_id, job_id) AND latest status is
       'queued'/'running'/'pending'/'submitted'
    2. notes mention 'waiting'/'pending'/'queued'/'poll' AND a job id exists
    3. action_log contains a recent EXTERNAL_WAIT entry
    """
    # Primary: check checkpoints for active remote job
    checkpoints = task.get("checkpoints", [])
    for cp in checkpoints[-3:]:
        facts = cp.get("facts", {})
        for job_key in ["runninghub_job_id", "remote_job_id", "rh_job_id", "job_id"]:
            if job_key in facts:
                status = (facts.get("latest_status") or facts.get("status") or "").lower()
                if status in ("queued", "running", "pending", "submitted"):
                    return True

    # Secondary: notes mention waiting + have job id
    notes = task.get("notes", [])
    has_waiting_note = any(
        any(kw in (n.lower() if isinstance(n, str) else "") for kw in ["waiting", "pending", "queued", "poll"])
        for n in notes[-3:]
    )
    if has_waiting_note:
        for cp in checkpoints[-3:]:
            facts = cp.get("facts", {})
            if any(k in facts for k in ["runninghub_job_id", "remote_job_id", "rh_job_id", "job_id"]):
                return True

    # Tertiary: action_log EXTERNAL_WAIT entry within last hour
    monitoring = task.get("monitoring", {})
    action_log = monitoring.get("action_log", [])
    now = datetime.now().astimezone()
    for entry in action_log[-5:]:
        entry_at = parse_ts(entry.get("at"))
        if entry_at and (now - entry_at).total_seconds() < 3600:
            if entry.get("failure_type") in FAILURE_TYPES.values():
                return True

    return False


def is_progress_updating(task, now):
    """
    Return True when progress_at was updated recently (within expected_interval).
    Agent is actively working; no stale nudge needed.
    """
    hb = task.get("heartbeat", {})
    last_progress_at = first_non_null(hb.get("last_progress_at"), task.get("last_checkpoint_at"))
    if not last_progress_at:
        return False
    age = age_seconds(now, last_progress_at)
    if age is None:
        return False
    expected_interval = hb.get("expected_interval_sec", 900)
    # Progress updating means last update within 2x expected interval
    return age <= expected_interval * 2
    ranked = {name: idx for idx, name in enumerate(STATE_PRIORITY)}
    return sorted(candidates, key=lambda item: ranked[item["state"]])[0]


def choose_state(candidates):
    ranked = {name: idx for idx, name in enumerate(STATE_PRIORITY)}
    return sorted(candidates, key=lambda item: ranked[item["state"]])[0]


def monitoring_config(task):
    hb = task.get("heartbeat", {})
    monitoring = dict(DEFAULTS)
    monitoring.update(task.get("monitoring", {}))
    expected_interval = hb.get("expected_interval_sec", 900)
    timeout_sec = hb.get("timeout_sec", 1800)
    monitoring["nudge_after_sec"] = monitoring["nudge_after_sec"] or timeout_sec
    monitoring["renotify_interval_sec"] = monitoring["renotify_interval_sec"] or expected_interval
    monitoring["blocked_escalate_after_sec"] = monitoring["blocked_escalate_after_sec"] or max(timeout_sec, expected_interval)
    return monitoring


def load_ledger(path: Path):
    return json.loads(path.read_text())


def save_ledger(path: Path, ledger):
    ledger["updated_at"] = now_iso()
    path.write_text(json.dumps(ledger, ensure_ascii=False, indent=2) + "\n")


def build_action_payload(task, chosen, now_iso_value):
    task_id = task.get("task_id")
    owner = task.get("owner")
    channel = task.get("channel")
    next_action = task.get("next_action")
    blocker = task.get("blocker")
    state = chosen["state"]

    if state == "NUDGE_MAIN_AGENT":
        return {
            "kind": "NUDGE_MAIN_AGENT",
            "deliver_to": owner,
            "channel": channel,
            "title": f"Execution nudge for {task_id}",
            "message": (
                f"Task {task_id} has stale progress. First try the recovery path: resume execution, rebuild/restart the stuck step if safe, "
                f"or reconcile missing ledger truth. Only escalate to BLOCKED after you confirm it cannot self-recover. "
                f"Next action on ledger: {next_action}."
            ),
            "facts": {
                "task_id": task_id,
                "status": task.get("status"),
                "reason": chosen["reason"],
                "next_action": next_action,
                "preferred_recovery": [
                    "resume_execution",
                    "rebuild_or_restart_safe_step",
                    "reconcile_missing_checkpoint_or_terminal_truth",
                ],
                "escalate_only_when": [
                    "system_problem_persists",
                    "required_resource_missing_and_not_self_recoverable",
                    "multiple_interventions_still_no_progress",
                ],
            },
            "monitor_contract": "monitor_only_updates_supervision_metadata",
            "created_at": now_iso_value,
        }
    if state == "OWNER_RECONCILE":
        return {
            "kind": "OWNER_RECONCILE",
            "deliver_to": owner,
            "channel": channel,
            "title": f"Owner reconciliation for {task_id}",
            "message": (
                f"Task {task_id} still has stale progress after prior nudges. Query the owner now and reconcile task truth. "
                f"Prioritize pushing the task forward: backfill missed checkpoints, resume/rebuild the stuck step if safe, or close it with terminal truth. "
                f"Only choose blocked escalation if the task cannot safely self-recover."
            ),
            "facts": {
                "task_id": task_id,
                "status": task.get("status"),
                "reason": chosen["reason"],
                "next_action": next_action,
                "branches": {
                    "A_IN_PROGRESS_FORGOT_LEDGER": "append missed checkpoint(s), refresh next_action, keep RUNNING",
                    "B_BLOCKED": "write blocker truth only after self-recovery is ruled out, then escalate via BLOCKED_ESCALATE",
                    "C_COMPLETED": "write COMPLETED plus validation evidence",
                    "D_NO_REPLY": "seek external evidence, retry recovery, or rebuild/restart safely before changing task truth",
                    "E_FORGOT_OR_NOT_DOING": "do not only log it; immediately push resume execution / 補做 path"
                }
            },
            "monitor_contract": "monitor_only_updates_supervision_metadata",
            "created_at": now_iso_value,
        }
    if state == "BLOCKED_ESCALATE":
        blk = blocker or {}
        current_step = task.get("current_checkpoint", "unknown")
        retry_count = task.get("monitoring", {}).get("retry_count", {})
        # Collect recent action log for "what was tried"
        action_log = task.get("monitoring", {}).get("action_log", [])
        recent_attempts = [
            {"at": e.get("at"), "state": e.get("state"), "reason": e.get("reason")}
            for e in action_log[-5:]
        ]
        return {
            "kind": "BLOCKED_ESCALATE",
            "deliver_to": owner,
            "channel": channel,
            "title": f"⚠️ Blocked escalation for {task_id}",
            "message": (
                f"Task {task_id} is blocked and has exhausted self-recovery.\n\n"
                f"📌 Stuck step: {current_step}\n"
                f"🔁 Retry history: {dict(retry_count)}\n"
                f"❌ Blocker: {blk.get('reason') or chosen['reason']}\n"
                f"   Need: {'; '.join(blk.get('need') or ['human decision required'])}\n"
                f"   Safe next step: {blk.get('safe_next_step') or 'depends on unblock decision'}\n\n"
                f"Monitor cron will STOP now (no more空燒). "
                f"Owner must resolve the blocker and update task truth."
            ),
            "facts": {
                "task_id": task_id,
                "status": task.get("status"),
                "current_checkpoint": current_step,
                "reason": chosen["reason"],
                "blocker": {
                    "reason": blk.get("reason"),
                    "need": blk.get("need", []),
                    "safe_next_step": blk.get("safe_next_step"),
                },
                "retry_count": retry_count,
                "recent_attempts": recent_attempts,
                "recommended_next_steps": [
                    f"Resolve blocker for step '{current_step}'",
                    "Update task truth (BLOCKED / FAILED / COMPLETED)",
                    "If unblocked: resume from safe_next_step and post fresh checkpoint",
                ],
            },
            "monitor_contract": "monitor_only_updates_supervision_metadata",
            "created_at": now_iso_value,
        }
    if state == "STOP_AND_DELETE":
        return {
            "kind": "STOP_AND_DELETE",
            "deliver_to": task.get("monitoring", {}).get("cron_owner", "long-task-monitor"),
            "channel": channel,
            "title": f"Stop monitor for {task_id}",
            "message": (
                f"Task {task_id} is terminal or no longer requires supervision. Delete/disable the monitor cron now."
            ),
            "facts": {
                "task_id": task_id,
                "status": task.get("status"),
                "reason": chosen["reason"],
            },
            "monitor_contract": "monitor_only_updates_supervision_metadata",
            "created_at": now_iso_value,
        }
    return None


def apply_supervision_update(task, report, now_iso_value):
    heartbeat = task.setdefault("heartbeat", {})
    monitoring = task.setdefault("monitoring", {})
    heartbeat["watchdog_state"] = report["state"]
    monitoring["last_action_at"] = now_iso_value
    monitoring["last_action_state"] = report["state"]
    monitoring["last_action_reason"] = report["reason"]
    monitoring["last_action_kind"] = report["action"]
    monitoring["last_action_payload"] = report.get("action_payload")

    # Persist retry_count dict so it survives across ticks
    if "retry_count" in report:
        monitoring["retry_count"] = report["retry_count"]

    if report["state"] == "NUDGE_MAIN_AGENT" and report["action"] == "send_execution_nudge":
        monitoring["nudge_count"] = int(monitoring.get("nudge_count", 0) or 0) + 1
        monitoring["last_nudge_at"] = now_iso_value
        monitoring["last_resume_request_at"] = now_iso_value
    elif report["state"] == "OWNER_RECONCILE":
        monitoring["owner_query_at"] = now_iso_value
        monitoring["last_reconcile_at"] = now_iso_value
        monitoring["reconcile_count"] = int(monitoring.get("reconcile_count", 0) or 0) + 1
    elif report["state"] == "BLOCKED_ESCALATE":
        monitoring["last_escalated_at"] = now_iso_value
        # Stop monitor cron immediately on BLOCKED_ESCALATE — no more空燒
        monitoring["cron_state"] = "DELETE_REQUESTED"
    elif report["state"] == "STOP_AND_DELETE":
        monitoring["cron_state"] = "DELETE_REQUESTED"

    log_entry = {
        "at": now_iso_value,
        "state": report["state"],
        "reason": report["reason"],
        "action": report["action"],
    }
    if report.get("action_payload"):
        log_entry["payload_kind"] = report["action_payload"]["kind"]
    monitoring.setdefault("action_log", []).append(log_entry)


def collect_paths(obj, prefix=""):
    paths = set()
    if isinstance(obj, dict):
        for key, value in obj.items():
            child = f"{prefix}.{key}" if prefix else key
            paths.add(child)
            paths |= collect_paths(value, child)
    return paths


def assert_only_supervision_changes(before_task, after_task):
    changed = set()
    all_paths = collect_paths(before_task) | collect_paths(after_task)
    for path in all_paths:
        before_value = lookup_path(before_task, path)
        after_value = lookup_path(after_task, path)
        if before_value != after_value:
            changed.add(path)
    disallowed = [path for path in changed if not any(path == allowed or path.startswith(f"{allowed}.") for allowed in SUPERVISION_ALLOWED_PATHS)]
    if disallowed:
        raise RuntimeError(f"Monitor wrote non-supervision fields: {sorted(disallowed)}")


def lookup_path(obj, path):
    node = obj
    for part in path.split("."):
        if not isinstance(node, dict) or part not in node:
            return None
        node = node[part]
    return node


def evaluate_task(task, now):
    status = task.get("status")
    hb = task.get("heartbeat", {})
    blocker = task.get("blocker")
    activation = task.get("activation", {})
    monitoring = monitoring_config(task)
    current_step = task.get("current_checkpoint", "unknown")

    expected_interval = hb.get("expected_interval_sec", 900)
    timeout_sec = hb.get("timeout_sec", 1800)
    nudge_after_sec = monitoring["nudge_after_sec"]
    blocked_escalate_after_sec = monitoring["blocked_escalate_after_sec"]

    last_progress_at = first_non_null(hb.get("last_progress_at"), task.get("last_checkpoint_at"))
    last_heartbeat_at = hb.get("last_heartbeat_at")
    last_progress_age = age_seconds(now, last_progress_at)
    last_heartbeat_age = age_seconds(now, last_heartbeat_at)
    last_nudge_age = age_seconds(now, monitoring.get("last_nudge_at"))

    nudge_count = int(monitoring.get("nudge_count", 0) or 0)
    max_nudges = int(monitoring.get("max_nudges", DEFAULTS["max_nudges"]) or DEFAULTS["max_nudges"])
    escalate_after_nudges = int(
        monitoring.get("escalate_after_nudges", DEFAULTS["escalate_after_nudges"]) or DEFAULTS["escalate_after_nudges"]
    )
    renotify_interval_sec = int(
        monitoring.get("renotify_interval_sec", expected_interval) or expected_interval
    )

    # ── Smart stale detection guards ──────────────────────────────────────
    # Skip stale/progress nudges when:
    # (a) progress_at is still updating → agent is working
    # (b) external return is pending (RunningHub queue/pending) → not stuck, just waiting
    progress_is_fresh = is_progress_updating(task, now)
    pending_external = has_pending_external_return(task)
    is_waiting_external = pending_external or progress_is_fresh

    should_renotify = last_nudge_age is None or last_nudge_age >= renotify_interval_sec
    candidates = []

    if status in TERMINAL_STATUSES:
        candidates.append({
            "state": "STOP_AND_DELETE",
            "reason": f"task status is terminal ({status}); monitor cron should self-delete",
            "action": "delete_monitor_cron",
        })

    if status == "BLOCKED":
        blocked_reason = (blocker or {}).get("reason") or "task marked BLOCKED"
        if monitoring.get("last_escalated_at"):
            candidates.append({
                "state": "STOP_AND_DELETE",
                "reason": f"{blocked_reason}; escalation already sent, monitor cron should self-delete",
                "action": "delete_monitor_cron",
            })
        else:
            # ── Retry-count check: same step + TIMEOUT failure 3x → BLOCKED_ESCALATE ──
            timeout_count = get_retry_count(monitoring, current_step, FAILURE_TYPES["TIMEOUT"])
            exec_count = get_retry_count(monitoring, current_step, FAILURE_TYPES["EXECUTION_ERROR"])
            ext_count = get_retry_count(monitoring, current_step, FAILURE_TYPES["EXTERNAL_WAIT"])
            max_retry = max(timeout_count, exec_count, ext_count)
            if max_retry >= MAX_RETRY_COUNT:
                candidates.append({
                    "state": "BLOCKED_ESCALATE",
                    "reason": (
                        f"{blocked_reason}; retry limit reached on step '{current_step}' "
                        f"(timeouts={timeout_count}, exec_errors={exec_count}, ext_waits={ext_count}, limit={MAX_RETRY_COUNT}); "
                        f"escalating to requester and stopping monitor cron"
                    ),
                    "action": "send_blocked_escalation_then_delete_cron",
                })
            elif last_progress_age is not None and last_progress_age >= blocked_escalate_after_sec:
                candidates.append({
                    "state": "BLOCKED_ESCALATE",
                    "reason": f"{blocked_reason}; blocked for {last_progress_age}s",
                    "action": "send_blocked_escalation_then_delete_cron",
                })
            else:
                candidates.append({
                    "state": "BLOCKED_ESCALATE",
                    "reason": f"{blocked_reason}; blocked tasks should escalate once, then stop monitor cron",
                    "action": "send_blocked_escalation_then_delete_cron",
                })

    if status in ACTIVE_STATUSES and not activation.get("announced"):
        candidates.append({
            "state": "NUDGE_MAIN_AGENT",
            "reason": "activation record missing while task is active",
            "action": "remind_main_agent_to_activate_and_resume",
        })

    # ── Smart stale: skip STALE_PROGRESS when progress is fresh or external return pending ──
    if status in ACTIVE_STATUSES and last_progress_age is not None and last_progress_age > timeout_sec:
        if is_waiting_external:
            candidates.append({
                "state": "OK",
                "reason": (
                    f"no checkpoint for {last_progress_age}s (> timeout_sec={timeout_sec}) "
                    f"but progress is updating / external return pending; skipping stale nudge"
                ),
                "action": "noop_external_wait",
            })
        else:
            # Increment retry count for TIMEOUT failure type
            new_count = increment_retry(monitoring, current_step, FAILURE_TYPES["TIMEOUT"])
            if new_count >= MAX_RETRY_COUNT:
                candidates.append({
                    "state": "BLOCKED_ESCALATE",
                    "reason": (
                        f"no checkpoint for {last_progress_age}s (> timeout_sec={timeout_sec}) on step '{current_step}'; "
                        f"TIMEOUT retry {new_count}/{MAX_RETRY_COUNT} → escalating to BLOCKED"
                    ),
                    "action": "send_blocked_escalation_then_delete_cron",
                })
            else:
                candidates.append({
                    "state": "STALE_PROGRESS",
                    "reason": f"no new checkpoint for {last_progress_age}s (> timeout_sec={timeout_sec}); TIMEOUT retry {new_count}/{MAX_RETRY_COUNT}",
                    "action": "flag_stale_progress_pre_gate",
                })

    # ── Smart stale: skip HEARTBEAT_DUE when external return pending OR progress updating ──
    if status in ACTIVE_STATUSES and last_heartbeat_age is not None and last_heartbeat_age > expected_interval:
        if pending_external or progress_is_fresh:
            candidates.append({
                "state": "OK",
                "reason": f"heartbeat due {last_heartbeat_age}s ago but external return pending; not nudging",
                "action": "noop_external_wait",
            })
        else:
            candidates.append({
                "state": "HEARTBEAT_DUE",
                "reason": f"no heartbeat for {last_heartbeat_age}s (> expected_interval_sec={expected_interval})",
                "action": "send_lightweight_supervision_reminder",
            })

    if status in ACTIVE_STATUSES and last_progress_age is not None and last_progress_age > nudge_after_sec:
        if is_waiting_external and not (last_progress_age > timeout_sec):
            # Already handled above; skip duplicate nudge
            pass
        elif nudge_count >= max_nudges:
            candidates.append({
                "state": "OWNER_RECONCILE",
                "reason": (
                    f"task exceeded max nudges ({nudge_count}/{max_nudges}) with no fresh checkpoint; query owner and reconcile task truth"
                ),
                "action": "query_owner_and_force_reconciliation",
            })
        elif nudge_count >= escalate_after_nudges and should_renotify:
            candidates.append({
                "state": "OWNER_RECONCILE",
                "reason": (
                    f"task already nudged {nudge_count} times with no fresh checkpoint; stale progress must escalate to owner reconciliation before blocker escalation"
                ),
                "action": "query_owner_and_force_reconciliation",
            })
        else:
            candidates.append({
                "state": "NUDGE_MAIN_AGENT",
                "reason": (
                    f"task has no fresh checkpoint for {last_progress_age}s; main agent should resume, post checkpoint, "
                    f"or mark COMPLETED/FAILED/BLOCKED"
                ),
                "action": "send_execution_nudge" if should_renotify else "nudge_already_sent_wait_for_interval",
            })

    if not candidates:
        # On OK: clear all retry counters since task is making forward progress
        if status in ACTIVE_STATUSES:
            clear_all_retries_for_step(monitoring, current_step)
        candidates.append({
            "state": "OK",
            "reason": "heartbeat/progress within thresholds",
            "action": "noop",
        })

    chosen = choose_state(candidates)
    payload = build_action_payload(task, chosen, now.isoformat(timespec="seconds"))
    return {
        "task_id": task.get("task_id"),
        "status": status,
        "state": chosen["state"],
        "reason": chosen["reason"],
        "action": chosen["action"],
        "action_payload": payload,
        "last_progress_age_sec": last_progress_age,
        "last_heartbeat_age_sec": last_heartbeat_age,
        "last_nudge_age_sec": last_nudge_age,
        "nudge_count": nudge_count,
        "max_nudges": max_nudges,
        "next_action": task.get("next_action"),
        "blocker": blocker,
        "current_step": current_step,
        "retry_count": dict(monitoring.get("retry_count", {})),
    }


def main():
    parser = argparse.ArgumentParser(description="Evaluate low-cost execution nudge state for long tasks")
    parser.add_argument("--ledger", type=Path, required=True)
    parser.add_argument("--only-active", action="store_true", help="Skip terminal tasks from output")
    parser.add_argument("--apply-supervision", action="store_true", help="Write supervision metadata back to ledger")
    args = parser.parse_args()

    ledger = load_ledger(args.ledger)
    now = datetime.now().astimezone()
    reports = []
    touched = False

    for task in ledger.get("tasks", []):
        if args.only_active and task.get("status") in TERMINAL_STATUSES:
            continue
        before_task = deepcopy(task)
        report = evaluate_task(task, now)
        if args.apply_supervision:
            apply_supervision_update(task, report, now.isoformat(timespec="seconds"))
            assert_only_supervision_changes(before_task, task)
            touched = True
        reports.append(report)

    if args.apply_supervision and touched:
        save_ledger(args.ledger, ledger)

    print(json.dumps({"generated_at": now.isoformat(timespec="seconds"), "reports": reports}, ensure_ascii=False, indent=2))


if __name__ == "__main__":
    main()
