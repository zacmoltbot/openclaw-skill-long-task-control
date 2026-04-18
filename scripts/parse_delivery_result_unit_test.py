#!/usr/bin/env python3
"""
Unit tests: parse_delivery_result (openclaw_ops.py)

Tests fail-closed delivery result parsing against noisy/mixed subprocess output.

Coverage:
  1. plugin noise prefix + trailing JSON → ok (no false failure)
  2. multiple lines, valid JSON not on last line → ok (reverse-scan finds it)
  3. pure valid JSON → ok
  4. empty stdout on zero exit → fail-closed
  5. nonzero exit + Discord error JSON in stderr → fail with error message
  6. nonzero exit + plain stderr → fail with stderr message
  7. nonzero exit + empty stderr → fail with exit code info
  8. stdout has lines but none are JSON → fail-closed
  9. JSON has no messageId/id → ok=True but message_ref=None
"""
from __future__ import annotations

import json
import subprocess
import sys
import unittest
import unittest.mock

ROOT = ROOT = __file__.rsplit("/", 2)[0]
sys.path.insert(0, str(ROOT))
from openclaw_ops import parse_delivery_result


def make_proc(*, returncode=0, stdout="", stderr=""):
    p = subprocess.CompletedProcess(args=[], returncode=returncode, stdout=stdout, stderr=stderr)
    return p


class TestParseDeliveryResult(unittest.TestCase):
    # ── Happy path: valid JSON found (possibly behind noise) ──────────────

    def test_plugin_noise_then_json(self):
        """Plugin noise prefix followed by valid JSON → ok."""
        proc = make_proc(
            returncode=0,
            stdout="[plugins] Loading discord plugin...\n[plugins] discord loaded.\n{\"ok\":true,\"messageId\":\"123456\"}",
            stderr="",
        )
        result = parse_delivery_result(proc)
        assert result["ok"] is True, result
        assert result["message_ref"] == "123456", result

    def test_multiple_lines_json_not_last(self):
        """Valid JSON not on last line (reverse-scan catches it)."""
        proc = make_proc(
            returncode=0,
            stdout="Loading...\nProcessing file 1 of 3\n{\"ok\":true,\"id\":\"mid_789\"}",
            stderr="",
        )
        result = parse_delivery_result(proc)
        assert result["ok"] is True, result
        assert result["message_ref"] == "mid_789", result

    def test_pure_json(self):
        """Plain JSON without noise → ok."""
        proc = make_proc(returncode=0, stdout='{"ok":true,"messageId":"999"}', stderr="")
        result = parse_delivery_result(proc)
        assert result["ok"] is True, result
        assert result["message_ref"] == "999", result

    def test_json_no_message_id_field(self):
        """JSON valid but has no messageId/id → ok=True, message_ref=None (not an error)."""
        proc = make_proc(returncode=0, stdout='{"ok":true,"other":"field"}', stderr="")
        result = parse_delivery_result(proc)
        assert result["ok"] is True, result
        assert result["message_ref"] is None, result

    # ── Fail-closed: cannot confirm success → fail ─────────────────────────

    def test_empty_stdout_zero_exit(self):
        """Zero exit but empty stdout → fail-closed (not silent success)."""
        proc = make_proc(returncode=0, stdout="", stderr="")
        result = parse_delivery_result(proc)
        assert result["ok"] is False, result
        assert "empty stdout" in result["error"], result

    def test_no_json_in_stdout(self):
        """Stdout has content but no valid JSON → fail-closed."""
        proc = make_proc(returncode=0, stdout="[plugins] something went wrong\nError: plugin exception\n", stderr="")
        result = parse_delivery_result(proc)
        assert result["ok"] is False, result
        assert "no valid JSON" in result["error"], result

    def test_whitespace_only_stdout(self):
        """Whitespace-only stdout → fail-closed."""
        proc = make_proc(returncode=0, stdout="   \n  \n  ", stderr="")
        result = parse_delivery_result(proc)
        assert result["ok"] is False, result

    # ── Nonzero exit → structured failure ──────────────────────────────────

    def test_nonzero_with_discord_error_json_in_stderr(self):
        """Nonzero exit + Discord error format JSON in stderr → fail with error message."""
        proc = make_proc(
            returncode=1,
            stdout="",
            stderr='{"message": "Cannot send message to non-existent channel", "code": 10003}',
        )
        result = parse_delivery_result(proc)
        assert result["ok"] is False, result
        assert "non-existent channel" in result["error"], result

    def test_nonzero_with_plain_stderr(self):
        """Nonzero exit + plain text stderr → fail with stderr content."""
        proc = make_proc(returncode=1, stdout="", stderr="openclaw: target not found")
        result = parse_delivery_result(proc)
        assert result["ok"] is False, result
        assert "target not found" in result["error"], result

    def test_nonzero_empty_stderr(self):
        """Nonzero exit + empty stderr → fail with exit code info."""
        proc = make_proc(returncode=2, stdout="something", stderr="")
        result = parse_delivery_result(proc)
        assert result["ok"] is False, result
        assert result["returncode"] == 2, result


def run():
    suite = unittest.TestLoader().loadTestsFromTestCase(TestParseDeliveryResult)
    runner = unittest.TextTestRunner(verbosity=2)
    result = runner.run(suite)
    return 0 if result.wasSuccessful() else 1


if __name__ == "__main__":
    sys.exit(run())
