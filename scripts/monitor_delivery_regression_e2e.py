#!/usr/bin/env python3
from __future__ import annotations

import json
import tempfile
from pathlib import Path

import openclaw_ops as ops
from reporting_contract import queue_update


ROOT = Path(__file__).resolve().parent.parent


def assert_true(cond: bool, msg: str):
    if not cond:
        raise AssertionError(msg)


def main():
    task = {
        "task_id": "delivery-regression-demo",
        "channel": "discord",
        "message": {
            "requester_channel": "1477237887136432162",
            "requester_channel_valid": True,
            "nudge_target": "1477237887136432162",
        },
        "reporting": {},
    }

    update = queue_update(
        task,
        event_type="STEP_COMPLETED",
        source_kind="CHECKPOINT",
        summary="shell step completed: Prompt A / App 2040425083629473794",
        checkpoint="step-01",
        facts={
            "execution_status": "DONE",
            "completion_evidence": "shell_exit_code_zero",
        },
        outputs=[
            "/tmp/openclaw/rh-output/demo/result_1.png",
            "/tmp/openclaw/rh-output/demo/result_2.jpg",
        ],
        next_action="Advance to next execution item",
    )

    raw_block = update["status_block"]
    user_msg = ops.render_user_update_message(update)

    assert_true("REPORTING HOOK" in raw_block, "fixture should still build raw reporting hook block")
    assert_true("REPORTING HOOK" not in user_msg, "user-facing message must not leak raw reporting hook")
    assert_true("shell=" not in user_msg, "user-facing message must not leak shell command")
    assert_true("result_1.png" in user_msg, "user-facing message should keep compact artifact preview")
    assert_true(user_msg.startswith("LTC 進度：step-01 完成"), "user-facing step message prefix changed unexpectedly")

    source = (ROOT / "scripts" / "openclaw_ops.py").read_text(encoding="utf-8")
    no_deliver_count = source.count('"--no-deliver"')
    assert_true(no_deliver_count >= 2, "monitor cron add paths must include --no-deliver")

    with tempfile.TemporaryDirectory() as td:
        sink = Path(td) / "delivery.json"
        # Monkeypatch delivery sink env path so send_user_update does not shell out.
        import os
        old = os.environ.get("LTC_DELIVERY_SINK_FILE")
        os.environ["LTC_DELIVERY_SINK_FILE"] = str(sink)
        try:
            sent = ops.send_user_update(task, update)
        finally:
            if old is None:
                os.environ.pop("LTC_DELIVERY_SINK_FILE", None)
            else:
                os.environ["LTC_DELIVERY_SINK_FILE"] = old
        assert_true(sent.get("ok") is True, "send_user_update should succeed via delivery sink")
        payloads = json.loads(sink.read_text(encoding="utf-8"))
        payload = payloads[-1]
        assert_true(payload["message"] == user_msg, "delivery sink should receive sanitized user-facing message")
        assert_true("REPORTING HOOK" not in payload["message"], "delivery sink payload must not leak raw reporting hook")

    print(json.dumps({
        "ok": True,
        "user_message": user_msg,
        "no_deliver_count": no_deliver_count,
    }, ensure_ascii=False, indent=2))


if __name__ == "__main__":
    main()
