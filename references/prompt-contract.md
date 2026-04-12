# Prompt contract

Use this contract when another agent instance loads `long-task-control` and needs the default lifecycle to happen *without remembering extra bootstrap lore*.

## Default contract

If the task is long-running by the detection checklist, treat this lifecycle as the default:

1. emit `ACTIVATED`
2. create `TASK START`
3. initialize the durable ledger
4. install the monitor cron
5. use `record-update` for STARTED / CHECKPOINT / BLOCKED / COMPLETED
6. let monitor drive nudge / reconcile / blocked-escalate / cleanup, with user-centered bias: first push resume / rebuild-safe-step / reconcile / 補做, and only use blocked escalation once self-recovery is no longer viable; if an external job is marked pending/running, require minimum `provider_evidence` (real provider job id / receipt / status handle / artifact handle). A weak pending claim without evidence must enter owner-evidence reconcile first, not be trusted as legitimate external wait
7. remove the monitor on terminal state

## Preferred integration command

Prefer one command instead of manually chaining activation + init + install-monitor:

```bash
python3 scripts/openclaw_ops.py --ledger state/long-task-ledger.json bootstrap-task <task_id> \
  --goal "<one sentence goal>" \
  --requester-channel <channel-id> \
  --workflow "Inspect inputs" \
  --workflow "Do the work" \
  --workflow "Validate and handoff" \
  --next-action "Publish the first STARTED update" \
  --message-ref "discord:msg:<activation-message-id>"
```

The command should be treated as the canonical entrypoint for OpenClaw-native long tasks.

## Owner update rule

After bootstrap, do **not** freehand the ledger mutations. Prefer:

```bash
python3 scripts/openclaw_ops.py --ledger state/long-task-ledger.json record-update <STARTED|CHECKPOINT|BLOCKED|COMPLETED> <task_id> ...
```

This keeps user-visible execution truth and ledger truth bound to the same operation.

Each such command now also creates a durable `pending_user_update` / `reporting.pending_updates[]` obligation. Treat that as mandatory follow-up: after the requester-visible message is actually sent, run `python3 scripts/task_ledger.py --ledger state/long-task-ledger.json ack-delivery <task_id> <update_id> --message-ref <message-ref>`.

## Advanced / manual path

Only fall back to separate `activation`, `init-task`, and `install-monitor` commands when you explicitly need split-phase control for debugging or custom orchestration.
