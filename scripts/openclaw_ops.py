#!/usr/bin/env python3
import argparse
import json
import os
import shlex
import subprocess
from datetime import datetime, timezone
from pathlib import Path
from typing import Any

from reporting_contract import ensure_reporting
from artifact_resolver import resolve_output_variants

STATE_CHOICES = ["STARTED", "STEP_PROGRESS", "STEP_COMPLETED", "BLOCKED", "TASK_COMPLETED"]

ROOT = Path(__file__).resolve().parent.parent
TASK_LEDGER = ROOT / "scripts" / "task_ledger.py"
MONITOR_NUDGE = ROOT / "scripts" / "monitor_nudge.py"
RUNNER_ENGINE = ROOT / "scripts" / "runner_engine.py"
EXECUTION_BRIDGE = ROOT / "scripts" / "execution_bridge.py"
DEFAULT_LEDGER = ROOT / "state" / "long-task-ledger.example.json"
DEFAULT_CHANNEL = "discord"
DEFAULT_TIMEZONE = "Asia/Taipei"
DEFAULT_AGENT = "main"
DEFAULT_SESSION_KEY = "agent:main:discord:channel:{channel}"
DEFAULT_MONITOR_EVERY = "5m"

ACTIVATION_TEMPLATE = """ACTIVATED
- skill: long-task-control
- announcement: 目前這個 task 會採用 long-task-control SKILL 執行
- reporting: 我會用 STEP_PROGRESS / STEP_COMPLETED / BLOCKED / TASK_COMPLETED 這類可驗證 observed truth 回報；有新事實才更新，不用模糊的「還在跑」敘述
- next: 接著建立 task record，開始第一個可驗證步驟
{task_note}""".strip()


def now_iso():
    return datetime.now(timezone.utc).astimezone().isoformat(timespec="seconds")


def run(*args, check=True, capture_output=True, shell=False):
    return subprocess.run(args, check=check, text=True, capture_output=capture_output, shell=shell)


def parse_json_from_mixed_output(text: str):
    lines = [line for line in (text or "").splitlines() if line.strip()]
    for idx in range(len(lines)):
        candidate = "\n".join(lines[idx:])
        try:
            return json.loads(candidate)
        except json.JSONDecodeError:
            continue
    raise SystemExit(f"Could not parse JSON from output:\n{text}")


def load_ledger(path: Path):
    if not path.exists():
        return {"version": 1, "updated_at": now_iso(), "tasks": []}
    return json.loads(path.read_text())


def save_ledger(path: Path, ledger: dict[str, Any]):
    path.parent.mkdir(parents=True, exist_ok=True)
    ledger["updated_at"] = now_iso()
    path.write_text(json.dumps(ledger, ensure_ascii=False, indent=2) + "\n")


def find_task(ledger: dict[str, Any], task_id: str):
    for task in ledger.get("tasks", []):
        if task.get("task_id") == task_id:
            return task
    raise SystemExit(f"Task not found: {task_id}")


def _extract_discord_target_from_session_key(session_key: str | None) -> str | None:
    if not session_key:
        return None
    parts = [part for part in str(session_key).split(":") if part]
    if len(parts) >= 2 and parts[-2] in {"channel", "user", "thread"} and parts[-1].isdigit():
        return parts[-1]
    return None


def normalize_delivery_target(channel: str | None, raw_target: str | None, *, task: dict[str, Any] | None = None, session_key: str | None = None) -> dict[str, Any]:
    channel_name = (channel or DEFAULT_CHANNEL or "discord").strip().lower()
    candidate = (raw_target or "").strip()
    result = {
        "channel": channel_name,
        "raw_target": raw_target,
        "target": candidate,
        "valid": True,
        "source": "input",
        "reason": None,
    }
    if not candidate:
        result.update(valid=False, reason="empty_target")
    elif channel_name != "discord":
        return result
    elif candidate.isdigit():
        return result
    elif candidate.startswith(("discord:channel:", "discord:user:", "discord:thread:")) and candidate.split(":")[-1].isdigit():
        result["target"] = candidate.split(":")[-1]
        result["source"] = "normalized_discord_uri"
        return result
    elif (candidate.startswith("<#") or candidate.startswith("<@")) and candidate.endswith(">") and candidate[2:-1].isdigit():
        result["target"] = candidate[2:-1]
        result["source"] = "normalized_discord_mention"
        return result
    else:
        fallback = (
            _extract_discord_target_from_session_key(session_key)
            or _extract_discord_target_from_session_key((task or {}).get("monitoring", {}).get("openclaw_session_key"))
            or _extract_discord_target_from_session_key((task or {}).get("monitoring", {}).get("executor_session_key"))
        )
        if fallback:
            result.update(target=fallback, source="session_key_fallback", valid=True, reason="normalized_from_invalid_discord_target")
            return result
        result.update(valid=False, reason="invalid_discord_target")
    return result


def requester_target_for(task: dict[str, Any], *, session_key: str | None = None) -> str:
    message = task.get("message", {})
    normalized = normalize_delivery_target(
        task.get("channel") or DEFAULT_CHANNEL,
        message.get("requester_channel") or message.get("nudge_target") or task.get("channel"),
        task=task,
        session_key=session_key,
    )
    return normalized["target"]


def append_delivery_sink(payload: dict[str, Any]):
    sink_path = os.environ.get("LTC_DELIVERY_SINK_FILE")
    if not sink_path:
        return None
    sink = Path(sink_path).expanduser()
    sink.parent.mkdir(parents=True, exist_ok=True)
    existing = []
    if sink.exists() and sink.read_text().strip():
        existing = json.loads(sink.read_text())
    existing.append(payload)
    sink.write_text(json.dumps(existing, ensure_ascii=False, indent=2) + "\n")
    return {"ok": True, "delivery_sink": str(sink), "payload": payload}


def render_user_update_message(update: dict[str, Any]) -> str:
    event_type = update.get("event_type") or "UPDATE"
    checkpoint = update.get("checkpoint") or "unknown-step"
    summary = update.get("summary") or ""
    outputs = [Path(item).name for item in (update.get("outputs") or []) if item]
    output_preview = ""
    if outputs:
        preview = ", ".join(outputs[:2])
        if len(outputs) > 2:
            preview += f" 等 {len(outputs)} 個 artifacts"
        output_preview = f"\n產出：{preview}"

    if event_type == "STEP_COMPLETED":
        return f"LTC 進度：{checkpoint} 完成\n{summary}{output_preview}".strip()
    if event_type == "COMPLETED_HANDOFF":
        return f"LTC 完成：{summary}{output_preview}".strip()
    if event_type == "BLOCKED_ESCALATE":
        blocker = update.get("blocker") or {}
        need = blocker.get("need") or []
        need_text = f"\n需要：{'; '.join(need[:2])}" if need else ""
        return f"LTC 卡住：{summary}{need_text}".strip()
    if event_type == "EXTERNAL_JOB_COMPLETED":
        return f"LTC 外部工作完成：{summary}{output_preview}".strip()
    if event_type == "WORKFLOW_SWITCH":
        return f"LTC 工作流切換：{summary}".strip()
    return update.get("summary") or update.get("status_block") or ""


def send_user_update(task: dict[str, Any], update: dict[str, Any]):
    target_info = normalize_delivery_target(
        task.get("channel") or DEFAULT_CHANNEL,
        task.get("message", {}).get("requester_channel") or task.get("message", {}).get("nudge_target") or task.get("channel"),
        task=task,
    )
    if not target_info.get("valid"):
        return {
            "ok": False,
            "error": f"invalid delivery target: channel={target_info['channel']} raw_target={target_info['raw_target']} reason={target_info['reason']}",
            "error_code": "INVALID_TARGET",
        }
    payload = {
        "task_id": task.get("task_id"),
        "update_id": update.get("update_id"),
        "event_type": update.get("event_type"),
        "channel": task.get("channel") or DEFAULT_CHANNEL,
        "target": target_info["target"],
        "message": render_user_update_message(update),
    }
    sink_result = append_delivery_sink(payload)
    if sink_result is not None:
        return {"ok": True, "delivered_via": "delivery-sink", "message_ref": f"sink:{update.get('update_id')}", "result": sink_result}

    cmd = [
        "openclaw", "message", "send",
        "--channel", payload["channel"],
        "--target", payload["target"],
        "--message", payload["message"],
        "--silent",
        "--json",
    ]
    proc = run(*cmd, check=False)
    if proc.returncode != 0:
        return {"ok": False, "error": proc.stderr.strip() or proc.stdout.strip(), "returncode": proc.returncode}
    result = parse_json_from_mixed_output(proc.stdout)
    message_ref = result.get("messageId") or result.get("message_id") or result.get("id")
    return {"ok": True, "delivered_via": "message.send", "message_ref": message_ref, "result": result}


def deliver_pending_updates(ledger_path: Path, task_id: str, *, delivered_via: str | None = None, note: str | None = None):
    ledger = load_ledger(ledger_path)
    task = find_task(ledger, task_id)
    reporting = ensure_reporting(task)
    delivered = []
    failed = []
    for update in list(reporting.get("pending_updates", [])):
        if update.get("delivered"):
            continue
        send_result = send_user_update(task, update)
        if send_result.get("ok") and delivered_via:
            send_result["delivered_via"] = delivered_via
            send_result.setdefault("message_ref", f"{delivered_via}:{update.get('update_id')}")
        if not send_result.get("ok"):
            failed.append({
                "update_id": update.get("update_id"),
                "error": send_result.get("error"),
                "error_code": send_result.get("error_code"),
                "returncode": send_result.get("returncode"),
            })
            continue
        ack_cmd = [
            "python3", str(TASK_LEDGER), "--ledger", str(ledger_path), "ack-delivery", task_id, update["update_id"],
            "--delivered-via", send_result["delivered_via"],
        ]
        if send_result.get("message_ref"):
            ack_cmd.extend(["--message-ref", str(send_result["message_ref"])])
        ack_note = note
        if note and send_result["delivered_via"] == "delivery-sink":
            ack_note = f"{note}; simulated delivery sink"
        elif note is None and send_result["delivered_via"] == "delivery-sink":
            ack_note = "Simulated delivery sink"
        elif ack_note is None and delivered_via and delivered_via != "message.send":
            ack_note = f"Synthetic delivery flush via {delivered_via}"
        if ack_note:
            ack_cmd.extend(["--note", ack_note])
        ack_proc = run(*ack_cmd)
        delivered.append({
            "update_id": update["update_id"],
            "delivered_via": send_result["delivered_via"],
            "message_ref": send_result.get("message_ref"),
            "ack": parse_json_from_mixed_output(ack_proc.stdout),
        })
    refreshed = load_ledger(ledger_path)
    refreshed_task = find_task(refreshed, task_id)
    refreshed_reporting = ensure_reporting(refreshed_task)
    return {
        "ok": True,
        "task_id": task_id,
        "delivered_count": len(delivered),
        "delivered_update_ids": [item["update_id"] for item in delivered],
        "pending_remaining": len(refreshed_reporting.get("pending_updates", [])),
        "delivered_total": len(refreshed_reporting.get("delivered_updates", [])),
        "failures": failed,
    }


def parse_key_values(items):
    pairs = {}
    for item in items or []:
        if "=" not in item:
            raise SystemExit(f"Invalid key=value item: {item}")
        key, value = item.split("=", 1)
        pairs[key.strip()] = value.strip()
    return pairs


def session_key_for(task, explicit=None):
    if explicit:
        return explicit
    channel_target = task.get("message", {}).get("nudge_target") or task.get("message", {}).get("requester_channel") or task.get("channel") or "unknown"
    return DEFAULT_SESSION_KEY.format(channel=channel_target)


def activation_block(task_note=None):
    note = f"- task_note: {task_note}" if task_note else "- task_note: <optional short task-specific note>"
    return ACTIVATION_TEMPLATE.format(task_note=note)


def task_start_block(task_id, goal, workflow=None, artifacts=None, first_action=None):
    lines = [
        "TASK START",
        f"- task_id: {task_id}",
        f"- goal: {goal}",
        "- workflow:",
    ]
    for idx, step in enumerate(workflow or [], start=1):
        lines.append(f"  {idx}. {step}")
    lines.append("- expected artifacts:")
    for item in artifacts or ["<add file/url/job handle>"]:
        lines.append(f"  - {item}")
    lines.append(f"- first action: {first_action or '<next concrete step>'}")
    return "\n".join(lines)


def default_monitor_name(task_id: str):
    return f"long-task-control monitor {task_id} @ {datetime.now(timezone.utc).strftime('%Y%m%dT%H%M%SZ')}"


def cron_prompt(ledger_path: Path, task_id: str, requester_channel: str, session_key: str):
    return f"""你是 OpenClaw 的 long-task-control monitor agent（check interval: 5分鐘）。你的唯一職責：讀取 task ledger，對 task `{task_id}` 執行一次低成本 monitor tick，必要時主動提醒 main agent 繼續做，並在 terminal 狀態或 BLOCKED_ESCALATE 時立即移除自己的 cron job。

⚠️ Smart stale detection 規則（重要）：
- 若 `progress_at` 仍在更新中（is_progress_updating=true）或外部 task 回傳正在 pending（RunningHub API queue/pending/running），不要判定 STALE_PROGRESS 或 HEARTBEAT_DUE。
- 只有「沒有 progress」且「沒有 pending external return」時才 nudge。

⚠️ 被動 Delivery Push（重要）：
每次 tick 必須主動檢查 ledger 中 `reporting.pending_updates[]` 是否有 `delivered=false` 的項目。若有，馬上用 `message.send` 推到 Discord channel `{requester_channel}`，不需要等 main agent 主動來問。成功發送後，更新 ledger (`delivered=true`)。

⚠️ 新 nudge delivery 架構（重要）：
- NUDGE_MAIN_AGENT / OWNER_RECONCILE → 用 sessions_send 直接喚醒 owner agent（main session: `{session_key}`），不是發 Discord
- BLOCKED_ESCALATE / TASK_COMPLETED / milestone step-complete updates → 才用 message.send 發 Discord 通知 user
- 這樣確保 owner agent 被喚醒續行，不靠 Discord 訊息被動等待

嚴格步驟：
1) exec：python3 {ROOT / 'scripts' / 'openclaw_ops.py'} --ledger {ledger_path} preview-tick {task_id}
2) 解析 JSON，只使用該命令回傳的 state / notify / notification / remove_monitor / reason / pending_user_updates_deliverable / next_action / current_step / retry_count。
3) 【delivery push，先於狀態評估】（每次 tick 必定執行，即使 state 是 OK）：
   - 若 `pending_user_updates_deliverable_count > 0`，對每一筆 `pending_updates` 中 `delivered=false` 的項目：
     (a) 先 exec：`python3 {ROOT / 'scripts' / 'openclaw_ops.py'} --ledger {ledger_path} flush-pending-updates {task_id} --delivered-via message.send`
     (b) flush-pending-updates 會使用 sanitized user-facing message（不是 raw status_block）逐筆 delivery，並自動 ack-delivery
     (c) 若 message.send 回傳失敗但訊息已出現在 channel，視為 delivered=true，仍更新 ledger
   - delivery push 不受 notify flag 限制；保證 user update 一定被被動發出。
4) 依 state 分流：
   - OK（含 noop_external_wait）：不要發 Discord；輸出 1 行 summary 即可。
   - HEARTBEAT_DUE / STALE_PROGRESS：不要發 Discord（這些只是 pre-gate warnings）；輸出 1 行 summary。
   - NUDGE_MAIN_AGENT：【sessions_send 到 owner agent main session，不要發 Discord】
     (a) 用 sessions_send 工具（tool call，不是 exec），sessionKey=`{session_key}`，發送以下格式的 actionable message：
         ```
         【long-task-control / execution nudge】
         task_id={task_id}
         Task stalled at step=<current_step>.
         請 main agent 立刻回來續行，先自救：
           - resume / rebuild-safe-step / reconcile 缺漏 checkpoint
           - 若真的無法自救，回報 BLOCKED
         next_action=<next_action>
         ```
     (b) sessions_send 成功後 summary 註明 nudge_via=sessions_send
     (c) 不要發 message.send 到 Discord；owner agent 收到 sessions_send 就會被喚醒
   - OWNER_RECONCILE：【sessions_send 到 owner agent main session，不要發 Discord】
     (a) 用 sessions_send 工具（tool call），sessionKey=`{session_key}`，發送以下格式：
         ```
         【long-task-control / owner reconcile】
         task_id={task_id}
         任務處於落後狀態，請儘快回報你所處的 branch：
           - A_IN_PROGRESS_FORGOT_LEDGER：還在跑但忘了更新 ledger，請補 checkpoint
           - B_BLOCKED：任務被 block，請寫 BLOCKED checkpoint
           - C_COMPLETED：任務已完成，請寫 TASK_COMPLETED truth
           - D_NO_REPLY：無法確認，先找外部 evidence
           - E_FORGOT_OR_NOT_DOING：忘了或沒在做，請立刻補做
         reason=<reason>
         ```
     (b) sessions_send 成功後 summary 註明 reconcile_via=sessions_send
   - BLOCKED_ESCALATE：【發 Discord，不要 sessions_send】
     (a) 用 message.send 發到 Discord channel `{requester_channel}`，內容一次交代清楚：
         - 哪個 step 卡住、retry 次數、嘗試了什麼、為什麼現在判定失敗、建議下一步
     (b) 發完後立即 exec：python3 {ROOT / 'scripts' / 'openclaw_ops.py'} --ledger {ledger_path} remove-monitor {task_id}
   - STOP_AND_DELETE：不要再發 Discord；直接 exec：python3 {ROOT / 'scripts' / 'openclaw_ops.py'} --ledger {ledger_path} remove-monitor {task_id}
5) message.send 內容必須簡短、fact-based；BLOCKED_ESCALATE 的 exception：可以稍長但要一次把 blocker 交代清楚。
6) remove-monitor 是 idempotent cleanup；若 job 已不存在但 ledger 成功標成 DELETED，也算 cron_removed=yes。
7) 這個 cron job 綁定的 main session key 是 `{session_key}`；所有 NUDGE / RECONCILE 訊息都透過 sessions_send 直接喚醒 owner agent。

輸出限制：最後只輸出一小段 JSON summary，包含 task_id、state、nudge_via、reconcile_via、notified_discord、cron_removed、delivery_push_count。"""


def format_notification(task, report):
    state = report["state"]
    payload = report.get("action_payload") or {}
    facts = payload.get("facts") or {}
    task_id = report["task_id"]
    next_action = report.get("next_action") or task.get("next_action")
    if state == "NUDGE_MAIN_AGENT":
        resume_token = facts.get("resume_token")
        current_step = facts.get("current_step") or report.get("current_step")
        return "\n".join([
            "【long-task-control / execution nudge】",
            f"task_id={task_id}",
            f"current_step={current_step}",
            f"state={state}",
            f"reason={report['reason']}",
            f"next_action={next_action}",
            f"resume_token={resume_token}",
            "請 main agent 立刻回來續行，先自救：resume / rebuild-safe-step / reconcile 缺漏 checkpoint；真的推不動才標記 BLOCKED。",
        ])
    if state in {"OWNER_RECONCILE", "TRUTH_INCONSISTENT"}:
        branches = facts.get("branches") or {}
        suspicious_jobs = facts.get("suspicious_external_jobs") or []
        required_provider_evidence = facts.get("required_provider_evidence") or []
        lines = [
            "【long-task-control / owner reconcile】",
            f"task_id={task_id}",
            f"current_step={facts.get('current_step') or report.get('current_step')}",
            f"state={state}",
            f"reason={report['reason']}",
            f"next_action={facts.get('next_action') or next_action}",
            f"resume_token={facts.get('resume_token')}",
        ]
        if suspicious_jobs:
            lines.append("弱證據 external pending claim，請先補 provider evidence：")
            for job in suspicious_jobs:
                lines.append(f"- provider={job.get('provider')} job_id={job.get('job_id')} status={job.get('status')}")
            lines.append("可接受 evidence: " + ", ".join(required_provider_evidence[:6]) + (" ..." if len(required_provider_evidence) > 6 else ""))
        lines.append("請 main agent 立即 reconcile owner truth，優先把任務往完成推：")
        for key in ["A_IN_PROGRESS_FORGOT_LEDGER", "B_BLOCKED", "C_COMPLETED", "D_NO_REPLY", "E_FORGOT_OR_NOT_DOING"]:
            if key in branches:
                lines.append(f"- {key}: {branches[key]}")
        return "\n".join(lines)
    if state == "BLOCKED_ESCALATE":
        blocker = facts.get("blocker") or task.get("blocker") or {}
        need = blocker.get("need") or []
        lines = [
            "【long-task-control / blocked escalate】",
            f"task_id={task_id}",
            f"state={state}",
            f"reason={report['reason']}",
            f"blocker_reason={blocker.get('reason')}",
        ]
        if need:
            lines.append("need=" + "; ".join(need))
        if blocker.get("safe_next_step"):
            lines.append(f"safe_next_step={blocker.get('safe_next_step')}")
        lines.append("請 main agent 對 requester 升級 blocker，之後停止 monitor cron。")
        return "\n".join(lines)
    return json.dumps({"task_id": task_id, "state": state, "reason": report["reason"]}, ensure_ascii=False)


def update_monitor_metadata(task, **kwargs):
    monitoring = task.setdefault("monitoring", {})
    for key, value in kwargs.items():
        monitoring[key] = value


def render_status_block(state, task_id, *, goal=None, checkpoint=None, workflow_steps=None, facts=None,
                        outputs=None, completed=None, validation=None, blocker=None, tried=None,
                        need=None, next_action=None):
    lines = [state, f"- task_id: {task_id}"]
    if goal and state in {"STARTED", "TASK_COMPLETED"}:
        lines.append(f"- goal: {goal}")
    if checkpoint:
        lines.append(f"- checkpoint: {checkpoint}")
    if state == "STARTED" and workflow_steps:
        lines.append("- workflow:")
        for idx, step in enumerate(workflow_steps, start=1):
            lines.append(f"  {idx}. {step}")
    if facts:
        lines.append("- verified facts:")
        for key, value in facts.items():
            lines.append(f"  - {key}={value}")
    if outputs:
        lines.append("- output artifacts:" if state == "TASK_COMPLETED" else "- outputs:")
        for item in outputs:
            lines.append(f"  - {item}")
    if completed and state == "TASK_COMPLETED":
        lines.append("- completed checkpoints:")
        for item in completed:
            lines.append(f"  - {item}")
    if tried and state == "BLOCKED":
        lines.append("- tried:")
        for item in tried:
            lines.append(f"  - {item}")
    if validation and state == "TASK_COMPLETED":
        lines.append("- validation:")
        for item in validation:
            lines.append(f"  - {item}")
    if state == "TASK_COMPLETED":
        lines.append("- background items still running:")
        lines.append("  - none")
    if blocker and state == "BLOCKED":
        lines.append(f"- blocker: {blocker}")
    if need and state == "BLOCKED":
        lines.append("- need:")
        for item in need:
            lines.append(f"  - {item}")
    if next_action:
        lines.append(f"- {'handoff' if state == 'TASK_COMPLETED' else 'next'}: {next_action}")
    return "\n".join(lines)


def validate_outputs(outputs):
    results = []
    for raw in outputs or []:
        path = Path(raw)
        if path.exists() and path.is_file():
            size = path.stat().st_size
            results.append(f"artifact_exists[{raw}]=true")
            results.append(f"artifact_size_bytes[{raw}]={size}")
        else:
            results.append(f"artifact_exists[{raw}]=false")
    return results


EXECUTOR_SESSION_KEY_PREFIX = "agent:executor:long-task:"


def executor_session_key(task_id: str) -> str:
    return f"{EXECUTOR_SESSION_KEY_PREFIX}{task_id}"


def executor_prompt(ledger_path: Path, task_id: str):
    """
    Generate the executor agent prompt.

    The executor is an OpenClaw subagent that:
    1. Reads the task ledger to find the current step
    2. Executes the next pending workflow step
    3. Writes STEP_PROGRESS / STEP_COMPLETED truth to the ledger
    4. Repeats until the workflow is done
    5. Writes TASK_COMPLETED when finished

    The executor is auto-resuming: it reads the ledger's current_checkpoint
    and starts from the next pending step. If step-01 is DONE and step-02 is
    RUNNING, it starts step-02 immediately.
    """
    return f"""你是 long-task-control 的 executor agent。你的職責：讀取 task ledger，執行下一個待做的步驟，寫入 checkpoint，重複直到 workflow 完成。

⚠️ 核心原則：
- 你只執行「下一個可做的步驟」，不是整個 task
- 每個步驟完成後，馬上寫 checkpoint 再繼續
- Auto-resume：讀 ledger 的 current_checkpoint，如果 step-01 DONE、step-02 RUNNING，就從 step-02 繼續
- 任一步驟失敗 → 寫 BLOCKED checkpoint，附上 blocker reason 和 need，馬上停下來等外部修復

執行循環：
1. exec: python3 {ROOT / 'scripts' / 'executor_engine.py'} --ledger {ledger_path} preview {task_id}
2. 解析 JSON，看 action 欄位：
   - "execute_step" → 執行它（見下）
   - "workflow_complete" → 寫 TASK_COMPLETED truth，輸出 "EXECUTOR_DONE"，结束
   - "already_completed" / "terminal_state" → 輸出 "EXECUTOR_DONE"，结束
   - "blocked" → 輸出 "EXECUTOR_BLOCKED"，结束
3. 執行步驟（"execute_step"）：
   a. 決定這步要做什麼（從 ledger 的 workflow[] / next_action / goal 推斷）
   b. 實際執行（shell command / API call / file operation 等）
   c. 成功後，exec: python3 {ROOT / 'scripts' / 'task_ledger.py'} --ledger {ledger_path} checkpoint {task_id} --event-type STEP_COMPLETED --summary "<描述這步完成了什麼>" --current-checkpoint <step-id> --next-action "<下一步做什麼>"
   d. 用 --fact 附上關鍵 output evidence（file path、job id、output URL 等）
   e. 如果有 artifact，用 --artifact 標記
4. 回到步驟 1（ledger 更新過，current_checkpoint 已前進）

重要：
- 每次只做一個 step，不要一口氣做很多
- 每個 step 完成後馬上寫 checkpoint，這樣萬一中斷，下一次啟動會從斷點繼續
- 如果 external job（RunningHub 等）需要等待，用 external-job command 寫 pending 狀態，不要 block 在那裡等
- 完成整個 workflow → 寫 TASK_COMPLETED truth，狀態 block 要包含所有 completed steps 和 output artifacts
- 輸出 "EXECUTOR_DONE" 表示整個 task 完成
- 如果 task 被 BLOCK → 馬上停止，輸出 "EXECUTOR_BLOCKED"

輸出格式（每輪）：
  {{"executed": "<step-id>", "summary": "<做了什麼>", "next": "<下一步或 EXECUTOR_DONE/EXECUTOR_BLOCKED>"}}
"""


def cmd_executor_preview(args):
    """Preview what the executor would do for a task without executing."""
    result = run(
        "python3",
        str(ROOT / "scripts" / "executor_engine.py"),
        "--ledger", str(args.ledger),
        "preview",
        args.task_id,
        check=True,
    )
    print(result.stdout.strip())


def parse_workflow_step_contract(step: dict[str, Any]) -> dict[str, Any]:
    raw_title = str(step.get("title") or "").strip()
    if not raw_title:
        return {"title": step.get("id") or "unnamed-step"}
    parts = [part.strip() for part in raw_title.split("::")]
    item: dict[str, Any] = {"title": parts[0]}
    for chunk in parts[1:]:
        if "=" not in chunk:
            continue
        key, value = chunk.split("=", 1)
        key = key.strip().lower().replace("-", "_")
        value = value.strip()
        if key in {"shell", "cwd", "next_action", "generic_manual_mode"}:
            item[key] = value
        elif key in {"artifact", "output"}:
            item.setdefault("expect_artifacts", []).append(value)
        elif key in {"artifacts", "outputs", "expect", "expect_artifacts"}:
            item.setdefault("expect_artifacts", []).extend([v.strip() for v in value.split("|") if v.strip()])
        elif key in {"timeout", "timeout_sec"}:
            try:
                item["timeout_sec"] = int(value)
            except ValueError:
                item["timeout_sec"] = value
        else:
            item[key] = value
    return item


def infer_generic_auto_action(task: dict[str, Any], step: dict[str, Any], parsed: dict[str, Any]) -> dict[str, Any]:
    if parsed.get("shell") or parsed.get("generic_manual_mode"):
        return {}
    haystack = " ".join([
        str(task.get("task_id") or ""),
        str(task.get("goal") or ""),
        str(step.get("id") or ""),
        str(step.get("title") or ""),
        str(parsed.get("title") or ""),
    ]).lower()
    if "discord" not in haystack:
        return {}
    if "target" not in haystack and "requester_channel" not in haystack:
        return {}
    if not any(token in haystack for token in ("normalize", "repair", "self-heal", "self heal", "heal", "fix")):
        return {}
    return {
        "generic_manual_mode": "auto_repair",
        "auto_action": "repair_requester_channel",
    }



def build_generic_job_spec(task: dict[str, Any], *, job_id: str, adapter: str):
    workflow = task.get("workflow") or []
    items = []
    for idx, step in enumerate(workflow, start=1):
        step_id = step.get("id") or f"step-{idx:02d}"
        parsed = parse_workflow_step_contract(step)
        inferred = infer_generic_auto_action(task, step, parsed)
        items.append({
            "item_id": step_id,
            "title": parsed.get("title") or step.get("title") or step_id,
            "checkpoint": step_id,
            "goal": task.get("goal"),
            "task_id": task.get("task_id"),
            "step_index": idx,
            **{k: v for k, v in parsed.items() if k != "title"},
            **inferred,
        })
    return {
        "job_id": job_id,
        "kind": "generic-long-task",
        "adapter": adapter,
        "mode": "serial",
        "bridge": {
            "ledger": str(task.get("_ledger_path")) if task.get("_ledger_path") else None,
            "task_id": task.get("task_id"),
        },
        "items": items,
    }


def cmd_init_execution_job(args):
    ledger = load_ledger(args.ledger)
    task = find_task(ledger, args.task_id)
    task["_ledger_path"] = str(args.ledger)
    workflow = task.get("workflow") or []
    if not workflow:
        raise SystemExit("Task has no workflow; cannot derive execution job")

    job_id = args.job_id or f"{args.task_id}-job"
    jobs_root = Path(args.jobs_root)
    spec = build_generic_job_spec(task, job_id=job_id, adapter=args.adapter)
    spec_path = jobs_root / job_id / "job-spec.json"
    spec_path.parent.mkdir(parents=True, exist_ok=True)
    spec_path.write_text(json.dumps(spec, ensure_ascii=False, indent=2) + "\n")

    init_cmd = [
        "python3", str(RUNNER_ENGINE), "--jobs-root", str(jobs_root), "init-job", str(spec_path),
        "--ledger", str(args.ledger), "--task-id", args.task_id,
    ]
    init_result = run(*init_cmd)
    print(json.dumps({
        "ok": True,
        "task_id": args.task_id,
        "job_id": job_id,
        "jobs_root": str(jobs_root),
        "spec_path": str(spec_path),
        "workflow_steps": [step.get("title") for step in workflow],
        "runner_init": parse_json_from_mixed_output(init_result.stdout),
        "next_commands": {
            "status": f"python3 scripts/runner_engine.py --jobs-root {jobs_root} status {job_id}",
            "run_one": f"python3 scripts/runner_engine.py --jobs-root {jobs_root} run-loop {job_id} --max-steps 1",
            "run_to_completion": f"python3 scripts/runner_engine.py --jobs-root {jobs_root} run-loop {job_id}",
        },
    }, ensure_ascii=False, indent=2))


def cmd_run_executor(args):
    """Spawn an executor subagent for a task. The executor drives the task to completion."""
    ledger = load_ledger(args.ledger)
    task = find_task(ledger, args.task_id)
    if not task:
        raise SystemExit(f"Task not found: {args.task_id}")

    task_id = args.task_id
    name = f"long-task executor {task_id} @ {datetime.now(timezone.utc).strftime('%Y%m%dT%H%M%SZ')}"
    session_key = executor_session_key(task_id)
    prompt = executor_prompt(args.ledger, task_id)

    # The executor runs as a cron job with a long timeout that wakes every 5 minutes.
    # Each tick: read ledger → execute next step → write checkpoint → exit.
    # The monitor cron watches the executor and handles GAP-1 / stalled escalation.
    # If the executor completes all steps, it writes TASK_COMPLETED and the monitor stops.
    add_cmd = [
        "openclaw", "cron", "add",
        "--json",
        "--name", name,
        "--agent", args.agent,
        "--session", "isolated",
        "--session-key", session_key,
        "--channel", DEFAULT_CHANNEL,
        "--wake", "now",
        "--message", prompt,
        "--timeout-seconds", str(args.timeout_seconds),
        "--thinking", args.thinking,
    ]
    if args.model:
        add_cmd.extend(["--model", args.model])
    if args.every:
        add_cmd.extend(["--every", args.every])
    else:
        add_cmd.extend(["--cron", args.cron_expr or "*/5 * * * *", "--tz", args.tz or DEFAULT_TIMEZONE])
    if args.light_context:
        add_cmd.append("--light-context")

    if args.dry_run:
        payload = {
            "id": f"dry-run-executor-{task_id}",
            "name": name,
            "sessionKey": session_key,
            "message": prompt,
        }
    else:
        payload = _run_cron_add_with_retry(add_cmd, args.ledger, args.task_id, disabled=False)

    # Record executor metadata in the task
    monitoring = task.setdefault("monitoring", {})
    monitoring["executor_cron_job_id"] = payload.get("id")
    monitoring["executor_cron_name"] = name
    monitoring["executor_session_key"] = session_key
    monitoring["executor_state"] = "ACTIVE"
    monitoring["executor_installed_at"] = now_iso()
    save_ledger(args.ledger, ledger)

    print(json.dumps({
        "ok": True,
        "task_id": task_id,
        "job": payload,
        "session_key": session_key,
        "prompt_preview": prompt[:500] + "...",
        "note": "Executor cron installed. It will wake every 5 min, execute the next pending step, write checkpoint, and exit. Monitor cron watches for stalls (GAP-1) and escalates if the executor stalls.",
    }, ensure_ascii=False, indent=2))


def _emit_record_result(args, *, facts, outputs, validation, ledger_result):
    ledger = load_ledger(args.ledger)
    task = find_task(ledger, args.task_id)
    reporting = ensure_reporting(task)
    pending_update = reporting.get("pending_updates", [])[-1] if reporting.get("pending_updates") else None
    print(json.dumps({
        "ok": True,
        "task_id": args.task_id,
        "state": args.state,
        "status_block": render_status_block(
            args.state,
            args.task_id,
            goal=task.get("goal"),
            checkpoint=getattr(args, "current_checkpoint", None) or task.get("current_checkpoint"),
            workflow_steps=[step.get("title") for step in task.get("workflow", [])],
            facts=facts,
            outputs=outputs,
            completed=getattr(args, "completed_checkpoint", None),
            validation=validation,
            blocker=args.summary if args.state == "BLOCKED" else None,
            tried=getattr(args, "tried", None),
            need=getattr(args, "need", None),
            next_action=getattr(args, "next_action", None) or task.get("next_action"),
        ),
        "ledger_stdout": ledger_result.stdout.strip(),
        "last_checkpoint_at": task.get("last_checkpoint_at"),
        "current_checkpoint": task.get("current_checkpoint"),
        "task_status": task.get("status"),
        "validation": task.get("validation", []),
        "pending_user_update": pending_update,
        "ack_delivery_command": (
            f"python3 scripts/task_ledger.py --ledger {args.ledger} ack-delivery {args.task_id} {pending_update['update_id']} --delivered-via message.send --message-ref <message-ref>"
            if pending_update else None
        ),
    }, ensure_ascii=False, indent=2))


def cmd_record_update(args):
    facts = parse_key_values(args.fact)
    outputs = args.output or []
    validation = list(args.validation or [])
    if args.state == "TASK_COMPLETED" and outputs:
        validation.extend(validate_outputs(outputs))

    if args.state == "BLOCKED":
        cmd = [
            "python3", str(TASK_LEDGER), "--ledger", str(args.ledger), "block", args.task_id,
            "--reason", args.summary,
            "--safe-next-step", args.next_action or args.safe_next_step or "Await unblock action",
        ]
        for item in args.need or []:
            cmd.extend(["--need", item])
        for key, value in facts.items():
            cmd.extend(["--fact", f"{key}={value}"])
        if args.resume_token:
            cmd.extend(["--resume-token", args.resume_token])
        if args.current_checkpoint:
            cmd.extend(["--current-checkpoint", args.current_checkpoint])
        if args.next_action:
            cmd.extend(["--next-action", args.next_action])
        ledger_result = run(*cmd)
    elif args.state == "STARTED":
        cmd = [
            "python3", str(TASK_LEDGER), "--ledger", str(args.ledger), "checkpoint", args.task_id,
            "--event-type", "STEP_PROGRESS",
            "--summary", args.summary,
        ]
        for key, value in facts.items():
            cmd.extend(["--fact", f"{key}={value}"])
        if args.current_checkpoint:
            cmd.extend(["--current-checkpoint", args.current_checkpoint])
        if args.next_action:
            cmd.extend(["--next-action", args.next_action])
        ledger_result = run(*cmd)
    else:
        event_type = args.state
        cmd = [
            "python3", str(TASK_LEDGER), "--ledger", str(args.ledger), "checkpoint", args.task_id,
            "--event-type", event_type,
            "--summary", args.summary,
        ]
        for key, value in facts.items():
            cmd.extend(["--fact", f"{key}={value}"])
        for item in outputs:
            cmd.extend(["--artifact", item])
        for item in validation:
            cmd.extend(["--validation", item])
        if args.resume_token:
            cmd.extend(["--resume-token", args.resume_token])
        if args.current_checkpoint:
            cmd.extend(["--current-checkpoint", args.current_checkpoint])
        if args.next_action:
            cmd.extend(["--next-action", args.next_action])
        ledger_result = run(*cmd)

    _emit_record_result(args, facts=facts, outputs=outputs, validation=validation, ledger_result=ledger_result)



def _record_reconciled_step_completed(args, *, chosen_path: str, summary: str, extra_facts: dict[str, str] | None = None):
    facts = parse_key_values(args.fact)
    facts[args.fact_key] = chosen_path
    for k, v in (extra_facts or {}).items():
        facts[k] = v
    outputs = [chosen_path]
    validation = []
    ledger_result = run(
        "python3", str(TASK_LEDGER), "--ledger", str(args.ledger), "checkpoint", args.task_id,
        "--event-type", "STEP_COMPLETED",
        "--summary", summary,
        "--artifact", chosen_path,
        *(sum((["--fact", f"{k}={v}"] for k, v in facts.items()), [])),
        *( ["--current-checkpoint", args.current_checkpoint] if args.current_checkpoint else [] ),
        *( ["--next-action", args.next_action] if args.next_action else [] ),
    )
    args.state = "STEP_COMPLETED"
    args.summary = summary
    _emit_record_result(args, facts=facts, outputs=outputs, validation=validation, ledger_result=ledger_result)


def cmd_reconcile_before_block(args):
    resolution = resolve_output_variants(args.expected_path, min_video_bytes=args.min_video_bytes)
    if resolution.get("resolved"):
        chosen = resolution["chosen_video"]["path"]
        summary = args.summary_if_resolved or f"Recovered output from artifact reconcile: {Path(chosen).name}"
        _record_reconciled_step_completed(args, chosen_path=chosen, summary=summary)
        return

    args.state = "BLOCKED"
    args.summary = args.summary_if_blocked or args.summary or f"Expected output not found after reconcile: {args.expected_path}"
    facts = parse_key_values(args.fact)
    facts.setdefault("failure_type", "EXTERNAL_WAIT")
    facts["reconcile_expected_path"] = str(args.expected_path)
    outputs = []
    validation = []
    cmd = [
        "python3", str(TASK_LEDGER), "--ledger", str(args.ledger), "block", args.task_id,
        "--reason", args.summary,
        "--safe-next-step", args.next_action or args.safe_next_step or "Investigate external output and retry",
    ]
    for item in args.need or []:
        cmd.extend(["--need", item])
    for key, value in facts.items():
        cmd.extend(["--fact", f"{key}={value}"])
    if args.current_checkpoint:
        cmd.extend(["--current-checkpoint", args.current_checkpoint])
    if args.next_action:
        cmd.extend(["--next-action", args.next_action])
    ledger_result = run(*cmd)
    _emit_record_result(args, facts=facts, outputs=outputs, validation=validation, ledger_result=ledger_result)


def cmd_recover_external_success(args):
    resolution = resolve_output_variants(args.expected_path, min_video_bytes=args.min_video_bytes)
    if not resolution.get("resolved"):
        raise SystemExit(f"No recoverable output found for: {args.expected_path}")
    chosen = resolution["chosen_video"]["path"]
    summary = args.summary or f"Recovered externally completed output after interrupted owner/executor: {Path(chosen).name}"
    _record_reconciled_step_completed(
        args,
        chosen_path=chosen,
        summary=summary,
        extra_facts={"recovered_from_external_truth": "true", "reconcile_expected_path": str(args.expected_path)},
    )


def cmd_activation(args):
    print(activation_block(args.task_note))


def run_init_task(args):
    cmd = [
        "python3", str(TASK_LEDGER), "--ledger", str(args.ledger), "init", args.task_id,
        "--goal", args.goal,
        "--owner", args.owner,
        "--channel", args.channel,
        "--next-action", args.next_action,
        "--expected-interval-sec", str(args.expected_interval_sec),
        "--timeout-sec", str(args.timeout_sec),
        "--max-nudges", str(args.max_nudges),
        "--escalate-after-nudges", str(args.escalate_after_nudges),
        "--blocked-escalate-after-sec", str(args.blocked_escalate_after_sec),
        "--renotify-interval-sec", str(args.renotify_interval_sec),
        "--message-ref", args.message_ref,
        "--summary", args.summary or f"Activation announced and {args.task_id} initialized",
    ]
    if args.activation_announced:
        cmd.append("--activation-announced")
    if args.nudge_after_sec is not None:
        cmd.extend(["--nudge-after-sec", str(args.nudge_after_sec)])
    for item in args.workflow or []:
        cmd.extend(["--workflow", item])
    for item in args.fact or []:
        cmd.extend(["--fact", item])
    for item in args.artifact or []:
        cmd.extend(["--artifact", item])
    for item in args.note or []:
        cmd.extend(["--note", item])
    result = run(*cmd)

    ledger = load_ledger(args.ledger)
    task = find_task(ledger, args.task_id)
    task.setdefault("message", {})["requester_channel_raw"] = args.requester_channel or task.get("channel")
    normalized_target = normalize_delivery_target(task.get("channel") or DEFAULT_CHANNEL, args.requester_channel or task.get("channel"), task=task)
    task["message"]["requester_channel"] = normalized_target["target"]
    task["message"]["requester_channel_valid"] = normalized_target["valid"]
    task["message"]["requester_channel_source"] = normalized_target["source"]
    if normalized_target.get("reason"):
        task["message"]["requester_channel_reason"] = normalized_target["reason"]
    task["message"]["nudge_channel"] = args.nudge_channel or task.get("channel")
    task["message"]["nudge_target"] = args.nudge_target or task["message"]["requester_channel"] or task.get("channel")
    # NOTE: do NOT write cron_state here — let the caller write ACTIVE only after
    # 'openclaw cron add' succeeds, or INSTALL_FAILED if it fails after retry.
    save_ledger(args.ledger, ledger)
    return result.stdout.strip(), task


def cmd_init_task(args):
    task_note = args.task_note or args.goal
    if args.print_activation:
        print(activation_block(task_note))
        print()
    stdout, task = run_init_task(args)
    print(json.dumps({
        "ok": True,
        "task_id": args.task_id,
        "activation": activation_block(task_note),
        "task_start": task_start_block(args.task_id, args.goal, workflow=args.workflow, artifacts=args.artifact, first_action=args.next_action),
        "ledger": str(args.ledger),
        "requester_channel": task["message"]["requester_channel"],
        "stdout": stdout,
    }, ensure_ascii=False, indent=2))


def cmd_render_prompt(args):
    ledger = load_ledger(args.ledger)
    task = find_task(ledger, args.task_id)
    session_key = session_key_for(task, args.session_key)
    requester_channel = normalize_delivery_target(
        task.get("channel") or DEFAULT_CHANNEL,
        args.requester_channel or task.get("message", {}).get("requester_channel") or task.get("message", {}).get("nudge_target") or task.get("channel"),
        task=task,
        session_key=session_key,
    )["target"]
    print(cron_prompt(args.ledger, args.task_id, requester_channel, session_key))


def _run_cron_add_with_retry(add_cmd, ledger_path, task_id, disabled):
    """Run 'openclaw cron add', retry once after 3-5s on failure, write INSTALL_FAILED to ledger on final failure."""
    import time, random, sys
    shell_cmd = " ".join(shlex.quote(part) for part in add_cmd)
    proc = run(shell_cmd, shell=True, check=False)
    if proc.returncode != 0:
        # first attempt failed — wait 3-5s then retry
        wait_sec = random.uniform(3.0, 5.0)
        time.sleep(wait_sec)
        proc = run(shell_cmd, shell=True, check=False)
        if proc.returncode != 0:
            # retry also failed — mark ledger as INSTALL_FAILED
            err_msg = f"exit={proc.returncode}; stderr={proc.stderr[:500]}"
            ledger = load_ledger(ledger_path)
            task = find_task(ledger, task_id)
            task.setdefault("monitoring", {})["cron_state"] = "INSTALL_FAILED"
            task.setdefault("monitoring", {})["install_signal"] = "INSTALL_FAILED"
            task.setdefault("monitoring", {})["cron_install_error"] = err_msg
            save_ledger(ledger_path, ledger)
            payload = {
                "ok": False,
                "signal": "INSTALL_FAILED",
                "task_id": task_id,
                "reason": "cron_add_failed_after_retry",
                "exit_code": proc.returncode,
                "stderr": proc.stderr[:200].strip(),
            }
            sys.stderr.write(json.dumps(payload, ensure_ascii=False) + "\n")
            sys.stderr.flush()
            raise SystemExit(1)
    return parse_json_from_mixed_output(proc.stdout)


def cmd_activate_task(args):
    task_note = args.task_note or args.goal
    init_stdout, _ = run_init_task(args)

    install_ns = argparse.Namespace(
        ledger=args.ledger,
        task_id=args.task_id,
        requester_channel=args.requester_channel,
        session_key=args.session_key,
        name=args.name,
        agent=args.agent,
        session=args.session,
        wake=args.wake,
        every=args.every,
        cron_expr=args.cron_expr,
        tz=args.tz,
        timeout_seconds=args.monitor_timeout_seconds,
        thinking=args.thinking,
        model=args.model,
        disabled=args.disabled,
        light_context=args.light_context,
        dry_run=args.dry_run,
    )

    # Reload ledger: run_init_task no longer writes cron_state (PENDING_INSTALL was removed),
    # so we need a fresh task reference after init completes.
    ledger = load_ledger(args.ledger)
    task = find_task(ledger, args.task_id)
    session_key = session_key_for(task, install_ns.session_key)
    requester_info = normalize_delivery_target(
        task.get("channel") or DEFAULT_CHANNEL,
        install_ns.requester_channel or task.get("message", {}).get("requester_channel_raw") or task.get("message", {}).get("requester_channel") or task.get("message", {}).get("nudge_target") or task.get("channel"),
        task=task,
        session_key=session_key,
    )
    requester_channel = requester_info["target"]
    task.setdefault("message", {})["requester_channel"] = requester_channel
    task["message"]["requester_channel_valid"] = requester_info["valid"]
    task["message"]["requester_channel_source"] = requester_info["source"]
    if requester_info.get("reason"):
        task["message"]["requester_channel_reason"] = requester_info["reason"]
    task["message"]["nudge_target"] = requester_channel
    name = install_ns.name or default_monitor_name(args.task_id)
    prompt = cron_prompt(args.ledger, args.task_id, requester_channel, session_key)
    add_cmd = [
        "openclaw", "cron", "add",
        "--json",
        "--name", name,
        "--agent", install_ns.agent,
        "--session", install_ns.session,
        "--session-key", session_key,
        "--channel", DEFAULT_CHANNEL,
        "--wake", install_ns.wake,
        "--message", prompt,
        "--timeout-seconds", str(install_ns.timeout_seconds),
        "--thinking", install_ns.thinking,
        "--no-deliver",
    ]
    if install_ns.model:
        add_cmd.extend(["--model", install_ns.model])
    if install_ns.every:
        add_cmd.extend(["--every", install_ns.every])
    else:
        add_cmd.extend(["--cron", install_ns.cron_expr, "--tz", install_ns.tz])
    if install_ns.disabled:
        add_cmd.append("--disabled")
    if install_ns.light_context:
        add_cmd.append("--light-context")

    if install_ns.dry_run:
        payload = {
            "id": f"dry-run-{args.task_id}",
            "name": name,
            "schedule": {"kind": "every", "every": install_ns.every} if install_ns.every else {"kind": "cron", "expr": install_ns.cron_expr, "tz": install_ns.tz},
            "sessionKey": session_key,
            "message": prompt,
        }
    else:
        payload = _run_cron_add_with_retry(add_cmd, args.ledger, args.task_id, install_ns.disabled)

    update_monitor_metadata(
        task,
        cron_state="ACTIVE" if not install_ns.disabled else "DISABLED",
        install_signal="INSTALL_OK",
        openclaw_cron_job_id=payload["id"],
        openclaw_cron_name=name,
        openclaw_session_key=session_key,
        openclaw_requester_channel=requester_channel,
        openclaw_schedule=payload.get("schedule") or ({"kind": "every", "every": install_ns.every} if install_ns.every else {"kind": "cron", "expr": install_ns.cron_expr, "tz": install_ns.tz}),
        cron_installed_at=now_iso(),
    )
    save_ledger(args.ledger, ledger)

    execution_job = None
    execution_start = None
    if getattr(args, "auto_execution", True):
        init_exec_ns = argparse.Namespace(
            ledger=args.ledger,
            task_id=args.task_id,
            jobs_root=args.jobs_root,
            job_id=args.execution_job_id,
            adapter=args.execution_adapter,
        )
        init_cmd = [
            "python3", str(ROOT / "scripts" / "openclaw_ops.py"), "--ledger", str(args.ledger), "init-execution-job", args.task_id,
            "--jobs-root", str(args.jobs_root),
            "--adapter", args.execution_adapter,
        ]
        if args.execution_job_id:
            init_cmd.extend(["--job-id", args.execution_job_id])
        init_result = run(*init_cmd)
        execution_job = parse_json_from_mixed_output(init_result.stdout)

        if getattr(args, "auto_start_execution", True):
            resolved_job_id = execution_job["job_id"]
            owner = args.execution_owner or f"bootstrap:{resolved_job_id}"
            run_cmd = [
                "python3", str(RUNNER_ENGINE), "--jobs-root", str(args.jobs_root), "run-loop", resolved_job_id,
                "--execution-owner", owner,
            ]
            if args.auto_run_max_steps is not None:
                run_cmd.extend(["--max-steps", str(args.auto_run_max_steps)])
            run_result = run(*run_cmd)
            execution_start = parse_json_from_mixed_output(run_result.stdout)
            execution_start["delivery_flush"] = deliver_pending_updates(
                args.ledger,
                args.task_id,
                note="Canonical execution live-path delivery flush",
            )

    print(json.dumps({
        "ok": True,
        "task_id": args.task_id,
        "activation": activation_block(task_note),
        "ledger": str(args.ledger),
        "requester_channel": task["message"]["requester_channel"],
        "task_start": task_start_block(args.task_id, args.goal, workflow=args.workflow, artifacts=args.artifact, first_action=args.next_action),
        "init_stdout": init_stdout,
        "job": payload,
        "prompt_preview": prompt,
        "execution": {
            "mode": "canonical-execution-first" if getattr(args, "auto_execution", True) else "manual-monitor-only",
            "job": execution_job,
            "first_run": execution_start,
        },
        "suggested_owner_updates": {
            "started": f"python3 scripts/openclaw_ops.py --ledger {args.ledger} record-update STARTED {args.task_id} --summary '<what actually started>' --current-checkpoint <step-id> --next-action '<next real action>' --fact key=value",
            "step_progress": f"python3 scripts/openclaw_ops.py --ledger {args.ledger} record-update STEP_PROGRESS {args.task_id} --summary '<what did you actually observe changing>' --current-checkpoint <step-id> --next-action '<next real action>' --fact key=value",
            "step_completed": f"python3 scripts/openclaw_ops.py --ledger {args.ledger} record-update STEP_COMPLETED {args.task_id} --summary '<which step actually completed>' --current-checkpoint <step-id> --next-action '<next real action>' --fact key=value",
            "blocked": f"python3 scripts/openclaw_ops.py --ledger {args.ledger} record-update BLOCKED {args.task_id} --summary '<observed blocker>' --current-checkpoint <step-id> --need '<required unblock action>' --next-action '<safe next step>' --fact failure_type=<observed failure>",
            "task_completed": f"python3 scripts/openclaw_ops.py --ledger {args.ledger} record-update TASK_COMPLETED {args.task_id} --summary '<completion evidence>' --current-checkpoint <step-id> --output <file> --fact key=value",
        },
        "next_commands": {
            "continue_execution": (
                f"python3 scripts/runner_engine.py --jobs-root {args.jobs_root} run-loop {(execution_job or {}).get('job_id', args.execution_job_id or f'{args.task_id}-job')} --execution-owner {args.execution_owner or f'owner:{args.task_id}'}"
                if getattr(args, "auto_execution", True) else None
            ),
            "preview_monitor": f"python3 scripts/openclaw_ops.py --ledger {args.ledger} preview-tick {args.task_id}",
        },
    }, ensure_ascii=False, indent=2))


def cmd_install_monitor(args):
    ledger = load_ledger(args.ledger)
    task = find_task(ledger, args.task_id)
    session_key = session_key_for(task, args.session_key)
    requester_info = normalize_delivery_target(
        task.get("channel") or DEFAULT_CHANNEL,
        args.requester_channel or task.get("message", {}).get("requester_channel_raw") or task.get("message", {}).get("requester_channel") or task.get("message", {}).get("nudge_target") or task.get("channel"),
        task=task,
        session_key=session_key,
    )
    requester_channel = requester_info["target"]
    task.setdefault("message", {})["requester_channel"] = requester_channel
    task["message"]["requester_channel_valid"] = requester_info["valid"]
    task["message"]["requester_channel_source"] = requester_info["source"]
    if requester_info.get("reason"):
        task["message"]["requester_channel_reason"] = requester_info["reason"]
    task["message"]["nudge_target"] = requester_channel
    name = args.name or default_monitor_name(args.task_id)
    prompt = cron_prompt(args.ledger, args.task_id, requester_channel, session_key)
    add_cmd = [
        "openclaw", "cron", "add",
        "--json",
        "--name", name,
        "--agent", args.agent,
        "--session", args.session,
        "--session-key", session_key,
        "--channel", DEFAULT_CHANNEL,
        "--wake", args.wake,
        "--message", prompt,
        "--timeout-seconds", str(args.timeout_seconds),
        "--thinking", args.thinking,
        "--no-deliver",
    ]
    if args.model:
        add_cmd.extend(["--model", args.model])
    if args.every:
        add_cmd.extend(["--every", args.every])
    else:
        add_cmd.extend(["--cron", args.cron_expr, "--tz", args.tz])
    if args.disabled:
        add_cmd.append("--disabled")
    if args.light_context:
        add_cmd.append("--light-context")

    payload = None
    if args.dry_run:
        payload = {
            "id": f"dry-run-{args.task_id}",
            "name": name,
            "schedule": {"kind": "every", "every": args.every} if args.every else {"kind": "cron", "expr": args.cron_expr, "tz": args.tz},
            "sessionKey": session_key,
            "message": prompt,
        }
    else:
        payload = _run_cron_add_with_retry(add_cmd, args.ledger, args.task_id, args.disabled)

    update_monitor_metadata(
        task,
        cron_state="ACTIVE" if not args.disabled else "DISABLED",
        install_signal="INSTALL_OK",
        openclaw_cron_job_id=payload["id"],
        openclaw_cron_name=name,
        openclaw_session_key=session_key,
        openclaw_requester_channel=requester_channel,
        openclaw_schedule=payload.get("schedule") or ({"kind": "every", "every": args.every} if args.every else {"kind": "cron", "expr": args.cron_expr, "tz": args.tz}),
        cron_installed_at=now_iso(),
    )
    save_ledger(args.ledger, ledger)
    print(json.dumps({
        "ok": True,
        "task_id": args.task_id,
        "job": payload,
        "prompt_preview": prompt,
        "ledger": str(args.ledger),
    }, ensure_ascii=False, indent=2))


def cmd_remove_monitor(args):
    ledger = load_ledger(args.ledger)
    task = find_task(ledger, args.task_id)
    monitoring = task.setdefault("monitoring", {})
    job_id = args.job_id or monitoring.get("openclaw_cron_job_id")
    removed = False
    removal_mode = "noop"
    if job_id:
        dry_remove = args.dry_run or str(job_id).startswith("dry-run-") or monitoring.get("cron_state") in {"DISABLED", "DELETED"}
        if dry_remove:
            removed = True
            removal_mode = "ledger-only"
        else:
            shell_cmd = " ".join(shlex.quote(part) for part in ["openclaw", "cron", "rm", "--json", job_id])
            proc = run(shell_cmd, shell=True, check=False)
            stdout = proc.stdout or ""
            stderr = proc.stderr or ""
            if proc.returncode == 0:
                removed = True
                removal_mode = "openclaw-cron-rm"
            elif "not found" in stdout.lower() or "not found" in stderr.lower() or "unknown job" in stdout.lower() or "unknown job" in stderr.lower():
                removed = True
                removal_mode = "already-absent"
            else:
                raise SystemExit(f"openclaw cron rm failed (exit={proc.returncode})\nSTDOUT:\n{stdout}\nSTDERR:\n{stderr}")
    monitoring["cron_state"] = "DELETED"
    monitoring["cron_removed_at"] = now_iso()
    save_ledger(args.ledger, ledger)
    print(json.dumps({"ok": True, "task_id": args.task_id, "job_id": job_id, "removed": removed, "removal_mode": removal_mode}, ensure_ascii=False, indent=2))


def cmd_rerun_task(args):
    facts = parse_key_values(args.fact)
    cmd = [
        "python3", str(TASK_LEDGER), "--ledger", str(args.ledger), "rerun", args.task_id,
        "--reason", args.reason,
    ]
    if args.summary:
        cmd.extend(["--summary", args.summary])
    if args.current_checkpoint:
        cmd.extend(["--current-checkpoint", args.current_checkpoint])
    if args.next_action:
        cmd.extend(["--next-action", args.next_action])
    if args.previous_status:
        cmd.extend(["--previous-status", args.previous_status])
    for key, value in facts.items():
        cmd.extend(["--fact", f"{key}={value}"])
    result = run(*cmd)
    payload = parse_json_from_mixed_output(result.stdout)
    print(json.dumps(payload, ensure_ascii=False, indent=2))


def cmd_ack_delivery(args):
    result = run(
        "python3", str(TASK_LEDGER), "--ledger", str(args.ledger), "ack-delivery", args.task_id, args.update_id,
        "--delivered-via", args.delivered_via,
        *( ["--message-ref", args.message_ref] if args.message_ref else [] ),
        *( ["--note", args.note] if args.note else [] ),
    )
    print(result.stdout.strip())



def cmd_flush_pending_updates(args):
    result = deliver_pending_updates(args.ledger, args.task_id, delivered_via=args.delivered_via, note=args.note)
    print(json.dumps({
        "ok": True,
        "task_id": args.task_id,
        "flushed_count": result["delivered_count"],
        "flushed_update_ids": result["delivered_update_ids"],
        "pending_remaining": result["pending_remaining"],
        "delivered_total": result["delivered_total"],
        "failures": result["failures"],
    }, ensure_ascii=False, indent=2))


def cmd_preview_tick(args):
    run_json = json.loads(run("python3", str(MONITOR_NUDGE), "--ledger", str(args.ledger), "--apply-supervision").stdout)
    ledger = load_ledger(args.ledger)
    task = find_task(ledger, args.task_id)
    report = next(item for item in run_json["reports"] if item["task_id"] == args.task_id)
    notify = report["state"] in {"NUDGE_MAIN_AGENT", "OWNER_RECONCILE", "TRUTH_INCONSISTENT", "BLOCKED_ESCALATE"}
    reporting = ensure_reporting(task)
    pending = reporting.get("pending_updates", [])
    deliverable = [u for u in pending if not u.get("delivered")]
    print(json.dumps({
        "task_id": args.task_id,
        "state": report["state"],
        "reason": report["reason"],
        "notify": notify,
        "notification": format_notification(task, report),
        "remove_monitor": report["state"] in {"BLOCKED_ESCALATE", "STOP_AND_DELETE"},
        "current_step": report.get("current_step"),
        "retry_count": report.get("retry_count", {}),
        "pending_user_updates": pending,
        "pending_user_updates_deliverable": [u["update_id"] for u in deliverable],
        "pending_user_updates_deliverable_count": len(deliverable),
        "truth_state": report.get("truth_state"),
        "inconsistencies": report.get("inconsistencies", []),
        "user_facing": report.get("user_facing") or task.get("derived", {}).get("user_facing"),
    }, ensure_ascii=False, indent=2))


def cmd_resolve_artifact(args):
    print(json.dumps(resolve_output_variants(args.expected_path, min_video_bytes=args.min_video_bytes), ensure_ascii=False, indent=2))


def cmd_reconcile_execution_terminal(args):
    job_id = args.job_id or f"{args.task_id}-job"
    job_path = args.jobs_root / job_id / "job.json"
    if not job_path.exists():
        raise SystemExit(f"job state not found: {job_path}")
    job = json.loads(job_path.read_text())
    status = str(job.get("status") or "").upper()
    failed = list(job.get("failed") or [])
    artifacts = [item.get("path") for item in (job.get("artifacts") or []) if item.get("path")]
    checkpoint = f"step-{len(job.get('items') or []):02d}" if (job.get('items') or []) else "step-00"

    if failed or status in {"FAILED", "BLOCKED"}:
        cmd = [
            "python3", str(EXECUTION_BRIDGE), "--ledger", str(args.ledger), "blocked", args.task_id,
            "--checkpoint", checkpoint,
            "--summary", f"Execution finished with failed items: {', '.join(failed) if failed else status.lower()}",
            "--safe-next-step", "Reconcile partial success truth: report existing artifacts, then decide whether to retry missing failed items",
            "--next-action", "Do not mark SUCCESS; report partial success and missing steps explicitly",
            "--fact", f"job_id={job_id}",
            "--fact", f"failed_items={','.join(failed)}",
            "--fact", "failure_type=EXECUTION_PARTIAL_FAILURE",
            "--fact", f"artifact_count={len(artifacts)}",
        ]
        result = run(*cmd)
        payload = {"ok": True, "task_id": args.task_id, "job_id": job_id, "reconciled_to": "BLOCKED", "failed": failed, "artifacts": artifacts, "stdout": result.stdout.strip()}
    else:
        cmd = [
            "python3", str(EXECUTION_BRIDGE), "--ledger", str(args.ledger), "task-completed", args.task_id,
            "--checkpoint", checkpoint,
            "--summary", f"Execution job completed: {job_id}",
            "--fact", f"job_id={job_id}",
            "--fact", f"completed_items={len(job.get('completed') or [])}",
        ]
        for art in artifacts:
            cmd.extend(["--artifact", art])
        result = run(*cmd)
        payload = {"ok": True, "task_id": args.task_id, "job_id": job_id, "reconciled_to": "TASK_COMPLETED", "artifacts": artifacts, "stdout": result.stdout.strip()}
    print(json.dumps(payload, ensure_ascii=False, indent=2))


def build_parser():
    p = argparse.ArgumentParser(description="OpenClaw-native lifecycle helpers for long-task-control")
    p.add_argument("--ledger", type=Path, default=DEFAULT_LEDGER)
    sp = p.add_subparsers(dest="command", required=True)

    activation_p = sp.add_parser("activation")
    activation_p.add_argument("--task-note")
    activation_p.set_defaults(func=cmd_activation)

    def add_task_init_args(parser):
        parser.add_argument("task_id")
        parser.add_argument("--goal", required=True)
        parser.add_argument("--owner", default="main-agent")
        parser.add_argument("--channel", default=DEFAULT_CHANNEL)
        parser.add_argument("--requester-channel")
        parser.add_argument("--nudge-channel")
        parser.add_argument("--nudge-target")
        parser.add_argument("--workflow", action="append")
        parser.add_argument("--activation-announced", action="store_true", default=True)
        parser.add_argument("--message-ref", default="openclaw:activation")
        parser.add_argument("--summary")
        parser.add_argument("--fact", action="append")
        parser.add_argument("--artifact", action="append")
        parser.add_argument("--note", action="append")
        parser.add_argument("--next-action", required=True)
        parser.add_argument("--expected-interval-sec", type=int, default=300)
        parser.add_argument("--timeout-sec", type=int, default=1800)
        parser.add_argument("--nudge-after-sec", type=int)
        parser.add_argument("--renotify-interval-sec", type=int, default=300)
        parser.add_argument("--max-nudges", type=int, default=3)
        parser.add_argument("--escalate-after-nudges", type=int, default=2)
        parser.add_argument("--blocked-escalate-after-sec", type=int, default=1800)
        parser.add_argument("--print-activation", action="store_true")
        parser.add_argument("--task-note")

    def add_monitor_install_args(parser):
        parser.add_argument("--requester-channel")
        parser.add_argument("--session-key")
        parser.add_argument("--name")
        parser.add_argument("--agent", default=DEFAULT_AGENT)
        parser.add_argument("--session", default="isolated")
        parser.add_argument("--wake", default="now")
        parser.add_argument("--every", default=DEFAULT_MONITOR_EVERY)
        parser.add_argument("--cron-expr")
        parser.add_argument("--tz", default=DEFAULT_TIMEZONE)
        parser.add_argument("--timeout-seconds", type=int, default=240)
        parser.add_argument("--thinking", default="low")
        parser.add_argument("--model")
        parser.add_argument("--disabled", action="store_true")
        parser.add_argument("--light-context", action="store_true")
        parser.add_argument("--dry-run", action="store_true")

    init_p = sp.add_parser("init-task")
    add_task_init_args(init_p)
    init_p.set_defaults(func=cmd_init_task)

    activate_p = sp.add_parser("activate-task")
    add_task_init_args(activate_p)
    activate_p.add_argument("--session-key")
    activate_p.add_argument("--name")
    activate_p.add_argument("--agent", default=DEFAULT_AGENT)
    activate_p.add_argument("--session", default="isolated")
    activate_p.add_argument("--wake", default="now")
    activate_p.add_argument("--every", default="5m")
    activate_p.add_argument("--cron-expr")
    activate_p.add_argument("--tz", default=DEFAULT_TIMEZONE)
    activate_p.add_argument("--monitor-timeout-seconds", type=int, default=240)
    activate_p.add_argument("--thinking", default="low")
    activate_p.add_argument("--model")
    activate_p.add_argument("--disabled", action="store_true")
    activate_p.add_argument("--light-context", action="store_true")
    activate_p.add_argument("--dry-run", action="store_true")
    activate_p.set_defaults(func=cmd_activate_task)

    bootstrap_p = sp.add_parser("bootstrap-task")
    add_task_init_args(bootstrap_p)
    bootstrap_p.add_argument("--session-key")
    bootstrap_p.add_argument("--name")
    bootstrap_p.add_argument("--agent", default=DEFAULT_AGENT)
    bootstrap_p.add_argument("--session", default="isolated")
    bootstrap_p.add_argument("--wake", default="now")
    bootstrap_p.add_argument("--every", default="5m")
    bootstrap_p.add_argument("--cron-expr")
    bootstrap_p.add_argument("--tz", default=DEFAULT_TIMEZONE)
    bootstrap_p.add_argument("--monitor-timeout-seconds", type=int, default=240)
    bootstrap_p.add_argument("--thinking", default="low")
    bootstrap_p.add_argument("--model")
    bootstrap_p.add_argument("--disabled", action="store_true")
    bootstrap_p.add_argument("--light-context", action="store_true")
    bootstrap_p.add_argument("--dry-run", action="store_true")
    bootstrap_p.add_argument("--jobs-root", default=str(ROOT / "state" / "jobs"))
    bootstrap_p.add_argument("--execution-job-id")
    bootstrap_p.add_argument("--execution-adapter", default="generic_manual")
    bootstrap_p.add_argument("--execution-owner")
    bootstrap_p.add_argument("--auto-run-max-steps", type=int)
    bootstrap_p.add_argument("--no-auto-execution", dest="auto_execution", action="store_false")
    bootstrap_p.add_argument("--no-auto-start-execution", dest="auto_start_execution", action="store_false")
    bootstrap_p.set_defaults(func=cmd_activate_task, auto_execution=True, auto_start_execution=True)

    prompt_p = sp.add_parser("render-monitor-prompt")
    prompt_p.add_argument("task_id")
    prompt_p.add_argument("--requester-channel")
    prompt_p.add_argument("--session-key")
    prompt_p.set_defaults(func=cmd_render_prompt)

    install_p = sp.add_parser("install-monitor")
    install_p.add_argument("task_id")
    add_monitor_install_args(install_p)
    install_p.set_defaults(func=cmd_install_monitor)

    rm_p = sp.add_parser("remove-monitor")
    rm_p.add_argument("task_id")
    rm_p.add_argument("--job-id")
    rm_p.add_argument("--dry-run", action="store_true")
    rm_p.set_defaults(func=cmd_remove_monitor)

    record_p = sp.add_parser("record-update")
    record_p.add_argument("state", choices=STATE_CHOICES)
    record_p.add_argument("task_id")
    record_p.add_argument("--summary", required=True)
    record_p.add_argument("--status")
    record_p.add_argument("--current-checkpoint")
    record_p.add_argument("--next-action")
    record_p.add_argument("--safe-next-step")
    record_p.add_argument("--fact", action="append")
    record_p.add_argument("--output", action="append")
    record_p.add_argument("--validation", action="append")
    record_p.add_argument("--completed-checkpoint", action="append")
    record_p.add_argument("--tried", action="append")
    record_p.add_argument("--need", action="append")
    record_p.add_argument("--resume-token")
    record_p.set_defaults(func=cmd_record_update)

    reconcile_p = sp.add_parser("reconcile-before-block")
    reconcile_p.add_argument("task_id")
    reconcile_p.add_argument("expected_path")
    reconcile_p.add_argument("--current-checkpoint")
    reconcile_p.add_argument("--next-action")
    reconcile_p.add_argument("--safe-next-step")
    reconcile_p.add_argument("--summary")
    reconcile_p.add_argument("--summary-if-resolved")
    reconcile_p.add_argument("--summary-if-blocked")
    reconcile_p.add_argument("--fact", action="append")
    reconcile_p.add_argument("--need", action="append")
    reconcile_p.add_argument("--fact-key", default="video_path")
    reconcile_p.add_argument("--min-video-bytes", type=int, default=500000)
    reconcile_p.set_defaults(func=cmd_reconcile_before_block)

    recover_p = sp.add_parser("recover-external-success")
    recover_p.add_argument("task_id")
    recover_p.add_argument("expected_path")
    recover_p.add_argument("--current-checkpoint", required=True)
    recover_p.add_argument("--next-action")
    recover_p.add_argument("--summary")
    recover_p.add_argument("--fact", action="append")
    recover_p.add_argument("--fact-key", default="video_path")
    recover_p.add_argument("--min-video-bytes", type=int, default=500000)
    recover_p.set_defaults(func=cmd_recover_external_success)

    rerun_p = sp.add_parser("rerun-task")
    rerun_p.add_argument("task_id")
    rerun_p.add_argument("--reason", required=True)
    rerun_p.add_argument("--summary")
    rerun_p.add_argument("--current-checkpoint")
    rerun_p.add_argument("--next-action")
    rerun_p.add_argument("--previous-status")
    rerun_p.add_argument("--fact", action="append")
    rerun_p.set_defaults(func=cmd_rerun_task)

    ack_p = sp.add_parser("ack-delivery")
    ack_p.add_argument("task_id")
    ack_p.add_argument("update_id")
    ack_p.add_argument("--delivered-via", default="message.send")
    ack_p.add_argument("--message-ref")
    ack_p.add_argument("--note")
    ack_p.set_defaults(func=cmd_ack_delivery)

    flush_p = sp.add_parser("flush-pending-updates")
    flush_p.add_argument("task_id")
    flush_p.add_argument("--delivered-via", default="monitor.delivery_push")
    flush_p.add_argument("--note")
    flush_p.set_defaults(func=cmd_flush_pending_updates)

    preview_p = sp.add_parser("preview-tick")
    preview_p.add_argument("task_id")
    preview_p.set_defaults(func=cmd_preview_tick)

    resolve_artifact_p = sp.add_parser("resolve-artifact")
    resolve_artifact_p.add_argument("expected_path")
    resolve_artifact_p.add_argument("--min-video-bytes", type=int, default=500000)
    resolve_artifact_p.set_defaults(func=cmd_resolve_artifact)

    reconcile_terminal_p = sp.add_parser("reconcile-execution-terminal")
    reconcile_terminal_p.add_argument("task_id")
    reconcile_terminal_p.add_argument("--jobs-root", type=Path, default=ROOT / "state" / "jobs")
    reconcile_terminal_p.add_argument("--job-id")
    reconcile_terminal_p.set_defaults(func=cmd_reconcile_execution_terminal)

    init_exec_job_p = sp.add_parser("init-execution-job")
    init_exec_job_p.add_argument("task_id")
    init_exec_job_p.add_argument("--jobs-root", default=str(ROOT / "state" / "jobs"))
    init_exec_job_p.add_argument("--job-id")
    init_exec_job_p.add_argument("--adapter", default="generic_manual")
    init_exec_job_p.set_defaults(func=cmd_init_execution_job)

    # Executor commands
    executor_preview_p = sp.add_parser("executor-preview")
    executor_preview_p.add_argument("task_id")
    executor_preview_p.set_defaults(func=cmd_executor_preview)

    run_executor_p = sp.add_parser("run-executor")
    run_executor_p.add_argument("task_id")
    run_executor_p.add_argument("--agent", default=DEFAULT_AGENT)
    run_executor_p.add_argument("--every", default="5m")
    run_executor_p.add_argument("--cron-expr")
    run_executor_p.add_argument("--tz", default=DEFAULT_TIMEZONE)
    run_executor_p.add_argument("--timeout-seconds", type=int, default=300)
    run_executor_p.add_argument("--thinking", default="medium")
    run_executor_p.add_argument("--model")
    run_executor_p.add_argument("--light-context", action="store_true")
    run_executor_p.add_argument("--dry-run", action="store_true")
    run_executor_p.set_defaults(func=cmd_run_executor)

    return p


def main():
    parser = build_parser()
    args = parser.parse_args()
    args.func(args)


if __name__ == "__main__":
    main()
