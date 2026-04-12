#!/usr/bin/env python3
from __future__ import annotations

from datetime import datetime, timezone
from typing import Any

VISIBLE_EXTERNAL_STATES = {"FAILED", "SWITCHED_WORKFLOW", "COMPLETED"}
VISIBLE_CHECKPOINT_KINDS = {"CHECKPOINT", "BLOCKED", "COMPLETED"}


def now_iso() -> str:
    return datetime.now(timezone.utc).astimezone().isoformat(timespec="seconds")


def ensure_reporting(task: dict[str, Any]) -> dict[str, Any]:
    reporting = task.setdefault("reporting", {})
    reporting.setdefault("delivery_seq", 0)
    reporting.setdefault("pending_updates", [])
    reporting.setdefault("delivered_updates", [])
    reporting.setdefault("required_event_types", [
        "STEP_COMPLETED",
        "EXTERNAL_JOB_COMPLETED",
        "WORKFLOW_SWITCH",
        "BLOCKED_ESCALATE",
        "COMPLETED_HANDOFF",
    ])
    return reporting


def next_update_id(task: dict[str, Any]) -> str:
    reporting = ensure_reporting(task)
    reporting["delivery_seq"] = int(reporting.get("delivery_seq", 0) or 0) + 1
    return f"{task.get('task_id')}:update-{reporting['delivery_seq']:03d}"


def build_status_block(event_type: str, *, task_id: str, summary: str, checkpoint: str | None = None,
                       facts: dict[str, Any] | None = None, next_action: str | None = None,
                       outputs: list[str] | None = None, blocker: dict[str, Any] | None = None) -> str:
    lines = [f"REPORTING HOOK / {event_type}", f"- task_id: {task_id}"]
    if checkpoint:
        lines.append(f"- checkpoint: {checkpoint}")
    lines.append(f"- summary: {summary}")
    if facts:
        lines.append("- verified facts:")
        for key, value in facts.items():
            lines.append(f"  - {key}={value}")
    if outputs:
        lines.append("- artifacts:")
        for item in outputs:
            lines.append(f"  - {item}")
    if blocker:
        lines.append(f"- blocker_reason: {blocker.get('reason')}")
        for item in blocker.get("need") or []:
            lines.append(f"- need: {item}")
        if blocker.get("safe_next_step"):
            lines.append(f"- safe_next_step: {blocker.get('safe_next_step')}")
    if next_action:
        lines.append(f"- next: {next_action}")
    return "\n".join(lines)


def queue_update(task: dict[str, Any], *, event_type: str, summary: str, source_kind: str,
                 checkpoint: str | None = None, facts: dict[str, Any] | None = None,
                 outputs: list[str] | None = None, next_action: str | None = None,
                 blocker: dict[str, Any] | None = None, required: bool = True) -> dict[str, Any]:
    reporting = ensure_reporting(task)
    update = {
        "update_id": next_update_id(task),
        "event_type": event_type,
        "source_kind": source_kind,
        "summary": summary,
        "checkpoint": checkpoint,
        "facts": facts or {},
        "outputs": outputs or [],
        "next_action": next_action,
        "required": required,
        "created_at": now_iso(),
        "delivered": False,
        "status_block": build_status_block(
            event_type,
            task_id=task.get("task_id"),
            summary=summary,
            checkpoint=checkpoint,
            facts=facts,
            next_action=next_action,
            outputs=outputs,
            blocker=blocker,
        ),
    }
    if blocker:
        update["blocker"] = blocker
    reporting["pending_updates"].append(update)
    return update


def maybe_queue_checkpoint_update(task: dict[str, Any], *, kind: str, summary: str, checkpoint: str | None,
                                  facts: dict[str, Any] | None = None, outputs: list[str] | None = None,
                                  next_action: str | None = None, blocker: dict[str, Any] | None = None):
    if kind == "CHECKPOINT" and checkpoint:
        return queue_update(task, event_type="STEP_COMPLETED", source_kind=kind, summary=summary,
                            checkpoint=checkpoint, facts=facts, outputs=outputs, next_action=next_action)
    if kind == "BLOCKED":
        return queue_update(task, event_type="BLOCKED_ESCALATE", source_kind=kind, summary=summary,
                            checkpoint=checkpoint, facts=facts, outputs=outputs, next_action=next_action,
                            blocker=blocker)
    if kind == "COMPLETED":
        return queue_update(task, event_type="COMPLETED_HANDOFF", source_kind=kind, summary=summary,
                            checkpoint=checkpoint, facts=facts, outputs=outputs, next_action=next_action)
    return None


def maybe_queue_external_update(task: dict[str, Any], *, state: str, summary: str, checkpoint: str | None,
                                facts: dict[str, Any] | None = None, next_action: str | None = None):
    if state == "COMPLETED":
        event_type = "EXTERNAL_JOB_COMPLETED"
    elif state == "SWITCHED_WORKFLOW":
        event_type = "WORKFLOW_SWITCH"
    elif state == "FAILED":
        event_type = "EXTERNAL_JOB_FAILED"
    else:
        return None
    return queue_update(task, event_type=event_type, source_kind=f"EXTERNAL_JOB:{state}", summary=summary,
                        checkpoint=checkpoint, facts=facts, next_action=next_action)


def acknowledge_update(task: dict[str, Any], update_id: str, *, delivered_via: str | None = None,
                       message_ref: str | None = None, note: str | None = None) -> dict[str, Any]:
    reporting = ensure_reporting(task)
    for idx, update in enumerate(reporting["pending_updates"]):
        if update.get("update_id") == update_id:
            update["delivered"] = True
            update["delivered_at"] = now_iso()
            if delivered_via:
                update["delivered_via"] = delivered_via
            if message_ref:
                update["message_ref"] = message_ref
            if note:
                update["delivery_note"] = note
            reporting["delivered_updates"].append(update)
            del reporting["pending_updates"][idx]
            return update
    raise SystemExit(f"Pending update not found: {update_id}")
