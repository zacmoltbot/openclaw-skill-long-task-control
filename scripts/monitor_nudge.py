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
FAILURE_TYPES = {
    "TIMEOUT": "TIMEOUT",
    "DOWNLOAD_TIMEOUT": "DOWNLOAD_TIMEOUT",
    "DOWNLOAD_INCOMPLETE": "DOWNLOAD_INCOMPLETE",
    "TRANSIENT_NETWORK": "TRANSIENT_NETWORK",
    "EXECUTION_ERROR": "EXECUTION_ERROR",
    "EXTERNAL_WAIT": "EXTERNAL_WAIT",
    "MISSING_EXTERNAL_EVIDENCE": "MISSING_EXTERNAL_EVIDENCE",
    "STEP_COMPLETED_BUT_STALLING": "STEP_COMPLETED_BUT_STALLING",
}
TRANSIENT_FAILURE_TYPES = {
    FAILURE_TYPES["TIMEOUT"],
    FAILURE_TYPES["DOWNLOAD_TIMEOUT"],
    FAILURE_TYPES["DOWNLOAD_INCOMPLETE"],
    FAILURE_TYPES["TRANSIENT_NETWORK"],
    FAILURE_TYPES["EXECUTION_ERROR"],
    FAILURE_TYPES["EXTERNAL_WAIT"],
}
STALLING_AFTER_SEC = 300  # one check interval; nudge faster for step-completion stalls
MAX_RETRY_COUNT = 3
MIN_RETRY_BEFORE_ESCALATE = 2
MAX_TASK_AGE_SEC = 3600
PENDING_EXTERNAL_STATES = {"SUBMITTED", "PENDING", "RUNNING", "RETRYING", "SWITCHED_WORKFLOW"}
PROVIDER_EVIDENCE_KEYS = {
    "provider_job_id",
    "submission_receipt",
    "submission_receipt_id",
    "provider_status_handle",
    "status_handle",
    "status_url",
    "provider_response_ref",
    "provider_receipt_ref",
    "artifact_path",
    "artifact_url",
    "output_file",
    "poll_token",
    "remote_job_id",
    "runninghub_job_id",
    "rh_job_id",
}
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
    "monitoring.install_signal",
    "monitoring.cron_install_error",
    "monitoring.gap1_nudged_steps",
    "monitoring.resume_requests",
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


def retry_key(step_id, failure_type):
    return f"{step_id}:{failure_type}"


def get_retry_count(monitoring, step_id, failure_type):
    counts = monitoring.get("retry_count", {})
    return int(counts.get(retry_key(step_id, failure_type), 0))


def increment_retry(monitoring, step_id, failure_type):
    counts = monitoring.setdefault("retry_count", {})
    key = retry_key(step_id, failure_type)
    counts[key] = counts.get(key, 0) + 1
    return counts[key]


def clear_all_retries_for_step(monitoring, step_id):
    counts = monitoring.get("retry_count", {})
    for key in list(counts.keys()):
        if key.startswith(f"{step_id}:"):
            del counts[key]


def nonempty(value):
    if value is None:
        return False
    if isinstance(value, str):
        return bool(value.strip())
    if isinstance(value, (list, dict, tuple, set)):
        return bool(value)
    return True


def collect_provider_evidence(job):
    evidence = {}
    raw = job.get("provider_evidence") or {}
    if isinstance(raw, dict):
        for key, value in raw.items():
            if nonempty(value):
                evidence[key] = value
    if nonempty(job.get("job_id")):
        evidence.setdefault("provider_job_id", job.get("job_id"))
    for key in PROVIDER_EVIDENCE_KEYS:
        if nonempty(job.get(key)):
            evidence.setdefault(key, job.get(key))
    history = job.get("history") or []
    if history:
        last_facts = (history[-1] or {}).get("facts") or {}
        if isinstance(last_facts, dict):
            for key in PROVIDER_EVIDENCE_KEYS:
                if nonempty(last_facts.get(key)):
                    evidence.setdefault(key, last_facts.get(key))
    return evidence


def evaluate_external_evidence(task):
    suspicious_jobs = []
    legit_pending_jobs = []
    for job in task.get("external_jobs", []):
        state = str(job.get("status", "")).upper()
        if state not in PENDING_EXTERNAL_STATES and not job.get("pending_external"):
            continue
        evidence = collect_provider_evidence(job)
        if evidence:
            legit_pending_jobs.append({
                "provider": job.get("provider"),
                "job_id": job.get("job_id"),
                "status": state,
                "evidence": evidence,
            })
        else:
            suspicious_jobs.append({
                "provider": job.get("provider"),
                "job_id": job.get("job_id"),
                "status": state,
                "missing_contract": sorted(PROVIDER_EVIDENCE_KEYS),
            })
    return {
        "has_legit_pending": bool(legit_pending_jobs),
        "legit_pending_jobs": legit_pending_jobs,
        "suspicious_jobs": suspicious_jobs,
    }


def has_pending_external_return(task):
    evaluation = evaluate_external_evidence(task)
    if evaluation["has_legit_pending"]:
        return True

    checkpoints = task.get("checkpoints", [])
    for cp in checkpoints[-3:]:
        facts = cp.get("facts", {})
        has_job_id = any(k in facts for k in ["runninghub_job_id", "remote_job_id", "rh_job_id", "job_id"])
        status = str(facts.get("latest_status") or facts.get("status") or facts.get("external_state") or "").lower()
        if has_job_id and status in ("queued", "running", "pending", "submitted", "retrying", "switched_workflow"):
            return True

    action_log = task.get("monitoring", {}).get("action_log", [])
    now = datetime.now().astimezone()
    for entry in action_log[-5:]:
        entry_at = parse_ts(entry.get("at"))
        if entry_at and (now - entry_at).total_seconds() < 3600 and entry.get("failure_type") == FAILURE_TYPES["EXTERNAL_WAIT"]:
            return True
    return False


def is_workflow_step_terminal(workflow, step_id):
    """Return True if the named step has a terminal state in the workflow list."""
    if not step_id or not workflow:
        return False
    for step in workflow:
        if step.get("id") == step_id or step.get("step_id") == step_id:
            state = str(step.get("state", "")).upper()
            return state in {"DONE", "COMPLETED", "FAILED", "BLOCKED"}
    return False


def has_external_job_with_evidence(task):
    """Return True if the task has at least one external job with provider evidence."""
    for job in task.get("external_jobs", []):
        state = str(job.get("status", "")).upper()
        if state not in PENDING_EXTERNAL_STATES and not job.get("pending_external"):
            continue
        if collect_provider_evidence(job):
            return True
    return False


def is_progress_updating(task, now):
    hb = task.get("heartbeat", {})
    last_progress_at = first_non_null(hb.get("last_progress_at"), task.get("last_checkpoint_at"))
    if not last_progress_at:
        return False
    age = age_seconds(now, last_progress_at)
    if age is None:
        return False
    expected_interval = hb.get("expected_interval_sec", 300)
    return age <= expected_interval * 2


def choose_state(candidates):
    ranked = {name: idx for idx, name in enumerate(STATE_PRIORITY)}
    return sorted(candidates, key=lambda item: ranked[item["state"]])[0]


def monitoring_config(task):
    hb = task.get("heartbeat", {})
    monitoring = dict(DEFAULTS)
    monitoring.update(task.get("monitoring", {}))
    expected_interval = hb.get("expected_interval_sec", 300)
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


def build_resume_token(task, chosen, now_iso_value):
    step = task.get("current_checkpoint") or "unknown"
    safe = now_iso_value.replace(":", "").replace("-", "")
    return f"{task.get('task_id')}:{step}:{chosen['state']}:{safe}"


def build_action_payload(task, chosen, now_iso_value):
    task_id = task.get("task_id")
    owner = task.get("owner")
    channel = task.get("channel")
    state = chosen["state"]
    blocker = task.get("blocker") or {}
    retry_count = task.get("monitoring", {}).get("retry_count", {})
    if state == "NUDGE_MAIN_AGENT":
        resume_token = build_resume_token(task, chosen, now_iso_value)
        return {
            "kind": state,
            "delivery": "sessions_send",
            "deliver_to": owner,
            "channel": channel,
            "title": f"Execution nudge for {task_id}",
            "message": f"Task {task_id} has stale progress. Resume / rebuild-safe-step / reconcile ledger truth. next_action={task.get('next_action')}",
            "facts": {"task_id": task_id, "current_step": task.get("current_checkpoint"), "reason": chosen["reason"], "next_action": task.get("next_action"), "resume_token": resume_token},
            "resume_token": resume_token,
            "resume_token": resume_token,
            "created_at": now_iso_value,
        }
    if state == "OWNER_RECONCILE":
        resume_token = build_resume_token(task, chosen, now_iso_value)
        return {
            "kind": state,
            "delivery": "sessions_send",
            "deliver_to": owner,
            "channel": channel,
            "title": f"Owner reconciliation for {task_id}",
            "message": f"Task {task_id} needs owner reconciliation. Backfill checkpoint / resume / or close with BLOCKED|COMPLETED truth.",
            "facts": {
                "task_id": task_id,
                "current_step": task.get("current_checkpoint"),
                "reason": chosen["reason"],
                "next_action": task.get("next_action"),
                "resume_token": resume_token,
                "suspicious_external_jobs": chosen.get("suspicious_external_jobs", []),
                "required_provider_evidence": sorted(PROVIDER_EVIDENCE_KEYS),
                "branches": {
                    "A_IN_PROGRESS_FORGOT_LEDGER": "append missed checkpoint(s), refresh next_action, keep RUNNING",
                    "B_BLOCKED": "write blocker truth only after self-recovery is ruled out, then escalate",
                    "C_COMPLETED": "write COMPLETED plus validation evidence",
                    "D_NO_REPLY": "seek external evidence before changing task truth",
                    "E_FORGOT_OR_NOT_DOING": "do not only log it; immediately resume execution / 補做",
                },
            },
            "created_at": now_iso_value,
        }
    if state == "BLOCKED_ESCALATE":
        action_log = task.get("monitoring", {}).get("action_log", [])
        recent_attempts = [{"at": e.get("at"), "state": e.get("state"), "reason": e.get("reason")} for e in action_log[-5:]]
        return {
            "kind": state,
            "deliver_to": owner,
            "channel": channel,
            "title": f"⚠️ Blocked escalation for {task_id}",
            "message": (
                f"Task {task_id} is BLOCKED. step={task.get('current_checkpoint')} retry={dict(retry_count)} "
                f"reason={blocker.get('reason') or chosen['reason']} safe_next_step={blocker.get('safe_next_step')}"
            ),
            "facts": {
                "task_id": task_id,
                "current_checkpoint": task.get("current_checkpoint"),
                "reason": chosen["reason"],
                "blocker": blocker,
                "retry_count": retry_count,
                "recent_attempts": recent_attempts,
                "suspicious_external_jobs": chosen.get("suspicious_external_jobs", []),
            },
            "created_at": now_iso_value,
        }
    if state == "STOP_AND_DELETE":
        return {
            "kind": state,
            "deliver_to": task.get("monitoring", {}).get("cron_owner", "long-task-monitor"),
            "channel": channel,
            "title": f"Stop monitor for {task_id}",
            "message": f"Task {task_id} is terminal or no longer requires supervision. Delete cron now.",
            "facts": {"task_id": task_id, "reason": chosen["reason"], "status": task.get("status")},
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
    if "retry_count" in report:
        monitoring["retry_count"] = report["retry_count"]
    if "gap1_nudged_steps" in report:
        monitoring["gap1_nudged_steps"] = report["gap1_nudged_steps"]
    if report["state"] == "NUDGE_MAIN_AGENT" and report["action"] == "send_execution_nudge":
        monitoring["nudge_count"] = int(monitoring.get("nudge_count", 0) or 0) + 1
        monitoring["last_nudge_at"] = now_iso_value
        monitoring["last_resume_request_at"] = now_iso_value
        payload = report.get("action_payload") or {}
        resume_token = payload.get("resume_token")
        if resume_token:
            requests = monitoring.setdefault("resume_requests", [])
            if not any(item.get("resume_token") == resume_token for item in requests):
                requests.append({
                    "resume_token": resume_token,
                    "requested_at": now_iso_value,
                    "request_kind": report["state"],
                    "current_step": report.get("current_step"),
                    "reason": report.get("reason"),
                    "next_action": report.get("next_action"),
                    "delivery": payload.get("delivery", "sessions_send"),
                })
    elif report["state"] == "OWNER_RECONCILE":
        payload = report.get("action_payload") or {}
        resume_token = payload.get("resume_token")
        if resume_token:
            requests = monitoring.setdefault("resume_requests", [])
            if not any(item.get("resume_token") == resume_token for item in requests):
                requests.append({
                    "resume_token": resume_token,
                    "requested_at": now_iso_value,
                    "request_kind": report["state"],
                    "current_step": report.get("current_step"),
                    "reason": report.get("reason"),
                    "next_action": report.get("next_action"),
                    "delivery": payload.get("delivery", "sessions_send"),
                })
        monitoring["owner_query_at"] = now_iso_value
        monitoring["last_reconcile_at"] = now_iso_value
        monitoring["reconcile_count"] = int(monitoring.get("reconcile_count", 0) or 0) + 1
    elif report["state"] in {"BLOCKED_ESCALATE", "STOP_AND_DELETE"}:
        monitoring["last_escalated_at"] = now_iso_value if report["state"] == "BLOCKED_ESCALATE" else monitoring.get("last_escalated_at")
        monitoring["cron_state"] = "DELETE_REQUESTED"
    action_log_entry = {
        "at": now_iso_value,
        "state": report["state"],
        "reason": report["reason"],
        "action": report["action"],
    }
    if report.get("action_payload"):
        action_log_entry["payload_kind"] = report["action_payload"]["kind"]
    if report.get("failure_type"):
        action_log_entry["failure_type"] = report["failure_type"]
    monitoring.setdefault("action_log", []).append(action_log_entry)


def collect_paths(obj, prefix=""):
    paths = set()
    if isinstance(obj, dict):
        for key, value in obj.items():
            child = f"{prefix}.{key}" if prefix else key
            paths.add(child)
            paths |= collect_paths(value, child)
    return paths


def lookup_path(obj, path):
    node = obj
    for part in path.split("."):
        if not isinstance(node, dict) or part not in node:
            return None
        node = node[part]
    return node


def assert_only_supervision_changes(before_task, after_task):
    changed = set()
    for path in collect_paths(before_task) | collect_paths(after_task):
        if lookup_path(before_task, path) != lookup_path(after_task, path):
            changed.add(path)
    disallowed = [path for path in changed if not any(path == allowed or path.startswith(f"{allowed}.") for allowed in SUPERVISION_ALLOWED_PATHS)]
    if disallowed:
        raise RuntimeError(f"Monitor wrote non-supervision fields: {sorted(disallowed)}")


def task_age_seconds(task, now):
    started_at = first_non_null(task.get("created_at"), task.get("activation", {}).get("announced_at"), (task.get("checkpoints") or [{}])[0].get("at"))
    return age_seconds(now, started_at)


def evaluate_task(task, now):
    status = task.get("status")
    hb = task.get("heartbeat", {})
    blocker = task.get("blocker")
    activation = task.get("activation", {})
    monitoring = monitoring_config(task)
    current_step = task.get("current_checkpoint", "unknown")
    expected_interval = hb.get("expected_interval_sec", 300)
    timeout_sec = hb.get("timeout_sec", 1800)
    nudge_after_sec = monitoring["nudge_after_sec"]
    blocked_escalate_after_sec = monitoring["blocked_escalate_after_sec"]
    last_progress_at = first_non_null(hb.get("last_progress_at"), task.get("last_checkpoint_at"))
    last_heartbeat_at = hb.get("last_heartbeat_at")
    last_progress_age = age_seconds(now, last_progress_at)
    last_heartbeat_age = age_seconds(now, last_heartbeat_at)
    last_nudge_age = age_seconds(now, monitoring.get("last_nudge_at"))
    task_age = task_age_seconds(task, now)
    nudge_count = int(monitoring.get("nudge_count", 0) or 0)
    max_nudges = int(monitoring.get("max_nudges", DEFAULTS["max_nudges"]) or DEFAULTS["max_nudges"])
    escalate_after_nudges = int(monitoring.get("escalate_after_nudges", DEFAULTS["escalate_after_nudges"]) or DEFAULTS["escalate_after_nudges"])
    renotify_interval_sec = int(monitoring.get("renotify_interval_sec", expected_interval) or expected_interval)
    progress_is_fresh = is_progress_updating(task, now)
    external_eval = evaluate_external_evidence(task)
    pending_external = external_eval["has_legit_pending"] or has_pending_external_return(task)
    suspicious_jobs = external_eval["suspicious_jobs"]
    suspicious_reconcile_count = get_retry_count(monitoring, current_step, FAILURE_TYPES["MISSING_EXTERNAL_EVIDENCE"])
    should_renotify = last_nudge_age is None or last_nudge_age >= renotify_interval_sec
    candidates = []

    if monitoring.get("install_signal") == "INSTALL_FAILED":
        candidates.append({"state": "BLOCKED_ESCALATE", "reason": f"monitor install failed: {monitoring.get('cron_install_error', 'unknown error')}", "action": "signal_install_failed_then_delete_cron"})

    if status in TERMINAL_STATUSES:
        candidates.append({"state": "STOP_AND_DELETE", "reason": f"task status is terminal ({status}); monitor cron should self-delete", "action": "delete_monitor_cron"})

    if status in ACTIVE_STATUSES and task_age is not None and task_age > MAX_TASK_AGE_SEC:
        candidates.append({"state": "BLOCKED_ESCALATE", "reason": f"task exceeded 60-minute wall clock limit ({task_age}s > {MAX_TASK_AGE_SEC}s)", "action": "enforce_max_task_age_then_delete_cron"})

    if status in ACTIVE_STATUSES and suspicious_jobs:
        reconcile_attempt = increment_retry(monitoring, current_step, FAILURE_TYPES["MISSING_EXTERNAL_EVIDENCE"])
        reason = f"external pending claim lacks provider evidence on step '{current_step}'; monitor must reconcile owner evidence before trusting external wait"
        if reconcile_attempt >= MAX_RETRY_COUNT:
            candidates.append({
                "state": "BLOCKED_ESCALATE",
                "reason": f"{reason}; missing-evidence reconcile retry {reconcile_attempt}/{MAX_RETRY_COUNT}",
                "action": "escalate_missing_external_evidence_then_delete_cron",
                "failure_type": FAILURE_TYPES["MISSING_EXTERNAL_EVIDENCE"],
                "suspicious_external_jobs": suspicious_jobs,
            })
        else:
            candidates.append({
                "state": "OWNER_RECONCILE",
                "reason": f"{reason}; request owner evidence reconcile {reconcile_attempt}/{MAX_RETRY_COUNT}",
                "action": "query_owner_for_provider_evidence",
                "failure_type": FAILURE_TYPES["MISSING_EXTERNAL_EVIDENCE"],
                "suspicious_external_jobs": suspicious_jobs,
            })

    transient_counts = {ft: get_retry_count(monitoring, current_step, ft) for ft in TRANSIENT_FAILURE_TYPES}
    max_transient_retry = max(transient_counts.values()) if transient_counts else 0
    blocker_failure_type = str((blocker or {}).get("failure_type") or "").upper()

    if status == "BLOCKED":
        timeout_count = get_retry_count(monitoring, current_step, FAILURE_TYPES["TIMEOUT"])
        exec_count = get_retry_count(monitoring, current_step, FAILURE_TYPES["EXECUTION_ERROR"])
        ext_count = get_retry_count(monitoring, current_step, FAILURE_TYPES["EXTERNAL_WAIT"])
        missing_evidence_count = get_retry_count(monitoring, current_step, FAILURE_TYPES["MISSING_EXTERNAL_EVIDENCE"])
        max_retry = max(timeout_count, exec_count, ext_count, missing_evidence_count, max_transient_retry)
        if blocker_failure_type in TRANSIENT_FAILURE_TYPES and max_transient_retry <= MIN_RETRY_BEFORE_ESCALATE:
            candidates.append({"state": "NUDGE_MAIN_AGENT", "reason": f"transient failure '{blocker_failure_type}' on step '{current_step}' has only retried {max_transient_retry}/{MIN_RETRY_BEFORE_ESCALATE}; retry before BLOCKED escalation", "action": "send_execution_nudge", "failure_type": blocker_failure_type})
        elif monitoring.get("last_escalated_at"):
            candidates.append({"state": "STOP_AND_DELETE", "reason": f"{(blocker or {}).get('reason') or 'task blocked'}; escalation already sent", "action": "delete_monitor_cron"})
        elif max_retry >= MAX_RETRY_COUNT:
            candidates.append({"state": "BLOCKED_ESCALATE", "reason": f"retry limit reached on step '{current_step}' (timeouts={timeout_count}, exec_errors={exec_count}, ext_waits={ext_count}, weak_external_claims={missing_evidence_count}, transient={max_transient_retry})", "action": "send_blocked_escalation_then_delete_cron"})
        elif last_progress_age is not None and last_progress_age >= blocked_escalate_after_sec and max_transient_retry > MIN_RETRY_BEFORE_ESCALATE:
            candidates.append({"state": "BLOCKED_ESCALATE", "reason": f"{(blocker or {}).get('reason') or 'task blocked'}; blocked for {last_progress_age}s after retry-first contract satisfied", "action": "send_blocked_escalation_then_delete_cron"})
        elif max_transient_retry > MIN_RETRY_BEFORE_ESCALATE:
            candidates.append({"state": "BLOCKED_ESCALATE", "reason": f"{(blocker or {}).get('reason') or 'task blocked'}; retry-first contract satisfied, escalate once then stop", "action": "send_blocked_escalation_then_delete_cron"})
        else:
            candidates.append({"state": "BLOCKED_ESCALATE", "reason": f"{(blocker or {}).get('reason') or 'task blocked'}; non-transient blocker escalates once then stop", "action": "send_blocked_escalation_then_delete_cron"})

    if status in ACTIVE_STATUSES and not activation.get("announced"):
        candidates.append({"state": "NUDGE_MAIN_AGENT", "reason": "activation record missing while task is active", "action": "remind_main_agent_to_activate_and_resume"})

    if status in ACTIVE_STATUSES and last_progress_age is not None and last_progress_age > timeout_sec:
        if pending_external or progress_is_fresh:
            candidates.append({"state": "OK", "reason": f"no checkpoint for {last_progress_age}s but agent is working / external job pending", "action": "noop_external_wait"})
        else:
            new_count = increment_retry(monitoring, current_step, FAILURE_TYPES["TIMEOUT"])
            if new_count >= MAX_RETRY_COUNT:
                candidates.append({"state": "BLOCKED_ESCALATE", "reason": f"no checkpoint for {last_progress_age}s on step '{current_step}'; TIMEOUT retry {new_count}/{MAX_RETRY_COUNT}", "action": "send_blocked_escalation_then_delete_cron", "failure_type": FAILURE_TYPES["TIMEOUT"]})
            else:
                candidates.append({"state": "STALE_PROGRESS", "reason": f"no new checkpoint for {last_progress_age}s (> timeout_sec={timeout_sec}); TIMEOUT retry {new_count}/{MAX_RETRY_COUNT}", "action": "flag_stale_progress_pre_gate", "failure_type": FAILURE_TYPES["TIMEOUT"]})

    if status in ACTIVE_STATUSES and last_heartbeat_age is not None and last_heartbeat_age > expected_interval:
        if pending_external or progress_is_fresh:
            candidates.append({"state": "OK", "reason": f"heartbeat overdue {last_heartbeat_age}s but agent is working / external job pending", "action": "noop_external_wait"})
        else:
            candidates.append({"state": "HEARTBEAT_DUE", "reason": f"no heartbeat for {last_heartbeat_age}s (> expected_interval_sec={expected_interval})", "action": "send_lightweight_supervision_reminder"})

    gap1_nudged_steps = set(monitoring.get("gap1_nudged_steps", []))
    if status in ACTIVE_STATUSES and last_progress_age is not None and last_progress_age > nudge_after_sec and not (pending_external or progress_is_fresh or suspicious_jobs) and current_step not in gap1_nudged_steps:
        if nudge_count >= max_nudges or (nudge_count >= escalate_after_nudges and should_renotify):
            candidates.append({"state": "OWNER_RECONCILE", "reason": f"task already nudged {nudge_count} times with no fresh checkpoint", "action": "query_owner_and_force_reconciliation"})
        else:
            candidates.append({"state": "NUDGE_MAIN_AGENT", "reason": f"task has no fresh checkpoint for {last_progress_age}s; main agent should resume / checkpoint / close loop", "action": "send_execution_nudge" if should_renotify else "nudge_already_sent_wait_for_interval"})

    # GAP-1 fix: "step completed but next step hasn't started" stall detection.
    # When the current checkpoint's workflow sub-state is terminal (DONE/COMPLETED/FAILED/BLOCKED),
    # no new checkpoint arrives within one check interval, and no external job is legitimately pending,
    # the task has stalled mid-stream.
    #
    # One-shot escalation: NUDGE once → immediately escalate on next tick.
    # Track nudged steps in `gap1_nudged_steps` set. If step already nudged → escalate now.
    # This prevents the "nudge storm" of repeated NUDGE_MAIN_AGENT every tick.
    if status in ACTIVE_STATUSES and not pending_external and not progress_is_fresh:
        current_step_id = task.get("current_checkpoint")
        workflow = task.get("workflow", [])
        if is_workflow_step_terminal(workflow, current_step_id):
            gap1_nudged_steps = set(monitoring.get("gap1_nudged_steps", []))
            if current_step_id in gap1_nudged_steps:
                # Already nudged for this step's GAP-1 → escalate immediately (one-shot escalation)
                candidates.append({
                    "state": "BLOCKED_ESCALATE",
                    "reason": f"step '{current_step_id}' already nudged but still stalled; one-shot escalation fired",
                    "action": "send_blocked_escalation_then_delete_cron",
                    "failure_type": FAILURE_TYPES["STEP_COMPLETED_BUT_STALLING"],
                })
            else:
                # First detection for this step → NUDGE once, record in set
                gap1_nudged_steps.add(current_step_id)
                monitoring["gap1_nudged_steps"] = sorted(gap1_nudged_steps)
                candidates.append({
                    "state": "NUDGE_MAIN_AGENT",
                    "reason": f"step '{current_step_id}' done but task stalled; no new checkpoint in {last_progress_age}s; advance to next step or write BLOCKED",
                    "action": "send_execution_nudge",
                    "failure_type": FAILURE_TYPES["STEP_COMPLETED_BUT_STALLING"],
                })
                # Record that we've nudged for this step (supervision metadata, written by apply_supervision_update)

    if not candidates:
        if status in ACTIVE_STATUSES and (progress_is_fresh or pending_external):
            clear_all_retries_for_step(monitoring, current_step)
        candidates.append({"state": "OK", "reason": "heartbeat/progress within thresholds", "action": "noop"})

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
        "task_age_sec": task_age,
        "nudge_count": nudge_count,
        "max_nudges": max_nudges,
        "next_action": task.get("next_action"),
        "blocker": blocker,
        "current_step": current_step,
        "retry_count": dict(monitoring.get("retry_count", {})),
        "pending_external": pending_external,
        "suspicious_external_jobs": suspicious_jobs,
        "failure_type": chosen.get("failure_type"),
        "external_evidence_ok": external_eval["has_legit_pending"],
        "gap1_nudged_steps": list(monitoring.get("gap1_nudged_steps", [])),
    }


def main():
    parser = argparse.ArgumentParser(description="Evaluate low-cost execution nudge state for long tasks")
    parser.add_argument("--ledger", type=Path, required=True)
    parser.add_argument("--only-active", action="store_true")
    parser.add_argument("--apply-supervision", action="store_true")
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
