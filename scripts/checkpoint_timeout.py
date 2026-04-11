#!/usr/bin/env python3
import argparse
import json
from datetime import datetime
from pathlib import Path


WATCHDOG_PRIORITY = [
    "MISSING_ACTIVATION",
    "COMPLETED_NO_VALIDATION",
    "BLOCKED_SILENT",
    "STALE_PROGRESS",
    "HEARTBEAT_DUE",
    "OK",
]


def parse_ts(value):
    if not value:
        return None
    return datetime.fromisoformat(value)


def age_seconds(now, value):
    ts = parse_ts(value)
    if not ts:
        return None
    return int((now - ts).total_seconds())


def choose_state(candidates):
    ranked = {name: idx for idx, name in enumerate(WATCHDOG_PRIORITY)}
    return sorted(candidates, key=lambda x: ranked.get(x, 999))[0]


def evaluate_task(task, now):
    hb = task.get("heartbeat", {})
    status = task.get("status")
    activation = task.get("activation", {})
    validation = task.get("validation", []) or []
    blocker = task.get("blocker")

    expected_interval = hb.get("expected_interval_sec", 900)
    timeout_sec = hb.get("timeout_sec", 1800)
    last_hb_age = age_seconds(now, hb.get("last_heartbeat_at"))
    last_progress_age = age_seconds(now, hb.get("last_progress_at") or task.get("last_checkpoint_at"))

    findings = []
    if status in {"RUNNING", "BLOCKED"} and not activation.get("announced"):
        findings.append("MISSING_ACTIVATION")
    if status == "COMPLETED" and not validation:
        findings.append("COMPLETED_NO_VALIDATION")
    if status == "BLOCKED" and not blocker:
        findings.append("BLOCKED_SILENT")
    if status == "RUNNING" and last_progress_age is not None and last_progress_age > timeout_sec:
        findings.append("STALE_PROGRESS")
    if status in {"RUNNING", "BLOCKED"} and last_hb_age is not None and last_hb_age > expected_interval:
        findings.append("HEARTBEAT_DUE")
    if not findings:
        findings.append("OK")

    state = choose_state(findings)
    return {
        "task_id": task.get("task_id"),
        "status": status,
        "watchdog_state": state,
        "findings": findings,
        "last_progress_age_sec": last_progress_age,
        "last_heartbeat_age_sec": last_hb_age,
        "next_action": task.get("next_action"),
    }


def main():
    p = argparse.ArgumentParser(description="Detect stale long-task checkpoints / heartbeat timeouts")
    p.add_argument("--ledger", type=Path, required=True)
    p.add_argument("--fail-on", action="append", default=[], help="Exit non-zero if any finding matches")
    args = p.parse_args()

    ledger = json.loads(args.ledger.read_text())
    now = datetime.now().astimezone()
    reports = [evaluate_task(task, now) for task in ledger.get("tasks", [])]

    print(json.dumps({"generated_at": now.isoformat(timespec="seconds"), "reports": reports}, ensure_ascii=False, indent=2))

    fail_on = set(args.fail_on)
    if fail_on:
        matched = any(any(f in fail_on for f in r["findings"]) for r in reports)
        raise SystemExit(1 if matched else 0)


if __name__ == "__main__":
    main()
