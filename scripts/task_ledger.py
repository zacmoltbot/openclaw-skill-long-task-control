#!/usr/bin/env python3
import argparse
import json
from datetime import datetime, timezone
from pathlib import Path
from typing import Any

from reporting_contract import acknowledge_update, ensure_reporting, maybe_queue_external_update

DEFAULT_LEDGER = Path("state/long-task-ledger.example.json")
TASK_STATUSES = {"PENDING", "RUNNING", "BLOCKED", "COMPLETED", "FAILED", "ABANDONED"}
OBSERVED_EVENT_TYPES = {
    "STARTED",
    "STEP_PROGRESS",
    "STEP_COMPLETED",
    "TASK_COMPLETED",
    "BLOCKED_CONFIRMED",
    "OWNER_RESUMED",
    "OWNER_REPLY_RECORDED",
    "EXTERNAL_OBSERVED",
    "DOWNLOAD_OBSERVED",
    "HEARTBEAT",
}
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
    "retry_count",
    "cron_install_error",
    "install_signal",
    "resume_requests",
    "action_log",
}
EXTERNAL_JOB_STATES = {"SUBMITTED", "PENDING", "RUNNING", "FAILED", "RETRYING", "SWITCHED_WORKFLOW", "COMPLETED"}
PENDING_EXTERNAL_STATES = {"SUBMITTED", "PENDING", "RUNNING", "RETRYING", "SWITCHED_WORKFLOW"}
TERMINAL_STEP_STATES = {"DONE", "BLOCKED", "FAILED"}
PROVIDER_EVIDENCE_KEYS = {
    "provider_job_id",
    "submission_receipt",
    "submission_receipt_id",
    "provider_status_handle",
    "status_handle",
    "status_url",
    "provider_response_ref",
    "provider_receipt_ref",
    "artifact_path",
    "artifact_url",
    "output_file",
    "poll_token",
    "remote_job_id",
    "runninghub_job_id",
    "rh_job_id",
}
OBSERVED_FAILURE_TYPES = {
    "TIMEOUT",
    "DOWNLOAD_TIMEOUT",
    "DOWNLOAD_INCOMPLETE",
    "TRANSIENT_NETWORK",
    "EXECUTION_ERROR",
    "EXECUTION_INTERRUPTED",
    "EXECUTION_PARTIAL_FAILURE",
    "EXTERNAL_WAIT",
    "PERMISSION_DENIED",
    "INVALID_INPUT",
    "UNSUPPORTED",
    "AUTH_REQUIRED",
}


def now_iso():
    return datetime.now(timezone.utc).astimezone().isoformat(timespec="seconds")


def load_ledger(path: Path):
    if not path.exists():
        return {"version": 2, "updated_at": now_iso(), "tasks": []}
    raw = path.read_text().strip()
    if not raw:
        return {"version": 2, "updated_at": now_iso(), "tasks": []}
    ledger = json.loads(raw)
    ledger.setdefault("version", 2)
    return ledger


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


def nonempty(value):
    if value is None:
        return False
    if isinstance(value, str):
        return bool(value.strip())
    if isinstance(value, (list, dict, tuple, set)):
        return bool(value)
    return True


def extract_provider_evidence(facts=None, seed=None):
    merged = {}
    if isinstance(seed, dict):
        for key, value in seed.items():
            if nonempty(value):
                merged[key] = value
    for key, value in (facts or {}).items():
        if key in PROVIDER_EVIDENCE_KEYS and nonempty(value):
            merged[key] = value
    return merged


def parse_expected_artifacts_from_workflow_step(step: dict[str, Any]) -> list[str]:
    raw_title = str(step.get("title") or "").strip()
    if not raw_title:
        return []
    artifacts: list[str] = []
    for chunk in [part.strip() for part in raw_title.split("::")][1:]:
        if "=" not in chunk:
            continue
        key, value = chunk.split("=", 1)
        key = key.strip().lower().replace("-", "_")
        value = value.strip()
        if not value:
            continue
        if key in {"artifact", "output"}:
            artifacts.append(value)
        elif key in {"artifacts", "outputs", "expect", "expect_artifacts"}:
            artifacts.extend([v.strip() for v in value.split("|") if v.strip()])
    deduped: list[str] = []
    seen: set[str] = set()
    for value in artifacts:
        if value not in seen:
            deduped.append(value)
            seen.add(value)
    return deduped


def discover_existing_workflow_artifacts(task: dict[str, Any]) -> list[str]:
    discovered: list[str] = []
    seen: set[str] = set()
    for step in task.get("workflow") or []:
        for path_str in parse_expected_artifacts_from_workflow_step(step):
            path = Path(path_str).expanduser()
            if path.exists():
                resolved = str(path)
                if resolved not in seen:
                    discovered.append(resolved)
                    seen.add(resolved)
    return discovered


def ensure_task_shape(task):
    task.setdefault("observations", [])
    task.setdefault("workflow", [])
    task.setdefault("external_jobs", [])
    task.setdefault("downloads", [])
    task.setdefault("artifacts", [])
    task.setdefault("validation", [])
    task.setdefault("notes", [])
    task.setdefault("blocker", None)
    task.setdefault("heartbeat", {})
    task.setdefault("monitoring", {})
    task.setdefault("observed", {})
    task.setdefault("derived", {})
    task.setdefault("checkpoints", [])
    task.setdefault("runs", [])
    task.setdefault("run_counter", 1)
    task.setdefault("active_run_id", "run-01")
    ensure_reporting(task)
    task["heartbeat"].setdefault("watchdog_state", "OK")
    task["heartbeat"].setdefault("expected_interval_sec", 300)
    task["heartbeat"].setdefault("timeout_sec", 1800)
    task["monitoring"].setdefault("nudge_count", 0)
    task["monitoring"].setdefault("reconcile_count", 0)
    task["monitoring"].setdefault("retry_count", {})
    task["monitoring"].setdefault("resume_requests", [])
    task["monitoring"].setdefault("action_log", [])
    task["observed"].setdefault("steps", {})
    task["observed"].setdefault("task_completion", None)
    task["observed"].setdefault("block", None)
    task["observed"].setdefault("owner", {})
    task["observed"].setdefault("external_jobs", {})
    task["observed"].setdefault("downloads", {})


def append_observation(task, *, event_type, summary, facts=None, step_id=None, status=None):
    if event_type not in OBSERVED_EVENT_TYPES:
        raise SystemExit(f"Unsupported observation type: {event_type}")
    ensure_task_shape(task)
    at = now_iso()
    obs = {
        "at": at,
        "event_type": event_type,
        "summary": summary,
        "facts": facts or {},
    }
    if step_id:
        obs["step_id"] = step_id
    if status:
        obs["status"] = status
    task["observations"].append(obs)
    return obs


def set_step_observation(task, step_id, *, state, summary, facts=None, at=None):
    observed_steps = task.setdefault("observed", {}).setdefault("steps", {})
    record = observed_steps.setdefault(step_id, {"step_id": step_id})
    if state == "IN_PROGRESS":
        record["last_progress_at"] = at or now_iso()
        record["last_progress_summary"] = summary
        record["progress_facts"] = facts or {}
    elif state == "COMPLETED":
        record["completed_at"] = at or now_iso()
        record["completion_summary"] = summary
        record["completion_facts"] = facts or {}
    elif state == "BLOCKED":
        record["blocked_at"] = at or now_iso()
        record["block_summary"] = summary
        record["block_facts"] = facts or {}
    elif state == "FAILED":
        record["failed_at"] = at or now_iso()
        record["failure_summary"] = summary
        record["failure_facts"] = facts or {}
    return record


def ensure_external_job(task, provider, job_id, workflow=None, app=None):
    ensure_task_shape(task)
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
        "workflow": workflow,
        "app": app,
        "status": "SUBMITTED",
        "submitted_at": now_iso(),
        "updated_at": now_iso(),
        "provider_evidence": {"provider_job_id": job_id},
        "history": [],
    }
    task["external_jobs"].append(job)
    return job


def record_external_job_event(task, *, provider, job_id, state, summary, facts=None, workflow=None, app=None):
    if state not in EXTERNAL_JOB_STATES:
        raise SystemExit(f"Unsupported external job state: {state}")
    job = ensure_external_job(task, provider, job_id, workflow=workflow, app=app)
    at = now_iso()
    previous_status = job.get("status")
    evidence = extract_provider_evidence(facts=facts, seed=job.get("provider_evidence"))
    job.update({
        "status": state,
        "updated_at": at,
        "pending_external": state in PENDING_EXTERNAL_STATES,
        "provider_evidence": evidence,
    })
    if workflow:
        job["workflow"] = workflow
    if app:
        job["app"] = app
    job.setdefault("history", []).append({
        "at": at,
        "state": state,
        "summary": summary,
        "facts": facts or {},
        "from_status": previous_status,
    })
    task.setdefault("observed", {}).setdefault("external_jobs", {})[f"{provider}:{job_id}"] = {
        "provider": provider,
        "job_id": job_id,
        "status": state,
        "observed_at": at,
        "workflow": workflow or job.get("workflow"),
        "app": app or job.get("app"),
        "provider_evidence": evidence,
        "summary": summary,
        "facts": facts or {},
    }
    append_observation(task, event_type="EXTERNAL_OBSERVED", summary=summary, facts={
        "provider": provider,
        "job_id": job_id,
        "external_state": state,
        **(facts or {}),
    })
    return job


def record_download_observation(task, *, summary, facts=None, artifact=None):
    ensure_task_shape(task)
    at = now_iso()
    facts = facts or {}
    download_id = facts.get("download_id") or artifact or f"download-{len(task.get('downloads', [])) + 1:02d}"
    status = (facts.get("download_status") or facts.get("status") or "OBSERVED").upper()
    observed = {
        "download_id": download_id,
        "observed_at": at,
        "status": status,
        "summary": summary,
        "facts": facts,
        "artifact": artifact,
    }
    task.setdefault("downloads", []).append(observed)
    task.setdefault("observed", {}).setdefault("downloads", {})[download_id] = observed
    append_observation(task, event_type="DOWNLOAD_OBSERVED", summary=summary, facts=facts)
    if artifact and artifact not in task.setdefault("artifacts", []):
        task["artifacts"].append(artifact)
    return observed


def ensure_resume_requests(task):
    return task.setdefault("monitoring", {}).setdefault("resume_requests", [])


def latest_resume_request(task, resume_token=None):
    requests = ensure_resume_requests(task)
    if resume_token:
        for item in reversed(requests):
            if item.get("resume_token") == resume_token:
                return item
        return None
    for item in reversed(requests):
        if not item.get("acknowledged_at"):
            return item
    return requests[-1] if requests else None


def ack_resume_request(task, *, resume_token=None, outcome=None, checkpoint=None, facts=None):
    req = latest_resume_request(task, resume_token=resume_token)
    if not req:
        return None
    at = now_iso()
    req["acknowledged_at"] = at
    if outcome:
        req["resume_outcome"] = outcome
        req["resume_outcome_at"] = at
    if checkpoint:
        req["acknowledged_checkpoint"] = checkpoint
    if facts:
        req.setdefault("ack_facts", {}).update(facts)
    task.setdefault("monitoring", {})["last_resume_request_at"] = req.get("requested_at", at)
    return req


def active_run_snapshot(task):
    ensure_task_shape(task)
    return {
        "run_id": task.get("active_run_id") or f"run-{int(task.get('run_counter', 1) or 1):02d}",
        "status": task.get("status"),
        "current_checkpoint": task.get("current_checkpoint"),
        "last_checkpoint_at": task.get("last_checkpoint_at"),
        "next_action": task.get("next_action"),
        "heartbeat": json.loads(json.dumps(task.get("heartbeat", {}))),
        "observed": json.loads(json.dumps(task.get("observed", {}))),
        "observations": json.loads(json.dumps(task.get("observations", []))),
        "checkpoints": json.loads(json.dumps(task.get("checkpoints", []))),
        "external_jobs": json.loads(json.dumps(task.get("external_jobs", []))),
        "downloads": json.loads(json.dumps(task.get("downloads", []))),
        "artifacts": json.loads(json.dumps(task.get("artifacts", []))),
        "validation": json.loads(json.dumps(task.get("validation", []))),
        "blocker": json.loads(json.dumps(task.get("blocker"))),
        "derived": json.loads(json.dumps(task.get("derived", {}))),
    }


def archive_active_run(task, *, terminal_status=None, ended_at=None, reason=None):
    ensure_task_shape(task)
    snapshot = active_run_snapshot(task)
    snapshot["started_at"] = task.get("created_at")
    snapshot["ended_at"] = ended_at or now_iso()
    if terminal_status:
        snapshot["status"] = terminal_status
    if reason:
        snapshot["end_reason"] = reason
    runs = task.setdefault("runs", [])
    runs[:] = [run for run in runs if run.get("run_id") != snapshot["run_id"]]
    runs.append(snapshot)
    return snapshot


def reset_active_run(task, *, run_id, started_at, summary, facts=None, current_checkpoint=None, next_action=None):
    ensure_task_shape(task)
    checkpoint = current_checkpoint or (task.get("workflow") or [{}])[0].get("id")
    task["active_run_id"] = run_id
    task["status"] = "RUNNING"
    task["current_checkpoint"] = checkpoint
    task["last_checkpoint_at"] = started_at
    task["next_action"] = next_action or task.get("next_action")
    task["heartbeat"] = {
        "expected_interval_sec": task.get("heartbeat", {}).get("expected_interval_sec", 300),
        "timeout_sec": task.get("heartbeat", {}).get("timeout_sec", 1800),
        "last_progress_at": started_at,
        "last_heartbeat_at": started_at,
        "watchdog_state": "OK",
    }
    task["observed"] = {
        "steps": {},
        "task_completion": None,
        "block": None,
        "owner": {},
        "external_jobs": {},
        "downloads": {},
    }
    task["observations"] = []
    task["checkpoints"] = []
    task["external_jobs"] = []
    task["downloads"] = []
    task["artifacts"] = []
    task["validation"] = []
    task["blocker"] = None
    task["derived"] = {}
    monitoring = task.setdefault("monitoring", {})
    monitoring["nudge_count"] = 0
    monitoring["reconcile_count"] = 0
    monitoring["resume_requests"] = []
    monitoring["action_log"] = []
    append_observation(task, event_type="STARTED", summary=summary, facts=facts or {}, step_id=checkpoint, status="RUNNING")
    if checkpoint:
        set_step_observation(task, checkpoint, state="IN_PROGRESS", summary=summary, facts=facts or {}, at=started_at)
    task["checkpoints"].append({
        "at": started_at,
        "kind": "STARTED",
        "summary": summary,
        "facts": facts or {},
        "run_id": run_id,
    })
    return task


def derive_user_facing_outcome(task: dict[str, Any], workflow_states: list[dict[str, Any]], *, completed_task: dict[str, Any] | None, blocked_task: dict[str, Any] | None, inconsistencies: list[str], pending_external: bool) -> dict[str, Any]:
    recorded_artifacts = [str(item) for item in task.get("artifacts") or [] if nonempty(item)]
    discovered_artifacts = discover_existing_workflow_artifacts(task)
    all_artifacts: list[str] = []
    seen: set[str] = set()
    for value in [*recorded_artifacts, *discovered_artifacts]:
        if value not in seen:
            all_artifacts.append(value)
            seen.add(value)

    done_steps = [step["id"] for step in workflow_states if step.get("state") == "DONE"]
    non_done_steps = [step["id"] for step in workflow_states if step.get("state") != "DONE"]
    interruption_like = False
    blocker = task.get("blocker") or {}
    failure_type = str(blocker.get("failure_type") or "").upper()
    if failure_type in {"EXECUTION_INTERRUPTED", "EXECUTION_PARTIAL_FAILURE", "DOWNLOAD_INCOMPLETE", "DOWNLOAD_TIMEOUT", "EXTERNAL_WAIT"}:
        interruption_like = True

    outcome_status = "IN_PROGRESS"
    headline = "Task is still in progress"
    honesty_notes: list[str] = []

    if completed_task and not non_done_steps:
        outcome_status = "SUCCESS"
        headline = completed_task.get("summary") or "Task completed with observed evidence"
    elif completed_task and non_done_steps and all_artifacts:
        outcome_status = "PARTIAL_SUCCESS"
        headline = completed_task.get("summary") or "Task completed with partial outputs"
        honesty_notes.append(f"Some workflow steps are not marked DONE in control-plane history: {', '.join(non_done_steps)}")
        honesty_notes.append("Do not present this run as full success; existing outputs should be framed as partial success")
    elif completed_task and non_done_steps:
        outcome_status = "FAILED"
        headline = completed_task.get("summary") or "Task completion claim conflicts with unfinished workflow steps"
        honesty_notes.append(f"Some workflow steps are not marked DONE in control-plane history: {', '.join(non_done_steps)}")
    elif blocked_task and all_artifacts:
        outcome_status = "PARTIAL_SUCCESS"
        headline = blocked_task.get("summary") or "Outputs exist, but execution/control-plane did not finish cleanly"
        honesty_notes.append("User-facing result should foreground existing outputs before interruption noise")
        if interruption_like:
            honesty_notes.append(f"Control-plane interruption observed: {failure_type}")
        if non_done_steps:
            honesty_notes.append(f"Remaining or unconfirmed steps: {', '.join(non_done_steps)}")
    elif blocked_task:
        outcome_status = "BLOCKED"
        headline = blocked_task.get("summary") or blocker.get("reason") or "Task is blocked"
    elif task.get("status") == "FAILED":
        outcome_status = "FAILED"
        headline = blocker.get("reason") or "Task failed"
    elif pending_external:
        outcome_status = "IN_PROGRESS"
        headline = "Task is waiting on legitimate external work"

    return {
        "outcome_status": outcome_status,
        "headline": headline,
        "done_steps": done_steps,
        "remaining_steps": non_done_steps,
        "artifacts": all_artifacts,
        "recorded_artifacts": recorded_artifacts,
        "discovered_artifacts": discovered_artifacts,
        "control_plane_status": task.get("status"),
        "control_plane_truth_state": "INCONSISTENT" if inconsistencies else "CONSISTENT",
        "honesty_notes": honesty_notes,
    }


def project_task(task):
    ensure_task_shape(task)
    workflow = task.get("workflow") or []
    observed_steps = task.setdefault("observed", {}).setdefault("steps", {})
    derived = task.setdefault("derived", {})
    inconsistencies = []
    current_checkpoint = task.get("current_checkpoint")

    workflow_states = []
    current_running_found = False
    latest_step_event_at = None
    for idx, step in enumerate(workflow):
        step_id = step.get("id")
        obs = observed_steps.get(step_id, {})
        derived_state = "PENDING"
        if obs.get("blocked_at"):
            derived_state = "BLOCKED"
        elif obs.get("failed_at"):
            derived_state = "FAILED"
        elif obs.get("completed_at"):
            derived_state = "DONE"
        elif obs.get("last_progress_at"):
            derived_state = "RUNNING"
        elif current_checkpoint == step_id and not current_running_found:
            derived_state = "RUNNING"
        workflow_states.append({"id": step_id, "title": step.get("title"), "state": derived_state})
        # NOTE: we do NOT write back to task["workflow"]; original is immutable.
        # derived["workflow"] is the canonical current state.
        if derived_state == "RUNNING":
            current_running_found = True
        if obs.get("completed_at") and obs.get("last_progress_at") and obs["last_progress_at"] > obs["completed_at"]:
            inconsistencies.append(f"step:{step_id}:progress_after_completion")
        if latest_step_event_at is None:
            latest_step_event_at = obs.get("completed_at") or obs.get("last_progress_at") or obs.get("blocked_at") or obs.get("failed_at")
        else:
            latest_step_event_at = max(filter(None, [latest_step_event_at, obs.get("completed_at"), obs.get("last_progress_at"), obs.get("blocked_at"), obs.get("failed_at")]))

    completed_task = task.get("observed", {}).get("task_completion")
    blocked_task = task.get("observed", {}).get("block")
    owner = task.get("observed", {}).get("owner", {})

    pending_external = False
    external_failures = []
    suspicious_external = []
    for key, job in task.get("observed", {}).get("external_jobs", {}).items():
        state = str(job.get("status") or "").upper()
        evidence = extract_provider_evidence(seed=job.get("provider_evidence"), facts=job.get("facts"))
        if state in PENDING_EXTERNAL_STATES:
            if evidence:
                pending_external = True
            else:
                suspicious_external.append(key)
        if state == "FAILED":
            external_failures.append(key)

    for download_id, item in task.get("observed", {}).get("downloads", {}).items():
        status = str(item.get("status") or "").upper()
        if status in {"INCOMPLETE", "CORRUPT"}:
            inconsistencies.append(f"download:{download_id}:{status.lower()}")

    if completed_task and blocked_task:
        inconsistencies.append("task:completed_and_blocked")
    if completed_task:
        undone = [step["id"] for step in workflow_states if step["state"] != "DONE"]
        if undone:
            inconsistencies.append("task:completed_but_steps_not_done:" + ",".join(undone))
    if blocked_task and completed_task:
        inconsistencies.append("task:block_conflicts_with_completion")
    if pending_external and completed_task:
        inconsistencies.append("task:pending_external_after_task_completed")
    for key in suspicious_external:
        inconsistencies.append(f"external:{key}:missing_provider_evidence")

    current_step = None
    for step in workflow_states:
        if step["state"] in {"RUNNING", "PENDING", "BLOCKED", "FAILED"}:
            current_step = step["id"]
            break
    if current_step is None and workflow_states:
        current_step = workflow_states[-1]["id"]

    if completed_task and not inconsistencies:
        derived_status = "COMPLETED"
        truth_state = "CONSISTENT"
    elif blocked_task and not inconsistencies:
        derived_status = "BLOCKED"
        truth_state = "CONSISTENT"
    elif inconsistencies:
        derived_status = task.get("status", "RUNNING")
        truth_state = "INCONSISTENT"
    else:
        derived_status = "RUNNING"
        truth_state = "CONSISTENT"

    # NOTE: we do NOT write root-level task fields.
    # Canonical state lives in derived.*; task["status"/"current_checkpoint"/"last_checkpoint_at"]
    # are owned by command handlers (init/checkpoint/block/etc), not by project_task().

    derived.update({
        "workflow": workflow_states,
        "current_step": current_step,
        "step_states": {item["id"]: item["state"] for item in workflow_states},
        "pending_external": pending_external,
        "suspicious_external_jobs": suspicious_external,
        "external_failures": external_failures,
        "truth_state": truth_state,
        "inconsistencies": inconsistencies,
        "status": derived_status,
        "owner_reply_kind": owner.get("last_reply_kind"),
        "last_observed_progress_at": latest_step_event_at,
    })
    derived["user_facing"] = derive_user_facing_outcome(
        task,
        workflow_states,
        completed_task=completed_task,
        blocked_task=blocked_task,
        inconsistencies=inconsistencies,
        pending_external=pending_external,
    )
    return task


def mark_owner_response(task, reply_kind, responded_at):
    monitoring = task.setdefault("monitoring", {})
    monitoring["owner_response_at"] = responded_at
    monitoring["owner_response_kind"] = reply_kind
    task.setdefault("observed", {}).setdefault("owner", {})["last_reply_kind"] = reply_kind
    task["observed"]["owner"]["last_reply_at"] = responded_at
    return monitoring


def queue_step_update(task, event_type, summary, checkpoint, facts=None, outputs=None, next_action=None, blocker=None):
    ensure_reporting(task)
    if event_type == "STEP_COMPLETED":
        from reporting_contract import queue_update
        return queue_update(task, event_type=event_type, source_kind=event_type, summary=summary,
                            checkpoint=checkpoint, facts=facts, outputs=outputs, next_action=next_action,
                            blocker=blocker)
    if event_type == "TASK_COMPLETED":
        from reporting_contract import queue_update
        return queue_update(task, event_type="COMPLETED_HANDOFF", source_kind=event_type, summary=summary,
                            checkpoint=checkpoint, facts=facts, outputs=outputs, next_action=next_action)
    if event_type == "BLOCKED_CONFIRMED":
        from reporting_contract import queue_update
        return queue_update(task, event_type="BLOCKED_ESCALATE", source_kind=event_type, summary=summary,
                            checkpoint=checkpoint, facts=facts, outputs=outputs, next_action=next_action,
                            blocker=blocker)
    return None


def mark_terminal_cleanup(task, *, terminal_status, reason=None):
    heartbeat = task.setdefault("heartbeat", {})
    monitoring = task.setdefault("monitoring", {})
    at = now_iso()
    cleanup_reason = reason or f"task status is terminal ({terminal_status})"
    heartbeat["watchdog_state"] = "STOP_AND_DELETE"
    monitoring["last_action_at"] = at
    monitoring["last_action_state"] = "STOP_AND_DELETE"
    monitoring["last_action_reason"] = cleanup_reason
    monitoring["last_action_kind"] = "delete_monitor_cron"
    monitoring["last_action_payload"] = {
        "kind": "STOP_AND_DELETE",
        "facts": {
            "task_id": task.get("task_id"),
            "status": terminal_status,
            "reason": cleanup_reason,
        },
        "created_at": at,
    }
    return at


def cmd_list(args):
    ledger = load_ledger(args.ledger)
    for task in ledger.get("tasks", []):
        project_task(task)
        print(f"{task.get('task_id')}\t{task.get('status')}\t{task.get('derived', {}).get('truth_state')}\t{task.get('current_checkpoint')}\t{task.get('next_action')}")


def cmd_init(args):
    ledger = load_ledger(args.ledger)
    if find_task(ledger, args.task_id):
        raise SystemExit(f"Task already exists: {args.task_id}")

    workflow = []
    for idx, step in enumerate(args.workflow or [], start=1):
        workflow.append({"id": f"step-{idx:02d}", "title": step, "state": "PENDING"})

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
        "run_counter": 1,
        "active_run_id": "run-01",
        "runs": [],
        "current_checkpoint": workflow[0]["id"] if workflow else None,
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
            "blocked_escalate_after_sec": args.blocked_escalate_after_sec or max(args.timeout_sec, args.expected_interval_sec),
            "nudge_count": 0,
            "reconcile_count": 0,
            "retry_count": {},
            "resume_requests": [],
            "action_log": [],
            "cron_state": "ACTIVE",
            "install_signal": "NOT_REQUESTED",
        },
        "observed": {
            "steps": {},
            "task_completion": None,
            "block": None,
            "owner": {},
            "external_jobs": {},
            "downloads": {},
        },
        "observations": [],
        "validation": [],
        "blocker": None,
        "artifacts": args.artifact or [],
        "external_jobs": [],
        "downloads": [],
        "next_action": args.next_action,
        "notes": args.note or [],
        "checkpoints": [],
    }
    start_facts = parse_fact(args.fact)
    append_observation(task, event_type="STARTED", summary=args.summary or "Task initialized", facts=start_facts, step_id=task["current_checkpoint"], status="RUNNING")
    if task["current_checkpoint"]:
        set_step_observation(task, task["current_checkpoint"], state="IN_PROGRESS", summary="Task initialized", facts=start_facts, at=started_at)
    task["checkpoints"].append({
        "at": started_at,
        "kind": "STARTED",
        "summary": args.summary or "Task initialized",
        "facts": start_facts,
        "run_id": task["active_run_id"],
    })
    project_task(task)
    ledger.setdefault("tasks", []).append(task)
    save_ledger(args.ledger, ledger)
    print(f"Initialized {args.task_id}")


def cmd_checkpoint(args):
    ledger = load_ledger(args.ledger)
    task = ensure_task(ledger, args.task_id)
    ensure_task_shape(task)
    step_id = args.current_checkpoint or task.get("current_checkpoint")
    facts = parse_fact(args.fact)
    event_type = args.event_type
    event_at = now_iso()

    task["current_checkpoint"] = step_id
    task["last_checkpoint_at"] = event_at
    task.setdefault("heartbeat", {})["last_progress_at"] = event_at
    task["heartbeat"]["last_heartbeat_at"] = event_at

    if event_type == "STEP_PROGRESS":
        append_observation(task, event_type="STEP_PROGRESS", summary=args.summary, facts=facts, step_id=step_id, status="RUNNING")
        set_step_observation(task, step_id, state="IN_PROGRESS", summary=args.summary, facts=facts, at=event_at)
        task["checkpoints"].append({"at": event_at, "kind": "STEP_PROGRESS", "summary": args.summary, "facts": facts})
        task["status"] = "RUNNING"
    elif event_type == "STEP_COMPLETED":
        append_observation(task, event_type="STEP_COMPLETED", summary=args.summary, facts=facts, step_id=step_id, status="COMPLETED")
        set_step_observation(task, step_id, state="COMPLETED", summary=args.summary, facts=facts, at=event_at)
        task["checkpoints"].append({"at": event_at, "kind": "STEP_COMPLETED", "summary": args.summary, "facts": facts})
        task["status"] = "RUNNING"
        queue_step_update(task, "STEP_COMPLETED", args.summary, step_id, facts=facts, outputs=args.artifact, next_action=args.next_action or task.get("next_action"))
    elif event_type == "TASK_COMPLETED":
        append_observation(task, event_type="TASK_COMPLETED", summary=args.summary, facts=facts, step_id=step_id, status="COMPLETED")
        task.setdefault("observed", {})["task_completion"] = {
            "completed_at": event_at,
            "summary": args.summary,
            "facts": facts,
        }
        task["checkpoints"].append({"at": event_at, "kind": "TASK_COMPLETED", "summary": args.summary, "facts": facts})
        task["status"] = "COMPLETED"
        if args.validation:
            task.setdefault("validation", []).extend(args.validation)
        if args.artifact:
            task.setdefault("artifacts", []).extend(args.artifact)
        mark_terminal_cleanup(task, terminal_status="COMPLETED")
        queue_step_update(task, "TASK_COMPLETED", args.summary, step_id, facts=facts, outputs=args.artifact, next_action=args.next_action or "None")
    else:
        raise SystemExit(f"Unsupported event_type for checkpoint: {event_type}")

    ack_resume_request(task, resume_token=args.resume_token, outcome=event_type, checkpoint=step_id, facts={"summary": args.summary})
    if args.artifact:
        task.setdefault("artifacts", []).extend(args.artifact)
    if args.next_action:
        task["next_action"] = args.next_action
    project_task(task)
    save_ledger(args.ledger, ledger)
    print(f"Recorded {event_type} for {args.task_id}")


def cmd_block(args):
    ledger = load_ledger(args.ledger)
    task = ensure_task(ledger, args.task_id)
    ensure_task_shape(task)
    facts = parse_fact(args.fact)
    if facts.get("failure_type") and facts["failure_type"] not in OBSERVED_FAILURE_TYPES:
        raise SystemExit(f"Unknown observed failure_type: {facts['failure_type']}")
    step_id = args.current_checkpoint or task.get("current_checkpoint")
    summary = args.reason
    blocked_at = now_iso()
    task["current_checkpoint"] = step_id
    task["last_checkpoint_at"] = blocked_at
    task.setdefault("heartbeat", {})["last_progress_at"] = blocked_at
    task["heartbeat"]["last_heartbeat_at"] = blocked_at
    append_observation(task, event_type="BLOCKED_CONFIRMED", summary=summary, facts=facts, step_id=step_id, status="BLOCKED")
    set_step_observation(task, step_id, state="BLOCKED", summary=summary, facts=facts, at=blocked_at)
    task.setdefault("observed", {})["block"] = {
        "blocked_at": blocked_at,
        "summary": summary,
        "facts": facts,
        "step_id": step_id,
        "need": args.need or [],
        "safe_next_step": args.safe_next_step,
    }
    task["blocker"] = {
        "reason": args.reason,
        "need": args.need or [],
        "safe_next_step": args.safe_next_step,
    }
    if facts.get("failure_type"):
        task["blocker"]["failure_type"] = facts["failure_type"]
    task["status"] = "BLOCKED"
    # A confirmed block supersedes any earlier terminal-completed claim.
    # Keep observations/history, but clear the active completed snapshot so derived truth
    # cannot become completed_and_blocked for the same control-plane state.
    task.get("observed", {}).pop("task_completion", None)
    task["checkpoints"].append({"at": now_iso(), "kind": "BLOCKED_CONFIRMED", "summary": summary, "facts": facts})
    ack_resume_request(task, resume_token=args.resume_token, outcome="BLOCKED_CONFIRMED", checkpoint=step_id, facts={"reason": args.reason})
    task["next_action"] = args.next_action or args.safe_next_step
    queue_step_update(task, "BLOCKED_CONFIRMED", summary, step_id, facts=facts, next_action=task.get("next_action"), blocker=task.get("blocker"))
    project_task(task)
    save_ledger(args.ledger, ledger)
    print(f"Blocked {args.task_id}")


def cmd_heartbeat(args):
    ledger = load_ledger(args.ledger)
    task = ensure_task(ledger, args.task_id)
    ensure_task_shape(task)
    hb_at = now_iso()
    task.setdefault("heartbeat", {})["last_heartbeat_at"] = hb_at
    if args.watchdog_state:
        task["heartbeat"]["watchdog_state"] = args.watchdog_state
    if args.note:
        task.setdefault("notes", []).append(args.note)
    append_observation(task, event_type="HEARTBEAT", summary=args.note or "Heartbeat observed", facts={}, step_id=task.get("current_checkpoint"))
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
    ensure_task_shape(task)
    reply_kind = normalize_owner_reply(args.reply)
    if reply_kind not in OWNER_REPLY_CHOICES:
        raise SystemExit(f"Unknown owner reply kind: {args.reply}")
    facts = parse_fact(args.fact)
    if args.message_ref:
        facts["owner_message_ref"] = args.message_ref
    responded_at = now_iso()
    mark_owner_response(task, reply_kind, responded_at)
    append_observation(task, event_type="OWNER_REPLY_RECORDED", summary=args.summary or reply_kind, facts={"owner_reply_kind": reply_kind, **facts}, step_id=args.current_checkpoint or task.get("current_checkpoint"))

    if reply_kind == "A_IN_PROGRESS_FORGOT_LEDGER":
        summary = args.summary or "Owner observed task still in progress; backfilled real progress truth"
        step_id = args.current_checkpoint or task.get("current_checkpoint")
        append_observation(task, event_type="OWNER_RESUMED", summary=summary, facts={"owner_reply_kind": reply_kind, **facts}, step_id=step_id, status="RUNNING")
        set_step_observation(task, step_id, state="IN_PROGRESS", summary=summary, facts=facts)
        task["status"] = "RUNNING"
        task["blocker"] = None
        task.setdefault("observed", {})["block"] = None
        if args.next_action:
            task["next_action"] = args.next_action
        ack_resume_request(task, resume_token=args.resume_token, outcome="OWNER_RESUMED", checkpoint=step_id, facts={"owner_reply_kind": reply_kind})
    elif reply_kind == "B_BLOCKED":
        if not args.reason or not args.safe_next_step:
            raise SystemExit("B_BLOCKED requires --reason and --safe-next-step")
        step_id = args.current_checkpoint or task.get("current_checkpoint")
        summary = args.summary or f"Owner confirmed blocker: {args.reason}"
        append_observation(task, event_type="BLOCKED_CONFIRMED", summary=summary, facts={"owner_reply_kind": reply_kind, **facts}, step_id=step_id, status="BLOCKED")
        set_step_observation(task, step_id, state="BLOCKED", summary=summary, facts=facts)
        task.setdefault("observed", {})["block"] = {
            "blocked_at": responded_at,
            "summary": summary,
            "facts": facts,
            "step_id": step_id,
            "need": args.need or [],
            "safe_next_step": args.safe_next_step,
        }
        task["blocker"] = {
            "reason": args.reason,
            "need": args.need or [],
            "safe_next_step": args.safe_next_step,
        }
        if facts.get("failure_type"):
            task["blocker"]["failure_type"] = facts["failure_type"]
        task["status"] = "BLOCKED"
        task["next_action"] = args.next_action or args.safe_next_step
        ack_resume_request(task, resume_token=args.resume_token, outcome="BLOCKED_CONFIRMED", checkpoint=step_id, facts={"owner_reply_kind": reply_kind})
        queue_step_update(task, "BLOCKED_CONFIRMED", summary, step_id, facts=facts, next_action=task.get("next_action"), blocker=task.get("blocker"))
    elif reply_kind == "C_COMPLETED":
        summary = args.summary or "Owner observed task completion and supplied completion truth"
        step_id = args.current_checkpoint or task.get("current_checkpoint")
        append_observation(task, event_type="TASK_COMPLETED", summary=summary, facts={"owner_reply_kind": reply_kind, **facts}, step_id=step_id, status="COMPLETED")
        task.setdefault("observed", {})["task_completion"] = {"completed_at": responded_at, "summary": summary, "facts": facts}
        task["status"] = "COMPLETED"
        task["blocker"] = None
        task.setdefault("observed", {})["block"] = None
        if args.artifact:
            task.setdefault("artifacts", []).extend(args.artifact)
        if args.validation:
            task.setdefault("validation", []).extend(args.validation)
        task["next_action"] = args.next_action or "None"
        mark_terminal_cleanup(task, terminal_status="COMPLETED")
        ack_resume_request(task, resume_token=args.resume_token, outcome="TASK_COMPLETED", checkpoint=step_id, facts={"owner_reply_kind": reply_kind})
        queue_step_update(task, "TASK_COMPLETED", summary, step_id, facts=facts, outputs=args.artifact, next_action=task.get("next_action"))
    elif reply_kind == "D_NO_REPLY":
        task.setdefault("notes", []).append(args.note or "Owner reply missing; monitor should request external observation instead of inventing task truth")
        if args.next_action:
            task["next_action"] = args.next_action
        ack_resume_request(task, resume_token=args.resume_token, outcome="OWNER_REPLY_RECORDED", checkpoint=args.current_checkpoint or task.get("current_checkpoint"), facts={"owner_reply_kind": reply_kind})
    elif reply_kind == "E_FORGOT_OR_NOT_DOING":
        old_run_id = task.get("active_run_id")
        old_status = task.get("status") or "ABANDONED"
        summary = args.summary or "Owner admitted task was not progressing; a fresh run was started instead of patching the old one"
        task.setdefault("notes", []).append(f"Owner reply {reply_kind} closed {old_run_id} and requested a fresh run")
        archive_active_run(task, terminal_status=old_status if old_status in TASK_STATUSES else "ABANDONED", reason="owner forgot or was not doing the work")
        task["run_counter"] = int(task.get("run_counter", 1) or 1) + 1
        new_run_id = f"run-{task['run_counter']:02d}"
        step_id = args.current_checkpoint or (task.get("workflow") or [{}])[0].get("id")
        reset_active_run(task, run_id=new_run_id, started_at=responded_at, summary=summary, facts={"owner_reply_kind": reply_kind, "resume_required": "true", "rerun_from": old_run_id, **facts}, current_checkpoint=step_id, next_action=args.next_action or "Resume execution immediately and publish observed truth before any further state claims")
        append_observation(task, event_type="OWNER_RESUMED", summary=summary, facts={"owner_reply_kind": reply_kind, "resume_required": "true", "rerun_from": old_run_id, **facts}, step_id=step_id, status="RUNNING")
        task["next_action"] = args.next_action or "Resume execution immediately and publish observed truth before any further state claims"
        ack_resume_request(task, resume_token=args.resume_token, outcome="OWNER_RESUMED", checkpoint=step_id, facts={"owner_reply_kind": reply_kind, "new_run_id": new_run_id})

    project_task(task)
    save_ledger(args.ledger, ledger)
    print(f"Recorded owner reply {reply_kind} for {args.task_id}")


def cmd_rerun(args):
    ledger = load_ledger(args.ledger)
    task = ensure_task(ledger, args.task_id)
    ensure_task_shape(task)
    facts = parse_fact(args.fact)
    rerun_at = now_iso()
    previous = archive_active_run(task, terminal_status=args.previous_status or task.get("status") or "ABANDONED", ended_at=rerun_at, reason=args.reason)
    task["run_counter"] = int(task.get("run_counter", 1) or 1) + 1
    new_run_id = f"run-{task['run_counter']:02d}"
    summary = args.summary or f"Fresh run started after: {args.reason}"
    reset_active_run(task, run_id=new_run_id, started_at=rerun_at, summary=summary, facts={"rerun_from": previous.get("run_id"), **facts}, current_checkpoint=args.current_checkpoint, next_action=args.next_action or task.get("next_action"))
    task.setdefault("notes", []).append(f"{new_run_id} created from {previous.get('run_id')} because: {args.reason}")
    project_task(task)
    save_ledger(args.ledger, ledger)
    print(json.dumps({
        "ok": True,
        "task_id": args.task_id,
        "previous_run": previous.get("run_id"),
        "previous_status": previous.get("status"),
        "active_run_id": new_run_id,
        "reason": args.reason,
        "current_checkpoint": task.get("current_checkpoint"),
        "next_action": task.get("next_action"),
    }, ensure_ascii=False, indent=2))


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
    job = record_external_job_event(task, provider=args.provider, job_id=args.job_id, state=args.state, summary=args.summary, facts=facts, workflow=args.workflow, app=args.app)
    if args.current_checkpoint:
        task["current_checkpoint"] = args.current_checkpoint
    if args.next_action:
        task["next_action"] = args.next_action
    maybe_queue_external_update(task, state=args.state, summary=args.summary, checkpoint=args.current_checkpoint or task.get("current_checkpoint"), facts=facts, next_action=args.next_action or task.get("next_action"))
    project_task(task)
    save_ledger(args.ledger, ledger)
    print(json.dumps({
        "ok": True,
        "task_id": args.task_id,
        "provider": args.provider,
        "job_id": args.job_id,
        "state": args.state,
        "pending_external": job.get("pending_external"),
        "provider_evidence": job.get("provider_evidence", {}),
        "job": job,
    }, ensure_ascii=False, indent=2))


def cmd_download(args):
    ledger = load_ledger(args.ledger)
    task = ensure_task(ledger, args.task_id)
    facts = parse_fact(args.fact)
    observed = record_download_observation(task, summary=args.summary, facts=facts, artifact=args.artifact)
    if args.next_action:
        task["next_action"] = args.next_action
    project_task(task)
    save_ledger(args.ledger, ledger)
    print(json.dumps({"ok": True, "task_id": args.task_id, "download": observed}, ensure_ascii=False, indent=2))


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
    cp_p.add_argument("--event-type", required=True, choices=["STEP_PROGRESS", "STEP_COMPLETED", "TASK_COMPLETED"])
    cp_p.add_argument("--summary", required=True)
    cp_p.add_argument("--current-checkpoint")
    cp_p.add_argument("--next-action")
    cp_p.add_argument("--fact", action="append")
    cp_p.add_argument("--artifact", action="append")
    cp_p.add_argument("--validation", action="append")
    cp_p.add_argument("--resume-token")
    cp_p.set_defaults(func=cmd_checkpoint)

    block_p = sp.add_parser("block")
    block_p.add_argument("task_id")
    block_p.add_argument("--reason", required=True)
    block_p.add_argument("--need", action="append")
    block_p.add_argument("--safe-next-step", required=True)
    block_p.add_argument("--current-checkpoint")
    block_p.add_argument("--next-action")
    block_p.add_argument("--fact", action="append")
    block_p.add_argument("--resume-token")
    block_p.set_defaults(func=cmd_block)

    hb_p = sp.add_parser("heartbeat")
    hb_p.add_argument("task_id")
    hb_p.add_argument("--watchdog-state")
    hb_p.add_argument("--note")
    hb_p.set_defaults(func=cmd_heartbeat)

    sup_p = sp.add_parser("supervisor-update")
    sup_p.add_argument("task_id")
    sup_p.add_argument("--watchdog-state")
    sup_p.add_argument("--monitoring", action="append")
    sup_p.set_defaults(func=cmd_supervisor_update)

    owner_p = sp.add_parser("owner-reply")
    owner_p.add_argument("task_id")
    owner_p.add_argument("--reply", required=True)
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
    owner_p.add_argument("--resume-token")
    owner_p.set_defaults(func=cmd_owner_reply)

    rerun_p = sp.add_parser("rerun")
    rerun_p.add_argument("task_id")
    rerun_p.add_argument("--reason", required=True)
    rerun_p.add_argument("--summary")
    rerun_p.add_argument("--current-checkpoint")
    rerun_p.add_argument("--next-action")
    rerun_p.add_argument("--previous-status", choices=sorted(TASK_STATUSES))
    rerun_p.add_argument("--fact", action="append")
    rerun_p.set_defaults(func=cmd_rerun)

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
    ext_p.add_argument("--failure-type", choices=sorted(OBSERVED_FAILURE_TYPES))
    ext_p.add_argument("--current-checkpoint")
    ext_p.add_argument("--next-action")
    ext_p.add_argument("--fact", action="append")
    ext_p.set_defaults(func=cmd_external_job)

    dl_p = sp.add_parser("download-observed")
    dl_p.add_argument("task_id")
    dl_p.add_argument("--summary", required=True)
    dl_p.add_argument("--artifact")
    dl_p.add_argument("--next-action")
    dl_p.add_argument("--fact", action="append")
    dl_p.set_defaults(func=cmd_download)

    return p


def main():
    parser = build_parser()
    args = parser.parse_args()
    args.func(args)


if __name__ == "__main__":
    main()
