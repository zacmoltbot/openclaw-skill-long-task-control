# OpenClaw-native runbook

Use this runbook when you want `long-task-control` to behave like a real OpenClaw workflow instead of a repo-local demo.

## Canonical flow

The canonical skill path is now **execution-first**:

1. `bootstrap-task`
2. auto `init-execution-job`
3. auto first `runner_engine.py run-loop` tick with execution ownership
4. monitor stays in the supervision lane: nudge / reconcile / blocked escalate / cleanup

For the generic path, encode executable workflow steps directly in `--workflow` using `:: key=value` parts, for example:

```bash
--workflow "Build draft :: shell=python3 build.py --out /tmp/draft.txt :: expect=/tmp/draft.txt"
```

If a step has no executable semantics, the generic path must block honestly instead of fake-completing.

That means `bootstrap-task` is no longer just “activation + monitor install”. It is the productized entrypoint that starts execution by default.

## Goal

Turn one long-running task into connected OpenClaw-native pieces:

1. activation message in the live session
2. durable ledger entry on disk
3. generic execution-plane job derived from the workflow
4. first execution tick started automatically
5. monitor cron for supervision / recovery / cleanup
6. message-driven delivery / nudge / reconcile / blocked-escalate / cleanup flow

The integration helper is `scripts/openclaw_ops.py`.

## Canonical architecture

```text
bootstrap-task
  ├─ activation + TASK START
  ├─ ledger init
  ├─ install monitor cron
  ├─ derive generic execution job
  └─ start first runner tick with execution ownership
       │
       ├─ runner_engine.py run-loop advances items
       ├─ executor_engine.py executes one item at a time
       ├─ execution_bridge.py syncs observed lifecycle back into ledger/reporting
       └─ monitor cron supervises the ledger truth
            ├─ passive delivery push
            ├─ nudge / reconcile when owner stalls
            ├─ blocked escalation when self-recovery is exhausted
            └─ STOP_AND_DELETE on terminal state
```

The monitor is **not** the primary execution owner. The execution plane is.

## Preferred default: one command boots and starts execution

```bash
python3 scripts/openclaw_ops.py --ledger state/long-task-ledger.json bootstrap-task <task_id> \
  --goal "<one sentence goal>" \
  --requester-channel <discord-channel-id> \
  --workflow "Inspect inputs" \
  --workflow "Run implementation" \
  --workflow "Validate and handoff" \
  --next-action "Start checkpoint 1" \
  --message-ref "discord:msg:<activation-message-id>"
```

Default bootstrap behavior now does all of this:

- creates the durable ledger task
- installs the monitor cron
- derives `<task_id>-job` under `state/jobs/`
- runs the first `runner_engine.py run-loop` tick automatically
- acquires execution ownership via runner lock / `--execution-owner`
- starts writing execution truth back into ledger/reporting immediately

The JSON response now includes:

- activation block
- `TASK START` block
- monitor install result
- `execution.job`
- `execution.first_run`
- follow-up `continue_execution` command

## Continue execution

After bootstrap, keep using the runner as the canonical executor:

```bash
python3 scripts/runner_engine.py --jobs-root state/jobs run-loop <task_id>-job --execution-owner owner:<task_id>
```

Use repeated invocations when you want bounded work per tick, or omit `--max-steps` to drain the serial workflow.

## Manual / advanced controls

Use these only when debugging or custom orchestration requires split phases.

### Bootstrap without auto execution

```bash
python3 scripts/openclaw_ops.py --ledger state/long-task-ledger.json bootstrap-task <task_id> \
  --goal "<goal>" \
  --requester-channel <discord-channel-id> \
  --workflow "Inspect inputs" \
  --workflow "Run implementation" \
  --workflow "Validate and handoff" \
  --next-action "Start checkpoint 1" \
  --message-ref "discord:msg:<activation-message-id>" \
  --no-auto-execution
```

### Split bootstrap from execution start

```bash
python3 scripts/openclaw_ops.py --ledger state/long-task-ledger.json init-execution-job <task_id>
python3 scripts/runner_engine.py --jobs-root state/jobs run-loop <task_id>-job --execution-owner owner:<task_id>
```

### Manual ledger/bootstrap path

```bash
python3 scripts/openclaw_ops.py activation --task-note "<short task note>"
python3 scripts/openclaw_ops.py --ledger state/long-task-ledger.json init-task <task_id> \
  --goal "<one sentence goal>" \
  --requester-channel <discord-channel-id> \
  --workflow "Inspect inputs" \
  --workflow "Run implementation" \
  --workflow "Validate and handoff" \
  --next-action "Start checkpoint 1" \
  --message-ref "discord:msg:<activation-message-id>"
python3 scripts/openclaw_ops.py --ledger state/long-task-ledger.json install-monitor <task_id>
```

Keep this out of the main docs path. It exists for debugging, not as the recommended operating model.

## Legacy / non-canonical path

`run-executor` remains in the repo only as a temporary legacy escape hatch for subagent-driven orchestration experiments.

```bash
python3 scripts/openclaw_ops.py --ledger state/long-task-ledger.json run-executor <task_id>
```

Do not treat it as the default path. Acceptance and main docs should assume the runner-based execution plane instead.

## What the monitor cron does

The installed cron job wakes an isolated OpenClaw agent and uses the prompt from:

```bash
python3 scripts/openclaw_ops.py --ledger state/long-task-ledger.json render-monitor-prompt <task_id>
```

Per tick it:

1. passively delivers any `reporting.pending_updates[]`
2. runs `monitor_nudge.py`
3. reads the selected `task_id`
4. decides whether to stay quiet, nudge, reconcile, escalate blocked, or delete the cron
5. removes itself on `BLOCKED_ESCALATE` or `STOP_AND_DELETE`

## Preview / operate

### Preview one monitor tick

```bash
python3 scripts/openclaw_ops.py --ledger state/long-task-ledger.json preview-tick <task_id>
```

### Remove a monitor manually

```bash
python3 scripts/openclaw_ops.py --ledger state/long-task-ledger.json remove-monitor <task_id>
```

### Install monitor without a real OpenClaw job

```bash
python3 scripts/openclaw_ops.py --ledger state/long-task-ledger.json install-monitor <task_id> --dry-run
```

## End-to-end acceptance

Canonical skill path:

```bash
python3 scripts/skill_live_acceptance_e2e.py
```

Execution-plane MVP bridge/regression:

```bash
python3 scripts/execution_plane_mvp_e2e.py
```

OpenClaw-native monitor-oriented regression:

```bash
python3 scripts/openclaw_native_e2e.py
```

## What the canonical acceptance proves

1. `bootstrap-task` returns activation + `TASK START`
2. `bootstrap-task` auto-derives the execution job
3. `bootstrap-task` auto-starts the first runner tick and obtains execution ownership
4. progress lands in ledger/reporting without hand-written job specs
5. later `run-loop` invocations continue from persisted on-disk state
6. terminal completion drives monitor cleanup state (`STOP_AND_DELETE`)

## Recommended real-world pattern

- main agent runs `bootstrap-task` as the default entrypoint
- main agent posts the returned activation + `TASK START` blocks
- bootstrap auto-starts execution through the runner
- owner/orchestrator re-invokes `runner_engine.py run-loop` as needed
- monitor supervises truth and cleanup; it does not own the main execution path
