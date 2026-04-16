#!/usr/bin/env python3
import argparse
import json
from copy import deepcopy
from datetime import datetime, timezone
from pathlib import Path

from task_ledger import PENDING_EXTERNAL_STATES, PROVIDER_EVIDENCE_KEYS, project_task

TERMINAL_STATUSES = {"COMPLETED", "FAILED", "ABANDONED"}
STATE_PRIORITY = [
    "STOP_AND_DELETE",
    "TRUTH_INCONSISTENT",
    "OWNER_RECONCILE",
    "BLOCKED_ESCALATE",
    "NUDGE_MAIN_AGENT",
    "STALE_PROGRESS",
    "HEARTBEAT_DUE",
    "OK",
]
SUPERVISION_ALLOWED_PATHS = {
    "heartbeat",
    "heartbeat.watchdog_state",
    "heartbeat.last_observed_progress_at",
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
    "monitoring.retry_count",
    "monitoring.install_signal",
    "monitoring.cron_install_error",
    "monitoring.resume_requests",
    "derived",
    "status",
    "current_checkpoint",
    "last_checkpoint_at",
    "heartbeat.last_progress_at",
    "heartbeat.last_observed_progress_at",
}
OBSERVED_TRANSIENT_FAILURES = {"TIMEOUT", "DOWNLOAD_TIMEOUT", "DOWNLOAD_INCOMPLETE", "TRANSIENT_NETWORK", "EXECUTION_ERROR", "EXTERNAL_WAIT"}


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


def choose_state(candidates):
    ranked = {name: idx for idx, name in enumerate(STATE_PRIORITY)}
    return sorted(candidates, key=lambda item: ranked[item["state"]])[0]


def load_ledger(path: Path):
    return json.loads(path.read_text())


def save_ledger(path: Path, ledger):
    ledger["updated_at"] = now_iso()
    path.write_text(json.dumps(ledger, ensure_ascii=False, indent=2) + "\n")


def monitoring_config(task):
    hb = task.get("heartbeat", {})
    raw = task.get("monitoring", {})
    expected_interval = hb.get("expected_interval_sec", 300)
    timeout_sec = hb.get("timeout_sec", 1800)
    return {
        "nudge_after_sec": raw.get("nudge_after_sec") or timeout_sec,
        "renotify_interval_sec": raw.get("renotify_interval_sec") or expected_interval,
        "max_nudges": int(raw.get("max_nudges", 3) or 3),
        "escalate_after_nudges": int(raw.get("escalate_after_nudges", 2) or 2),
        "blocked_escalate_after_sec": raw.get("blocked_escalate_after_sec") or max(timeout_sec, expected_interval),
    }


def build_resume_token(task, chosen, now_iso_value):
    step = task.get("derived", {}).get("current_step") or task.get("current_checkpoint") or "unknown"
    safe = now_iso_value.replace(":", "").replace("-", "")
    return f"{task.get('task_id')}:{step}:{chosen['state']}:{safe}"


def build_action_payload(task, chosen, now_iso_value):
    task_id = task.get("task_id")
    owner = task.get("owner")
    channel = task.get("channel")
    derived = task.get("derived", {})
    step = derived.get("current_step") or task.get("current_checkpoint")
    if chosen["state"] in {"NUDGE_MAIN_AGENT", "OWNER_RECONCILE", "TRUTH_INCONSISTENT"}:
        resume_token = build_resume_token(task, chosen, now_iso_value)
        message = chosen.get("message") or chosen["reason"]
        return {
            "kind": chosen["state"],
            "delivery": "sessions_send",
            "deliver_to": owner,
            "channel": channel,
            "title": f"{chosen['state']} for {task_id}",
            "message": message,
            "facts": {
                "task_id": task_id,
                "current_step": step,
                "reason": chosen["reason"],
                "next_action": task.get("next_action"),
                "resume_token": resume_token,
                "inconsistencies": derived.get("inconsistencies", []),
                "suspicious_external_jobs": derived.get("suspicious_external_jobs", []),
                "required_provider_evidence": sorted(PROVIDER_EVIDENCE_KEYS),
                "branches": {
                    "A_IN_PROGRESS_FORGOT_LEDGER": "還在做，只是忘了補 ledger；請立刻補真實 checkpoint",
                    "B_BLOCKED": "已確認卡住；請寫 BLOCKED truth 與具體 unblock need",
                    "C_COMPLETED": "其實已完成；請補 TASK_COMPLETED truth 與 validation evidence",
                    "D_NO_REPLY": "目前無法確認；先找外部 evidence，不要猜測 task truth",
                    "E_FORGOT_OR_NOT_DOING": "承認沒做或忘了做；請立刻 resume 並補做",
                },
            },
            "resume_token": resume_token,
            "created_at": now_iso_value,
        }
    if chosen["state"] == "BLOCKED_ESCALATE":
        return {
            "kind": chosen["state"],
            "deliver_to": owner,
            "channel": channel,
            "title": f"⚠️ Blocked escalation for {task_id}",
            "message": chosen["reason"],
            "facts": {
                "task_id": task_id,
                "current_checkpoint": step,
                "reason": chosen["reason"],
                "blocker": task.get("blocker"),
                "retry_count": task.get("monitoring", {}).get("retry_count", {}),
                "inconsistencies": derived.get("inconsistencies", []),
            },
            "created_at": now_iso_value,
        }
    if chosen["state"] == "STOP_AND_DELETE":
        return {
            "kind": chosen["state"],
            "deliver_to": task.get("monitoring", {}).get("cron_owner", "long-task-monitor"),
            "channel": channel,
            "title": f"Stop monitor for {task_id}",
            "message": chosen["reason"],
            "facts": {"task_id": task_id, "status": task.get("status"), "reason": chosen["reason"]},
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
    if report["state"] == "NUDGE_MAIN_AGENT":
        monitoring["nudge_count"] = int(monitoring.get("nudge_count", 0) or 0) + 1
        monitoring["last_nudge_at"] = now_iso_value
    if report["state"] in {"OWNER_RECONCILE", "TRUTH_INCONSISTENT"}:
        monitoring["owner_query_at"] = now_iso_value
        monitoring["last_reconcile_at"] = now_iso_value
        monitoring["reconcile_count"] = int(monitoring.get("reconcile_count", 0) or 0) + 1
    if report["state"] == "BLOCKED_ESCALATE":
        monitoring["last_escalated_at"] = now_iso_value
        monitoring["cron_state"] = "DELETE_REQUESTED"
    if report["state"] == "STOP_AND_DELETE":
        monitoring["cron_state"] = "DELETE_REQUESTED"
    if report["state"] in {"NUDGE_MAIN_AGENT", "OWNER_RECONCILE", "TRUTH_INCONSISTENT"}:
        payload = report.get("action_payload") or {}
        token = payload.get("resume_token")
        if token:
            requests = monitoring.setdefault("resume_requests", [])
            if not any(item.get("resume_token") == token for item in requests):
                requests.append({
                    "resume_token": token,
                    "requested_at": now_iso_value,
                    "request_kind": report["state"],
                    "current_step": report.get("current_step"),
                    "reason": report.get("reason"),
                    "next_action": report.get("next_action"),
                    "delivery": payload.get("delivery", "sessions_send"),
                })
            monitoring["last_resume_request_at"] = now_iso_value
    monitoring.setdefault("action_log", []).append({
        "at": now_iso_value,
        "state": report["state"],
        "reason": report["reason"],
        "action": report["action"],
        "payload_kind": (report.get("action_payload") or {}).get("kind"),
    })


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


def evaluate_task(task, now):
    project_task(task)
    derived = task.get("derived", {})
    hb = task.get("heartbeat", {})
    monitoring = task.get("monitoring", {})
    cfg = monitoring_config(task)
    status = task.get("status")
    current_step = derived.get("current_step") or task.get("current_checkpoint") or "unknown"
    last_progress_age = age_seconds(now, first_non_null(task.get("last_checkpoint_at"), derived.get("last_observed_progress_at"), hb.get("last_progress_at")))
    last_heartbeat_age = age_seconds(now, hb.get("last_heartbeat_at"))
    last_nudge_age = age_seconds(now, monitoring.get("last_nudge_at"))
    should_renotify = last_nudge_age is None or last_nudge_age >= cfg["renotify_interval_sec"]
    nudge_count = int(monitoring.get("nudge_count", 0) or 0)
    candidates = []


    if status == "BLOCKED" and monitoring.get("last_escalated_at"):
        candidates.append({"state": "STOP_AND_DELETE", "reason": "blocked escalation already delivered", "action": "delete_monitor_cron"})

    user_facing = derived.get("user_facing") or {}
    outcome_status = user_facing.get("outcome_status")
    outcome_artifacts = user_facing.get("artifacts") or []

    if derived.get("truth_state") == "INCONSISTENT":
        candidates.append({
            "state": "TRUTH_INCONSISTENT",
            "reason": "observed truth is incomplete or contradictory; reconcile owner/external observations before any OK/pending judgement",
            "action": "request_truth_reconcile",
            "message": "Observed truth is inconsistent. First observe the real world and backfill truth; do not guess pending/OK from partial data.",
        })

    if outcome_status == "PARTIAL_SUCCESS":
        candidates.append({
            "state": "OWNER_RECONCILE",
            "reason": "outputs already exist, so reconcile to a user-facing partial-success/success result before surfacing control-plane interruption noise",
            "action": "request_result_reconcile",
            "message": "Useful outputs already exist. Reconcile the task from the user/result point of view first: report partial success or success with the produced artifacts, then mention any interruption/cleanup issue honestly.",
        })

    if status in TERMINAL_STATUSES:
        inconsistencies = derived.get("inconsistencies", [])
        # "completed_but_steps_not_done" is an informational marker (steps were skipped),
        # not a real recoverable inconsistency. For monitoring purposes, treat terminal
        # tasks with only this inconsistency as clean terminal → STOP_AND_DELETE.
        non_step_inconsistencies = [i for i in inconsistencies if i != "task:completed_but_steps_not_done" and not i.startswith("task:completed")]
        if non_step_inconsistencies or derived.get("truth_state") == "INCONSISTENT":
            candidates.append({"state": "TRUTH_INCONSISTENT", "reason": f"task status is terminal ({status}) but has unresolved inconsistencies", "action": "request_owner_reconcile_then_delete"})
        else:
            candidates.append({"state": "STOP_AND_DELETE", "reason": f"task status is terminal ({status})", "action": "delete_monitor_cron"})

    if derived.get("suspicious_external_jobs"):
        candidates.append({
            "state": "OWNER_RECONCILE",
            "reason": "external pending claim lacks provider evidence; owner must observe and backfill real external truth first",
            "action": "request_external_truth_reconcile",
            "message": "External job claim is missing provider evidence. Observe provider response / handle / receipt first, then backfill truth.",
        })

    if status == "BLOCKED" and not monitoring.get("last_escalated_at"):
        failure_type = str((task.get("blocker") or {}).get("failure_type") or "").upper()
        if failure_type in OBSERVED_TRANSIENT_FAILURES:
            step_key = f"{current_step}:{failure_type}"
            retry_count = int(monitoring.get("retry_count", {}).get(step_key, 0) or 0)
            if retry_count < 2:
                candidates.append({
                    "state": "OWNER_RECONCILE",
                    "reason": f"blocked on observed transient failure {failure_type}; retry-first contract requires observed retry/resume truth before escalation ({retry_count}/2)",
                    "action": "request_retry_truth",
                })
            else:
                candidates.append({"state": "BLOCKED_ESCALATE", "reason": f"blocked after observed transient failure {failure_type} retries exhausted", "action": "send_blocked_escalation_then_delete_cron"})
        else:
            candidates.append({"state": "BLOCKED_ESCALATE", "reason": "blocked confirmed by observed truth", "action": "send_blocked_escalation_then_delete_cron"})

    if status == "RUNNING" and derived.get("pending_external"):
        candidates.append({"state": "OK", "reason": "observed external truth shows legitimate pending external work", "action": "noop_external_wait"})

    expected_interval = hb.get("expected_interval_sec", 300)
    timeout_sec = hb.get("timeout_sec", 1800)
    if status == "RUNNING" and last_progress_age is not None and last_progress_age > timeout_sec and not derived.get("pending_external") and derived.get("truth_state") == "CONSISTENT":
        if nudge_count >= cfg["escalate_after_nudges"] and should_renotify:
            candidates.append({"state": "OWNER_RECONCILE", "reason": f"no observed progress truth for {last_progress_age}s after prior nudges", "action": "query_owner_for_truth"})
        elif should_renotify:
            candidates.append({"state": "NUDGE_MAIN_AGENT", "reason": f"no observed progress truth for {last_progress_age}s; owner must resume and backfill truth", "action": "send_execution_nudge"})
        else:
            candidates.append({"state": "STALE_PROGRESS", "reason": f"progress truth stale for {last_progress_age}s; waiting for renotify interval", "action": "wait_for_renotify"})
    elif status == "RUNNING" and last_heartbeat_age is not None and last_heartbeat_age > expected_interval and not derived.get("pending_external") and derived.get("truth_state") == "CONSISTENT":
        candidates.append({"state": "HEARTBEAT_DUE", "reason": f"heartbeat overdue for {last_heartbeat_age}s", "action": "send_light_reminder"})

    if not candidates:
        candidates.append({"state": "OK", "reason": "observed truth and derived state are consistent within thresholds", "action": "noop"})

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
        "next_action": task.get("next_action"),
        "current_step": current_step,
        "pending_external": derived.get("pending_external"),
        "truth_state": derived.get("truth_state"),
        "inconsistencies": derived.get("inconsistencies", []),
        "suspicious_external_jobs": derived.get("suspicious_external_jobs", []),
        "retry_count": monitoring.get("retry_count", {}),
        "user_facing": user_facing,
        "outcome_artifact_count": len(outcome_artifacts),
    }


def main():
    parser = argparse.ArgumentParser(description="Evaluate execution supervision for long tasks")
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
