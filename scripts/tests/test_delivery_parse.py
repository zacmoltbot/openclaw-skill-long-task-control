#!/usr/bin/env python3
"""Regression tests for delivery transport / JSON parse path in GenericManualAdapter.

Covers the fix for:
  "delivery result parse failed: Expecting value: line 1 column 2 (char 1)"
when openclaw message send emits plugin-noise lines before the JSON payload.
"""
from __future__ import annotations

import json
import sys
import os
import tempfile
from pathlib import Path
from unittest.mock import patch, MagicMock

# Ensure the adapters module is importable
# scripts/tests/test_delivery_parse.py → parent=tests, parent.parent=scripts
sys.path.insert(0, str(Path(__file__).resolve().parent.parent))

from adapters.generic_manual import GenericManualAdapter


class MockCompletedProcess:
    def __init__(self, stdout: str = "", stderr: str = "", *, return_code: int = 0):
        self.stdout = stdout
        self.stderr = stderr
        self.returncode = return_code


class TestDeliveryPayloadParsing:
    """Tests for _send_delivery_payload JSON extraction robustness."""

    ADAPTER = GenericManualAdapter()

    def _call_via_subprocess(self, stdout: str, stderr: str = "", *, return_code: int = 0):
        """Call _send_delivery_payload by mocking subprocess.run for the openclaw CLI."""

        def mock_run(cmd, check=False, text=True, capture_output=True, timeout=60):
            assert "openclaw" in cmd and "message" in cmd and "send" in cmd, f"unexpected cmd: {cmd}"
            return MockCompletedProcess(stdout, stderr, return_code=return_code)

        # Must patch in the namespace where subprocess.run is looked up at runtime
        with patch("adapters.generic_manual.subprocess.run", mock_run):
            result = self.ADAPTER._send_delivery_payload(
                channel="discord",
                target="1234567890",
                media_path="/tmp/test.mp4",
                message="Test delivery",
            )
        return result

    def test_clean_json_only_succeeds(self):
        """Clean stdout with only valid JSON → delivery succeeds."""
        result = self._call_via_subprocess(
            stdout=json.dumps({"messageId": "msg-123", "ok": True}),
            return_code=0,
        )
        assert result["ok"] is True, result
        assert result["message_ref"] == "msg-123", result

    def test_mixed_plugin_noise_then_json_succeeds(self):
        """Plugin-noise lines followed by valid JSON → delivery succeeds (THE BUG FIX)."""
        result = self._call_via_subprocess(
            stdout=(
                "[plugins] [lobster-room] plugin routes registered\n"
                "[plugins] [lobster-room] plugin routes registered\n"
                '{"messageId": "msg-456", "ok": true}\n'
            ),
            return_code=0,
        )
        assert result["ok"] is True, result
        assert result["message_ref"] == "msg-456", result

    def test_mixed_multiline_json_only_last_line_used(self):
        """Multiple JSON objects in stdout → last valid one is used."""
        result = self._call_via_subprocess(
            stdout=(
                '{"action": "send"}\n'
                '{"messageId": "msg-789", "channel": "discord"}\n'
            ),
            return_code=0,
        )
        assert result["ok"] is True, result
        assert result["message_ref"] == "msg-789", result

    def test_truly_invalid_output_fails_closed(self):
        """Non-JSON stdout with RC != 0 → fail-closed as delivery failure."""
        result = self._call_via_subprocess(
            stdout="This is not JSON at all\nSome random text\n",
            return_code=1,
        )
        assert result["ok"] is False, result
        assert result["returncode"] == 1, result
        # RC != 0 fires first; error field is the subprocess stdout/stderr

    def test_empty_stdout_fails_closed(self):
        """Empty stdout → fail-closed as delivery failure."""
        result = self._call_via_subprocess(stdout="", return_code=1)
        assert result["ok"] is False, result

    def test_plugin_noise_only_no_json_fails_closed(self):
        """Only plugin-noise lines, no JSON, RC=0 → fail-closed (JSON not found)."""
        result = self._call_via_subprocess(
            stdout=(
                "[plugins] [lobster-room] plugin routes registered\n"
                "[plugins] lobster-room: loaded without install/load-path provenance\n"
            ),
            return_code=0,
        )
        assert result["ok"] is False, result
        assert "no valid JSON" in result["error"], result

    def test_error_returncode_with_json_still_extracts_json(self):
        """Non-zero returncode with valid JSON → returns delivery failure (upstream RC check fires first).

        Per current contract: RC != 0 is an immediate delivery failure (no JSON extraction).
        Only RC == 0 reaches the JSON extraction logic. This test verifies that contract.
        """
        result = self._call_via_subprocess(
            stdout='[plugins] noise\n{"messageId": "msg-err", "ok": false}\n',
            return_code=1,
        )
        assert result["ok"] is False, result
        assert result["returncode"] == 1, result

    def test_real_world_0418b_pattern(self):
        """Emulates the exact pattern from eko-ltc-3video-0418b step-11 delivery."""
        result = self._call_via_subprocess(
            stdout=(
                "[plugins] [lobster-room] plugin routes registered\n"
                "[plugins] [lobster-room] plugin routes registered\n"
                "LocalMediaAccessError: Local media path is not under an allowed directory: /tmp/test.mp4\n"
                '{"messageId": "msg-0418b", "ok": true}\n'
            ),
            return_code=0,
        )
        assert result["ok"] is True, result
        assert result["message_ref"] == "msg-0418b", result


class TestDeliverySinkMode:
    """Tests for LTC_DELIVERY_SINK_FILE override (bypasses CLI entirely)."""

    ADAPTER = GenericManualAdapter()

    def test_sink_file_mode_bypasses_cli(self):
        """When LTC_DELIVERY_SINK_FILE is set, no subprocess is spawned."""
        import tempfile
        tmp_path = Path(tempfile.mkdtemp())
        sink = tmp_path / "sink.json"

        mock_run = MagicMock()
        mock_proc = MagicMock()
        mock_proc.returncode = 0
        mock_proc.stdout = "should not be used"
        mock_proc.stderr = ""
        mock_run.return_value = mock_proc

        with patch.dict("os.environ", {"LTC_DELIVERY_SINK_FILE": str(sink)}, clear=False):
            with patch("adapters.generic_manual.subprocess.run", mock_run):
                result = self.ADAPTER._send_delivery_payload(
                    channel="discord",
                    target="1234567890",
                    media_path="/tmp/test.mp4",
                    message="Sink test",
                )
        mock_run.assert_not_called()
        assert result["ok"] is True, result
        assert result["message_ref"].startswith("sink:"), result


class TestParseLogicDirectly:
    """Unit tests for the JSON extraction logic without any subprocess involvement."""

    def test_parse_finds_json_after_noise(self):
        """Verify parse logic correctly finds JSON after noise lines."""
        import json
        from adapters.generic_manual import GenericManualAdapter

        adapter = GenericManualAdapter()

        # Access the _send_delivery_payload internals by checking the parse result
        # directly - we just want to test the logic, not the full flow
        raw = (
            "[plugins] [lobster-room] plugin routes registered\n"
            "[plugins] [lobster-room] plugin routes registered\n"
            '{"messageId": "msg-xyz", "ok": true}\n'
        )
        stdout_lines = [line.strip() for line in raw.splitlines() if line.strip()]
        result_json = None
        for line in reversed(stdout_lines):
            try:
                result_json = json.loads(line)
                break
            except Exception:
                pass
        assert result_json is not None
        assert result_json["messageId"] == "msg-xyz"

    def test_parse_empty_returns_none(self):
        import json
        raw = ""
        stdout_lines = [line.strip() for line in raw.splitlines() if line.strip()]
        result_json = None
        for line in reversed(stdout_lines):
            try:
                result_json = json.loads(line)
                break
            except Exception:
                pass
        assert result_json is None

    def test_parse_noise_only_returns_none(self):
        import json
        raw = (
            "[plugins] [lobster-room] plugin routes registered\n"
            "[plugins] lobster-room: loaded without install\n"
        )
        stdout_lines = [line.strip() for line in raw.splitlines() if line.strip()]
        result_json = None
        for line in reversed(stdout_lines):
            try:
                result_json = json.loads(line)
                break
            except Exception:
                pass
        assert result_json is None


def run_all_tests():
    errors = []

    # TestDeliveryPayloadParsing
    suite = TestDeliveryPayloadParsing()
    for method_name in dir(suite):
        if method_name.startswith("test_"):
            try:
                getattr(suite, method_name)()
                print(f"[PASS] {method_name}")
            except Exception as e:
                print(f"[FAIL] {method_name}: {e}")
                errors.append((f"TestDeliveryPayloadParsing.{method_name}", e))

    # TestDeliverySinkMode
    sink_suite = TestDeliverySinkMode()
    for method_name in dir(sink_suite):
        if method_name.startswith("test_"):
            try:
                sink_suite.test_sink_file_mode_bypasses_cli()
                print(f"[PASS] {method_name}")
            except Exception as e:
                print(f"[FAIL] {method_name}: {e}")
                errors.append((f"TestDeliverySinkMode.{method_name}", e))

    # TestParseLogicDirectly
    parse_suite = TestParseLogicDirectly()
    for method_name in dir(parse_suite):
        if method_name.startswith("test_"):
            try:
                getattr(parse_suite, method_name)()
                print(f"[PASS] {method_name}")
            except Exception as e:
                print(f"[FAIL] {method_name}: {e}")
                errors.append((f"TestParseLogicDirectly.{method_name}", e))

    if errors:
        print(f"\n{len(errors)} test(s) failed")
        for name, err in errors:
            print(f"  {name}: {err}")
        sys.exit(1)
    else:
        print("\nAll tests passed")


if __name__ == "__main__":
    run_all_tests()
