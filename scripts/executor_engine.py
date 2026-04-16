#!/usr/bin/env python3
from __future__ import annotations

import argparse
import json

from job_models import ArtifactRecord, FailureRecord, JobStore, now_iso
from adapters.generic_manual import GenericManualAdapter
from adapters.runninghub_matrix import RunningHubMatrixAdapter
from execution_bridge import ExecutionBridge


ADAPTERS = {
    "generic_manual": GenericManualAdapter,
    "runninghub_matrix": RunningHubMatrixAdapter,
}


def load_adapter(name: str):
    if name not in ADAPTERS:
        raise SystemExit(f"Unknown adapter: {name}")
    return ADAPTERS[name]()


def normalize_resumable_item(state, adapter):
    item = state.next_runnable()
    if not item:
        return None
    if item.status == "RUNNING":
        if adapter.can_resume({"item": item.payload, "status": item.status, "attempts": item.attempts}):
            item.resume_count += 1
            item.status = "RETRY"
            item.execution_owner = None
            item.execution_claimed_at = None
        else:
            raise SystemExit(f"Item {item.item_id} is RUNNING but adapter cannot resume it")
    return item


def cmd_preview(args):
    store = JobStore(args.jobs_root)
    state = store.load(args.job_id)
    adapter = load_adapter(state.adapter)
    item = normalize_resumable_item(state, adapter)
    if item:
        store.save(state)
    if not item:
        print(json.dumps({"action": "workflow_complete", "job_id": state.job_id}, ensure_ascii=False))
        return
    print(json.dumps({
        "action": "execute_item",
        "job_id": state.job_id,
        "item_id": item.item_id,
        "title": item.title,
        "adapter": state.adapter,
        "attempts": item.attempts,
        "resume_count": item.resume_count,
        "checkpoint": state.checkpoint_for_item(item),
    }, ensure_ascii=False))


def cmd_run_next(args):
    store = JobStore(args.jobs_root)
    state = store.load(args.job_id)
    adapter = load_adapter(state.adapter)
    bridge = ExecutionBridge(state.bridge)
    item = normalize_resumable_item(state, adapter)
    if not item:
        state.status = "COMPLETED"
        state.execution["last_terminal_at"] = now_iso()
        store.save(state)
        store.append_progress(state.job_id, {"kind": "JOB_COMPLETED", "summary": "No pending items remain"})
        bridge.sync_task_completed(state)
        print(json.dumps({"ok": True, "status": "COMPLETED", "job_id": state.job_id}, ensure_ascii=False))
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
        item.status = "BLOCKED"
        item.blocked_reason = submit_result.blocked_reason or submit_result.summary
        item.execution_owner = None
        item.execution_claimed_at = None
        state.status = "BLOCKED"
        state.blocked_reason = item.blocked_reason
        state.execution["last_terminal_at"] = now_iso()
        if submit_result.facts:
            item.failures.append(FailureRecord(code="BLOCKED", summary=submit_result.summary, retryable=False, facts=submit_result.facts))
        store.save(state)
        store.append_progress(state.job_id, {"kind": "ITEM_BLOCKED", "item_id": item.item_id, "summary": submit_result.summary, "facts": submit_result.facts})
        bridge.sync_item_blocked(state, item, item_index, summary=submit_result.summary, reason=item.blocked_reason, facts=submit_result.facts)
        print(json.dumps({"ok": False, "status": "BLOCKED", "job_id": state.job_id, "item_id": item.item_id, "summary": submit_result.summary}, ensure_ascii=False))
        return

    if submit_result.status not in {"submitted", "completed", "observed", "collected"}:
        item.status = "FAILED"
        item.execution_owner = None
        item.execution_claimed_at = None
        state.failed.append(item.item_id)
        item.failures.append(FailureRecord(code=submit_result.status.upper(), summary=submit_result.summary, retryable=False, facts=submit_result.facts))
        store.save(state)
        store.append_progress(state.job_id, {"kind": "ITEM_FAILED", "item_id": item.item_id, "summary": submit_result.summary, "facts": submit_result.facts})
        print(json.dumps({"ok": False, "status": "FAILED", "job_id": state.job_id, "item_id": item.item_id, "summary": submit_result.summary}, ensure_ascii=False))
        return

    finalize_result = adapter.finalize(prepared, {**adapter_context, "submit": submit_result.facts})
    if finalize_result.status in {"blocked", "BLOCKED"}:
        item.status = "BLOCKED"
        item.blocked_reason = finalize_result.blocked_reason or finalize_result.summary
        item.execution_owner = None
        item.execution_claimed_at = None
        state.status = "BLOCKED"
        state.blocked_reason = item.blocked_reason
        state.execution["last_terminal_at"] = now_iso()
        if finalize_result.facts:
            item.failures.append(FailureRecord(code="BLOCKED_FINALIZE", summary=finalize_result.summary, retryable=False, facts=finalize_result.facts))
        store.save(state)
        store.append_progress(state.job_id, {"kind": "ITEM_BLOCKED", "item_id": item.item_id, "summary": finalize_result.summary, "facts": finalize_result.facts, "phase": "finalize"})
        bridge.sync_item_blocked(state, item, item_index, summary=finalize_result.summary, reason=item.blocked_reason, facts=finalize_result.facts)
        print(json.dumps({"ok": False, "status": "BLOCKED", "job_id": state.job_id, "item_id": item.item_id, "summary": finalize_result.summary, "phase": "finalize"}, ensure_ascii=False))
        return

    if finalize_result.status not in {"completed", "COMPLETED"}:
        item.status = "FAILED"
        item.execution_owner = None
        item.execution_claimed_at = None
        state.failed.append(item.item_id)
        item.failures.append(FailureRecord(code=f"FINALIZE_{finalize_result.status.upper()}", summary=finalize_result.summary, retryable=False, facts=finalize_result.facts))
        store.save(state)
        store.append_progress(state.job_id, {"kind": "ITEM_FAILED", "item_id": item.item_id, "summary": finalize_result.summary, "facts": finalize_result.facts, "phase": "finalize"})
        print(json.dumps({"ok": False, "status": "FAILED", "job_id": state.job_id, "item_id": item.item_id, "summary": finalize_result.summary, "phase": "finalize"}, ensure_ascii=False))
        return

    item.status = "DONE"
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
