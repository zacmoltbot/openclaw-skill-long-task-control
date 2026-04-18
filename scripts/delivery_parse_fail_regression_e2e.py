#!/usr/bin/env python3
"""
Regression: delivery subprocess non-JSON stdout must NOT leave step in phantom RUNNING.

Issue: generic_manual._send_delivery_payload() called json.loads(proc.stdout) without
error handling.  When subprocess stdout contains plugin noise / mixed output / empty
string, JSONDecodeError bubbles up through _deliver_artifacts -> finalize -> executor,
leaving the step stuck in RUNNING with empty failures[].

Fix:
  1. _send_delivery_payload now catches JSONDecodeError and returns {"ok": False, ...}
     so the delivery failure is returned as a structured AdapterResult.
  2. executor_engine wraps adapter.finalize() in try/except so any unhandled adapter
     exception routes through handle_failed_item and emits proper terminal truth.

This test:
  - Unit: adapter.finalize() returns failed result (not exception) when subprocess
    stdout is non-JSON.
  - E2E: cmd_run_next converges the item to BLOCKED (after retry budget exhausted)
    or RETRY, never left in RUNNING.
"""
from __future__ import annotations

import json
import os
import subprocess
import tempfile
import unittest.mock
from pathlib import Path

import job_models as jm
from adapters.generic_manual import GenericManualAdapter
from executor_engine import cmd_run_next, load_adapter
from job_models import JobState, JobStore, WorkItem


# ---------------------------------------------------------------------------
# Helpers
# ---------------------------------------------------------------------------

def make_delivery_job(job_id: str, *, retry_budget: int = 1) -> tuple[JobState, Path, Path]:
    """
    Build a minimal LTC job whose sole item is a deliver_artifacts step.
    Returns (state, jobs_root, ledger_path).
    """
    jobs_root = Path(tempfile.mkdtemp(prefix="ltc_regression_"))
    ledger_path = jobs_root / "ledger.json"
    ledger_path.parent.mkdir(parents=True, exist_ok=True)

    ledger_payload = {
        "tasks": [{
            "task_id": "test-ltc-delivery-parse-fail",
            "channel": "discord",
            "message": {
                "requester_channel": "1477237887136432162",
                "requester_channel_valid": True,
                "nudge_target": "1477237887136432162",
            },
            "monitoring": {},
        }]
    }
    ledger_path.write_text(json.dumps(ledger_payload, ensure_ascii=False, indent=2) + "\n")

    # Create a real artifact file so delivery reaches the JSON-parse failure
    # (not blocked for missing files)
    artifact_path = Path("/tmp/ltc_regression_artifact.png")
    artifact_path.parent.mkdir(parents=True, exist_ok=True)
    artifact_path.write_bytes(b"\x89PNG\r\n\x1a\n" + b"regression test artifact")

    item = WorkItem(
        item_id="step-01",
        title="Deliver artifacts",
        payload={
            "generic_manual_mode": "auto_repair",
            "auto_action": "deliver_artifacts",
            "deliver_artifacts": [str(artifact_path)],
            "deliver_caption": "test delivery",
            "retry_budget": retry_budget,
        },
        status="PENDING",  # PENDING so executor actually runs finalize (not pre-block via normalize)
        attempts=0,
    )

    state = JobState(
        job_id=job_id,
        kind="long_task_control",
        adapter="generic_manual",
        mode="serial",
        status="RUNNING",
        current_index=0,
        items=[item],
        bridge={"ledger": str(ledger_path), "task_id": "test-ltc-delivery-parse-fail"},
        execution={"last_run_owner": "test"},
    )

    store = JobStore(jobs_root)
    store.save(state)
    return state, jobs_root, ledger_path


class FakeCompletedProc:
    """Subprocess result with non-JSON stdout."""
    returncode = 0
    stdout = "PluginNoiseHere\nSome random output\n"  # NOT valid JSON
    stderr = ""


# ---------------------------------------------------------------------------
# Unit test: adapter.finalize() returns structured failure, not exception
# ---------------------------------------------------------------------------

def test_adapter_finalize_returns_structured_failure():
    """
    When _send_delivery_payload receives non-JSON stdout from the subprocess,
    finalize() must return an AdapterResult with status='failed' (not raise).
    It must NOT leave the item in phantom RUNNING.
    """
    adapter = GenericManualAdapter()

    fake_state = {
        "bridge": {
            "ledger": str(Path(tempfile.mktemp(suffix=".json"))),
            "task_id": "fake-task",
        },
    }
    # Write a minimal fake ledger so _bridge_task_context doesn't return None
    fake_ledger = Path(fake_state["bridge"]["ledger"])
    fake_ledger.parent.mkdir(parents=True, exist_ok=True)
    fake_ledger.write_text(json.dumps({
        "tasks": [{
            "task_id": "fake-task",
            "channel": "discord",
            "message": {
                "requester_channel": "1477237887136432162",
                "requester_channel_valid": True,
                "nudge_target": "1477237887136432162",
            },
        }]
    }) + "\n")

    # Create a real (empty) file so delivery code tries to send and hits the
    # JSON-parse-failure path, rather than returning BLOCKED for missing files.
    fake_artifact_path = Path("/tmp/fake_artifact.png")
    fake_artifact_path.parent.mkdir(parents=True, exist_ok=True)
    fake_artifact_path.write_bytes(b"\x89PNG\r\n\x1a\n" + b"fake png content")

    fake_item = {
        "generic_manual_mode": "auto_repair",
        "auto_action": "deliver_artifacts",
        "deliver_artifacts": [str(fake_artifact_path)],
        "deliver_caption": "test",
        "message": "LTC test delivery",
    }

    # Patch subprocess.run so it returns non-JSON stdout
    with unittest.mock.patch.object(subprocess, "run", return_value=FakeCompletedProc()):
        result = adapter.finalize(fake_item, fake_state)

    # Must NOT be an exception
    assert not isinstance(result, Exception), \
        f"finalize() must not raise; got {type(result).__name__}: {result}"

    # Must be a structured failed result (not blocked, which would mean missing files)
    assert result.status == "failed", \
        f"expected status='failed' (JSON parse error), got status='{result.status}': {result.summary}"

    # The facts must contain the parse error details
    assert "error" in result.facts or any("parse" in str(v).lower() for v in result.facts.values()), \
        f"facts should contain parse error; got {result.facts}"

    print(json.dumps({
        "test": "adapter_finalize_returns_structured_failure",
        "status": "PASS",
        "result_status": result.status,
        "result_summary": result.summary,
        "result_facts": result.facts,
    }, ensure_ascii=False, indent=2))


# ---------------------------------------------------------------------------
# Unit test: adapter finalizer records failure reason when delivery fails
# ---------------------------------------------------------------------------

def test_adapter_finalize_failure_includes_parse_details():
    """
    Verify the failed AdapterResult includes useful debugging facts:
    - error field mentions 'parse failed'
    - raw_output_preview is captured
    """
    adapter = GenericManualAdapter()

    fake_ledger_path = Path(tempfile.mktemp(suffix=".json"))
    fake_ledger_path.parent.mkdir(parents=True, exist_ok=True)
    fake_ledger_path.write_text(json.dumps({
        "tasks": [{
            "task_id": "fake-task-2",
            "channel": "discord",
            "message": {
                "requester_channel": "1477237887136432162",
                "requester_channel_valid": True,
                "nudge_target": "1477237887136432162",
            },
        }]
    }) + "\n")

    fake_state = {
        "bridge": {
            "ledger": str(fake_ledger_path),
            "task_id": "fake-task-2",
        },
    }

    # Create a real file so delivery reaches the JSON-parse failure path
    fake_artifact_path2 = Path("/tmp/fake_artifact2.png")
    fake_artifact_path2.parent.mkdir(parents=True, exist_ok=True)
    fake_artifact_path2.write_bytes(b"\x89PNG\r\n\x1a\n" + b"fake png content 2")

    fake_item = {
        "generic_manual_mode": "auto_repair",
        "auto_action": "deliver_artifacts",
        "deliver_artifacts": [str(fake_artifact_path2)],
        "deliver_caption": "test 2",
    }

    with unittest.mock.patch.object(subprocess, "run", return_value=FakeCompletedProc()):
        result = adapter.finalize(fake_item, fake_state)

    assert result.status == "failed", f"expected failed (JSON parse), got {result.status}: {result.summary}"
    facts_str = json.dumps(result.facts)
    assert "parse" in facts_str.lower() or "json" in facts_str.lower(), \
        f"facts should mention parse/json error; got: {result.facts}"

    print(json.dumps({
        "test": "adapter_finalize_failure_includes_parse_details",
        "status": "PASS",
        "facts": result.facts,
    }, ensure_ascii=False, indent=2))


# ---------------------------------------------------------------------------
# E2E test: executor converges to terminal BLOCKED after retry budget exhausted
# ---------------------------------------------------------------------------

def test_executor_converges_to_blocked_after_delivery_parse_failure():
    """
    End-to-end: with retry_budget=0 (exhausted), cmd_run_next must emit ITEM_BLOCKED
    (not leave the item in RUNNING).  Verify progress ledger contains BLOCKED truth.
    """
    job_id = "eko-ltc-delivery-parse-fail-regression"
    state, jobs_root, ledger_path = make_delivery_job(job_id, retry_budget=0)

    import argparse
    args = argparse.Namespace(
        job_id=job_id,
        jobs_root=str(jobs_root),
        execution_owner="regression_test",
    )

    # Patch subprocess.run so delivery returns non-JSON stdout
    with unittest.mock.patch.object(subprocess, "run", return_value=FakeCompletedProc()):
        import io, sys
        old_stdout = sys.stdout
        old_stderr = sys.stderr
        sys.stdout = io.StringIO()
        sys.stderr = io.StringIO()
        try:
            cmd_run_next(args)
        finally:
            sys.stdout = old_stdout
            sys.stderr = old_stderr

    # Reload state from disk
    store = JobStore(jobs_root)
    loaded = store.load(job_id)
    item = loaded.items[0]

    # MUST NOT be RUNNING — that's the bug we're fixing
    assert item.status != "RUNNING", \
        f"BUG: item is still RUNNING after delivery failure! status={item.status}"

    # Must be BLOCKED (retry budget exhausted) or FAILED
    assert item.status in ("BLOCKED", "FAILED"), \
        f"expected BLOCKED or FAILED, got status='{item.status}'"

    # Failures list must be populated (not empty)
    assert len(item.failures) > 0, \
        f"failures[] must not be empty; got {item.failures}"

    # Check progress ledger contains terminal event
    progress_file = store.progress_file(job_id)
    progress_events = [json.loads(line) for line in progress_file.read_text().strip().split("\n") if line.strip()]
    terminal_kinds = {e["kind"] for e in progress_events}
    assert "ITEM_BLOCKED" in terminal_kinds or "ITEM_FAILED" in terminal_kinds, \
        f"progress must contain ITEM_BLOCKED or ITEM_FAILED; got {terminal_kinds}"

    print(json.dumps({
        "test": "executor_converges_to_blocked_after_delivery_parse_failure",
        "status": "PASS",
        "item_status": item.status,
        "item_failures": [{"code": f.code, "summary": f.summary} for f in item.failures],
        "progress_kinds": list(terminal_kinds),
        "job_status": loaded.status,
    }, ensure_ascii=False, indent=2))


# ---------------------------------------------------------------------------
# E2E test: executor retries when retry budget remains
# ---------------------------------------------------------------------------

def test_executor_retries_when_budget_remains():
    """
    With retry_budget=2 and first attempt fails with parse error,
    executor must schedule RETRY (not BLOCKED), allowing a second attempt.
    """
    job_id = "eko-ltc-delivery-parse-fail-retry"
    state, jobs_root, ledger_path = make_delivery_job(job_id, retry_budget=2)

    import argparse
    args = argparse.Namespace(
        job_id=job_id,
        jobs_root=str(jobs_root),
        execution_owner="regression_test",
    )

    with unittest.mock.patch.object(subprocess, "run", return_value=FakeCompletedProc()):
        import io, sys
        old_stdout = sys.stdout
        old_stderr = sys.stderr
        captured = io.StringIO()
        sys.stdout = captured
        sys.stderr = io.StringIO()
        try:
            cmd_run_next(args)
        finally:
            sys.stdout = old_stdout
            sys.stderr = old_stderr

    # Reload state
    store = JobStore(jobs_root)
    loaded = store.load(job_id)
    item = loaded.items[0]

    # Must NOT be RUNNING
    assert item.status != "RUNNING", \
        f"BUG: item is still RUNNING; status={item.status}"

    # With budget remaining, should be RETRY
    assert item.status == "RETRY", \
        f"expected status='RETRY' (budget=2), got '{item.status}'"

    # Failures must be recorded
    assert len(item.failures) > 0, f"failures[] must not be empty; got {item.failures}"

    # Progress should contain ITEM_RETRY_SCHEDULED
    progress_file = store.progress_file(job_id)
    progress_events = [json.loads(line) for line in progress_file.read_text().strip().split("\n") if line.strip()]
    terminal_kinds = {e["kind"] for e in progress_events}
    assert "ITEM_RETRY_SCHEDULED" in terminal_kinds, \
        f"progress must contain ITEM_RETRY_SCHEDULED; got {terminal_kinds}"

    print(json.dumps({
        "test": "executor_retries_when_budget_remains",
        "status": "PASS",
        "item_status": item.status,
        "item_failures": [{"code": f.code, "summary": f.summary} for f in item.failures],
        "progress_kinds": list(terminal_kinds),
    }, ensure_ascii=False, indent=2))


# ---------------------------------------------------------------------------
# Main
# ---------------------------------------------------------------------------

if __name__ == "__main__":
    print("=" * 60)
    print("delivery_parse_fail_regression: BEGIN")
    print("=" * 60)

    test_adapter_finalize_returns_structured_failure()
    test_adapter_finalize_failure_includes_parse_details()
    test_executor_converges_to_blocked_after_delivery_parse_failure()
    test_executor_retries_when_budget_remains()

    print("=" * 60)
    print("ALL PASS")
    print("=" * 60)
