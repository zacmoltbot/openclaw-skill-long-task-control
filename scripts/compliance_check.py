#!/usr/bin/env python3
import argparse
import json
import re
from pathlib import Path

VAGUE_PATTERNS = [
    r"still working",
    r"working on it",
    r"繼續處理",
    r"還在跑",
    r"快好了",
    r"差不多",
]


def text_has_vague_progress(text):
    if not text:
        return False
    lowered = text.lower()
    return any(re.search(pattern, lowered, re.IGNORECASE) for pattern in VAGUE_PATTERNS)


def check_ledger(ledger):
    findings = []
    for task in ledger.get("tasks", []):
        task_id = task.get("task_id")
        prefix = f"[{task_id}]"
        status = task.get("status")
        activation = task.get("activation", {})
        checkpoints = task.get("checkpoints", []) or []
        blocker = task.get("blocker")
        heartbeat = task.get("heartbeat", {})

        if status in {"RUNNING", "BLOCKED", "COMPLETED"} and not activation.get("announced"):
            findings.append({"severity": "error", "code": "missing_activation", "message": f"{prefix} task active but activation.announced=false"})

        if status in {"RUNNING", "BLOCKED", "COMPLETED"} and not task_id:
            findings.append({"severity": "error", "code": "missing_task_id", "message": f"{prefix} active task missing task_id"})

        last_progress = heartbeat.get("last_progress_at") or task.get("last_checkpoint_at")
        timeout_sec = heartbeat.get("timeout_sec")
        if status == "RUNNING" and timeout_sec and not last_progress:
            findings.append({"severity": "error", "code": "missing_checkpoint_clock", "message": f"{prefix} running task missing progress timestamp for timeout tracking"})

        if status == "BLOCKED" and not blocker:
            findings.append({"severity": "error", "code": "blocked_silent", "message": f"{prefix} status=BLOCKED but blocker payload missing"})

        if status == "COMPLETED" and not task.get("validation"):
            findings.append({"severity": "warn", "code": "completed_without_validation", "message": f"{prefix} completed task has no validation evidence"})

        for idx, cp in enumerate(checkpoints, start=1):
            summary = cp.get("summary", "")
            facts = cp.get("facts") or {}
            if text_has_vague_progress(summary) and not facts:
                findings.append({"severity": "warn", "code": "vague_progress", "message": f"{prefix} checkpoint #{idx} uses vague progress language without facts: {summary}"})

            if cp.get("kind") in {"CHECKPOINT", "STARTED", "COMPLETED", "BLOCKED"} and not task_id:
                findings.append({"severity": "error", "code": "checkpoint_without_task_id", "message": f"{prefix} checkpoint #{idx} exists but task_id missing"})

    return findings


def main():
    p = argparse.ArgumentParser(description="Check long-task-control ledger compliance")
    p.add_argument("--ledger", type=Path, required=True)
    p.add_argument("--fail-on-severity", choices=["warn", "error"])
    args = p.parse_args()

    ledger = json.loads(args.ledger.read_text())
    findings = check_ledger(ledger)
    print(json.dumps({"finding_count": len(findings), "findings": findings}, ensure_ascii=False, indent=2))

    if args.fail_on_severity:
        order = {"warn": 1, "error": 2}
        threshold = order[args.fail_on_severity]
        should_fail = any(order[item["severity"]] >= threshold for item in findings)
        raise SystemExit(1 if should_fail else 0)


if __name__ == "__main__":
    main()
