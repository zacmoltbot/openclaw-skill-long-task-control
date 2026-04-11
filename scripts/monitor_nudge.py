#!/usr/bin/env python3
import argparse
import json
from datetime import datetime
from pathlib import Path

TERMINAL_STATUSES = {"COMPLETED", "FAILED", "ABANDONED"}
ACTIVE_STATUSES = {"PENDING", "RUNNING"}
STATE_PRIORITY = [
    "STOP_AND_DELETE",
    "BLOCKED_ESCALATE",
    "NUDGE_MAIN_AGENT",
    "STALE_PROGRESS",
    "HEARTBEAT_DUE",
    "OK",
]
DEFAULTS = {
    "nudge_after_sec": None,
    "renotify_interval_sec": None,
    "max_nudges": 3,
    "escalate_after_nudges": 2,
    "blocked_escalate_after_sec": None,
}


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


def evaluate_task(task, now):
    status = task.get("status")
    hb = task.get("heartbeat", {})
    blocker = task.get("blocker")
    activation = task.get("activation", {})
    monitoring = monitoring_config(task)

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
        if last_progress_age is not None and last_progress_age >= blocked_escalate_after_sec:
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

    if status in ACTIVE_STATUSES and last_progress_age is not None and last_progress_age > timeout_sec:
        candidates.append({
            "state": "STALE_PROGRESS",
            "reason": f"no new checkpoint for {last_progress_age}s (> timeout_sec={timeout_sec})",
            "action": "flag_stale_progress_pre_gate",
        })

    if status in ACTIVE_STATUSES and last_heartbeat_age is not None and last_heartbeat_age > expected_interval:
        candidates.append({
            "state": "HEARTBEAT_DUE",
            "reason": f"no heartbeat for {last_heartbeat_age}s (> expected_interval_sec={expected_interval})",
            "action": "send_lightweight_supervision_reminder",
        })

    if status in ACTIVE_STATUSES and last_progress_age is not None and last_progress_age > nudge_after_sec:
        if nudge_count >= max_nudges:
            candidates.append({
                "state": "BLOCKED_ESCALATE",
                "reason": (
                    f"task exceeded max nudges ({nudge_count}/{max_nudges}); escalate instead of sending more reminders"
                ),
                "action": "escalate_human_or_owner_then_delete_cron",
            })
        elif nudge_count >= escalate_after_nudges and should_renotify:
            candidates.append({
                "state": "BLOCKED_ESCALATE",
                "reason": (
                    f"task already nudged {nudge_count} times with no fresh checkpoint; escalate to avoid infinite reminder loop"
                ),
                "action": "escalate_human_or_owner_then_delete_cron",
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
        candidates.append({
            "state": "OK",
            "reason": "heartbeat/progress within thresholds",
            "action": "noop",
        })

    chosen = choose_state(candidates)
    return {
        "task_id": task.get("task_id"),
        "status": status,
        "state": chosen["state"],
        "reason": chosen["reason"],
        "action": chosen["action"],
        "last_progress_age_sec": last_progress_age,
        "last_heartbeat_age_sec": last_heartbeat_age,
        "last_nudge_age_sec": last_nudge_age,
        "nudge_count": nudge_count,
        "max_nudges": max_nudges,
        "next_action": task.get("next_action"),
        "blocker": blocker,
    }


def main():
    parser = argparse.ArgumentParser(description="Evaluate low-cost execution nudge state for long tasks")
    parser.add_argument("--ledger", type=Path, required=True)
    parser.add_argument("--only-active", action="store_true", help="Skip terminal tasks from output")
    args = parser.parse_args()

    ledger = json.loads(args.ledger.read_text())
    now = datetime.now().astimezone()
    reports = []
    for task in ledger.get("tasks", []):
        if args.only_active and task.get("status") in TERMINAL_STATUSES:
            continue
        reports.append(evaluate_task(task, now))

    print(json.dumps({"generated_at": now.isoformat(timespec="seconds"), "reports": reports}, ensure_ascii=False, indent=2))


if __name__ == "__main__":
    main()
