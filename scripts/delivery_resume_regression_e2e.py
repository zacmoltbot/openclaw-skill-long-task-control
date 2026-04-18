#!/usr/bin/env python3
"""
Regression: multi-artifact delivery resume semantics.
"""
from __future__ import annotations

import json
import subprocess
import sys
import tempfile
import unittest.mock
from pathlib import Path

sys.path.insert(0, str(Path(__file__).parent.parent))

from adapters.generic_manual import GenericManualAdapter


def make_ledger(tmpdir: Path) -> tuple[Path, dict]:
    ledger_path = tmpdir / "ledger.json"
    ledger_payload = {
        "tasks": [{
            "task_id": "resume-semantics-test",
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
    return ledger_path, ledger_payload


def make_artifacts(tmpdir: Path, count: int) -> list[str]:
    artifacts = []
    for i in range(count):
        p = tmpdir / f"artifact_{i}.png"
        p.write_bytes(b"\x89PNG\r\n\x1a\n" + f"regression artifact {i}".encode())
        artifacts.append(str(p))
    return artifacts


class DeliverySim:
    call_num = 0
    fail_at_call: int | None = None

    @classmethod
    def reset(cls, fail_at_call: int | None = None):
        cls.call_num = 0
        cls.fail_at_call = fail_at_call

    @classmethod
    def make_proc(cls):
        cls.call_num += 1
        if cls.fail_at_call is not None and cls.call_num == cls.fail_at_call:
            return subprocess.CompletedProcess(args=[], returncode=0, stdout="[plugins] error\n", stderr="")
        return subprocess.CompletedProcess(args=[], returncode=0, stdout='{"ok":true,"messageId":"mid_%d"}' % cls.call_num, stderr="")


class FakeItemState:
    def __init__(self):
        self.facts = {}


def test_retry_resumes_from_next_artifact():
    """After K confirmed + K+1 failed, retry must skip K confirmed artifacts."""
    adapter = GenericManualAdapter()
    with tempfile.TemporaryDirectory() as tmpdir:
        tmp = Path(tmpdir)
        ledger_path, _ = make_ledger(tmp)
        artifacts = make_artifacts(tmp, 3)

        item_state = FakeItemState()
        fake_state = {
            "bridge": {"ledger": str(ledger_path), "task_id": "resume-semantics-test"},
            "adapter_context": {"item_state": item_state},
        }
        fake_item = {
            "generic_manual_mode": "auto_repair",
            "auto_action": "deliver_artifacts",
            "deliver_artifacts": artifacts,
            "deliver_caption": "全部完成！",
        }

        # First attempt: artifact_0 succeeds, artifact_1 fails
        DeliverySim.reset(fail_at_call=2)
        with unittest.mock.patch.object(subprocess, "run",
                side_effect=lambda *a, **kw: DeliverySim.make_proc()):
            result1 = adapter.finalize(fake_item, fake_state)

        assert result1.status == "blocked"
        assert result1.facts["delivered_count"] == 1
        assert "artifact_1" in result1.facts.get("failed_media", "")
        assert "delivery_progress" in item_state.facts

        progress = json.loads(item_state.facts["delivery_progress"])
        confirmed = {k: v for k, v in progress.items() if v != "pending"}
        assert len(confirmed) == 1, f"expected 1 confirmed, got {len(confirmed)}: {confirmed}"

        # Retry: artifact_0 skipped (confirmed), artifact_1 retried and fails again
        DeliverySim.reset(fail_at_call=1)  # first call of retry fails
        send_calls = []
        def track_send(*a, **kw):
            args = a[0] if a else []
            if args and '--media' in args:
                send_calls.append(args)
            return DeliverySim.make_proc()

        with unittest.mock.patch.object(subprocess, "run", side_effect=track_send):
            result2 = adapter.finalize(fake_item, fake_state)

        assert result2.status == "blocked"
        assert len(send_calls) == 1, f"expected 1 send (artifact_1 retry), got {len(send_calls)}"
        assert result2.facts["delivered_count"] == 1, \
            f"total delivered should remain 1, got {result2.facts['delivered_count']}"

        print(json.dumps({
            "test": "retry_resumes_from_next_artifact",
            "status": "PASS",
            "result1_delivered": result1.facts["delivered_count"],
            "result2_send_calls": len(send_calls),
            "result2_total_delivered": result2.facts["delivered_count"],
        }, ensure_ascii=False, indent=2))


def test_retry_allows_subsequent_untried_artifacts():
    """After K confirmed + K+1 failed, retry must also attempt K+2, K+3..."""
    adapter = GenericManualAdapter()
    with tempfile.TemporaryDirectory() as tmpdir:
        tmp = Path(tmpdir)
        ledger_path, _ = make_ledger(tmp)
        artifacts = make_artifacts(tmp, 4)

        item_state = FakeItemState()
        fake_state = {
            "bridge": {"ledger": str(ledger_path), "task_id": "resume-semantics-test"},
            "adapter_context": {"item_state": item_state},
        }
        fake_item = {
            "generic_manual_mode": "auto_repair",
            "auto_action": "deliver_artifacts",
            "deliver_artifacts": artifacts,
            "deliver_caption": "全部完成！",
        }

        # First attempt: artifacts 0,1,2 succeed; artifact_3 fails
        DeliverySim.reset(fail_at_call=4)
        with unittest.mock.patch.object(subprocess, "run",
                side_effect=lambda *a, **kw: DeliverySim.make_proc()):
            result1 = adapter.finalize(fake_item, fake_state)

        assert result1.status == "blocked"
        assert result1.facts["delivered_count"] == 3, \
            f"expected 3 delivered on first attempt, got {result1.facts['delivered_count']}"
        assert "artifact_3" in result1.facts.get("failed_media", "")

        # Retry: all succeed (only artifact_3 needs retry)
        DeliverySim.reset(fail_at_call=None)
        send_calls = []
        def track_send(*a, **kw):
            args = a[0] if a else []
            if args and '--media' in args:
                send_calls.append(args)
            return DeliverySim.make_proc()

        with unittest.mock.patch.object(subprocess, "run", side_effect=track_send):
            result2 = adapter.finalize(fake_item, fake_state)

        assert result2.status == "completed", f"expected completed, got {result2.status}"
        # Only 1 artifact send (artifact_3 retried); artifacts 0-2 were skipped
        assert len(send_calls) == 1, \
            f"retry should make 1 artifact send (artifact_3 retried), got {len(send_calls)}"
        assert result2.facts["delivered_count"] == 4, \
            f"total delivered should be 4, got {result2.facts['delivered_count']}"

        print(json.dumps({
            "test": "retry_allows_subsequent_untried_artifacts",
            "status": "PASS",
            "result1_delivered": result1.facts["delivered_count"],
            "result2_send_calls": len(send_calls),
            "result2_total_delivered": result2.facts["delivered_count"],
        }, ensure_ascii=False, indent=2))


def test_delivery_progress_cleaned_on_completion():
    """On full success, delivery_progress is removed from facts."""
    adapter = GenericManualAdapter()
    with tempfile.TemporaryDirectory() as tmpdir:
        tmp = Path(tmpdir)
        ledger_path, _ = make_ledger(tmp)
        artifacts = make_artifacts(tmp, 2)

        item_state = FakeItemState()
        fake_state = {
            "bridge": {"ledger": str(ledger_path), "task_id": "resume-semantics-test"},
            "adapter_context": {"item_state": item_state},
        }
        fake_item = {
            "generic_manual_mode": "auto_repair",
            "auto_action": "deliver_artifacts",
            "deliver_artifacts": artifacts,
            "deliver_caption": "全部完成！",
        }

        DeliverySim.reset(fail_at_call=None)
        with unittest.mock.patch.object(subprocess, "run",
                side_effect=lambda *a, **kw: DeliverySim.make_proc()):
            result = adapter.finalize(fake_item, fake_state)

        assert result.status == "completed"
        assert "delivery_progress" not in item_state.facts, \
            f"delivery_progress should be cleaned; got {item_state.facts.get('delivery_progress')}"

        print(json.dumps({"test": "delivery_progress_cleaned_on_completion", "status": "PASS"}, ensure_ascii=False, indent=2))


def test_blocked_retriable_not_failed():
    """Transport parse error → BLOCKED (retriable), not FAILED."""
    adapter = GenericManualAdapter()
    with tempfile.TemporaryDirectory() as tmpdir:
        tmp = Path(tmpdir)
        ledger_path, _ = make_ledger(tmp)
        artifacts = make_artifacts(tmp, 1)

        item_state = FakeItemState()
        fake_state = {
            "bridge": {"ledger": str(ledger_path), "task_id": "resume-semantics-test"},
            "adapter_context": {"item_state": item_state},
        }
        fake_item = {
            "generic_manual_mode": "auto_repair",
            "auto_action": "deliver_artifacts",
            "deliver_artifacts": artifacts,
        }

        with unittest.mock.patch.object(subprocess, "run",
                return_value=subprocess.CompletedProcess(args=[], returncode=0, stdout="[plugins] error\n", stderr="")):
            result = adapter.finalize(fake_item, fake_state)

        assert result.status == "blocked", f"expected blocked, got {result.status}"
        assert result.blocked_reason == "DELIVERY_TRANSPORT_FAILURE"
        assert result.facts.get("retriable") is True

        print(json.dumps({
            "test": "blocked_retriable_not_failed",
            "status": "PASS",
            "result_status": result.status,
            "blocked_reason": result.blocked_reason,
            "retriable": result.facts.get("retriable"),
        }, ensure_ascii=False, indent=2))


if __name__ == "__main__":
    print("=" * 60)
    print("delivery_resume_regression: BEGIN")
    print("=" * 60)

    test_retry_resumes_from_next_artifact()
    test_retry_allows_subsequent_untried_artifacts()
    test_delivery_progress_cleaned_on_completion()
    test_blocked_retriable_not_failed()

    print("=" * 60)
    print("ALL PASS")
    print("=" * 60)
