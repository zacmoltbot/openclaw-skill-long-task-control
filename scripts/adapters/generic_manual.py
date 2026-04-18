from __future__ import annotations

import json
import os
import subprocess
from pathlib import Path
from typing import Any

from .base import AdapterResult


class GenericManualAdapter:
    """Generic execution-first adapter.

    This is the baseline adapter for arbitrary long tasks. It can execute explicitly
    described local actions (currently shell commands + deterministic local repair actions)
    and falls back to an honesty-first human gate when a step has no executable semantics.
    """

    name = "generic_manual"
    EXPLICIT_DEMO_MODE = "synthetic_demo"
    AUTO_REPAIR_MODE = "auto_repair"
    EXTERNAL_OBSERVED_MODE = "external_observed"
    REQUESTER_CHANNEL_REPAIR_ACTION = "repair_requester_channel"
    DELIVER_ARTIFACTS_ACTION = "deliver_artifacts"

    def _mode_for(self, item: dict[str, Any]) -> str:
        return str(item.get("generic_manual_mode") or ("shell" if item.get("shell") else "human_gate"))

    def _auto_action_for(self, item: dict[str, Any]) -> str | None:
        raw = item.get("auto_action") or item.get("repair_action") or item.get("action")
        value = str(raw).strip() if raw is not None else ""
        return value or None

    def _title_for(self, item: dict[str, Any]) -> str:
        return str(item.get("title") or item.get("id") or "unnamed-item")

    def _cwd_for(self, item: dict[str, Any]) -> str | None:
        cwd = item.get("cwd")
        return str(Path(cwd).expanduser()) if cwd else None

    def _timeout_for(self, item: dict[str, Any]) -> int:
        raw = item.get("timeout_sec")
        try:
            return max(1, int(raw)) if raw is not None else 600
        except Exception:
            return 600

    def _expected_artifacts(self, item: dict[str, Any]) -> list[str]:
        values: list[str] = []
        for key in ("expect_artifacts", "artifacts", "outputs"):
            raw = item.get(key)
            if isinstance(raw, list):
                values.extend(str(v) for v in raw if str(v).strip())
        single = item.get("artifact") or item.get("output")
        if single:
            values.append(str(single))
        deduped: list[str] = []
        seen: set[str] = set()
        for value in values:
            if value not in seen:
                deduped.append(value)
                seen.add(value)
        return deduped

    def _batch_result_path(self, item: dict[str, Any]) -> str | None:
        raw = item.get("batch_result") or item.get("batch_results")
        if raw is None:
            return None
        value = str(raw).strip()
        return value or None

    def _delivery_artifacts(self, item: dict[str, Any]) -> list[str]:
        values: list[str] = []
        for key in ("deliver_artifacts", "deliver_outputs", "deliver_media"):
            raw = item.get(key)
            if isinstance(raw, list):
                values.extend(str(v) for v in raw if str(v).strip())
            elif raw is not None:
                values.extend([v.strip() for v in str(raw).split("|") if v.strip()])
        if not values:
            values = self._expected_artifacts(item)
        deduped: list[str] = []
        seen: set[str] = set()
        for value in values:
            if value not in seen:
                deduped.append(value)
                seen.add(value)
        return deduped

    def _batch_min_success(self, item: dict[str, Any]) -> int:
        raw = item.get("batch_min_success")
        try:
            return max(0, int(raw)) if raw is not None else 1
        except Exception:
            return 1

    def _batch_entry_list(self, payload: Any) -> list[Any] | None:
        if isinstance(payload, list):
            return payload
        if not isinstance(payload, dict):
            return None
        for key in ("entries", "results", "items", "submissions", "jobs", "runs"):
            value = payload.get(key)
            if isinstance(value, list):
                return value
        return None

    def _batch_summary_counts(self, payload: Any) -> dict[str, Any]:
        counts: dict[str, Any] = {"success_count": None, "failure_count": None, "total_count": None}
        if isinstance(payload, dict):
            for key in ("success_count", "succeeded", "successes", "ok_count", "completed_count"):
                if payload.get(key) is not None:
                    try:
                        counts["success_count"] = int(payload.get(key))
                        break
                    except Exception:
                        pass
            for key in ("failure_count", "failed", "failures", "error_count", "failed_count"):
                if payload.get(key) is not None:
                    try:
                        counts["failure_count"] = int(payload.get(key))
                        break
                    except Exception:
                        pass
            for key in ("total_count", "total", "count", "submission_count", "job_count"):
                if payload.get(key) is not None:
                    try:
                        counts["total_count"] = int(payload.get(key))
                        break
                    except Exception:
                        pass

        entries = self._batch_entry_list(payload)
        if entries is not None:
            inferred_success = 0
            inferred_failure = 0
            for entry in entries:
                normalized = json.dumps(entry, ensure_ascii=False).lower() if isinstance(entry, (dict, list)) else str(entry).lower()
                if isinstance(entry, dict):
                    flags = [
                        entry.get("success"),
                        entry.get("ok"),
                        entry.get("passed"),
                    ]
                    if any(flag is True for flag in flags):
                        inferred_success += 1
                        continue
                    if any(flag is False for flag in flags):
                        inferred_failure += 1
                        continue
                    status_values = [entry.get(k) for k in ("status", "state", "result", "outcome") if entry.get(k) is not None]
                    normalized = " ".join(str(v).lower() for v in status_values) or normalized
                if any(token in normalized for token in ("success", "succeeded", "completed", "ok", "passed")):
                    inferred_success += 1
                elif any(token in normalized for token in ("fail", "failed", "error", "invalid", "timeout", "rejected", "cancel")):
                    inferred_failure += 1
            counts["success_count"] = inferred_success if counts["success_count"] is None else counts["success_count"]
            counts["failure_count"] = inferred_failure if counts["failure_count"] is None else counts["failure_count"]
            counts["total_count"] = len(entries) if counts["total_count"] is None else counts["total_count"]
            counts["sample_entries"] = entries[:3]
        else:
            counts["sample_entries"] = []

        if counts["total_count"] is None and counts["success_count"] is not None and counts["failure_count"] is not None:
            counts["total_count"] = counts["success_count"] + counts["failure_count"]
        if counts["failure_count"] is None and counts["total_count"] is not None and counts["success_count"] is not None:
            counts["failure_count"] = max(0, counts["total_count"] - counts["success_count"])
        return counts

    def _evaluate_batch_result(self, item: dict[str, Any]) -> tuple[str, dict[str, Any]] | None:
        batch_result = self._batch_result_path(item)
        if not batch_result:
            return None
        path = Path(batch_result).expanduser()
        if not path.exists():
            return "missing", {"batch_result": str(path)}
        try:
            payload = json.loads(path.read_text())
        except Exception as exc:
            return "invalid", {"batch_result": str(path), "parse_error": str(exc)}
        counts = self._batch_summary_counts(payload)
        min_success = self._batch_min_success(item)
        return "ok", {
            "batch_result": str(path),
            "batch_min_success": min_success,
            "batch_success_count": counts.get("success_count"),
            "batch_failure_count": counts.get("failure_count"),
            "batch_total_count": counts.get("total_count"),
            "batch_sample_entries": counts.get("sample_entries") or [],
        }

    def _bridge_task_context(self, state: dict[str, Any]) -> tuple[Path | None, dict[str, Any] | None, dict[str, Any] | None]:
        bridge = state.get("bridge") or {}
        ledger_path = bridge.get("ledger")
        task_id = bridge.get("task_id")
        if not ledger_path or not task_id:
            return None, None, None
        ledger = Path(str(ledger_path)).expanduser()
        if not ledger.exists():
            return ledger, None, None
        payload = json.loads(ledger.read_text())
        task = next((item for item in payload.get("tasks", []) if item.get("task_id") == task_id), None)
        return ledger, payload, task

    def _extract_discord_target_from_session_key(self, session_key: str | None) -> str | None:
        if not session_key:
            return None
        parts = [part for part in str(session_key).split(":") if part]
        for idx, part in enumerate(parts[:-1]):
            if part in {"channel", "user", "thread"} and parts[idx + 1].isdigit():
                return parts[idx + 1]
        tail = parts[-1] if parts else ""
        return tail if tail.isdigit() else None

    def _normalize_discord_target(self, raw_target: str | None, *, task: dict[str, Any] | None = None) -> dict[str, Any]:
        candidate = str(raw_target or "").strip()
        result = {
            "raw_target": raw_target,
            "target": candidate,
            "valid": True,
            "source": "input",
            "reason": None,
        }
        if not candidate:
            result.update(valid=False, reason="empty_target")
            return result
        if candidate.isdigit():
            return result
        if candidate.startswith(("discord:channel:", "discord:user:", "discord:thread:")) and candidate.split(":")[-1].isdigit():
            result["target"] = candidate.split(":")[-1]
            result["source"] = "normalized_discord_uri"
            return result
        if (candidate.startswith("<#") or candidate.startswith("<@")) and candidate.endswith(">") and candidate[2:-1].isdigit():
            result["target"] = candidate[2:-1]
            result["source"] = "normalized_discord_mention"
            return result
        task = task or {}
        fallback = (
            self._extract_discord_target_from_session_key(task.get("monitoring", {}).get("openclaw_session_key"))
            or self._extract_discord_target_from_session_key(task.get("monitoring", {}).get("executor_session_key"))
        )
        if fallback:
            result.update(target=fallback, source="session_key_fallback", valid=True, reason="normalized_from_invalid_discord_target")
            return result
        result.update(valid=False, reason="invalid_discord_target")
        return result

    def _repair_requester_channel(self, item: dict[str, Any], state: dict[str, Any]) -> AdapterResult:
        ledger_path, ledger_payload, task = self._bridge_task_context(state)
        if ledger_path is None:
            return AdapterResult(status="failed", summary=f"auto-repair missing bridge ledger for: {self._title_for(item)}", facts={"item": item, "auto_action": self.REQUESTER_CHANNEL_REPAIR_ACTION})
        if task is None or ledger_payload is None:
            return AdapterResult(status="failed", summary=f"auto-repair task context missing for: {self._title_for(item)}", facts={"item": item, "auto_action": self.REQUESTER_CHANNEL_REPAIR_ACTION, "ledger": str(ledger_path)})

        message = task.setdefault("message", {})
        raw_target = message.get("requester_channel_raw") or message.get("requester_channel") or message.get("nudge_target")
        normalized = self._normalize_discord_target(raw_target, task=task)
        if not normalized["valid"]:
            return AdapterResult(
                status="failed",
                summary=f"requester channel repair could not normalize discord target: {self._title_for(item)}",
                facts={
                    "item": item,
                    "auto_action": self.REQUESTER_CHANNEL_REPAIR_ACTION,
                    "raw_target": raw_target,
                    "reason": normalized.get("reason"),
                },
                next_action="Provide a valid numeric Discord target or a session key with a channel/user/thread id",
            )

        before = {
            "requester_channel": message.get("requester_channel"),
            "nudge_target": message.get("nudge_target"),
            "requester_channel_source": message.get("requester_channel_source"),
            "requester_channel_valid": message.get("requester_channel_valid"),
        }
        message["requester_channel"] = normalized["target"]
        message["requester_channel_valid"] = True
        message["requester_channel_source"] = normalized["source"]
        if normalized.get("reason"):
            message["requester_channel_reason"] = normalized["reason"]
        else:
            message.pop("requester_channel_reason", None)
        message["nudge_target"] = normalized["target"]
        ledger_path.write_text(json.dumps(ledger_payload, ensure_ascii=False, indent=2) + "\n")

        return AdapterResult(
            status="submitted",
            summary=f"Auto-repaired requester channel target: {self._title_for(item)}",
            facts={
                "item": item,
                "auto_action": self.REQUESTER_CHANNEL_REPAIR_ACTION,
                "repair_applied": True,
                "before_requester_channel": before.get("requester_channel"),
                "after_requester_channel": normalized["target"],
                "requester_channel_source": normalized["source"],
                "requester_channel_reason": normalized.get("reason"),
            },
            next_action="Validate repaired delivery target and continue execution",
        )

    def _send_delivery_payload(self, *, channel: str, target: str, media_path: str, message: str) -> dict[str, Any]:
        sink_path = os.environ.get("LTC_DELIVERY_SINK_FILE")
        payload = {
            "channel": channel,
            "target": target,
            "media": media_path,
            "message": message,
        }
        if sink_path:
            sink = Path(sink_path).expanduser()
            sink.parent.mkdir(parents=True, exist_ok=True)
            existing = []
            if sink.exists() and sink.read_text().strip():
                existing = json.loads(sink.read_text())
            existing.append(payload)
            sink.write_text(json.dumps(existing, ensure_ascii=False, indent=2) + "\n")
            return {"ok": True, "message_ref": f"sink:{Path(media_path).name if media_path else 'msg'}", "payload": payload}

        cmd = [
            "openclaw", "message", "send",
            "--channel", channel,
            "--target", target,
            "--message", message,
            "--silent",
            "--json",
        ]
        if media_path:
            cmd.extend(["--media", media_path])

        proc = subprocess.run(cmd, check=False, text=True, capture_output=True, timeout=60)

        # Use shared fail-closed parser that handles:
        #   - plugin noise prefix on stdout (parses from end seeking valid JSON)
        #   - nonzero exit with structured stderr (Discord error format)
        #   - empty stdout → fail (not silent success)
        #   - missing messageId/id → message_ref=None but still ok=True
        from openclaw_ops import parse_delivery_result
        parsed = parse_delivery_result(proc)
        if not parsed.get("ok"):
            return {"ok": False, "error": parsed.get("error"), "returncode": proc.returncode, "raw_output_preview": parsed.get("raw_output_preview", "")}
        return {"ok": True, "message_ref": parsed.get("message_ref"), "result": parsed.get("result")}

    def _deliver_artifacts(self, item: dict[str, Any], state: dict[str, Any]) -> AdapterResult:
        ledger_path, ledger_payload, task = self._bridge_task_context(state)
        if ledger_path is None:
            return AdapterResult(status="failed", summary=f"artifact delivery missing bridge ledger for: {self._title_for(item)}", facts={"item": item, "auto_action": self.DELIVER_ARTIFACTS_ACTION})
        if task is None or ledger_payload is None:
            return AdapterResult(status="failed", summary=f"artifact delivery task context missing for: {self._title_for(item)}", facts={"item": item, "auto_action": self.DELIVER_ARTIFACTS_ACTION, "ledger": str(ledger_path)})

        message = task.setdefault("message", {})
        raw_target = message.get("requester_channel_raw") or message.get("requester_channel") or message.get("nudge_target")
        normalized = self._normalize_discord_target(raw_target, task=task)
        if not normalized["valid"]:
            return AdapterResult(
                status="blocked",
                summary=f"artifact delivery target invalid: {self._title_for(item)}",
                blocked_reason="INVALID_DELIVERY_TARGET",
                facts={"raw_target": raw_target, "reason": normalized.get("reason"), "auto_action": self.DELIVER_ARTIFACTS_ACTION},
                next_action="Repair requester_channel before delivery",
            )

        paths = [p for p in self._delivery_artifacts(item) if Path(p).expanduser().exists()]
        if not paths:
            return AdapterResult(
                status="blocked",
                summary=f"artifact delivery missing files: {self._title_for(item)}",
                blocked_reason="MISSING_DELIVERY_ARTIFACTS",
                facts={"requested_artifacts": self._delivery_artifacts(item), "auto_action": self.DELIVER_ARTIFACTS_ACTION},
                next_action="Generate or resolve artifacts before delivery",
            )

        # ── Resume support: load previously-confirmed deliveries ──────────────
        # delivery_progress is written to item_state.facts after each successful
        # per-artifact send and cleaned up when the item completes.
        # On retry we resume from the next unconfirmed artifact — no duplicates.
        adapter_context = state.get("adapter_context", {})
        item_state = adapter_context.get("item_state")
        progress_raw = (item_state.facts or {}).get("delivery_progress") if item_state else None
        delivery_progress: dict[str, str] = {}
        if progress_raw:
            try:
                delivery_progress = json.loads(progress_raw) if isinstance(progress_raw, str) else dict(progress_raw)
            except (json.JSONDecodeError, TypeError):
                delivery_progress = {}

        # Resume logic: skip confirmed artifacts, but track which were already attempted.
        # delivery_progress key = artifact path, value = message_ref (or "pending" if attempted but failed)
        # On retry: skip confirmed (real ref), retry "pending", skip non-existent (never attempted).
        delivered = []
        delivery_start_idx = 0
        for idx, media_path in enumerate(paths):
            media_str = str(Path(media_path).expanduser())
            prev_ref = delivery_progress.get(media_str, "")
            if prev_ref and prev_ref != "pending":
                # Already confirmed delivered — skip and record it
                delivered.append({
                    "media": media_str,
                    "message_ref": prev_ref,
                })
                delivery_start_idx = idx + 1
                continue
            if prev_ref == "pending":
                # Previously attempted but failed — retry it (do NOT break here)
                delivery_start_idx = idx + 1
                continue
            # Not in progress at all — this is the first untried artifact; start delivery from here
            delivery_start_idx = idx + 1
            break

        # Caption is item-level per-artifact: neutral, shows progress, does NOT
        # claim task completion. Task completion is reported only via the
        # terminal summary AFTER all artifacts are confirmed delivered.
        total = len(paths)
        caption_base = f"交付項目"

        for idx, media_path in enumerate(paths, start=1):
            if idx < delivery_start_idx:
                continue  # already confirmed (or already-pending-but-being-retried) above
            abs_path = str(Path(media_path).expanduser())
            caption = f"{caption_base} ({idx}/{total})"
            send_result = self._send_delivery_payload(
                channel=str(task.get("channel") or "discord"),
                target=normalized["target"],
                media_path=abs_path,
                message=caption,
            )
            if not send_result.get("ok"):
                return AdapterResult(
                    status="blocked",  # BLOCKED (retriable) not failed — parse/network errors are transient
                    summary=f"artifact delivery blocked: {self._title_for(item)}",
                    blocked_reason="DELIVERY_TRANSPORT_FAILURE",
                    facts={
                        "auto_action": self.DELIVER_ARTIFACTS_ACTION,
                        "failed_media": abs_path,
                        "delivered_count": len(delivered),
                        "delivery_progress": json.dumps(delivery_progress),
                        "error": send_result.get("error"),
                        "error_type": "DELIVERY_TRANSPORT_FAILURE",
                        "retriable": True,
                    },
                    next_action="Retry delivery after fixing message/media send failure",
                )
            msg_ref = send_result.get("message_ref")
            delivered.append({"media": abs_path, "message_ref": msg_ref})
            # Persist progress so retry resumes from the next artifact (no duplicates)
            delivery_progress[abs_path] = msg_ref or "pending"
            if item_state is not None:
                item_state.facts = item_state.facts or {}
                item_state.facts["delivery_progress"] = json.dumps(delivery_progress)

        # ── All artifacts delivered — clean up progress and send summary ─────
        if item_state is not None:
            item_state.facts = item_state.facts or {}
            item_state.facts.pop("delivery_progress", None)

        task_caption = item.get("deliver_caption") or item.get("message")
        summary_fact_note = ""
        if task_caption:
            summary_result = self._send_delivery_payload(
                channel=str(task.get("channel") or "discord"),
                target=normalized["target"],
                media_path="",  # text-only summary
                message=str(task_caption),
            )
            if not summary_result.get("ok"):
                summary_fact_note = f" (summary send failed: {summary_result.get('error')})"
            else:
                summary_fact_note = ""
        else:
            summary_fact_note = ""

        return AdapterResult(
            status="completed",
            summary=f"Delivered {len(delivered)} artifact(s) to requester: {self._title_for(item)}{summary_fact_note}",
            facts={
                "auto_action": self.DELIVER_ARTIFACTS_ACTION,
                "delivery_target": normalized["target"],
                "delivered_count": len(delivered),
                "delivery_message_refs": [d.get("message_ref") for d in delivered],
                "delivery_resumed_from": delivery_start_idx - 1,  # 0-based index of first attempted artifact
                "completion_evidence": "artifact_delivery_succeeded",
            },
            artifacts=[entry["media"] for entry in delivered],
            next_action=item.get("next_action"),
        )

    def plan(self, job_spec: dict[str, Any], state: dict[str, Any]) -> dict[str, Any]:
        return {
            "items": job_spec.get("items", []),
            "mode": job_spec.get("mode", "serial"),
        }

    def prepare(self, item: dict[str, Any], context: dict[str, Any]) -> dict[str, Any]:
        prepared = dict(item)
        prepared.setdefault("expected_artifacts", self._expected_artifacts(prepared))
        return prepared

    def submit(self, item: dict[str, Any], state: dict[str, Any]) -> AdapterResult:
        mode = self._mode_for(item)
        if mode == self.EXPLICIT_DEMO_MODE:
            return AdapterResult(
                status="submitted",
                summary=f"Prepared synthetic generic item: {self._title_for(item)}",
                facts={"item": item, "generic_manual_mode": mode, "synthetic_execution": True},
                next_action="Synthetic/demo path only: continue execution-plane bridge test",
            )

        auto_action = self._auto_action_for(item)
        if mode == self.EXTERNAL_OBSERVED_MODE:
            return AdapterResult(
                status="blocked",
                summary=f"external-observed step is owner-driven and cannot be auto-executed: {self._title_for(item)}",
                blocked_reason="OWNER_DRIVEN_EXTERNAL_STEP",
                facts={
                    "item": item,
                    "generic_manual_mode": mode,
                    "owner_action_required": True,
                    "reason": "external_observed_owner_driven",
                },
                next_action="Run the real external workflow outside generic_manual, then write EXTERNAL_OBSERVED / STEP_PROGRESS / STEP_COMPLETED truth from actual evidence",
            )
        if mode == self.AUTO_REPAIR_MODE or auto_action == self.REQUESTER_CHANNEL_REPAIR_ACTION:
            return self._repair_requester_channel(item, state)
        if auto_action == self.DELIVER_ARTIFACTS_ACTION:
            return AdapterResult(
                status="submitted",
                summary=f"Prepared artifact delivery step: {self._title_for(item)}",
                facts={"auto_action": self.DELIVER_ARTIFACTS_ACTION, "deliver_artifacts": self._delivery_artifacts(item)},
                next_action="Deliver resolved artifacts to requester and verify delivery evidence",
            )

        shell_cmd = item.get("shell")
        if shell_cmd:
            try:
                proc = subprocess.run(
                    str(shell_cmd),
                    shell=True,
                    cwd=self._cwd_for(item),
                    text=True,
                    capture_output=True,
                    timeout=self._timeout_for(item),
                    check=False,
                )
            except subprocess.TimeoutExpired as exc:
                return AdapterResult(
                    status="failed",
                    summary=f"shell step timed out: {self._title_for(item)}",
                    facts={
                        "shell": str(shell_cmd),
                        "timeout_sec": str(self._timeout_for(item)),
                        "stdout_tail": (exc.stdout or "")[-500:],
                        "stderr_tail": (exc.stderr or "")[-500:],
                    },
                    next_action="Inspect the command, fix the timeout or make the step resumable, then retry",
                )
            facts = {
                "shell": str(shell_cmd),
                "cwd": self._cwd_for(item),
                "exit_code": str(proc.returncode),
                "stdout_tail": (proc.stdout or "")[-1000:],
                "stderr_tail": (proc.stderr or "")[-1000:],
            }
            if proc.returncode != 0:
                return AdapterResult(
                    status="failed",
                    summary=f"shell step failed: {self._title_for(item)}",
                    facts=facts,
                    next_action="Inspect stderr/stdout evidence, fix the command or inputs, then retry",
                )
            return AdapterResult(
                status="submitted",
                summary=f"shell step executed: {self._title_for(item)}",
                facts=facts,
                next_action="Validate expected artifacts / outputs before advancing",
            )

        summary = f"generic_manual cannot execute '{self._title_for(item)}' without explicit executable semantics"
        return AdapterResult(
            status="blocked",
            summary=summary,
            blocked_reason="OWNER_ACTION_REQUIRED",
            facts={
                "item": item,
                "generic_manual_mode": mode,
                "owner_action_required": True,
                "reason": "no_real_execution_semantics",
                "accepted_generic_actions": ["shell", self.REQUESTER_CHANNEL_REPAIR_ACTION, self.DELIVER_ARTIFACTS_ACTION],
            },
            next_action="Add an executable step contract (for example shell=...) or execute/observe the step manually and write observed truth",
        )

    def observe(self, item: dict[str, Any], state: dict[str, Any]) -> AdapterResult:
        return AdapterResult(
            status="observed",
            summary="Generic adapter observation is local-only; finalize checks command result and artifacts",
            facts={"item": item, "generic_manual_mode": self._mode_for(item)},
        )

    def collect(self, item: dict[str, Any], state: dict[str, Any]) -> AdapterResult:
        artifacts = [p for p in self._expected_artifacts(item) if Path(p).expanduser().exists()]
        return AdapterResult(
            status="collected",
            summary="Collected local artifact presence for generic step",
            facts={"item": item, "generic_manual_mode": self._mode_for(item), "artifacts_found": artifacts},
            artifacts=artifacts,
        )

    def finalize(self, item: dict[str, Any], state: dict[str, Any]) -> AdapterResult:
        mode = self._mode_for(item)
        if mode == self.EXPLICIT_DEMO_MODE:
            return AdapterResult(
                status="completed",
                summary=f"Synthetic generic item finalized: {self._title_for(item)}",
                facts={"item": item, "generic_manual_mode": mode, "synthetic_execution": True},
            )

        auto_action = self._auto_action_for(item)
        if mode == self.EXTERNAL_OBSERVED_MODE:
            return AdapterResult(
                status="blocked",
                summary=f"external-observed step requires real owner/external evidence before completion: {self._title_for(item)}",
                blocked_reason="OWNER_DRIVEN_EXTERNAL_STEP",
                facts={
                    "item": item,
                    "generic_manual_mode": mode,
                    "owner_action_required": True,
                    "reason": "external_observed_no_auto_finalize",
                },
                next_action="Do not finalize from placeholder shell success; record real external/download/completion truth only after actual work is observed",
            )
        # NOTE: deliver_artifacts must be checked BEFORE the generic AUTO_REPAIR_MODE
        # fallback — otherwise mode=AUTO_REPAIR_MODE with auto_action=deliver_artifacts
        # hits the early-return and never calls _deliver_artifacts.
        if auto_action == self.DELIVER_ARTIFACTS_ACTION:
            return self._deliver_artifacts(item, state)
        if mode == self.AUTO_REPAIR_MODE or auto_action == self.REQUESTER_CHANNEL_REPAIR_ACTION:
            return AdapterResult(
                status="completed",
                summary=f"Auto-repair completed: {self._title_for(item)}",
                facts={
                    "item": item,
                    "generic_manual_mode": mode,
                    "auto_action": auto_action or self.REQUESTER_CHANNEL_REPAIR_ACTION,
                    "completion_evidence": "deterministic_local_config_repair",
                },
                next_action=item.get("next_action"),
            )

        shell_cmd = item.get("shell")
        if shell_cmd:
            missing = [p for p in self._expected_artifacts(item) if not Path(p).expanduser().exists()]
            artifacts = [p for p in self._expected_artifacts(item) if Path(p).expanduser().exists()]
            batch_eval = self._evaluate_batch_result(item)
            if batch_eval:
                batch_state, batch_facts = batch_eval
                if batch_state == "missing":
                    return AdapterResult(
                        status="blocked",
                        summary=f"shell batch step ran but batch result evidence is missing: {self._title_for(item)}",
                        blocked_reason="MISSING_BATCH_RESULT",
                        facts={"shell": str(shell_cmd), **batch_facts, "artifacts_found": artifacts},
                        next_action="Write the declared batch_result summary JSON before marking the step complete",
                    )
                if batch_state == "invalid":
                    return AdapterResult(
                        status="blocked",
                        summary=f"shell batch step produced unreadable batch result evidence: {self._title_for(item)}",
                        blocked_reason="INVALID_BATCH_RESULT",
                        facts={"shell": str(shell_cmd), **batch_facts, "artifacts_found": artifacts},
                        next_action="Fix the batch summary JSON format so generic execution can evaluate success/failure honestly",
                    )
                success_count = int(batch_facts.get("batch_success_count") or 0)
                min_success = int(batch_facts.get("batch_min_success") or 1)
                if success_count < min_success:
                    return AdapterResult(
                        status="blocked",
                        summary=f"shell batch step did not meet success threshold: {self._title_for(item)}",
                        blocked_reason="BATCH_RESULT_THRESHOLD_NOT_MET",
                        facts={
                            "shell": str(shell_cmd),
                            **batch_facts,
                            "artifacts_found": artifacts,
                            "completion_evidence": "batch_result_summary",
                        },
                        next_action="Inspect the batch_result failure evidence, fix inputs/workflow, then retry so at least one internal submission succeeds",
                    )
                artifacts = [*artifacts, batch_facts["batch_result"]] if batch_facts.get("batch_result") not in artifacts else artifacts
                return AdapterResult(
                    status="completed",
                    summary=f"shell batch step completed: {self._title_for(item)}",
                    facts={
                        "shell": str(shell_cmd),
                        **batch_facts,
                        "artifacts_found": artifacts,
                        "completion_evidence": "batch_result_summary",
                    },
                    artifacts=artifacts,
                    next_action=item.get("next_action"),
                )
            if missing:
                return AdapterResult(
                    status="blocked",
                    summary=f"shell step ran but expected artifacts are missing: {self._title_for(item)}",
                    blocked_reason="MISSING_COMPLETION_EVIDENCE",
                    facts={
                        "shell": str(shell_cmd),
                        "missing_artifacts": missing,
                        "artifacts_found": artifacts,
                    },
                    next_action="Verify the command output path or add the right completion evidence before marking the step complete",
                )
            # Defensive gate: if expect_artifacts was declared but none exist on disk,
            # block even if the shell exited 0 — prevents silent artifact-loss where the
            # job is marked DONE but no file was ever written (e.g. RunningHub task
            # succeeded at HTTP level but produced no output, or output was cleaned up).
            expected = self._expected_artifacts(item)
            if expected and not artifacts:
                return AdapterResult(
                    status="blocked",
                    summary=f"shell step exited 0 but produced zero artifacts: {self._title_for(item)}",
                    blocked_reason="ZERO_ARTIFACTS_DESPITE_SUCCESS",
                    facts={
                        "shell": str(shell_cmd),
                        "expected_artifacts": expected,
                        "artifacts_found": artifacts,
                    },
                    next_action="Verify the command actually wrote the expected output file; check for early exit or silent failure in the wrapper script",
                )
            return AdapterResult(
                status="completed",
                summary=f"shell step completed: {self._title_for(item)}",
                facts={
                    "shell": str(shell_cmd),
                    "artifacts_found": artifacts,
                    "completion_evidence": "shell_exit_code_zero",
                },
                artifacts=artifacts,
                next_action=item.get("next_action"),
            )

        summary = f"generic_manual will not finalize '{self._title_for(item)}' without explicit execution evidence"
        return AdapterResult(
            status="blocked",
            summary=summary,
            blocked_reason="OWNER_ACTION_REQUIRED",
            facts={
                "item": item,
                "generic_manual_mode": mode,
                "owner_action_required": True,
                "reason": "no_completion_evidence",
            },
            next_action="Write real STEP_COMPLETED/TASK_COMPLETED truth only after actual execution or swap to a real adapter",
        )

    def can_resume(self, item_state: dict[str, Any]) -> bool:
        return True

    def is_human_gate(self, item_state: dict[str, Any]) -> bool:
        return self._mode_for(item_state.get("item", {}) if "item" in item_state else item_state) == "human_gate"
