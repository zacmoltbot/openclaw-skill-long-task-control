#!/usr/bin/env python3
import argparse
import json
from datetime import datetime, timezone
from pathlib import Path

DEFAULT_LEDGER = Path("state/long-task-ledger.example.json")


def now_iso():
    return datetime.now(timezone.utc).astimezone().isoformat(timespec="seconds")


def load_ledger(path: Path):
    if not path.exists():
        return {"version": 1, "updated_at": now_iso(), "tasks": []}
    return json.loads(path.read_text())


def save_ledger(path: Path, ledger):
    path.parent.mkdir(parents=True, exist_ok=True)
    ledger["updated_at"] = now_iso()
    path.write_text(json.dumps(ledger, ensure_ascii=False, indent=2) + "\n")


def find_task(ledger, task_id):
    for task in ledger.get("tasks", []):
        if task.get("task_id") == task_id:
            return task
    return None


def ensure_task(ledger, task_id):
    task = find_task(ledger, task_id)
    if not task:
        raise SystemExit(f"Task not found: {task_id}")
    return task


def parse_fact(values):
    facts = {}
    for item in values or []:
        if "=" not in item:
            raise SystemExit(f"Invalid --fact value: {item}; expected key=value")
        key, value = item.split("=", 1)
        facts[key.strip()] = value.strip()
    return facts


def cmd_list(args):
    ledger = load_ledger(args.ledger)
    for task in ledger.get("tasks", []):
        print(f"{task.get('task_id')}\t{task.get('status')}\t{task.get('current_checkpoint')}\t{task.get('next_action')}")


def cmd_init(args):
    ledger = load_ledger(args.ledger)
    if find_task(ledger, args.task_id):
        raise SystemExit(f"Task already exists: {args.task_id}")

    workflow = []
    for idx, step in enumerate(args.workflow or [], start=1):
        workflow.append({
            "id": f"step-{idx:02d}",
            "title": step,
            "state": "RUNNING" if idx == 1 else "PENDING",
        })

    task = {
        "task_id": args.task_id,
        "skill": "long-task-control",
        "goal": args.goal,
        "status": "RUNNING",
        "channel": args.channel,
        "owner": args.owner,
        "activation": {
            "announced": args.activation_announced,
            "announced_at": args.activation_at or now_iso(),
            "message_ref": args.message_ref,
        },
        "workflow": workflow,
        "current_checkpoint": workflow[0]["id"] if workflow else None,
        "checkpoints": [
            {
                "at": now_iso(),
                "kind": "STARTED",
                "summary": args.summary or "Task initialized",
                "facts": parse_fact(args.fact),
            }
        ],
        "last_checkpoint_at": now_iso(),
        "heartbeat": {
            "expected_interval_sec": args.expected_interval_sec,
            "timeout_sec": args.timeout_sec,
            "last_progress_at": now_iso(),
            "last_heartbeat_at": now_iso(),
            "watchdog_state": "OK",
        },
        "validation": [],
        "blocker": None,
        "artifacts": args.artifact or [],
        "next_action": args.next_action,
        "notes": args.note or [],
    }
    ledger.setdefault("tasks", []).append(task)
    save_ledger(args.ledger, ledger)
    print(f"Initialized {args.task_id}")


def cmd_checkpoint(args):
    ledger = load_ledger(args.ledger)
    task = ensure_task(ledger, args.task_id)
    entry = {
        "at": now_iso(),
        "kind": args.kind,
        "summary": args.summary,
        "facts": parse_fact(args.fact),
    }
    task.setdefault("checkpoints", []).append(entry)
    task["last_checkpoint_at"] = entry["at"]
    task.setdefault("heartbeat", {})["last_progress_at"] = entry["at"]
    task["status"] = args.status or task.get("status", "RUNNING")
    if args.current_checkpoint:
        task["current_checkpoint"] = args.current_checkpoint
    if args.next_action:
        task["next_action"] = args.next_action
    if args.artifact:
        task.setdefault("artifacts", []).extend(args.artifact)
    if task.get("status") != "BLOCKED":
        task["blocker"] = None
    save_ledger(args.ledger, ledger)
    print(f"Recorded {args.kind} for {args.task_id}")


def cmd_block(args):
    ledger = load_ledger(args.ledger)
    task = ensure_task(ledger, args.task_id)
    blocked_at = now_iso()
    task["status"] = "BLOCKED"
    task["blocker"] = {
        "reason": args.reason,
        "need": args.need or [],
        "safe_next_step": args.safe_next_step,
    }
    task["last_checkpoint_at"] = blocked_at
    task.setdefault("heartbeat", {})["last_progress_at"] = blocked_at
    task.setdefault("checkpoints", []).append({
        "at": blocked_at,
        "kind": "BLOCKED",
        "summary": args.reason,
        "facts": parse_fact(args.fact),
    })
    if args.current_checkpoint:
        task["current_checkpoint"] = args.current_checkpoint
    if args.next_action:
        task["next_action"] = args.next_action
    save_ledger(args.ledger, ledger)
    print(f"Blocked {args.task_id}")


def cmd_heartbeat(args):
    ledger = load_ledger(args.ledger)
    task = ensure_task(ledger, args.task_id)
    hb_at = now_iso()
    task.setdefault("heartbeat", {})["last_heartbeat_at"] = hb_at
    if args.watchdog_state:
        task["heartbeat"]["watchdog_state"] = args.watchdog_state
    if args.note:
        task.setdefault("notes", []).append(args.note)
    save_ledger(args.ledger, ledger)
    print(f"Heartbeat updated for {args.task_id}")


def build_parser():
    p = argparse.ArgumentParser(description="Manage long-task ledger state")
    p.add_argument("--ledger", type=Path, default=DEFAULT_LEDGER)
    sp = p.add_subparsers(dest="command", required=True)

    list_p = sp.add_parser("list")
    list_p.set_defaults(func=cmd_list)

    init_p = sp.add_parser("init")
    init_p.add_argument("task_id")
    init_p.add_argument("--goal", required=True)
    init_p.add_argument("--channel", default="unknown")
    init_p.add_argument("--owner", default="agent")
    init_p.add_argument("--workflow", action="append")
    init_p.add_argument("--activation-announced", action="store_true")
    init_p.add_argument("--activation-at")
    init_p.add_argument("--message-ref")
    init_p.add_argument("--summary")
    init_p.add_argument("--fact", action="append")
    init_p.add_argument("--artifact", action="append")
    init_p.add_argument("--note", action="append")
    init_p.add_argument("--next-action", required=True)
    init_p.add_argument("--expected-interval-sec", type=int, default=900)
    init_p.add_argument("--timeout-sec", type=int, default=1800)
    init_p.set_defaults(func=cmd_init)

    cp_p = sp.add_parser("checkpoint")
    cp_p.add_argument("task_id")
    cp_p.add_argument("--kind", default="CHECKPOINT", choices=["STARTED", "CHECKPOINT", "COMPLETED", "BLOCKED"])
    cp_p.add_argument("--summary", required=True)
    cp_p.add_argument("--status")
    cp_p.add_argument("--current-checkpoint")
    cp_p.add_argument("--next-action")
    cp_p.add_argument("--fact", action="append")
    cp_p.add_argument("--artifact", action="append")
    cp_p.set_defaults(func=cmd_checkpoint)

    block_p = sp.add_parser("block")
    block_p.add_argument("task_id")
    block_p.add_argument("--reason", required=True)
    block_p.add_argument("--need", action="append")
    block_p.add_argument("--safe-next-step", required=True)
    block_p.add_argument("--current-checkpoint")
    block_p.add_argument("--next-action")
    block_p.add_argument("--fact", action="append")
    block_p.set_defaults(func=cmd_block)

    hb_p = sp.add_parser("heartbeat")
    hb_p.add_argument("task_id")
    hb_p.add_argument("--watchdog-state")
    hb_p.add_argument("--note")
    hb_p.set_defaults(func=cmd_heartbeat)

    return p


def main():
    parser = build_parser()
    args = parser.parse_args()
    args.func(args)


if __name__ == "__main__":
    main()
