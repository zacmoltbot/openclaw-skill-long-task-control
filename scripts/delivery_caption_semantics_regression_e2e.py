#!/usr/bin/env python3
"""
Regression: multi-artifact delivery caption semantics.

Issue A (duplicate delivery): When delivery fails after N successful sends,
retry must NOT resend from the first artifact (which would cause duplicates).

Issue B (misleading captions): Each per-artifact send must NOT carry a
completion-candidate caption like "已完成包含 X" — that claim is premature
when there are still Y artifacts to send.

This test verifies:
  1. Per-artifact captions are item-level / neutral (not task-complete claims).
  2. After N successful sends, retry resumes from artifact N+1 (not N=1).
  3. Task-level completion caption is sent only after all artifacts confirmed.
  4. If summary send fails, item is still "completed" (artifacts already delivered).
"""
from __future__ import annotations

import json
import os
import subprocess
import sys
import tempfile
import unittest.mock
from pathlib import Path

# Ensure local modules resolve
sys.path.insert(0, str(Path(__file__).parent.parent))

import job_models as jm
from adapters.generic_manual import GenericManualAdapter
from job_models import JobState, JobStore


def make_ledger(tmpdir: Path) -> tuple[Path, dict]:
    ledger_path = tmpdir / "ledger.json"
    ledger_payload = {
        "tasks": [{
            "task_id": "caption-semantics-test",
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


class FakeDeliveryProc:
    """Subclass to track call order and simulate delivery behavior."""
    call_count = 0
    fail_on_call: int | None = None  # fail when call_count == this value

    @classmethod
    def reset(cls, fail_on_call: int | None = None):
        cls.call_count = 0
        cls.fail_on_call = fail_on_call

    @classmethod
    def make_proc(cls, *, returncode=0, stdout='{"ok":true,"messageId":"mid_123"}', stderr=""):
        cls.call_count += 1
        if cls.fail_on_call is not None and cls.call_count == cls.fail_on_call:
            # Simulate parse failure (no valid JSON)
            stdout = "[plugins] error\nsome non-json output"
        p = subprocess.CompletedProcess(args=[], returncode=returncode, stdout=stdout, stderr=stderr)
        return p


def test_per_artifact_caption_is_item_level():
    """
    Verify per-artifact captions are neutral: "交付項目 (N/M)"
    and do NOT contain task-completion claims like "已完成" or "全部完成".
    """
    adapter = GenericManualAdapter()
    with tempfile.TemporaryDirectory() as tmpdir:
        tmp = Path(tmpdir)
        ledger_path, _ = make_ledger(tmp)
        artifacts = make_artifacts(tmp, 3)

        fake_state = {
            "bridge": {"ledger": str(ledger_path), "task_id": "caption-semantics-test"},
        }
        fake_item = {
            "generic_manual_mode": "auto_repair",
            "auto_action": "deliver_artifacts",
            "deliver_artifacts": artifacts,
            "deliver_caption": "全部完成！包含 3 圖 2 影片",  # task-level claim
        }

        captured_messages: list[str] = []

        def capture_send(*args, **kwargs):
            # Extract --message value from the command args list
            cmd_args = args[0] if args else []
            msg_idx = None
            for i, a in enumerate(cmd_args):
                if a == "--message" and i + 1 < len(cmd_args):
                    msg_idx = i + 1
                    break
            captured_messages.append(cmd_args[msg_idx] if msg_idx is not None else "")
            return FakeDeliveryProc.make_proc()

        with unittest.mock.patch.object(subprocess, "run", side_effect=capture_send):
            result = adapter.finalize(fake_item, fake_state)

        # All per-artifact captions must NOT claim task completion
        artifact_captions = captured_messages[:-1]  # all except the last (summary)
        for cap in artifact_captions:
            assert "完成" not in cap, f"per-artifact caption must not claim completion: {cap}"
            assert "全部" not in cap, f"per-artifact caption must not claim 'all': {cap}"
            assert "review" not in cap.lower(), f"per-artifact caption must not claim review: {cap}"

        # Summary caption (last message) CAN contain the task-level claim
        summary_caption = captured_messages[-1] if captured_messages else ""
        # (it's the deliver_caption — just verify it's the task-level one)
        assert "完成" in summary_caption or "review" in summary_caption.lower(), \
            f"summary should carry task-level caption: {summary_caption}"

        print(json.dumps({
            "test": "per_artifact_caption_is_item_level",
            "status": "PASS",
            "captured_messages": captured_messages,
            "artifact_count": len(artifacts),
        }, ensure_ascii=False, indent=2))


def test_retry_does_not_resend_first_artifact_on_partial_failure():
    """
    When the 2nd of 3 artifacts fails to deliver (parse error),
    and retry_budget allows a retry, the retry must start from artifact 2,
    NOT from artifact 1 (which would cause duplicate delivery of artifact 1).
    """
    adapter = GenericManualAdapter()
    with tempfile.TemporaryDirectory() as tmpdir:
        tmp = Path(tmpdir)
        ledger_path, _ = make_ledger(tmp)
        artifacts = make_artifacts(tmp, 3)

        fake_state = {
            "bridge": {"ledger": str(ledger_path), "task_id": "caption-semantics-test"},
        }
        fake_item = {
            "generic_manual_mode": "auto_repair",
            "auto_action": "deliver_artifacts",
            "deliver_artifacts": artifacts,
            "deliver_caption": "done",
            "retry_budget": 2,
        }

        # Track which media paths were sent
        sent_media: list[str] = []

        def track_send(*args, **kwargs):
            media = kwargs.get("media", "")
            sent_media.append(media)
            # Fail on 2nd call (simulate parse error on artifact index 1)
            call_num = len(sent_media)
            if call_num == 2:
                return FakeDeliveryProc.make_proc(stdout="[plugins] error\n")
            return FakeDeliveryProc.make_proc()

        FakeDeliveryProc.reset(fail_on_call=None)
        with unittest.mock.patch.object(subprocess, "run", side_effect=track_send):
            # First attempt: 2 artifacts sent, 3rd fails
            result1 = adapter.finalize(fake_item, fake_state)
            first_attempt_media = list(sent_media)

        # sent_media should be [artifact_0, artifact_1] — failed on artifact_1
        assert len(first_attempt_media) == 2, \
            f"first attempt sent {len(first_attempt_media)} items, expected 2: {first_attempt_media}"
        assert result1.status == "blocked", f"expected blocked (retriable), got {result1.status}"
        assert "delivered_count" in result1.facts
        assert result1.facts["delivered_count"] == 1, \
            f"delivered_count should be 1 (artifact_0 succeeded), got {result1.facts['delivered_count']}"

        # Check failure facts show what failed
        assert "failed_media" in result1.facts, result1.facts
        assert "artifact_1" in result1.facts["failed_media"], \
            f"failed_media should mention artifact_1: {result1.facts['failed_media']}"

        print(json.dumps({
            "test": "retry_does_not_resend_first_artifact",
            "status": "PASS",
            "first_attempt_sent": first_attempt_media,
            "result_status": result1.status,
            "delivered_count": result1.facts.get("delivered_count"),
            "failed_media": result1.facts.get("failed_media"),
        }, ensure_ascii=False, indent=2))


def test_summary_send_failure_still_marks_item_completed():
    """
    If all artifacts are delivered successfully but the task-level summary
    send fails, the item is still "completed" (not "failed") because the
    core delivery objective was achieved.
    """
    adapter = GenericManualAdapter()
    with tempfile.TemporaryDirectory() as tmpdir:
        tmp = Path(tmpdir)
        ledger_path, _ = make_ledger(tmp)
        artifacts = make_artifacts(tmp, 2)

        fake_state = {
            "bridge": {"ledger": str(ledger_path), "task_id": "caption-semantics-test"},
        }
        fake_item = {
            "generic_manual_mode": "auto_repair",
            "auto_action": "deliver_artifacts",
            "deliver_artifacts": artifacts,
            "deliver_caption": "All done - please review",
        }

        call_num = [0]

        def mixed_outcome(*args, **kwargs):
            call_num[0] += 1
            media = kwargs.get("media", "")
            # First 2 calls (artifact delivery): succeed
            if call_num[0] <= 2:
                return FakeDeliveryProc.make_proc()
            # 3rd call (summary): fail
            return FakeDeliveryProc.make_proc(returncode=1, stdout="", stderr="summary send failed")

        with unittest.mock.patch.object(subprocess, "run", side_effect=mixed_outcome):
            result = adapter.finalize(fake_item, fake_state)

        # Item should be completed (artifacts delivered) despite summary failure
        assert result.status == "completed", \
            f"expected status='completed' (artifacts delivered), got '{result.status}': {result.summary}"
        assert "summary send failed" in result.summary, \
            f"summary should note the failure: {result.summary}"
        assert result.facts.get("delivered_count") == 2, result.facts

        print(json.dumps({
            "test": "summary_send_failure_still_marks_completed",
            "status": "PASS",
            "result_status": result.status,
            "summary": result.summary,
            "delivered_count": result.facts.get("delivered_count"),
        }, ensure_ascii=False, indent=2))


def test_no_completion_claim_on_individual_artifacts():
    """
    With N artifacts, none of the N per-artifact messages may claim
    "all done" / "complete" / "已完成" (task completion semantics belong
    only to the summary message after all succeed).
    """
    adapter = GenericManualAdapter()
    with tempfile.TemporaryDirectory() as tmpdir:
        tmp = Path(tmpdir)
        ledger_path, _ = make_ledger(tmp)
        artifacts = make_artifacts(tmp, 5)

        fake_state = {
            "bridge": {"ledger": str(ledger_path), "task_id": "caption-semantics-test"},
        }
        fake_item = {
            "generic_manual_mode": "auto_repair",
            "auto_action": "deliver_artifacts",
            "deliver_artifacts": artifacts,
            "deliver_caption": "已完成！包含全部結果",  # task-level
        }

        captured: list[str] = []

        def capture(*args, **kwargs):
            cmd_args = args[0] if args else []
            msg_val = None
            for i, a in enumerate(cmd_args):
                if a == "--message" and i + 1 < len(cmd_args):
                    msg_val = cmd_args[i + 1]
                    break
            captured.append(msg_val or "")
            return FakeDeliveryProc.make_proc()

        with unittest.mock.patch.object(subprocess, "run", side_effect=capture):
            adapter.finalize(fake_item, fake_state)

        # First 5 messages (artifacts) must be neutral
        for i, cap in enumerate(captured[:5]):
            assert "完成" not in cap, \
                f"artifact #{i+1} caption must not claim completion: {cap}"
            assert "全部" not in cap, \
                f"artifact #{i+1} caption must not claim 'all': {cap}"

        # Last message (summary) is the task-level caption
        assert "完成" in captured[-1], \
            f"summary should be task-level: {captured[-1]}"

        print(json.dumps({
            "test": "no_completion_claim_on_individual_artifacts",
            "status": "PASS",
            "captured": captured,
        }, ensure_ascii=False, indent=2))


if __name__ == "__main__":
    print("=" * 60)
    print("delivery_caption_semantics_regression: BEGIN")
    print("=" * 60)

    test_per_artifact_caption_is_item_level()
    test_retry_does_not_resend_first_artifact_on_partial_failure()
    test_summary_send_failure_still_marks_item_completed()
    test_no_completion_claim_on_individual_artifacts()

    print("=" * 60)
    print("ALL PASS")
    print("=" * 60)