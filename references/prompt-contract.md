# Prompt contract

Use this contract when another agent instance loads `long-task-control` and needs the default lifecycle to happen without remembering bootstrap lore.

## Default contract

For a long-running task, the default lifecycle is now:

1. run `bootstrap-task`
2. let `bootstrap-task` auto-create the durable ledger
3. let `bootstrap-task` install the monitor cron
4. let `bootstrap-task` auto-derive the generic execution job from the workflow
5. let `bootstrap-task` auto-run the first `runner_engine.py run-loop` tick with execution ownership
6. continue execution with `runner_engine.py run-loop` as the canonical execution owner
7. use `record-update` only when the owner is directly writing observed truth outside the runner bridge
8. keep the monitor in the supervision lane: nudge / reconcile / blocked-escalate / cleanup
9. remove the monitor on terminal state

## Canonical integration command

Prefer one canonical command instead of manually chaining activation + init + install-monitor + hand-written job spec:

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

This canonical path now means:

- activation + `TASK START`
- durable ledger init
- monitor install
- auto `init-execution-job`
- auto first `run-loop`
- execution ownership obtained by the runner immediately

Continue with:

```bash
python3 scripts/runner_engine.py --jobs-root state/jobs run-loop <task_id>-job --execution-owner owner:<task_id>
```

## Owner update rule

After bootstrap, do **not** freehand ledger mutations. Prefer:

```bash
python3 scripts/openclaw_ops.py --ledger state/long-task-ledger.json record-update <STARTED|STEP_PROGRESS|STEP_COMPLETED|BLOCKED|TASK_COMPLETED> <task_id> ...
```

Use this only when truth comes from outside the runner bridge.

Each such command creates a durable `pending_user_update` obligation. After the requester-visible message is actually sent, run:

```bash
python3 scripts/task_ledger.py --ledger state/long-task-ledger.json ack-delivery <task_id> <update_id> --message-ref <message-ref>
```

## Monitor contract

The monitor is a supervisor, not the execution owner.

Per tick it must:

- passively deliver `reporting.pending_updates[]`
- detect stale / heartbeat / truth-inconsistent states
- nudge or reconcile the owner when execution stalls
- escalate blocked only after observed truth justifies it
- remove itself on terminal state

## GAP-1: one-shot nudge + escalate

When the monitor detects a step is terminal but the next step has not started:

1. first tick: `NUDGE_MAIN_AGENT` once
2. second tick for the same stalled gap: `BLOCKED_ESCALATE`
3. no repeated spam for the same gap event

## Legacy / non-canonical path

If you explicitly need subagent-driven execution, `run-executor` remains only as a legacy fallback. It is not the main docs path and should not be used for skill acceptance.

## Advanced / manual path

Only fall back to separate `activation`, `init-task`, `install-monitor`, `init-execution-job`, or `run-executor` commands when you explicitly need split-phase debugging or custom orchestration.
