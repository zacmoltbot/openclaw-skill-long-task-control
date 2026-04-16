#!/usr/bin/env python3
from __future__ import annotations

import argparse
import contextlib
import io
import json
import signal
from pathlib import Path

from job_models import FailureRecord, JobState, JobStore, WorkItem, now_iso


def cmd_init_job(args):
    store = JobStore(args.jobs_root)
    spec = json.loads(Path(args.spec).read_text())
    items = [
        WorkItem(
            item_id=item.get("item_id") or f"item-{idx:03d}",
            title=item.get("title") or item.get("item_id") or f"item-{idx:03d}",
            payload=item,
        )
        for idx, item in enumerate(spec.get("items", []), start=1)
    ]
    bridge = spec.get("bridge", {}) or {}
    if args.ledger:
        bridge["ledger"] = args.ledger
    if args.task_id:
        bridge["task_id"] = args.task_id
    state = JobState(
        job_id=spec["job_id"],
        kind=spec.get("kind", "generic"),
        adapter=spec.get("adapter", "generic_manual"),
        mode=spec.get("mode", "serial"),
        status="RUNNING",
        items=items,
        bridge=bridge,
        execution={
            "resume_supported": True,
            "lock_mode": "single-writer",
            "last_run_owner": None,
            "last_run_started_at": None,
            "last_run_finished_at": None,
            "last_resume_at": None,
            "last_resume_reason": None,
        },
    )
    store.save(state)
    store.append_progress(state.job_id, {"kind": "JOB_CREATED", "item_count": len(items), "adapter": state.adapter, "bridge_enabled": bool(bridge.get("ledger") and bridge.get("task_id"))})
    print(json.dumps({"ok": True, "job_id": state.job_id, "item_count": len(items), "adapter": state.adapter, "bridge": bridge}, ensure_ascii=False))


def cmd_status(args):
    store = JobStore(args.jobs_root)
    state = store.load(args.job_id)
    print(json.dumps({
        "job_id": state.job_id,
        "status": state.status,
        "adapter": state.adapter,
        "mode": state.mode,
        "current_index": state.current_index,
        "completed": state.completed,
        "failed": state.failed,
        "blocked_reason": state.blocked_reason,
        "remaining": [i.item_id for i in state.items if i.status in {"PENDING", "RETRY", "RUNNING"}],
        "bridge": state.bridge,
        "execution": state.execution,
    }, ensure_ascii=False, indent=2))


class ExecutionInterrupted(SystemExit):
    def __init__(self, signal_name: str):
        super().__init__(128)
        self.signal_name = signal_name


def _install_signal_handlers():
    previous: dict[int, any] = {}

    def handler(signum, frame):
        raise ExecutionInterrupted(signal.Signals(signum).name)

    for signum in (signal.SIGTERM, signal.SIGINT):
        previous[signum] = signal.getsignal(signum)
        signal.signal(signum, handler)
    return previous


def _restore_signal_handlers(previous):
    for signum, handler in previous.items():
        signal.signal(signum, handler)


def _mark_interrupted_truth(store: JobStore, state: JobState, *, owner: str, signal_name: str) -> JobState:
    from execution_bridge import ExecutionBridge

    bridge = ExecutionBridge(state.bridge)
    item = state.next_runnable()
    if item and item.status == "RUNNING":
        item.status = "BLOCKED"
        item.blocked_reason = "EXECUTION_INTERRUPTED"
        item.execution_owner = None
        item.execution_claimed_at = None
        item.failures.append(FailureRecord(
            code="EXECUTION_INTERRUPTED",
            summary=f"Canonical execution interrupted externally ({signal_name})",
            retryable=True,
            facts={"interrupted_signal": signal_name, "execution_owner": owner},
        ))
    state.status = "BLOCKED"
    state.blocked_reason = "EXECUTION_INTERRUPTED"
    state.execution["last_run_finished_at"] = now_iso()
    state.execution["last_terminal_at"] = now_iso()
    state.execution["last_interrupted_at"] = now_iso()
    state.execution["last_interrupted_signal"] = signal_name
    state.execution["last_interrupted_owner"] = owner
    store.save(state)
    store.append_progress(state.job_id, {
        "kind": "JOB_INTERRUPTED",
        "signal": signal_name,
        "execution_owner": owner,
        "item_id": getattr(item, "item_id", None),
        "checkpoint": state.checkpoint_for_item(item) if item else None,
    })
    bridge.sync_interrupted(
        state,
        item,
        state.current_index if item else None,
        signal_name=signal_name,
        summary=f"Execution interrupted externally while running {getattr(item, 'title', state.job_id)}",
    )
    return store.load(state.job_id)


def cmd_run_loop(args):
    from executor_engine import cmd_run_next

    store = JobStore(args.jobs_root)
    owner = args.execution_owner or f"runner:{args.job_id}"
    acquired, lock_info = store.try_acquire_lock(args.job_id, owner=owner)
    if not acquired:
        print(json.dumps({"ok": False, "job_id": args.job_id, "status": "LOCKED", "lock": lock_info}, ensure_ascii=False))
        return

    previous_handlers = _install_signal_handlers()
    try:
        state = store.load(args.job_id)
        if state.mode != "serial":
            raise SystemExit(f"Unsupported mode: {state.mode}")
        state.execution["last_run_owner"] = owner
        state.execution["last_run_started_at"] = now_iso()
        store.save(state)

        steps = 0
        while True:
            state = store.load(args.job_id)
            if state.status in {"COMPLETED", "BLOCKED", "FAILED", "ABANDONED"}:
                break
            ns = argparse.Namespace(jobs_root=args.jobs_root, job_id=args.job_id, execution_owner=owner)
            with contextlib.redirect_stdout(io.StringIO()):
                cmd_run_next(ns)
            steps += 1
            if args.max_steps and steps >= args.max_steps:
                break

        state = store.load(args.job_id)
        state.execution["last_run_finished_at"] = now_iso()
        store.save(state)
        print(json.dumps({"ok": True, "job_id": state.job_id, "status": state.status, "steps_executed": steps}, ensure_ascii=False))
    except ExecutionInterrupted as exc:
        state = _mark_interrupted_truth(store, store.load(args.job_id), owner=owner, signal_name=exc.signal_name)
        print(json.dumps({"ok": False, "job_id": state.job_id, "status": state.status, "interrupted": True, "signal": exc.signal_name}, ensure_ascii=False))
    finally:
        _restore_signal_handlers(previous_handlers)
        store.release_lock(args.job_id, owner=owner)


def build_parser():
    p = argparse.ArgumentParser(description="Execution-plane serial runner")
    p.add_argument("--jobs-root", default="state/jobs")
    sp = p.add_subparsers(dest="command", required=True)
    initp = sp.add_parser("init-job")
    initp.add_argument("spec")
    initp.add_argument("--ledger")
    initp.add_argument("--task-id")
    initp.set_defaults(func=cmd_init_job)
    status = sp.add_parser("status")
    status.add_argument("job_id")
    status.set_defaults(func=cmd_status)
    run_loop = sp.add_parser("run-loop")
    run_loop.add_argument("job_id")
    run_loop.add_argument("--max-steps", type=int)
    run_loop.add_argument("--execution-owner")
    run_loop.set_defaults(func=cmd_run_loop)
    return p


if __name__ == "__main__":
    args = build_parser().parse_args()
    args.func(args)
