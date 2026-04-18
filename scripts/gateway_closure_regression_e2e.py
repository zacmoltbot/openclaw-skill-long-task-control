#!/usr/bin/env python3
"""
Regression: gateway normal closure + JSON parse failure resilience.

When openclaw cron add succeeds (returncode=0) but stdout contains non-JSON
gateway closure text, the install must NOT raise SystemExit.
It should fall back to extracting what it can from stdout.
"""
from __future__ import annotations

import json
import subprocess
import sys
import tempfile
import os
from pathlib import Path

sys.path.insert(0, str(Path(__file__).parent.parent))

from scripts.openclaw_ops import _run_cron_add_with_retry, load_ledger, find_task


def make_script(tmpdir: Path, name: str, content: str) -> str:
    """Write a temp script and return its path."""
    p = tmpdir / name
    p.write_text(content)
    return str(p)


def test_cron_add_succeeds_with_parseable_stdout(tmpdir: Path):
    """Happy path: returncode=0 with parseable JSON stdout."""
    script = make_script(tmpdir, "ok.py",
        "import json,sys; json.dump({'id':'job-42'}, sys.stdout)")
    result = _run_cron_add_with_retry(
        add_cmd=["python3", script],
        ledger_path=tmpdir / "ledger.json",
        task_id="test-parse-ok",
        disabled=False,
    )
    assert result.get("id") == "job-42", f"expected id=job-42, got {result}"
    return {"test": "cron_add_succeeds_with_parseable_stdout", "status": "PASS", "result": result}


def test_cron_add_mixed_output_falls_back(tmpdir: Path):
    """
    returncode=0 but stdout is mixed gateway noise + JSON.
    The JSON should be extracted via sliding window, not raise SystemExit.
    """
    script_content = (
        "import sys\n"
        "sys.stdout.write('[openclaw] loading\\n')\n"
        "sys.stdout.write('{\"ok\":true,\"id\":\"job-99\"}\\n')\n"
        "sys.stdout.write('[gateway] normal closure\\n')\n"
    )
    script = make_script(tmpdir, "mixed.py", script_content)
    result = _run_cron_add_with_retry(
        add_cmd=["python3", script],
        ledger_path=tmpdir / "ledger.json",
        task_id="test-mixed-output",
        disabled=False,
    )
    assert "id" in result, f"expected id field in result, got {result}"
    # Should have extracted the JSON id, not the parse-fallback
    assert result.get("id") == "job-99", f"expected id=job-99, got {result}"
    return {"test": "cron_add_mixed_output_falls_back", "status": "PASS", "result": result}


def test_cron_add_no_json_falls_back(tmpdir: Path):
    """
    returncode=0 but stdout is pure text (no JSON).
    Should return a safe fallback payload, not raise SystemExit.
    """
    script_content = (
        "import sys\n"
        "sys.stdout.write('[gateway] normal closure\\n')\n"
        "sys.stdout.write('job created\\n')\n"
    )
    script = make_script(tmpdir, "nojson.py", script_content)
    result = _run_cron_add_with_retry(
        add_cmd=["python3", script],
        ledger_path=tmpdir / "ledger.json",
        task_id="test-no-json",
        disabled=False,
    )
    assert "id" in result, f"expected fallback id field, got {result}"
    return {"test": "cron_add_no_json_falls_back", "status": "PASS", "result": result}


def test_cron_add_fails_after_retry(tmpdir: Path):
    """
    Both attempts fail (returncode != 0). Should raise SystemExit(1)
    and mark ledger as INSTALL_FAILED.
    """
    script_content = "import sys; sys.exit(1)\n"
    script = make_script(tmpdir, "fail.py", script_content)
    ledger_path = tmpdir / "ledger.json"
    ledger_path.write_text(json.dumps({
        "tasks": [{"task_id": "test-retry-fail", "monitoring": {}}]
    }) + "\n")

    try:
        _run_cron_add_with_retry(
            add_cmd=["python3", script],
            ledger_path=ledger_path,
            task_id="test-retry-fail",
            disabled=False,
        )
        assert False, "expected SystemExit(1)"
    except SystemExit as e:
        assert e.code == 1, f"expected exit code 1, got {e.code}"

    saved = json.loads(ledger_path.read_text())
    task = find_task(saved, "test-retry-fail")
    assert task["monitoring"].get("cron_state") == "INSTALL_FAILED", \
        f"expected INSTALL_FAILED, got {task['monitoring']}"
    return {"test": "cron_add_fails_after_retry", "status": "PASS"}


if __name__ == "__main__":
    print("=" * 60)
    print("gateway_closure_regression: BEGIN")
    print("=" * 60)

    with tempfile.TemporaryDirectory() as tmpdir:
        tmp = Path(tmpdir)
        results = [
            test_cron_add_succeeds_with_parseable_stdout(tmp),
            test_cron_add_mixed_output_falls_back(tmp),
            test_cron_add_no_json_falls_back(tmp),
            test_cron_add_fails_after_retry(tmp),
        ]
        for r in results:
            print(json.dumps(r, ensure_ascii=False, indent=2))

    print("=" * 60)
    print("ALL PASS")
    print("=" * 60)
