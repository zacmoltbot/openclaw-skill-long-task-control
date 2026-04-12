# Prompt contract

Use this contract when another agent instance loads `long-task-control` and needs the default lifecycle to happen *without remembering extra bootstrap lore*.

## Default contract

If the task is long-running by the detection checklist, treat this lifecycle as the default:

1. emit `ACTIVATED`
2. create `TASK START`
3. initialize the durable ledger
4. install the monitor cron
5. spawn the executor subagent (for multi-stage tasks)
6. use `record-update` for STARTED / CHECKPOINT / BLOCKED / COMPLETED
7. let monitor drive nudge / reconcile / blocked-escalate / cleanup, with user-centered bias: first push resume / rebuild-safe-step / reconcile / 補做, and only use blocked escalation once self-recovery is no longer viable; if an external job is marked pending/running, require minimum `provider_evidence` (real provider job id / receipt / status handle / artifact handle). A weak pending claim without evidence must enter owner-evidence reconcile first, not be trusted as legitimate external wait
8. GAP-1 one-shot nudge + escalate: NUDGE once if a step is done but next hasn't started, then immediately BLOCKED_ESCALATE on next tick if still stalled
9. passive delivery push: monitor checks `pending_updates[]` every tick and sends to Discord without waiting for main agent
10. remove the monitor on terminal state

## Executor prompt contract

When the executor subagent receives the task goal and workflow, it follows this loop:

```
loop:
  preview = executor_engine.py preview <task_id>
  if preview.action == "execute_step":
    execute the step (shell/API/file operation)
    task_ledger.py checkpoint <task_id> --kind CHECKPOINT --summary "..." --current-checkpoint <step-id> --next-action "..."
    goto loop
  elif preview.action == "workflow_complete":
    task_ledger.py checkpoint <task_id> --kind COMPLETED --summary "..."
    output "EXECUTOR_DONE"; exit
  elif preview.action == "blocked":
    output "EXECUTOR_BLOCKED"; exit
  else:
    output "EXECUTOR_DONE"; exit
```

Auto-resume: the executor reads the ledger's `current_checkpoint` on each wake and continues from the next pending step. If step-01 is DONE and step-02 is RUNNING, step-02 starts immediately.

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

# Then spawn the executor:
python3 scripts/openclaw_ops.py --ledger state/long-task-ledger.json run-executor <task_id>
```

The command should be treated as the canonical entrypoint for OpenClaw-native long tasks.

## Owner update rule

After bootstrap, do **not** freehand the ledger mutations. Prefer:

```bash
python3 scripts/openclaw_ops.py --ledger state/long-task-ledger.json record-update <STARTED|CHECKPOINT|BLOCKED|COMPLETED> <task_id> ...
```

This keeps user-visible execution truth and ledger truth bound to the same operation.

Each such command now also creates a durable `pending_user_update` / `reporting.pending_updates[]` obligation. Treat that as mandatory follow-up: after the requester-visible message is actually sent, run `python3 scripts/task_ledger.py --ledger state/long-task-ledger.json ack-delivery <task_id> <update_id> --message-ref <message-ref>`.

## GAP-1: one-shot nudge + escalate

When the monitor detects a step is terminal (DONE/COMPLETED) but the next step hasn't started:

1. First tick: `NUDGE_MAIN_AGENT` once; record step in `monitoring.gap1_nudged_steps[]`
2. Second tick (same step still stalled): immediately `BLOCKED_ESCALATE`; cron deletes itself
3. No repeat Discord notifications for the same GAP-1 event

## Passive delivery push

Every monitor tick MUST check and send pending updates before deciding state:
- Check `reporting.pending_updates[]` for `delivered=false`
- Send each to Discord via `message.send`
- `ack-delivery` after successful send
- This is mandatory, not conditional — always execute before state evaluation

## Advanced / manual path

Only fall back to separate `activation`, `init-task`, `install-monitor`, and `run-executor` commands when you explicitly need split-phase control for debugging or custom orchestration.
