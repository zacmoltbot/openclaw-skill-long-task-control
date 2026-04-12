#!/usr/bin/env python3
import argparse
import json
import shlex
import subprocess
from datetime import datetime, timezone
from pathlib import Path
from typing import Any

from reporting_contract import ensure_reporting

STATE_CHOICES = ["STARTED", "CHECKPOINT", "BLOCKED", "COMPLETED"]

ROOT = Path(__file__).resolve().parent.parent
TASK_LEDGER = ROOT / "scripts" / "task_ledger.py"
MONITOR_NUDGE = ROOT / "scripts" / "monitor_nudge.py"
DEFAULT_LEDGER = ROOT / "state" / "long-task-ledger.example.json"
DEFAULT_CHANNEL = "discord"
DEFAULT_TIMEZONE = "Asia/Taipei"
DEFAULT_AGENT = "main"
DEFAULT_SESSION_KEY = "agent:main:discord:channel:{channel}"
DEFAULT_MONITOR_EVERY = "5m"

ACTIVATION_TEMPLATE = """ACTIVATED
- skill: long-task-control
- announcement: 目前這個 task 會採用 long-task-control SKILL 執行
- reporting: 我會用 checkpoint / blocker / completed 這類可驗證狀態回報進度；有新事實才更新，不用模糊的「還在跑」敘述
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

嚴格步驟：
1) exec：python3 {ROOT / 'scripts' / 'openclaw_ops.py'} --ledger {ledger_path} preview-tick {task_id}
2) 解析 JSON，只使用該命令回傳的 state / notify / notification / remove_monitor / reason / pending_user_updates_deliverable。
3) 被動 delivery push（每次 tick 必定執行）：
   - 若 `pending_user_updates_deliverable_count > 0`，對每一筆 `pending_user_updates` 中 `delivered=false` 的項目：
     (a) 用 `message.send --channel {requester_channel} --message "<update['status_block']>"` 發送到 Discord
     (b) 若 message.send 回傳成功，exec：`python3 {ROOT / 'scripts' / 'openclaw_ops.py'} --ledger {ledger_path} ack-delivery {task_id} <update_id> --delivered-via message.send`
     (c) 若 message.send 回傳失敗但訊息已出現在 channel，視為 delivered=true，仍更新 ledger
   - 這層 delivery push 不需要 main agent 主動觸發；monitor 自己被動執行，確保 user update 不會「你不來問就不報」。
4) 依 state 分流（僅在 notify=true 時發 Discord，delivery push 不受此限制）：
   - OK（含 noop_external_wait）：不要發 Discord；輸出 1 行 summary 即可。
   - HEARTBEAT_DUE / STALE_PROGRESS：不要發 Discord（這些只是 pre-gate warnings）；輸出 1 行 summary。
   - NUDGE_MAIN_AGENT / OWNER_RECONCILE：只有在 notify=true 時才用 message.send 發到 Discord channel `{requester_channel}`，內容直接使用 notification；若 message.send 回傳失敗但訊息已出現在 channel，視為 delivered=true，summary 註明 delivery=best_effort。
   - BLOCKED_ESCALATE：
     (a) 用 message.send 發到 Discord channel `{requester_channel}`，notification 內容需一次講清楚：哪個 step 卡住、retry 次數、嘗試了什麼、為什麼現在判定失敗、建議下一步。
     (b) 發完後立即 exec：python3 {ROOT / 'scripts' / 'openclaw_ops.py'} --ledger {ledger_path} remove-monitor {task_id}
   - STOP_AND_DELETE：不要再發 Discord；直接 exec：python3 {ROOT / 'scripts' / 'openclaw_ops.py'} --ledger {ledger_path} remove-monitor {task_id}
5) message.send 內容必須簡短、fact-based；BLOCKED_ESCALATE 的 exception：可以稍長但要一次把 blocker 交代清楚，不要分多次補述。
6) remove-monitor 是 idempotent cleanup；若 job 已不存在但 ledger 成功標成 DELETED，也算 cron_removed=yes。
7) 這個 cron job 綁定的 main session key 是 `{session_key}`；NUDGE_MAIN_AGENT 文案要明確寫「請 main agent 先自救：resume / rebuild-safe-step / reconcile 缺漏 checkpoint」，只有真的無法自救才等 BLOCKED_ESCALATE。

輸出限制：最後只輸出一小段 JSON summary，包含 task_id、state、notified、cron_removed、delivery、pending_delivered。"""


def format_notification(task, report):
    state = report["state"]
    payload = report.get("action_payload") or {}
    facts = payload.get("facts") or {}
    task_id = report["task_id"]
    next_action = report.get("next_action") or task.get("next_action")
    if state == "NUDGE_MAIN_AGENT":
        return "\n".join([
            "【long-task-control / execution nudge】",
            f"task_id={task_id}",
            f"state={state}",
            f"reason={report['reason']}",
            f"next_action={next_action}",
            "請 main agent 立刻回來續行，先自救：resume / rebuild-safe-step / reconcile 缺漏 checkpoint；真的推不動才標記 BLOCKED。",
        ])
    if state == "OWNER_RECONCILE":
        branches = facts.get("branches") or {}
        suspicious_jobs = facts.get("suspicious_external_jobs") or []
        required_provider_evidence = facts.get("required_provider_evidence") or []
        lines = [
            "【long-task-control / owner reconcile】",
            f"task_id={task_id}",
            f"state={state}",
            f"reason={report['reason']}",
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
    if goal and state in {"STARTED", "COMPLETED"}:
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
        lines.append("- output artifacts:" if state == "COMPLETED" else "- outputs:")
        for item in outputs:
            lines.append(f"  - {item}")
    if completed and state == "COMPLETED":
        lines.append("- completed checkpoints:")
        for item in completed:
            lines.append(f"  - {item}")
    if tried and state == "BLOCKED":
        lines.append("- tried:")
        for item in tried:
            lines.append(f"  - {item}")
    if validation and state == "COMPLETED":
        lines.append("- validation:")
        for item in validation:
            lines.append(f"  - {item}")
    if state == "COMPLETED":
        lines.append("- background items still running:")
        lines.append("  - none")
    if blocker and state == "BLOCKED":
        lines.append(f"- blocker: {blocker}")
    if need and state == "BLOCKED":
        lines.append("- need:")
        for item in need:
            lines.append(f"  - {item}")
    if next_action:
        lines.append(f"- {'handoff' if state == 'COMPLETED' else 'next'}: {next_action}")
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


def cmd_record_update(args):
    facts = parse_key_values(args.fact)
    outputs = args.output or []
    validation = list(args.validation or [])
    if args.state == "COMPLETED" and outputs:
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
        if args.current_checkpoint:
            cmd.extend(["--current-checkpoint", args.current_checkpoint])
        if args.next_action:
            cmd.extend(["--next-action", args.next_action])
        ledger_result = run(*cmd)
    else:
        status = "COMPLETED" if args.state == "COMPLETED" else (args.status or "RUNNING")
        summary = args.summary or f"{args.state} recorded via execution wrapper"
        cmd = [
            "python3", str(TASK_LEDGER), "--ledger", str(args.ledger), "checkpoint", args.task_id,
            "--kind", args.state,
            "--summary", summary,
            "--status", status,
        ]
        for key, value in facts.items():
            cmd.extend(["--fact", f"{key}={value}"])
        for item in outputs:
            cmd.extend(["--artifact", item])
        if args.current_checkpoint:
            cmd.extend(["--current-checkpoint", args.current_checkpoint])
        if args.next_action:
            cmd.extend(["--next-action", args.next_action])
        ledger_result = run(*cmd)
        if args.state == "COMPLETED" and validation:
            ledger = load_ledger(args.ledger)
            task = find_task(ledger, args.task_id)
            task.setdefault("validation", []).extend(validation)
            save_ledger(args.ledger, ledger)

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
            checkpoint=args.current_checkpoint or task.get("current_checkpoint"),
            workflow_steps=[step.get("title") for step in task.get("workflow", [])],
            facts=facts,
            outputs=outputs,
            completed=args.completed_checkpoint,
            validation=validation,
            blocker=args.summary if args.state == "BLOCKED" else None,
            tried=args.tried,
            need=args.need,
            next_action=args.next_action or task.get("next_action"),
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
    task.setdefault("message", {})["requester_channel"] = args.requester_channel or task.get("channel")
    task["message"]["nudge_channel"] = args.nudge_channel or task.get("channel")
    task["message"]["nudge_target"] = args.nudge_target or args.requester_channel or task.get("channel")
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
    requester_channel = args.requester_channel or task.get("message", {}).get("nudge_target") or task.get("channel")
    session_key = session_key_for(task, args.session_key)
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
    requester_channel = install_ns.requester_channel or task.get("message", {}).get("nudge_target") or task.get("channel")
    session_key = session_key_for(task, install_ns.session_key)
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
        "--model", install_ns.model,
    ]
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
        "suggested_owner_updates": {
            "started": f"python3 scripts/openclaw_ops.py --ledger {args.ledger} record-update STARTED {args.task_id} --summary '<what actually started>' --current-checkpoint <step-id> --next-action '<next real action>' --fact key=value",
            "checkpoint": f"python3 scripts/openclaw_ops.py --ledger {args.ledger} record-update CHECKPOINT {args.task_id} --summary '<what verifiably changed>' --current-checkpoint <step-id> --next-action '<next real action>' --fact key=value",
            "blocked": f"python3 scripts/openclaw_ops.py --ledger {args.ledger} record-update BLOCKED {args.task_id} --summary '<blocker>' --current-checkpoint <step-id> --need '<required unblock action>' --next-action '<safe next step>'",
            "completed": f"python3 scripts/openclaw_ops.py --ledger {args.ledger} record-update COMPLETED {args.task_id} --summary '<completion evidence>' --current-checkpoint <step-id> --output <file> --fact key=value",
        },
    }, ensure_ascii=False, indent=2))


def cmd_install_monitor(args):
    ledger = load_ledger(args.ledger)
    task = find_task(ledger, args.task_id)
    requester_channel = args.requester_channel or task.get("message", {}).get("nudge_target") or task.get("channel")
    session_key = session_key_for(task, args.session_key)
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
        "--model", args.model,
    ]
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


def cmd_preview_tick(args):
    run_json = json.loads(run("python3", str(MONITOR_NUDGE), "--ledger", str(args.ledger), "--apply-supervision").stdout)
    ledger = load_ledger(args.ledger)
    task = find_task(ledger, args.task_id)
    report = next(item for item in run_json["reports"] if item["task_id"] == args.task_id)
    notify = report["state"] in {"NUDGE_MAIN_AGENT", "OWNER_RECONCILE", "BLOCKED_ESCALATE"}
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
    }, ensure_ascii=False, indent=2))


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
        parser.add_argument("--model", default="minimax/MiniMax-M2.7")
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
    activate_p.add_argument("--model", default="minimax/MiniMax-M2.7")
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
    bootstrap_p.add_argument("--model", default="minimax/MiniMax-M2.7")
    bootstrap_p.add_argument("--disabled", action="store_true")
    bootstrap_p.add_argument("--light-context", action="store_true")
    bootstrap_p.add_argument("--dry-run", action="store_true")
    bootstrap_p.set_defaults(func=cmd_activate_task)

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
    record_p.set_defaults(func=cmd_record_update)

    preview_p = sp.add_parser("preview-tick")
    preview_p.add_argument("task_id")
    preview_p.set_defaults(func=cmd_preview_tick)
    return p


def main():
    parser = build_parser()
    args = parser.parse_args()
    args.func(args)


if __name__ == "__main__":
    main()
