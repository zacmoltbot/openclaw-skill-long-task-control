#!/usr/bin/env python3
import argparse
import json
import shlex
import subprocess
from datetime import datetime, timezone
from pathlib import Path
from typing import Any

ROOT = Path(__file__).resolve().parent.parent
TASK_LEDGER = ROOT / "scripts" / "task_ledger.py"
MONITOR_NUDGE = ROOT / "scripts" / "monitor_nudge.py"
DEFAULT_LEDGER = ROOT / "state" / "long-task-ledger.example.json"
DEFAULT_CHANNEL = "discord"
DEFAULT_TIMEZONE = "Asia/Taipei"
DEFAULT_AGENT = "main"
DEFAULT_SESSION_KEY = "agent:main:discord:channel:{channel}"

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


def cron_prompt(ledger_path: Path, task_id: str, requester_channel: str, session_key: str):
    return f"""你是 OpenClaw 的 long-task-control monitor agent。你的唯一職責：讀取 task ledger，對 task `{task_id}` 執行一次低成本 monitor tick，必要時主動提醒 main agent 繼續做，並在 terminal 狀態時移除自己的 cron job。

嚴格步驟：
1) exec：python3 {MONITOR_NUDGE} --ledger {ledger_path} --apply-supervision --only-active
2) 解析輸出的 JSON，只取 task_id=`{task_id}` 那一筆 report。
3) 依 report.state 分流：
   - OK / HEARTBEAT_DUE / STALE_PROGRESS：不要發 Discord；輸出 1 行 summary 即可。
   - NUDGE_MAIN_AGENT：用 message.send 發到 Discord channel `{requester_channel}`，內容要包含 task_id、reason、next_action，明確要求 main agent 回來續行或補 checkpoint。
   - OWNER_RECONCILE：用 message.send 發到 Discord channel `{requester_channel}`，內容要包含 A/B/C/D/E reconcile branches，要求 main agent 立即對 owner truth 做 reconcile。
   - BLOCKED_ESCALATE：用 message.send 發到 Discord channel `{requester_channel}`，內容要包含 blocker reason / need / safe_next_step；然後 exec：python3 {ROOT / 'scripts' / 'openclaw_ops.py'} --ledger {ledger_path} remove-monitor {task_id}
   - STOP_AND_DELETE：不要再提醒；直接 exec：python3 {ROOT / 'scripts' / 'openclaw_ops.py'} --ledger {ledger_path} remove-monitor {task_id}
4) 如果有發 Discord 訊息，文字必須簡短、fact-based、不可重述整個任務歷史。
5) 若 remove-monitor 執行成功，輸出 summary 時必須標明 cron_removed=yes。
6) 這個 cron job 綁定的 main session key 是 `{session_key}`；提醒文案要明確寫「請 main agent 繼續做 / reconcile / 收尾」。

輸出限制：最後只輸出一小段 JSON summary，包含 task_id、state、notified、cron_removed。"""


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
            "請 main agent 立刻回來續行、補 checkpoint，或明確標記 COMPLETED / FAILED / BLOCKED。",
        ])
    if state == "OWNER_RECONCILE":
        branches = facts.get("branches") or {}
        lines = [
            "【long-task-control / owner reconcile】",
            f"task_id={task_id}",
            f"state={state}",
            f"reason={report['reason']}",
            "請 main agent 立即 reconcile owner truth：",
        ]
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


def cmd_activation(args):
    print(activation_block(args.task_note))


def cmd_init_task(args):
    task_note = args.task_note or args.goal
    if args.print_activation:
        print(activation_block(task_note))
        print()
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
    task.setdefault("monitoring", {})["cron_state"] = "PENDING_INSTALL"
    save_ledger(args.ledger, ledger)

    print(json.dumps({
        "ok": True,
        "task_id": args.task_id,
        "activation": activation_block(task_note),
        "ledger": str(args.ledger),
        "requester_channel": task["message"]["requester_channel"],
        "stdout": result.stdout.strip(),
    }, ensure_ascii=False, indent=2))


def cmd_render_prompt(args):
    ledger = load_ledger(args.ledger)
    task = find_task(ledger, args.task_id)
    requester_channel = args.requester_channel or task.get("message", {}).get("nudge_target") or task.get("channel")
    session_key = session_key_for(task, args.session_key)
    print(cron_prompt(args.ledger, args.task_id, requester_channel, session_key))


def cmd_install_monitor(args):
    ledger = load_ledger(args.ledger)
    task = find_task(ledger, args.task_id)
    requester_channel = args.requester_channel or task.get("message", {}).get("nudge_target") or task.get("channel")
    session_key = session_key_for(task, args.session_key)
    name = args.name or f"long-task-control monitor {args.task_id}"
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
        shell_cmd = " ".join(shlex.quote(part) for part in add_cmd)
        proc = run(shell_cmd, shell=True, check=False)
        if proc.returncode != 0:
            raise SystemExit(f"openclaw cron add failed (exit={proc.returncode})\nSTDOUT:\n{proc.stdout}\nSTDERR:\n{proc.stderr}")
        payload = parse_json_from_mixed_output(proc.stdout)

    update_monitor_metadata(
        task,
        cron_state="ACTIVE" if not args.disabled else "DISABLED",
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
    if job_id and not args.dry_run:
        shell_cmd = " ".join(shlex.quote(part) for part in ["openclaw", "cron", "rm", "--json", job_id])
        proc = run(shell_cmd, shell=True, check=False)
        if proc.returncode != 0:
            raise SystemExit(f"openclaw cron rm failed (exit={proc.returncode})\nSTDOUT:\n{proc.stdout}\nSTDERR:\n{proc.stderr}")
        removed = True
    elif job_id:
        removed = True
    monitoring["cron_state"] = "DELETED"
    monitoring["cron_removed_at"] = now_iso()
    save_ledger(args.ledger, ledger)
    print(json.dumps({"ok": True, "task_id": args.task_id, "job_id": job_id, "removed": removed}, ensure_ascii=False, indent=2))


def cmd_preview_tick(args):
    run_json = json.loads(run("python3", str(MONITOR_NUDGE), "--ledger", str(args.ledger), "--apply-supervision").stdout)
    ledger = load_ledger(args.ledger)
    task = find_task(ledger, args.task_id)
    report = next(item for item in run_json["reports"] if item["task_id"] == args.task_id)
    print(json.dumps({
        "task_id": args.task_id,
        "state": report["state"],
        "notification": format_notification(task, report),
        "remove_monitor": report["state"] in {"BLOCKED_ESCALATE", "STOP_AND_DELETE"},
    }, ensure_ascii=False, indent=2))


def build_parser():
    p = argparse.ArgumentParser(description="OpenClaw-native lifecycle helpers for long-task-control")
    p.add_argument("--ledger", type=Path, default=DEFAULT_LEDGER)
    sp = p.add_subparsers(dest="command", required=True)

    activation_p = sp.add_parser("activation")
    activation_p.add_argument("--task-note")
    activation_p.set_defaults(func=cmd_activation)

    init_p = sp.add_parser("init-task")
    init_p.add_argument("task_id")
    init_p.add_argument("--goal", required=True)
    init_p.add_argument("--owner", default="main-agent")
    init_p.add_argument("--channel", default=DEFAULT_CHANNEL)
    init_p.add_argument("--requester-channel")
    init_p.add_argument("--nudge-channel")
    init_p.add_argument("--nudge-target")
    init_p.add_argument("--workflow", action="append")
    init_p.add_argument("--activation-announced", action="store_true", default=True)
    init_p.add_argument("--message-ref", default="openclaw:activation")
    init_p.add_argument("--summary")
    init_p.add_argument("--fact", action="append")
    init_p.add_argument("--artifact", action="append")
    init_p.add_argument("--note", action="append")
    init_p.add_argument("--next-action", required=True)
    init_p.add_argument("--expected-interval-sec", type=int, default=900)
    init_p.add_argument("--timeout-sec", type=int, default=1800)
    init_p.add_argument("--nudge-after-sec", type=int)
    init_p.add_argument("--renotify-interval-sec", type=int, default=900)
    init_p.add_argument("--max-nudges", type=int, default=3)
    init_p.add_argument("--escalate-after-nudges", type=int, default=2)
    init_p.add_argument("--blocked-escalate-after-sec", type=int, default=1800)
    init_p.add_argument("--print-activation", action="store_true")
    init_p.add_argument("--task-note")
    init_p.set_defaults(func=cmd_init_task)

    prompt_p = sp.add_parser("render-monitor-prompt")
    prompt_p.add_argument("task_id")
    prompt_p.add_argument("--requester-channel")
    prompt_p.add_argument("--session-key")
    prompt_p.set_defaults(func=cmd_render_prompt)

    install_p = sp.add_parser("install-monitor")
    install_p.add_argument("task_id")
    install_p.add_argument("--requester-channel")
    install_p.add_argument("--session-key")
    install_p.add_argument("--name")
    install_p.add_argument("--agent", default=DEFAULT_AGENT)
    install_p.add_argument("--session", default="isolated")
    install_p.add_argument("--wake", default="now")
    install_p.add_argument("--every", default="10m")
    install_p.add_argument("--cron-expr")
    install_p.add_argument("--tz", default=DEFAULT_TIMEZONE)
    install_p.add_argument("--timeout-seconds", type=int, default=240)
    install_p.add_argument("--thinking", default="low")
    install_p.add_argument("--model", default="minimax/MiniMax-M2.7")
    install_p.add_argument("--disabled", action="store_true")
    install_p.add_argument("--light-context", action="store_true")
    install_p.add_argument("--dry-run", action="store_true")
    install_p.set_defaults(func=cmd_install_monitor)

    rm_p = sp.add_parser("remove-monitor")
    rm_p.add_argument("task_id")
    rm_p.add_argument("--job-id")
    rm_p.add_argument("--dry-run", action="store_true")
    rm_p.set_defaults(func=cmd_remove_monitor)

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
