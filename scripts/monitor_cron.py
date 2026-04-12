#!/usr/bin/env python3
import argparse
import json
import subprocess
from datetime import datetime, timezone
from pathlib import Path

ROOT = Path(__file__).resolve().parent.parent
DEFAULT_LEDGER = ROOT / "state" / "long-task-ledger.example.json"
DEFAULT_CRON_DIR = ROOT / "state" / "monitor-crons"
MONITOR_SCRIPT = ROOT / "scripts" / "monitor_nudge.py"
OPS_SCRIPT = ROOT / "scripts" / "openclaw_ops.py"


def now_iso():
    return datetime.now(timezone.utc).astimezone().isoformat(timespec="seconds")


def load_json(path: Path, default):
    if not path.exists():
        return default
    return json.loads(path.read_text())


def load_ledger(path: Path):
    return load_json(path, {"version": 1, "updated_at": now_iso(), "tasks": []})


def save_json(path: Path, payload):
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(json.dumps(payload, ensure_ascii=False, indent=2) + "\n")


def find_task(ledger, task_id):
    for task in ledger.get("tasks", []):
        if task.get("task_id") == task_id:
            return task
    raise SystemExit(f"Task not found: {task_id}")


def cron_file(cron_dir: Path, task_id: str):
    return cron_dir / f"{task_id}.cron.json"


def cmd_install(args):
    ledger = load_ledger(args.ledger)
    task = find_task(ledger, args.task_id)
    cron_path = cron_file(args.cron_dir, args.task_id)
    payload = {
        "task_id": args.task_id,
        "created_at": now_iso(),
        "cron_spec": args.cron_spec,
        "ledger": str(args.ledger),
        "monitor_script": str(MONITOR_SCRIPT),
        "wrapper_command": (
            f"cd {ROOT} && python3 {ROOT / 'scripts' / 'monitor_cron.py'} run-once "
            f"--ledger {args.ledger} --task-id {args.task_id} --cron-dir {args.cron_dir}"
        ),
        "channel": task.get("channel"),
        "owner": task.get("owner"),
        "status_at_install": task.get("status"),
    }
    save_json(cron_path, payload)

    task.setdefault("monitoring", {})["cron_state"] = "ACTIVE"
    task["monitoring"]["cron_file"] = str(cron_path)
    task["monitoring"]["cron_installed_at"] = payload["created_at"]
    task["monitoring"].setdefault("cron_owner", "long-task-monitor")
    save_json(args.ledger, ledger)

    print(json.dumps({"ok": True, "task_id": args.task_id, "cron_file": str(cron_path), "cron_spec": args.cron_spec}, ensure_ascii=False, indent=2))


def cmd_list(args):
    rows = []
    for path in sorted(args.cron_dir.glob("*.cron.json")):
        rows.append(load_json(path, {}))
    print(json.dumps({"cron_count": len(rows), "crons": rows}, ensure_ascii=False, indent=2))


def cmd_remove(args):
    cron_path = cron_file(args.cron_dir, args.task_id)
    existed = cron_path.exists()
    if existed:
        cron_path.unlink()

    ledger = load_ledger(args.ledger)
    try:
        task = find_task(ledger, args.task_id)
    except SystemExit:
        task = None
    if task:
        task.setdefault("monitoring", {})["cron_state"] = "DELETED"
        task["monitoring"]["cron_removed_at"] = now_iso()
        save_json(args.ledger, ledger)

    print(json.dumps({"ok": True, "task_id": args.task_id, "removed": existed, "cron_file": str(cron_path)}, ensure_ascii=False, indent=2))




def ack_delivery(ledger_path: Path, task_id: str, update_id: str, *, delivered_via: str, note: str | None = None):
    cmd = [
        "python3", str(OPS_SCRIPT), "--ledger", str(ledger_path), "ack-delivery", task_id, update_id,
        "--delivered-via", delivered_via,
    ]
    if note:
        cmd.extend(["--note", note])
    return json.loads(subprocess.run(cmd, check=True, text=True, capture_output=True).stdout)


def push_pending_updates(ledger_path: Path, task_id: str):
    ledger = load_ledger(ledger_path)
    task = find_task(ledger, task_id)
    reporting = task.get("reporting", {})
    pushed = []
    for update in list(reporting.get("pending_updates", [])):
        if update.get("delivered"):
            continue
        ack_delivery(ledger_path, task_id, update["update_id"], delivered_via="monitor.delivery_push", note="Simulated monitor delivery push")
        pushed.append(update["update_id"])
    return pushed

def cmd_run_once(args):
    cron_path = cron_file(args.cron_dir, args.task_id)
    if not cron_path.exists():
        raise SystemExit(f"Monitor cron not installed for {args.task_id}: {cron_path}")

    delivery_push_ids = push_pending_updates(args.ledger, args.task_id)

    proc = subprocess.run(
        ["python3", str(MONITOR_SCRIPT), "--ledger", str(args.ledger), "--apply-supervision"],
        check=True,
        text=True,
        capture_output=True,
    )
    payload = json.loads(proc.stdout)
    report = next((item for item in payload.get("reports", []) if item.get("task_id") == args.task_id), None)
    if not report:
        raise SystemExit(f"Task {args.task_id} not present in monitor output")

    cron_removed = False
    if report["state"] in {"BLOCKED_ESCALATE", "STOP_AND_DELETE"}:
        cron_path.unlink(missing_ok=True)
        cron_removed = True
        ledger = load_ledger(args.ledger)
        task = find_task(ledger, args.task_id)
        task.setdefault("monitoring", {})["cron_state"] = "DELETED"
        task["monitoring"]["cron_removed_at"] = now_iso()
        save_json(args.ledger, ledger)

    print(json.dumps({
        "ok": True,
        "task_id": args.task_id,
        "report": report,
        "cron_removed": cron_removed,
        "cron_file": str(cron_path),
        "delivery_push_count": len(delivery_push_ids),
        "delivery_push_update_ids": delivery_push_ids,
    }, ensure_ascii=False, indent=2))


def build_parser():
    p = argparse.ArgumentParser(description="Pseudo cron wrapper for long-task-control monitor lifecycle")
    p.add_argument("--ledger", type=Path, default=DEFAULT_LEDGER)
    p.add_argument("--cron-dir", type=Path, default=DEFAULT_CRON_DIR)
    sp = p.add_subparsers(dest="command", required=True)

    install = sp.add_parser("install")
    install.add_argument("task_id")
    install.add_argument("--cron-spec", default="*/10 * * * *")
    install.set_defaults(func=cmd_install)

    list_p = sp.add_parser("list")
    list_p.set_defaults(func=cmd_list)

    remove = sp.add_parser("remove")
    remove.add_argument("task_id")
    remove.set_defaults(func=cmd_remove)

    run_once = sp.add_parser("run-once")
    run_once.add_argument("--task-id", required=True)
    run_once.set_defaults(func=cmd_run_once)
    return p


def main():
    parser = build_parser()
    args = parser.parse_args()
    args.func(args)


if __name__ == "__main__":
    main()
