#!/usr/bin/env python3
from __future__ import annotations

import argparse
import json
import subprocess
from pathlib import Path
from typing import Any

ROOT = Path(__file__).resolve().parent.parent
TASK_LEDGER = ROOT / "scripts" / "task_ledger.py"


def run(*args: str) -> str:
    proc = subprocess.run(args, text=True, capture_output=True, check=True)
    return proc.stdout.strip()


def ledger_cmd(ledger: Path, *parts: str) -> list[str]:
    return ["python3", str(TASK_LEDGER), "--ledger", str(ledger), *parts]


def sync_started(ledger: Path, task_id: str, checkpoint: str, summary: str, next_action: str | None = None, facts: dict[str, Any] | None = None) -> str:
    cmd = ledger_cmd(ledger, "checkpoint", task_id, "--event-type", "STEP_PROGRESS", "--summary", summary, "--current-checkpoint", checkpoint)
    if next_action:
        cmd.extend(["--next-action", next_action])
    for k, v in (facts or {}).items():
        cmd.extend(["--fact", f"{k}={v}"])
    return run(*cmd)


def sync_completed(ledger: Path, task_id: str, checkpoint: str, summary: str, next_action: str | None = None, facts: dict[str, Any] | None = None, artifacts: list[str] | None = None) -> str:
    cmd = ledger_cmd(ledger, "checkpoint", task_id, "--event-type", "STEP_COMPLETED", "--summary", summary, "--current-checkpoint", checkpoint)
    if next_action:
        cmd.extend(["--next-action", next_action])
    for k, v in (facts or {}).items():
        cmd.extend(["--fact", f"{k}={v}"])
    for a in (artifacts or []):
        cmd.extend(["--artifact", a])
    return run(*cmd)


def sync_blocked(ledger: Path, task_id: str, checkpoint: str, reason: str, safe_next_step: str, next_action: str | None = None, need: list[str] | None = None, facts: dict[str, Any] | None = None) -> str:
    cmd = ledger_cmd(ledger, "block", task_id, "--reason", reason, "--safe-next-step", safe_next_step, "--current-checkpoint", checkpoint)
    if next_action:
        cmd.extend(["--next-action", next_action])
    for n in (need or []):
        cmd.extend(["--need", n])
    for k, v in (facts or {}).items():
        cmd.extend(["--fact", f"{k}={v}"])
    return run(*cmd)


def sync_task_completed(ledger: Path, task_id: str, checkpoint: str, summary: str, facts: dict[str, Any] | None = None, artifacts: list[str] | None = None, validation: list[str] | None = None) -> str:
    cmd = ledger_cmd(ledger, "checkpoint", task_id, "--event-type", "TASK_COMPLETED", "--summary", summary, "--current-checkpoint", checkpoint)
    for k, v in (facts or {}).items():
        cmd.extend(["--fact", f"{k}={v}"])
    for a in (artifacts or []):
        cmd.extend(["--artifact", a])
    for v in (validation or []):
        cmd.extend(["--validation", v])
    return run(*cmd)


def sync_interrupted(ledger: Path, task_id: str, checkpoint: str, summary: str, *, reason: str, next_action: str | None = None, need: list[str] | None = None, facts: dict[str, Any] | None = None) -> str:
    interruption_facts = {"failure_type": "EXECUTION_INTERRUPTED", **(facts or {})}
    cmd = ledger_cmd(ledger, "block", task_id, "--reason", reason, "--safe-next-step", "Resume or rerun the interrupted execution path, then publish fresh observed truth", "--current-checkpoint", checkpoint)
    if next_action:
        cmd.extend(["--next-action", next_action])
    for n in (need or ["Re-enter canonical execution path after interruption", "Verify whether any partial side effects completed before resuming"]):
        cmd.extend(["--need", n])
    for k, v in interruption_facts.items():
        cmd.extend(["--fact", f"{k}={v}"])
    return run(*cmd)


class ExecutionBridge:
    """Map generic execution-plane item lifecycle into existing task_ledger truth/reporting.

    This stays generic: the bridge only needs a ledger path + task id + item checkpoint mapping.
    """

    def __init__(self, config: dict[str, Any] | None):
        config = config or {}
        self.enabled = bool(config.get("ledger") and config.get("task_id"))
        self.ledger = Path(config["ledger"]).expanduser() if config.get("ledger") else None
        self.task_id = config.get("task_id")

    def checkpoint_for(self, item: Any, index: int) -> str:
        payload = getattr(item, "payload", {}) or {}
        return str(payload.get("checkpoint") or f"step-{index + 1:02d}")

    def _item_facts(self, item: Any, index: int, extra: dict[str, Any] | None = None) -> dict[str, Any]:
        facts = {
            "job_id": getattr(item, "payload", {}).get("job_id") or None,
            "execution_item_id": getattr(item, "item_id"),
            "execution_index": str(index + 1),
            "execution_title": getattr(item, "title"),
            "execution_adapter": extra.get("adapter") if extra else None,
        }
        if extra:
            facts.update({k: v for k, v in extra.items() if v is not None})
        return {k: v for k, v in facts.items() if v is not None}

    def sync_item_started(self, state: Any, item: Any, index: int) -> None:
        if not self.enabled:
            return
        sync_started(
            self.ledger,
            self.task_id,
            self.checkpoint_for(item, index),
            f"Execution started: {item.title}",
            next_action=getattr(item, "next_action", None) or "Execute current item and publish terminal truth",
            facts=self._item_facts(item, index, {"adapter": getattr(state, "adapter", None), "job_id": getattr(state, "job_id", None), "execution_status": getattr(item, "status", None)}),
        )

    def sync_item_completed(self, state: Any, item: Any, index: int, *, summary: str, artifacts: list[str] | None = None, facts: dict[str, Any] | None = None) -> None:
        if not self.enabled:
            return
        next_action = "Advance to next execution item" if index < len(state.items) - 1 else "Finalize job completion"
        sync_completed(
            self.ledger,
            self.task_id,
            self.checkpoint_for(item, index),
            summary,
            next_action=next_action,
            facts=self._item_facts(item, index, {"adapter": getattr(state, "adapter", None), "job_id": getattr(state, "job_id", None), "execution_status": getattr(item, "status", None), **(facts or {})}),
            artifacts=artifacts,
        )

    def sync_item_blocked(self, state: Any, item: Any, index: int, *, summary: str, reason: str | None = None, facts: dict[str, Any] | None = None) -> None:
        if not self.enabled:
            return
        sync_blocked(
            self.ledger,
            self.task_id,
            self.checkpoint_for(item, index),
            reason or summary,
            safe_next_step="Unblock the current execution item, then rerun the job loop",
            next_action="Wait for unblock action, then resume execution loop",
            need=["Resolve execution-plane blocker for current item"],
            facts=self._item_facts(item, index, {"adapter": getattr(state, "adapter", None), "job_id": getattr(state, "job_id", None), "execution_status": getattr(item, "status", None), **(facts or {})}),
        )

    def sync_task_completed(self, state: Any) -> None:
        if not self.enabled:
            return
        checkpoint = f"step-{len(getattr(state, 'items', [])):02d}" if getattr(state, 'items', []) else "step-00"
        failed_items = list(getattr(state, "failed", []) or [])
        if failed_items:
            sync_blocked(
                self.ledger,
                self.task_id,
                checkpoint,
                f"Execution finished with failed items: {', '.join(failed_items)}",
                safe_next_step="Reconcile partial success truth: deliver/report existing outputs, then decide whether to retry missing failed items",
                next_action="Do not mark SUCCESS; report partial success with missing items explicitly",
                need=[
                    "Acknowledge missing/failed workflow items explicitly",
                    "Report existing delivered/generated artifacts before retry planning",
                ],
                facts={
                    "job_id": getattr(state, "job_id", None),
                    "execution_adapter": getattr(state, "adapter", None),
                    "completed_items": str(len(getattr(state, "completed", []) or [])),
                    "failed_items": ",".join(failed_items),
                    "failure_type": "EXECUTION_PARTIAL_FAILURE",
                },
            )
            return
        sync_task_completed(
            self.ledger,
            self.task_id,
            checkpoint,
            f"Execution job completed: {getattr(state, 'job_id', 'unknown-job')}",
            facts={
                "job_id": getattr(state, "job_id", None),
                "execution_adapter": getattr(state, "adapter", None),
                "completed_items": str(len(getattr(state, "completed", []) or [])),
            },
            artifacts=[a.path for a in getattr(state, "artifacts", [])],
        )

    def sync_interrupted(self, state: Any, item: Any | None, index: int | None, *, signal_name: str | None = None, summary: str | None = None) -> None:
        if not self.enabled:
            return
        if item is not None:
            checkpoint = self.checkpoint_for(item, index or 0)
            item_id = getattr(item, "item_id", None)
            item_title = getattr(item, "title", None)
            execution_status = getattr(item, "status", None)
        else:
            checkpoint = f"step-{((getattr(state, 'current_index', 0) or 0) + 1):02d}"
            item_id = None
            item_title = None
            execution_status = getattr(state, "status", None)
        sync_interrupted(
            self.ledger,
            self.task_id,
            checkpoint,
            summary or f"Execution interrupted while running {getattr(state, 'job_id', 'unknown-job')}",
            reason=f"Canonical execution interrupted externally{f' ({signal_name})' if signal_name else ''}",
            next_action="Inspect partial progress/side effects, then resume or rerun the interrupted execution loop",
            facts={
                "job_id": getattr(state, "job_id", None),
                "execution_adapter": getattr(state, "adapter", None),
                "execution_item_id": item_id,
                "execution_title": item_title,
                "execution_status": execution_status,
                "interrupted_signal": signal_name,
            },
        )


def main() -> None:
    parser = argparse.ArgumentParser(description="Bridge execution-plane item results into task_ledger truth/reporting")
    parser.add_argument("--ledger", type=Path, required=True)
    parser.add_argument("command", choices=["started", "completed", "blocked", "task-completed"])
    parser.add_argument("task_id")
    parser.add_argument("--checkpoint", required=True)
    parser.add_argument("--summary", required=True)
    parser.add_argument("--next-action")
    parser.add_argument("--safe-next-step")
    parser.add_argument("--fact", action="append")
    parser.add_argument("--artifact", action="append")
    parser.add_argument("--need", action="append")
    parser.add_argument("--validation", action="append")
    args = parser.parse_args()

    facts: dict[str, Any] = {}
    for item in args.fact or []:
        k, v = item.split("=", 1)
        facts[k] = v

    if args.command == "started":
        print(sync_started(args.ledger, args.task_id, args.checkpoint, args.summary, next_action=args.next_action, facts=facts))
    elif args.command == "completed":
        print(sync_completed(args.ledger, args.task_id, args.checkpoint, args.summary, next_action=args.next_action, facts=facts, artifacts=args.artifact))
    elif args.command == "blocked":
        if not args.safe_next_step:
            raise SystemExit("--safe-next-step is required for blocked")
        print(sync_blocked(args.ledger, args.task_id, args.checkpoint, args.summary, args.safe_next_step, next_action=args.next_action, need=args.need, facts=facts))
    elif args.command == "task-completed":
        print(sync_task_completed(args.ledger, args.task_id, args.checkpoint, args.summary, facts=facts, artifacts=args.artifact, validation=args.validation))


if __name__ == "__main__":
    main()
