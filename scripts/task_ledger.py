#!/usr/bin/env python3
import argparse
import json
from datetime import datetime, timezone
from pathlib import Path

from reporting_contract import acknowledge_update, ensure_reporting, maybe_queue_checkpoint_update, maybe_queue_external_update

DEFAULT_LEDGER = Path("state/long-task-ledger.example.json")
OWNER_REPLY_CHOICES = {
    "A_IN_PROGRESS_FORGOT_LEDGER",
    "B_BLOCKED",
    "C_COMPLETED",
    "D_NO_REPLY",
    "E_FORGOT_OR_NOT_DOING",
}
OWNER_REPLY_ALIASES = {
    "A": "A_IN_PROGRESS_FORGOT_LEDGER",
    "B": "B_BLOCKED",
    "C": "C_COMPLETED",
    "D": "D_NO_REPLY",
    "E": "E_FORGOT_OR_NOT_DOING",
    "IN_PROGRESS": "A_IN_PROGRESS_FORGOT_LEDGER",
    "FORGOT_LEDGER": "A_IN_PROGRESS_FORGOT_LEDGER",
    "BLOCKED": "B_BLOCKED",
    "COMPLETED": "C_COMPLETED",
    "NO_REPLY": "D_NO_REPLY",
    "FORGOT": "E_FORGOT_OR_NOT_DOING",
    "NOT_DOING": "E_FORGOT_OR_NOT_DOING",
    "RESUME": "E_FORGOT_OR_NOT_DOING",
}
SUPERVISION_ALLOWED_KEYS = {
    "watchdog_state",
    "nudge_count",
    "last_nudge_at",
    "last_escalated_at",
    "last_action_at",
    "last_action_state",
    "last_action_reason",
    "last_action_kind",
    "last_action_payload",
    "cron_state",
    "owner_query_at",
    "owner_response_at",
    "owner_response_kind",
    "reconcile_count",
    "last_reconcile_at",
    "last_resume_request_at",
    "recovery_attempt_count",
    "retry_count",
    "cron_install_error",
    "install_signal",
}
EXTERNAL_JOB_STATES = {
    "SUBMITTED",
    "PENDING",
    "RUNNING",
    "FAILED",
    "RETRYING",
    "SWITCHED_WORKFLOW",
    "COMPLETED",
}
PENDING_EXTERNAL_STATES = {"SUBMITTED", "PENDING", "RUNNING", "RETRYING", "SWITCHED_WORKFLOW"}


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


def normalize_owner_reply(value: str):
    reply = value.strip().upper()
    return OWNER_REPLY_ALIASES.get(reply, reply)


def append_checkpoint(task, *, kind, summary, facts=None, touch_progress=True):
    at = now_iso()
    task.setdefault("checkpoints", []).append({
        "at": at,
        "kind": kind,
        "summary": summary,
        "facts": facts or {},
    })
    task["last_checkpoint_at"] = at
    if touch_progress:
        task.setdefault("heartbeat", {})["last_progress_at"] = at
    return at


def mark_owner_response(task, reply_kind, responded_at):
    monitoring = task.setdefault("monitoring", {})
    monitoring["owner_response_at"] = responded_at
    monitoring["owner_response_kind"] = reply_kind
    return monitoring


def ensure_external_job(task, provider, job_id, workflow=None, app=None):
    task.setdefault("external_jobs", [])
    for job in task["external_jobs"]:
        if job.get("provider") == provider and job.get("job_id") == job_id:
            if workflow:
                job["workflow"] = workflow
            if app:
                job["app"] = app
            return job
    job = {
        "provider": provider,
        "job_id": job_id,
        "status": "SUBMITTED",
        "submitted_at": now_iso(),
        "updated_at": now_iso(),
        "workflow": workflow,
        "app": app,
        "pending_external": True,
        "history": [],
        "failure_count": 0,
        "switch_count": 0,
    }
    task["external_jobs"].append(job)
    return job


def record_external_job_event(task, *, provider, job_id, state, summary, facts=None, workflow=None, app=None, current_checkpoint=None, next_action=None):
    if state not in EXTERNAL_JOB_STATES:
        raise SystemExit(f"Unsupported external job state: {state}")
    job = ensure_external_job(task, provider, job_id, workflow=workflow, app=app)
    at = now_iso()
    previous_status = job.get("status")
    job["status"] = state
    job["updated_at"] = at
    job["pending_external"] = state in PENDING_EXTERNAL_STATES
    if workflow:
        job["workflow"] = workflow
    if app:
        job["app"] = app
    event = {
        "at": at,
        "state": state,
        "summary": summary,
        "facts": facts or {},
    }
    if previous_status:
        event["from_status"] = previous_status
    job.setdefault("history", []).append(event)
    if state == "FAILED":
        job["failure_count"] = int(job.get("failure_count", 0) or 0) + 1
    if state == "SWITCHED_WORKFLOW":
        job["switch_count"] = int(job.get("switch_count", 0) or 0) + 1

    cp_facts = {
        "external_provider": provider,
        "job_id": job_id,
        "external_state": state.lower(),
        "latest_status": state.lower(),
    }
    cp_facts.update(facts or {})
    append_checkpoint(task, kind="CHECKPOINT", summary=summary, facts=cp_facts, touch_progress=state not in {"PENDING", "RUNNING", "RETRYING"})
    task.setdefault("heartbeat", {})["last_heartbeat_at"] = at
    if current_checkpoint:
        task["current_checkpoint"] = current_checkpoint
    if next_action:
        task["next_action"] = next_action
    return job


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

    started_at = now_iso()
    task = {
        "task_id": args.task_id,
        "skill": "long-task-control",
        "goal": args.goal,
        "status": "RUNNING",
        "channel": args.channel,
        "owner": args.owner,
        "created_at": started_at,
        "activation": {
            "announced": args.activation_announced,
            "announced_at": args.activation_at or started_at,
            "message_ref": args.message_ref,
        },
        "workflow": workflow,
        "current_checkpoint": workflow[0]["id"] if workflow else None,
        "checkpoints": [
            {
                "at": started_at,
                "kind": "STARTED",
                "summary": args.summary or "Task initialized",
                "facts": parse_fact(args.fact),
            }
        ],
        "last_checkpoint_at": started_at,
        "heartbeat": {
            "expected_interval_sec": args.expected_interval_sec,
            "timeout_sec": args.timeout_sec,
            "last_progress_at": started_at,
            "last_heartbeat_at": started_at,
            "watchdog_state": "OK",
        },
        "monitoring": {
            "nudge_after_sec": args.nudge_after_sec or args.timeout_sec,
            "renotify_interval_sec": args.renotify_interval_sec or args.expected_interval_sec,
            "max_nudges": args.max_nudges,
            "escalate_after_nudges": args.escalate_after_nudges,
            "nudge_count": 0,
            "last_nudge_at": None,
            "owner_query_at": None,
            "owner_response_at": None,
            "owner_response_kind": None,
            "reconcile_count": 0,
            "last_reconcile_at": None,
            "last_resume_request_at": None,
            "recovery_attempt_count": 0,
            "last_escalated_at": None,
            "blocked_escalate_after_sec": args.blocked_escalate_after_sec or max(args.timeout_sec, args.expected_interval_sec),
            "cron_state": "ACTIVE",
            "retry_count": {},
            "install_signal": "NOT_REQUESTED",
        },
        "validation": [],
        "blocker": None,
        "artifacts": args.artifact or [],
        "external_jobs": [],
        "next_action": args.next_action,
        "notes": args.note or [],
    }
    ensure_reporting(task)
    ledger.setdefault("tasks", []).append(task)
    save_ledger(args.ledger, ledger)
    print(f"Initialized {args.task_id}")


def cmd_checkpoint(args):
    ledger = load_ledger(args.ledger)
    task = ensure_task(ledger, args.task_id)
    entry_facts = parse_fact(args.fact)
    append_checkpoint(task, kind=args.kind, summary=args.summary, facts=entry_facts)
    task["status"] = args.status or task.get("status", "RUNNING")
    if args.current_checkpoint:
        task["current_checkpoint"] = args.current_checkpoint
    if args.next_action:
        task["next_action"] = args.next_action
    if args.artifact:
        task.setdefault("artifacts", []).extend(args.artifact)
    if task.get("status") != "BLOCKED":
        task["blocker"] = None
    maybe_queue_checkpoint_update(
        task,
        kind=args.kind,
        summary=args.summary,
        checkpoint=args.current_checkpoint or task.get("current_checkpoint"),
        facts=entry_facts,
        outputs=args.artifact,
        next_action=args.next_action or task.get("next_action"),
    )
    save_ledger(args.ledger, ledger)
    print(f"Recorded {args.kind} for {args.task_id}")


def cmd_block(args):
    ledger = load_ledger(args.ledger)
    task = ensure_task(ledger, args.task_id)
    task["status"] = "BLOCKED"
    task["blocker"] = {
        "reason": args.reason,
        "need": args.need or [],
        "safe_next_step": args.safe_next_step,
    }
    append_checkpoint(task, kind="BLOCKED", summary=args.reason, facts=parse_fact(args.fact))
    if args.current_checkpoint:
        task["current_checkpoint"] = args.current_checkpoint
    if args.next_action:
        task["next_action"] = args.next_action
    maybe_queue_checkpoint_update(
        task,
        kind="BLOCKED",
        summary=args.reason,
        checkpoint=args.current_checkpoint or task.get("current_checkpoint"),
        facts=parse_fact(args.fact),
        next_action=args.next_action or task.get("next_action"),
        blocker=task.get("blocker"),
    )
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


def cmd_supervisor_update(args):
    ledger = load_ledger(args.ledger)
    task = ensure_task(ledger, args.task_id)
    heartbeat = task.setdefault("heartbeat", {})
    monitoring = task.setdefault("monitoring", {})

    if args.watchdog_state:
        heartbeat["watchdog_state"] = args.watchdog_state
    for item in args.monitoring or []:
        if "=" not in item:
            raise SystemExit(f"Invalid --monitoring value: {item}; expected key=value")
        key, value = item.split("=", 1)
        key = key.strip()
        if key not in SUPERVISION_ALLOWED_KEYS:
            raise SystemExit(f"Disallowed supervision key: {key}. Monitor may update supervision metadata only, not task truth.")
        raw = value.strip()
        monitoring[key] = json.loads(raw) if raw.startswith(("{", "[", '"')) or raw in {"true", "false", "null"} else raw

    save_ledger(args.ledger, ledger)
    print(f"Supervisor metadata updated for {args.task_id}")


def cmd_owner_reply(args):
    ledger = load_ledger(args.ledger)
    task = ensure_task(ledger, args.task_id)
    reply_kind = normalize_owner_reply(args.reply)
    if reply_kind not in OWNER_REPLY_CHOICES:
        raise SystemExit(f"Unknown owner reply kind: {args.reply}")

    facts = parse_fact(args.fact)
    facts["owner_reply_kind"] = reply_kind
    if args.message_ref:
        facts["owner_message_ref"] = args.message_ref

    responded_at = now_iso()
    monitoring = mark_owner_response(task, reply_kind, responded_at)
    task.setdefault("heartbeat", {})["last_heartbeat_at"] = responded_at
    task.setdefault("notes", [])

    queued_update = None

    if reply_kind == "A_IN_PROGRESS_FORGOT_LEDGER":
        task["status"] = "RUNNING"
        task["blocker"] = None
        task.setdefault("heartbeat", {})["watchdog_state"] = "OK"
        monitoring["recovery_attempt_count"] = int(monitoring.get("recovery_attempt_count", 0) or 0) + 1
        summary = args.summary or "Owner confirmed work was in progress but ledger updates were missing"
        append_checkpoint(task, kind="CHECKPOINT", summary=summary, facts=facts)
        if args.current_checkpoint:
            task["current_checkpoint"] = args.current_checkpoint
        if args.next_action:
            task["next_action"] = args.next_action
        queued_update = maybe_queue_checkpoint_update(
            task,
            kind="CHECKPOINT",
            summary=summary,
            checkpoint=args.current_checkpoint or task.get("current_checkpoint"),
            facts=facts,
            next_action=args.next_action or task.get("next_action"),
        )
        task["notes"].append("Owner reconcile A: ledger backfilled and task remains RUNNING")
    elif reply_kind == "B_BLOCKED":
        if not args.reason or not args.safe_next_step:
            raise SystemExit("B_BLOCKED requires --reason and --safe-next-step")
        task["status"] = "BLOCKED"
        task["blocker"] = {
            "reason": args.reason,
            "need": args.need or [],
            "safe_next_step": args.safe_next_step,
        }
        task.setdefault("heartbeat", {})["watchdog_state"] = "BLOCKED_ESCALATE"
        summary = args.summary or f"Owner confirmed blocker: {args.reason}"
        append_checkpoint(task, kind="BLOCKED", summary=summary, facts=facts)
        if args.current_checkpoint:
            task["current_checkpoint"] = args.current_checkpoint
        task["next_action"] = args.next_action or args.safe_next_step
        queued_update = maybe_queue_checkpoint_update(
            task,
            kind="BLOCKED",
            summary=summary,
            checkpoint=args.current_checkpoint or task.get("current_checkpoint"),
            facts=facts,
            next_action=task.get("next_action"),
            blocker=task.get("blocker"),
        )
        task["notes"].append("Owner reconcile B: task truth moved to BLOCKED and is ready for escalation")
    elif reply_kind == "C_COMPLETED":
        task["status"] = "COMPLETED"
        task["blocker"] = None
        task.setdefault("heartbeat", {})["watchdog_state"] = "STOP_AND_DELETE"
        summary = args.summary or "Owner confirmed task was already completed"
        append_checkpoint(task, kind="COMPLETED", summary=summary, facts=facts)
        if args.current_checkpoint:
            task["current_checkpoint"] = args.current_checkpoint
        if args.artifact:
            task.setdefault("artifacts", []).extend(args.artifact)
        if args.validation:
            task.setdefault("validation", []).extend(args.validation)
        elif not task.get("validation"):
            task.setdefault("validation", []).append("owner-confirmed completion; add stronger validation evidence when available")
        task["next_action"] = args.next_action or "None"
        queued_update = maybe_queue_checkpoint_update(
            task,
            kind="COMPLETED",
            summary=summary,
            checkpoint=args.current_checkpoint or task.get("current_checkpoint"),
            facts=facts,
            outputs=args.artifact,
            next_action=task.get("next_action"),
        )
        task["notes"].append("Owner reconcile C: task closed as COMPLETED")
    elif reply_kind == "D_NO_REPLY":
        task.setdefault("heartbeat", {})["watchdog_state"] = "OWNER_RECONCILE"
        monitoring["last_action_state"] = "OWNER_RECONCILE"
        monitoring["recovery_attempt_count"] = int(monitoring.get("recovery_attempt_count", 0) or 0) + 1
        task["notes"].append(args.note or "Owner did not reply; seek external evidence, try safe rebuild/resume, and avoid changing task truth without proof")
        task["next_action"] = args.next_action or task.get("next_action") or "Seek external evidence or rebuild/restart the stuck step safely, then publish a real checkpoint or BLOCKED truth"
    elif reply_kind == "E_FORGOT_OR_NOT_DOING":
        task["status"] = "RUNNING"
        task["blocker"] = None
        task.setdefault("heartbeat", {})["watchdog_state"] = "NUDGE_MAIN_AGENT"
        monitoring["recovery_attempt_count"] = int(monitoring.get("recovery_attempt_count", 0) or 0) + 1
        monitoring["last_resume_request_at"] = responded_at
        summary = args.summary or "Owner admitted the task was forgotten/not being done; resume execution is required now"
        facts.setdefault("resume_required", "true")
        append_checkpoint(task, kind="CHECKPOINT", summary=summary, facts=facts)
        if args.current_checkpoint:
            task["current_checkpoint"] = args.current_checkpoint
        task["next_action"] = args.next_action or "Resume execution immediately and post the next real checkpoint"
        queued_update = maybe_queue_checkpoint_update(
            task,
            kind="CHECKPOINT",
            summary=summary,
            checkpoint=args.current_checkpoint or task.get("current_checkpoint"),
            facts=facts,
            next_action=task.get("next_action"),
        )
        task["notes"].append("Owner reconcile E: task resumed by rule; not just logged")

    save_ledger(args.ledger, ledger)
    print(f"Recorded owner reply {reply_kind} for {args.task_id}")


def cmd_ack_delivery(args):
    ledger = load_ledger(args.ledger)
    task = ensure_task(ledger, args.task_id)
    update = acknowledge_update(task, args.update_id, delivered_via=args.delivered_via, message_ref=args.message_ref, note=args.note)
    save_ledger(args.ledger, ledger)
    print(json.dumps({"ok": True, "task_id": args.task_id, "update": update}, ensure_ascii=False, indent=2))


def cmd_external_job(args):
    ledger = load_ledger(args.ledger)
    task = ensure_task(ledger, args.task_id)
    facts = parse_fact(args.fact)
    if args.failure_type:
        facts.setdefault("failure_type", args.failure_type)
    if args.workflow:
        facts.setdefault("workflow", args.workflow)
    if args.app:
        facts.setdefault("app", args.app)
    job = record_external_job_event(
        task,
        provider=args.provider,
        job_id=args.job_id,
        state=args.state,
        summary=args.summary,
        facts=facts,
        workflow=args.workflow,
        app=args.app,
        current_checkpoint=args.current_checkpoint,
        next_action=args.next_action,
    )
    if args.state == "FAILED":
        monitoring = task.setdefault("monitoring", {})
        current_step = args.current_checkpoint or task.get("current_checkpoint") or "unknown"
        failure_type = args.failure_type or "EXTERNAL_WAIT"
        key = f"{current_step}:{failure_type}"
        counts = monitoring.setdefault("retry_count", {})
        counts[key] = int(counts.get(key, 0) or 0) + 1
    queued_update = maybe_queue_external_update(
        task,
        state=args.state,
        summary=args.summary,
        checkpoint=args.current_checkpoint or task.get("current_checkpoint"),
        facts=facts,
        next_action=args.next_action or task.get("next_action"),
    )
    save_ledger(args.ledger, ledger)
    print(json.dumps({
        "ok": True,
        "task_id": args.task_id,
        "provider": args.provider,
        "job_id": args.job_id,
        "state": args.state,
        "pending_external": job.get("pending_external"),
        "job": job,
    }, ensure_ascii=False, indent=2))


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
    init_p.add_argument("--expected-interval-sec", type=int, default=300)
    init_p.add_argument("--timeout-sec", type=int, default=1800)
    init_p.add_argument("--nudge-after-sec", type=int)
    init_p.add_argument("--renotify-interval-sec", type=int)
    init_p.add_argument("--max-nudges", type=int, default=3)
    init_p.add_argument("--escalate-after-nudges", type=int, default=2)
    init_p.add_argument("--blocked-escalate-after-sec", type=int)
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

    sup_p = sp.add_parser("supervisor-update")
    sup_p.add_argument("task_id")
    sup_p.add_argument("--watchdog-state")
    sup_p.add_argument("--monitoring", action="append", help="Repeatable key=value for supervision metadata only")
    sup_p.set_defaults(func=cmd_supervisor_update)

    owner_p = sp.add_parser("owner-reply")
    owner_p.add_argument("task_id")
    owner_p.add_argument("--reply", required=True, help="A/B/C/D/E or full owner-reconcile branch name")
    owner_p.add_argument("--summary")
    owner_p.add_argument("--reason")
    owner_p.add_argument("--need", action="append")
    owner_p.add_argument("--safe-next-step")
    owner_p.add_argument("--current-checkpoint")
    owner_p.add_argument("--next-action")
    owner_p.add_argument("--artifact", action="append")
    owner_p.add_argument("--validation", action="append")
    owner_p.add_argument("--fact", action="append")
    owner_p.add_argument("--note")
    owner_p.add_argument("--message-ref")
    owner_p.set_defaults(func=cmd_owner_reply)

    ack_p = sp.add_parser("ack-delivery")
    ack_p.add_argument("task_id")
    ack_p.add_argument("update_id")
    ack_p.add_argument("--delivered-via", default="message.send")
    ack_p.add_argument("--message-ref")
    ack_p.add_argument("--note")
    ack_p.set_defaults(func=cmd_ack_delivery)

    ext_p = sp.add_parser("external-job")
    ext_p.add_argument("task_id")
    ext_p.add_argument("--provider", required=True)
    ext_p.add_argument("--job-id", required=True)
    ext_p.add_argument("--state", required=True, choices=sorted(EXTERNAL_JOB_STATES))
    ext_p.add_argument("--summary", required=True)
    ext_p.add_argument("--workflow")
    ext_p.add_argument("--app")
    ext_p.add_argument("--failure-type", choices=["EXTERNAL_WAIT", "EXECUTION_ERROR"])
    ext_p.add_argument("--current-checkpoint")
    ext_p.add_argument("--next-action")
    ext_p.add_argument("--fact", action="append")
    ext_p.set_defaults(func=cmd_external_job)

    return p


def main():
    parser = build_parser()
    args = parser.parse_args()
    args.func(args)


if __name__ == "__main__":
    main()
