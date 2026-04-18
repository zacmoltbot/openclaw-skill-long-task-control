#!/usr/bin/env python3
from __future__ import annotations

import argparse
import json
from pathlib import Path

from job_models import ArtifactRecord, FailureRecord, JobStore, WorkItem, now_iso
from adapters.generic_manual import GenericManualAdapter
from adapters.runninghub_matrix import RunningHubMatrixAdapter
from execution_bridge import ExecutionBridge


ADAPTERS = {
    "generic_manual": GenericManualAdapter,
    "runninghub_matrix": RunningHubMatrixAdapter,
}

RETRYABLE_FAILURE_CODES = {
    "TIMEOUT",
    "FINALIZE_TIMEOUT",
    "TRANSIENT_NETWORK",
    "EXECUTION_ERROR",
    "EXECUTION_INTERRUPTED",
    "EXTERNAL_WAIT",
}


def _safe_excerpt(value: str | None, limit: int = 240) -> str | None:
    if value is None:
        return None
    text = " ".join(str(value).split())
    return text[:limit] if text else None


def record_executor_observation(bridge: ExecutionBridge, state, *, item=None, phase: str, status: str, summary: str | None = None, facts: dict | None = None):
    if not bridge.enabled or not bridge.ledger or not bridge.task_id or not bridge.ledger.exists():
        return
    payload = json.loads(bridge.ledger.read_text())
    task = next((candidate for candidate in payload.get("tasks", []) if candidate.get("task_id") == bridge.task_id), None)
    if task is None:
        return
    monitoring = task.setdefault("monitoring", {})
    health = monitoring.setdefault("executor_health", {})
    now = now_iso()
    item_id = getattr(item, "item_id", None)
    checkpoint = bridge.checkpoint_for(item, state.current_index) if item is not None else None
    event = {
        "at": now,
        "phase": phase,
        "status": status,
        "job_id": state.job_id,
        "job_status": state.status,
        "item_id": item_id,
        "checkpoint": checkpoint,
        "summary": _safe_excerpt(summary),
        "facts": facts or {},
    }
    history = monitoring.setdefault("executor_history", [])
    history.append(event)
    if len(history) > 20:
        del history[:-20]
    monitoring["executor_last_event"] = event
    monitoring["executor_last_event_at"] = now
    monitoring["executor_last_phase"] = phase
    monitoring["executor_last_status"] = status
    monitoring["executor_last_job_status"] = state.status
    monitoring["executor_last_item_id"] = item_id
    monitoring["executor_last_checkpoint"] = checkpoint
    if summary:
        monitoring["executor_last_summary"] = _safe_excerpt(summary)
    if status in {"RUNNING", "DONE", "COMPLETED"}:
        monitoring["executor_state"] = "RUNNING" if status == "RUNNING" else "OK"
        monitoring["executor_consecutive_errors"] = 0
        monitoring["executor_consecutive_timeouts"] = 0
        health["state"] = monitoring["executor_state"]
        health["last_success_at"] = now
        health["consecutive_errors"] = 0
        health["consecutive_timeouts"] = 0
    elif status in {"RETRY", "FAILED", "BLOCKED", "INTERRUPTED", "TIMEOUT"}:
        monitoring["executor_state"] = "TIMEOUT" if status == "TIMEOUT" else "ERROR"
        monitoring["executor_consecutive_errors"] = int(monitoring.get("executor_consecutive_errors", 0) or 0) + 1
        if status == "TIMEOUT":
            monitoring["executor_consecutive_timeouts"] = int(monitoring.get("executor_consecutive_timeouts", 0) or 0) + 1
        health["state"] = monitoring["executor_state"]
        health["consecutive_errors"] = monitoring["executor_consecutive_errors"]
        health["consecutive_timeouts"] = int(monitoring.get("executor_consecutive_timeouts", 0) or 0)
    bridge.ledger.write_text(json.dumps(payload, ensure_ascii=False, indent=2) + "\n")


def load_adapter(name: str):
    if name not in ADAPTERS:
        raise SystemExit(f"Unknown adapter: {name}")
    return ADAPTERS[name]()


def expected_artifacts(item: WorkItem) -> list[str]:
    payload = item.payload or {}
    values: list[str] = []
    for key in ("expect_artifacts", "artifacts", "outputs"):
        raw = payload.get(key)
        if isinstance(raw, list):
            values.extend(str(v) for v in raw if str(v).strip())
    for key in ("artifact", "output"):
        raw = payload.get(key)
        if raw:
            values.append(str(raw))
    deduped: list[str] = []
    seen: set[str] = set()
    for value in values:
        if value not in seen:
            deduped.append(value)
            seen.add(value)
    return deduped


def retry_budget(item: WorkItem) -> int:
    raw = item.payload.get("retry_budget")
    if raw is None:
        return 1
    try:
        return max(0, int(raw))
    except Exception:
        return 1


def attempts_allowed(item: WorkItem) -> int:
    return 1 + retry_budget(item)


def last_failure(item: WorkItem) -> FailureRecord | None:
    return item.failures[-1] if item.failures else None


def is_retryable_failure(item: WorkItem, adapter) -> bool:
    failure = last_failure(item)
    if not failure:
        return False
    if failure.retryable:
        return True
    if str(failure.code or "").upper() in RETRYABLE_FAILURE_CODES:
        return True
    suggestion = getattr(adapter, "suggest_retry", lambda *_: {"retryable": False})({
        "item": item.payload,
        "status": item.status,
        "attempts": item.attempts,
        "failures": [f.code for f in item.failures],
    }) or {}
    return bool(suggestion.get("retryable"))


def reconcile_from_artifacts(state, item: WorkItem, item_index: int, bridge: ExecutionBridge, store: JobStore, *, reason: str) -> bool:
    paths = [p for p in expected_artifacts(item) if Path(p).expanduser().exists()]
    if not paths:
        return False
    item.status = "DONE"
    item.blocked_reason = None
    item.execution_owner = None
    item.execution_claimed_at = None
    item.completed_at = item.completed_at or now_iso()
    for p in paths:
        resolved = str(Path(p).expanduser())
        if not any(existing.path == resolved for existing in item.artifacts):
            rec = ArtifactRecord(path=resolved)
            item.artifacts.append(rec)
        if not any(existing.path == resolved for existing in state.artifacts):
            state.artifacts.append(ArtifactRecord(path=resolved))
    if item.item_id not in state.completed:
        state.completed.append(item.item_id)
    if state.current_index <= item_index and item_index < len(state.items) - 1:
        state.current_index = item_index + 1
        state.status = "RUNNING"
        state.blocked_reason = None
    elif item_index >= len(state.items) - 1 and not state.failed:
        state.status = "COMPLETED"
        state.blocked_reason = None
        state.execution["last_terminal_at"] = now_iso()
    store.save(state)
    store.append_progress(state.job_id, {
        "kind": "ITEM_RECONCILED",
        "item_id": item.item_id,
        "summary": reason,
        "artifacts": paths,
    })
    bridge.sync_item_completed(state, item, item_index, summary=reason, artifacts=paths, facts={"reconciled_from_artifacts": True})
    if state.status == "COMPLETED":
        bridge.sync_task_completed(state)
    return True


def _emit_item_blocked(state, item, idx, store, bridge, *, reason: str, summary: str | None = None):
    """Emit ITEM_BLOCKED progress + bridge sync for a terminal blocked item.

    Called when normalize_resumable_item transitions an item to BLOCKED
    outside the normal handle_failed_item path (e.g. retry budget exhausted
    on a RUNNING item, or adapter cannot resume).
    """
    item_index = idx
    store.append_progress(state.job_id, {
        "kind": "ITEM_BLOCKED",
        "item_id": item.item_id,
        "summary": summary or f"Item blocked: {reason}",
        "reason": reason,
        "attempt": item.attempts,
        "retry_budget": retry_budget(item),
    })
    bridge.sync_item_blocked(
        state, item, item_index,
        summary=summary or f"Item blocked after retry budget exhausted: {item.title}",
        reason=reason,
        facts={"attempt": item.attempts, "retry_budget": retry_budget(item), "source": "normalize_resumable_item"},
    )
    record_executor_observation(
        bridge, state, item=item,
        phase="item_terminal",
        status="BLOCKED",
        summary=summary or f"Item blocked: {reason}",
        facts={"reason": reason, "attempt": item.attempts, "retry_budget": retry_budget(item), "source": "normalize_resumable_item"},
    )


def normalize_resumable_item(state, adapter, bridge: ExecutionBridge, store: JobStore):
    for idx, item in enumerate(state.items):
        if item.status == "DONE":
            continue
        state.current_index = idx
        if item.status == "RUNNING":
            if reconcile_from_artifacts(state, item, idx, bridge, store, reason=f"Recovered interrupted item from observed artifacts: {item.title}"):
                return normalize_resumable_item(state, adapter, bridge, store)
            if adapter.can_resume({"item": item.payload, "status": item.status, "attempts": item.attempts}):
                if item.attempts < attempts_allowed(item):
                    item.resume_count += 1
                    item.status = "RETRY"
                    item.execution_owner = None
                    item.execution_claimed_at = None
                    state.status = "RUNNING"
                    state.blocked_reason = None
                    store.save(state)
                    return item
                # Exhausted retry budget on a RUNNING item — emit terminal blocked truth
                reason = item.blocked_reason or "RETRY_BUDGET_EXHAUSTED"
                item.status = "BLOCKED"
                item.blocked_reason = reason
                state.status = "BLOCKED"
                state.blocked_reason = reason
                state.execution["last_terminal_at"] = now_iso()
                store.save(state)
                _emit_item_blocked(state, item, idx, store, bridge, reason=reason, summary=f"Retry budget exhausted: {item.title}")
                return None
            # Adapter cannot resume a RUNNING item — terminal blocked (not recoverable)
            reason = item.blocked_reason or f"Item {item.item_id} is RUNNING but adapter cannot resume it"
            state.status = "BLOCKED"
            state.blocked_reason = reason
            state.execution["last_terminal_at"] = now_iso()
            store.save(state)
            _emit_item_blocked(state, item, idx, store, bridge, reason=reason, summary=f"Cannot resume: {item.title}")
            return None
        if item.status in {"BLOCKED", "FAILED"}:
            if reconcile_from_artifacts(state, item, idx, bridge, store, reason=f"Reconciled previously interrupted item from observed artifacts: {item.title}"):
                return normalize_resumable_item(state, adapter, bridge, store)
            if is_retryable_failure(item, adapter) and item.attempts < attempts_allowed(item):
                item.resume_count += 1
                item.status = "RETRY"
                item.blocked_reason = None
                item.execution_owner = None
                item.execution_claimed_at = None
                state.status = "RUNNING"
                state.blocked_reason = None
                state.execution["last_resume_at"] = now_iso()
                state.execution["last_resume_reason"] = "retryable_failure"
                store.save(state)
                return item
            state.status = "BLOCKED" if item.status == "BLOCKED" else "FAILED"
            state.blocked_reason = item.blocked_reason or (last_failure(item).summary if last_failure(item) else f"{item.status} item requires reconcile")
            state.execution["last_terminal_at"] = now_iso()
            store.save(state)
            return None
        if item.status in {"PENDING", "RETRY"}:
            state.status = "RUNNING"
            state.blocked_reason = None
            return item
    return None


def classify_failure(result, *, phase: str):
    status = str(result.status or "FAILED").upper()
    facts = dict(result.facts or {})
    observed_failure_type = str(facts.get("failure_type") or "").upper()
    if not observed_failure_type:
        observed_failure_type = "TIMEOUT" if "timeout" in str(result.summary or "").lower() else "EXECUTION_ERROR"
    code = f"{phase}_{status}" if phase != "submit" else status
    retryable = bool(
        facts.get("retryable") in {True, "true", "1", 1}
        or observed_failure_type in RETRYABLE_FAILURE_CODES
        or status in RETRYABLE_FAILURE_CODES
        or "timeout" in str(result.summary or "").lower()
    )
    facts["failure_type"] = observed_failure_type
    return FailureRecord(code=code, summary=result.summary, retryable=retryable, facts=facts)


def handle_failed_item(*, state, item, item_index, store, bridge, failure: FailureRecord, summary: str):
    item.failures.append(failure)
    item.execution_owner = None
    item.execution_claimed_at = None
    exhausted = item.attempts >= attempts_allowed(item)
    if failure.retryable and not exhausted:
        item.status = "RETRY"
        state.status = "RUNNING"
        state.blocked_reason = None
        store.save(state)
        store.append_progress(state.job_id, {"kind": "ITEM_RETRY_SCHEDULED", "item_id": item.item_id, "summary": summary, "attempt": item.attempts, "retry_budget": retry_budget(item)})
        record_executor_observation(bridge, state, item=item, phase="retry_scheduled", status="RETRY", summary=summary, facts={**failure.facts, "attempt": item.attempts, "retry_budget": retry_budget(item)})
        print(json.dumps({"ok": False, "status": "RETRY", "job_id": state.job_id, "item_id": item.item_id, "summary": summary, "attempt": item.attempts, "retry_budget": retry_budget(item)}, ensure_ascii=False))
        return
    item.status = "BLOCKED" if failure.retryable else "FAILED"
    item.blocked_reason = "RETRY_BUDGET_EXHAUSTED" if failure.retryable and exhausted else failure.summary
    if item.status == "FAILED" and item.item_id not in state.failed:
        state.failed.append(item.item_id)
    state.status = item.status
    state.blocked_reason = item.blocked_reason
    state.execution["last_terminal_at"] = now_iso()
    store.save(state)
    store.append_progress(state.job_id, {"kind": "ITEM_BLOCKED" if item.status == "BLOCKED" else "ITEM_FAILED", "item_id": item.item_id, "summary": summary, "facts": failure.facts, "attempt": item.attempts, "retry_budget": retry_budget(item)})
    bridge.sync_item_blocked(state, item, item_index, summary=summary, reason=item.blocked_reason, facts={**failure.facts, "retry_budget": retry_budget(item), "attempts": item.attempts})
    record_executor_observation(bridge, state, item=item, phase="item_terminal", status=item.status, summary=summary, facts={**failure.facts, "attempt": item.attempts, "retry_budget": retry_budget(item)})
    print(json.dumps({"ok": False, "status": item.status, "job_id": state.job_id, "item_id": item.item_id, "summary": summary, "attempt": item.attempts, "retry_budget": retry_budget(item)}, ensure_ascii=False))


def cmd_preview(args):
    store = JobStore(args.jobs_root)
    state = store.load(args.job_id)
    adapter = load_adapter(state.adapter)
    bridge = ExecutionBridge(state.bridge)
    item = normalize_resumable_item(state, adapter, bridge, store)
    if not item:
        action = "workflow_complete" if state.status == "COMPLETED" else "blocked"
        print(json.dumps({"action": action, "job_id": state.job_id, "job_status": state.status, "blocked_reason": state.blocked_reason}, ensure_ascii=False))
        return
    print(json.dumps({
        "action": "execute_item",
        "job_id": state.job_id,
        "item_id": item.item_id,
        "title": item.title,
        "adapter": state.adapter,
        "attempts": item.attempts,
        "resume_count": item.resume_count,
        "retry_budget": retry_budget(item),
        "checkpoint": state.checkpoint_for_item(item),
    }, ensure_ascii=False))


def cmd_run_next(args):
    store = JobStore(args.jobs_root)
    state = store.load(args.job_id)
    adapter = load_adapter(state.adapter)
    bridge = ExecutionBridge(state.bridge)
    item = normalize_resumable_item(state, adapter, bridge, store)
    if not item:
        if state.status == "COMPLETED":
            state.execution["last_terminal_at"] = now_iso()
            store.save(state)
            store.append_progress(state.job_id, {"kind": "JOB_COMPLETED", "summary": "No pending items remain"})
            bridge.sync_task_completed(state)
            record_executor_observation(bridge, state, phase="job_terminal", status="COMPLETED", summary="No pending items remain")
            print(json.dumps({"ok": True, "status": "COMPLETED", "job_id": state.job_id}, ensure_ascii=False))
            return
        record_executor_observation(bridge, state, phase="job_terminal", status=state.status, summary=state.blocked_reason or f"job is {state.status}")
        print(json.dumps({"ok": False, "status": state.status, "job_id": state.job_id, "blocked_reason": state.blocked_reason}, ensure_ascii=False))
        return

    item_index = state.current_index
    item.attempts += 1
    item.started_at = item.started_at or now_iso()
    item.status = "RUNNING"
    item.execution_owner = args.execution_owner
    item.execution_claimed_at = now_iso()
    state.status = "RUNNING"
    state.execution["last_run_owner"] = args.execution_owner
    state.execution["last_run_started_at"] = now_iso()
    state.execution["last_item_id"] = item.item_id
    store.save(state)
    store.append_progress(state.job_id, {
        "kind": "ITEM_STARTED",
        "item_id": item.item_id,
        "title": item.title,
        "attempt": item.attempts,
        "resume_count": item.resume_count,
        "execution_owner": args.execution_owner,
    })
    record_executor_observation(bridge, state, item=item, phase="item_started", status="RUNNING", summary=f"Executor started {item.title}", facts={"attempt": item.attempts, "resume_count": item.resume_count, "execution_owner": args.execution_owner})
    bridge.sync_item_started(state, item, item_index)

    adapter_context = {
        "job": state.job_id,
        "item_id": item.item_id,
        "bridge": state.bridge,
        "job_state": state,
        "item_state": item,
    }
    prepared = adapter.prepare(item.payload, adapter_context)
    submit_result = adapter.submit(prepared, {**adapter_context, "prepared": prepared})

    if submit_result.status in {"blocked", "BLOCKED"}:
        failure = FailureRecord(code="BLOCKED", summary=submit_result.summary, retryable=False, facts=submit_result.facts or {})
        item.failures.append(failure)
        item.status = "BLOCKED"
        item.blocked_reason = submit_result.blocked_reason or submit_result.summary
        item.execution_owner = None
        item.execution_claimed_at = None
        state.status = "BLOCKED"
        state.blocked_reason = item.blocked_reason
        state.execution["last_terminal_at"] = now_iso()
        store.save(state)
        store.append_progress(state.job_id, {"kind": "ITEM_BLOCKED", "item_id": item.item_id, "summary": submit_result.summary, "facts": submit_result.facts})
        bridge.sync_item_blocked(state, item, item_index, summary=submit_result.summary, reason=item.blocked_reason, facts=submit_result.facts)
        record_executor_observation(bridge, state, item=item, phase="submit", status="BLOCKED", summary=submit_result.summary, facts=submit_result.facts)
        print(json.dumps({"ok": False, "status": "BLOCKED", "job_id": state.job_id, "item_id": item.item_id, "summary": submit_result.summary}, ensure_ascii=False))
        return

    if submit_result.status not in {"submitted", "completed", "observed", "collected"}:
        failure = classify_failure(submit_result, phase="submit")
        handle_failed_item(state=state, item=item, item_index=item_index, store=store, bridge=bridge, failure=failure, summary=submit_result.summary)
        return

    try:
        finalize_result = adapter.finalize(prepared, {**adapter_context, "submit": submit_result.facts})
    except Exception as exc:
        # Any adapter exception during finalize must NOT leave the step in phantom RUNNING.
        # Route through handle_failed_item so retry/budget logic is applied properly.
        failure = FailureRecord(
            code="FINALIZE_EXCEPTION",
            summary=f"finalize raised unhandled exception: {exc}",
            retryable=True,
            facts={
                "exception_type": type(exc).__name__,
                "exception_msg": str(exc),
                "phase": "finalize",
            },
        )
        handle_failed_item(state=state, item=item, item_index=item_index, store=store, bridge=bridge, failure=failure, summary=failure.summary)
        return

    if finalize_result.status in {"blocked", "BLOCKED"}:
        failure = FailureRecord(code="BLOCKED_FINALIZE", summary=finalize_result.summary, retryable=False, facts=finalize_result.facts or {})
        item.failures.append(failure)
        item.status = "BLOCKED"
        item.blocked_reason = finalize_result.blocked_reason or finalize_result.summary
        item.execution_owner = None
        item.execution_claimed_at = None
        state.status = "BLOCKED"
        state.blocked_reason = item.blocked_reason
        state.execution["last_terminal_at"] = now_iso()
        store.save(state)
        store.append_progress(state.job_id, {"kind": "ITEM_BLOCKED", "item_id": item.item_id, "summary": finalize_result.summary, "facts": finalize_result.facts, "phase": "finalize"})
        bridge.sync_item_blocked(state, item, item_index, summary=finalize_result.summary, reason=item.blocked_reason, facts=finalize_result.facts)
        record_executor_observation(bridge, state, item=item, phase="finalize", status="BLOCKED", summary=finalize_result.summary, facts=finalize_result.facts)
        print(json.dumps({"ok": False, "status": "BLOCKED", "job_id": state.job_id, "item_id": item.item_id, "summary": finalize_result.summary, "phase": "finalize"}, ensure_ascii=False))
        return

    if finalize_result.status not in {"completed", "COMPLETED"}:
        failure = classify_failure(finalize_result, phase="finalize")
        handle_failed_item(state=state, item=item, item_index=item_index, store=store, bridge=bridge, failure=failure, summary=finalize_result.summary)
        return

    item.status = "DONE"
    item.blocked_reason = None
    item.execution_owner = None
    item.execution_claimed_at = None
    item.completed_at = now_iso()
    item.next_action = finalize_result.next_action
    if finalize_result.artifacts:
        for p in finalize_result.artifacts:
            rec = ArtifactRecord(path=p)
            item.artifacts.append(rec)
            state.artifacts.append(rec)
    if item.item_id not in state.completed:
        state.completed.append(item.item_id)
    if state.current_index < len(state.items) - 1:
        state.current_index += 1
        state.status = "RUNNING"
    else:
        state.status = "BLOCKED" if state.failed else "COMPLETED"
        if state.failed:
            state.blocked_reason = f"Execution finished with failed items: {', '.join(state.failed)}"
        state.execution["last_terminal_at"] = now_iso()
    store.save(state)
    store.append_progress(state.job_id, {"kind": "ITEM_COMPLETED", "item_id": item.item_id, "summary": finalize_result.summary, "artifacts": [a.path for a in item.artifacts]})
    bridge.sync_item_completed(state, item, item_index, summary=finalize_result.summary, artifacts=[a.path for a in item.artifacts], facts=finalize_result.facts)
    if state.status in {"COMPLETED", "BLOCKED"}:
        bridge.sync_task_completed(state)
    record_executor_observation(bridge, state, item=item, phase="item_completed", status="DONE", summary=finalize_result.summary, facts={**(finalize_result.facts or {}), "artifacts": [a.path for a in item.artifacts]})
    print(json.dumps({
        "ok": True,
        "status": item.status,
        "job_id": state.job_id,
        "item_id": item.item_id,
        "summary": finalize_result.summary,
        "artifacts": [a.path for a in item.artifacts],
        "job_status": state.status,
        "failed_items": state.failed,
    }, ensure_ascii=False))


def build_parser():
    p = argparse.ArgumentParser(description="Execution-plane single-item executor")
    p.add_argument("--jobs-root", default="state/jobs")
    sp = p.add_subparsers(dest="command", required=True)
    preview = sp.add_parser("preview")
    preview.add_argument("job_id")
    preview.set_defaults(func=cmd_preview)
    run_next = sp.add_parser("run-next")
    run_next.add_argument("job_id")
    run_next.add_argument("--execution-owner", default="executor_engine")
    run_next.set_defaults(func=cmd_run_next)
    return p


if __name__ == "__main__":
    args = build_parser().parse_args()
    args.func(args)
