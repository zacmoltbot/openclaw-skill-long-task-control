from __future__ import annotations

from typing import Any

from .base import AdapterResult


class RunningHubMatrixAdapter:
    """First concrete domain adapter for serial RunningHub media jobs.

    Scope for initial integration:
    - normalize one matrix item into a single executable unit
    - carry image/audio/workflow metadata through the execution plane
    - leave actual remote submission/observation wiring to the next iteration

    This keeps the repo honest: we add the adapter contract now, but do not pretend
    the durable external loop is finished until submit/observe/collect is wired.
    """

    name = "runninghub_matrix"

    def plan(self, job_spec: dict[str, Any], state: dict[str, Any]) -> dict[str, Any]:
        items = []
        for idx, item in enumerate(job_spec.get("items", []), start=1):
            items.append({
                "item_id": item.get("item_id") or f"rh-{idx:03d}",
                "title": item.get("title") or f"{item.get('still_key','still')} × {item.get('workflow_key','workflow')}",
                "still_key": item.get("still_key"),
                "still_path": item.get("still_path"),
                "workflow_key": item.get("workflow_key"),
                "workflow_id": item.get("workflow_id"),
                "audio_path": item.get("audio_path"),
                "prompt": item.get("prompt"),
                "output_kind": item.get("output_kind", "video"),
                "meta": item.get("meta", {}),
            })
        return {"items": items, "mode": job_spec.get("mode", "serial")}

    def prepare(self, item: dict[str, Any], context: dict[str, Any]) -> dict[str, Any]:
        required = ["still_path", "workflow_id"]
        missing = [k for k in required if not item.get(k)]
        if missing:
            item = dict(item)
            item["_prepare_error"] = f"missing required fields: {', '.join(missing)}"
        return item

    def submit(self, item: dict[str, Any], state: dict[str, Any]) -> AdapterResult:
        if item.get("_prepare_error"):
            return AdapterResult(
                status="blocked",
                summary=item["_prepare_error"],
                blocked_reason=item["_prepare_error"],
                facts={"item": item},
            )
        return AdapterResult(
            status="submitted",
            summary=f"RunningHub matrix item prepared: {item.get('title')}",
            facts={
                "still_key": item.get("still_key"),
                "still_path": item.get("still_path"),
                "workflow_key": item.get("workflow_key"),
                "workflow_id": item.get("workflow_id"),
                "audio_path": item.get("audio_path"),
                "prompt": item.get("prompt"),
            },
            next_action="Submit to RunningHub remote workflow and record provider job id",
        )

    def observe(self, item: dict[str, Any], state: dict[str, Any]) -> AdapterResult:
        return AdapterResult(
            status="observed",
            summary="RunningHub observation loop not wired yet",
            facts={"workflow_id": item.get("workflow_id"), "item_id": item.get("item_id")},
        )

    def collect(self, item: dict[str, Any], state: dict[str, Any]) -> AdapterResult:
        return AdapterResult(
            status="collected",
            summary="RunningHub artifact collection not wired yet",
            facts={"workflow_id": item.get("workflow_id"), "item_id": item.get("item_id")},
        )

    def finalize(self, item: dict[str, Any], state: dict[str, Any]) -> AdapterResult:
        return AdapterResult(
            status="completed",
            summary=f"RunningHub matrix placeholder finalize: {item.get('title')}",
            facts={
                "still_key": item.get("still_key"),
                "workflow_key": item.get("workflow_key"),
                "workflow_id": item.get("workflow_id"),
            },
        )
