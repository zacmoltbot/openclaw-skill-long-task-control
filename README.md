# long-task-control

`long-task-control` is a durable execution skill for long-running or multi-stage work.

It is no longer just a monitor-first reminder layer. The current design is:

- **execution-first**
- **outcome-first**
- **delivery-aware**
- **honest about partial success**

## What it does now

From the user point of view, LTC should:

- take ownership of a long task
- decompose it into concrete workflow steps
- execute what is actually executable
- persist evidence and progress to a durable ledger
- survive interruption / silence / session loss
- deliver user-visible artifacts explicitly
- reconcile the final result from real outputs, not just control-plane optimism

## Current architecture

```text
bootstrap-task
  -> durable task ledger
  -> execution job
  -> runner/executor advances work
  -> monitor supervises stale/inconsistent states
  -> delivery flush sends pending user-facing updates
  -> terminal reconcile closes the task honestly
```

### Main components

- `scripts/task_ledger.py`
  - canonical durable task state
  - checkpoints, blockers, owner replies, delivery acknowledgements
- `scripts/executor_engine.py`
  - execution-plane loop for workflow steps
- `scripts/execution_bridge.py`
  - bridges execution truth back into task ledger
- `scripts/openclaw_ops.py`
  - bootstrap, monitor install/remove, pending update flush, artifact resolution, terminal reconcile
- `scripts/monitor_nudge.py`
  - deterministic monitor policy / nudge / reconcile / stop-delete decisions
- `scripts/adapters/generic_manual.py`
  - generic step executor, now including explicit artifact delivery

## Key product semantics

### 1. Outcome-first reporting

User-facing truth is derived from evidence and outputs, not only raw task status.

Important user-facing states:

- `IN_PROGRESS`
- `SUCCESS`
- `PARTIAL_SUCCESS`
- `BLOCKED`
- `FAILED`

This means LTC can now say:

- outputs exist
- some steps failed
- therefore the honest result is **partial success**

instead of falsely saying `SUCCESS` or pretending nothing useful happened.

### 2. Explicit artifact delivery

A task is not considered truly handed off just because files exist on disk.

LTC now supports delivery as a formal workflow step:

- `auto_action=deliver_artifacts`
- `deliver_artifacts=<path1>|<path2>`
- `deliver_caption=<user-facing message>`

Example:

```bash
--workflow "Build image :: shell=python3 make_image.py --out /tmp/image.png :: expect=/tmp/image.png"
--workflow "Deliver result :: auto_action=deliver_artifacts :: deliver_artifacts=/tmp/image.png :: deliver_caption=Here is the generated result for review"
```

That closes the earlier gap where artifacts existed locally but were never actually delivered to the user.

### 3. Honest terminal semantics

If execution ends with failed / missing required steps:

- LTC must **not** claim `SUCCESS`
- terminal truth becomes **BLOCKED / PARTIAL_SUCCESS / owner reconcile**
- monitor and preview must reflect that honestly

This was a real bug found in live RunningHub tests and is now fixed.

### 4. Monitor no longer leaks raw internal reporting

Monitor delivery now uses sanitized user-facing updates instead of raw internal `REPORTING HOOK` blocks.

Also:

- monitor cron is installed with `--no-deliver`
- isolated cron summary should not auto-announce raw internal state to Discord
- pending updates are flushed via `flush-pending-updates`

## Canonical workflow syntax

Supported step keys include:

- `shell=<command>`
- `expect=<path>`
- `artifacts=<path1>|<path2>`
- `cwd=<dir>`
- `timeout=<sec>`
- `next_action=<text>`
- `batch_result=<path-to-json-summary>`
- `batch_min_success=<n>`
- `auto_action=deliver_artifacts`
- `deliver_artifacts=<path1>|<path2>`
- `deliver_caption=<user-facing delivery text>`

## Canonical bootstrap example

```bash
python3 scripts/openclaw_ops.py --ledger state/long-task-ledger.json bootstrap-task demo-task \
  --goal "Inspect inputs, generate outputs, deliver them, and close the task honestly" \
  --requester-channel 1477237887136432162 \
  --workflow "Inspect :: shell=printf 'inspected\n' > /tmp/inspected.txt :: expect=/tmp/inspected.txt" \
  --workflow "Build draft :: shell=cp /tmp/inspected.txt /tmp/draft.txt :: expect=/tmp/draft.txt" \
  --workflow "Deliver draft :: auto_action=deliver_artifacts :: deliver_artifacts=/tmp/draft.txt :: deliver_caption=Draft ready for review" \
  --next-action "Start step-01"
```

## What changed through the recent live RunningHub tests

The recent Eko / RunningHub live tests pushed LTC from monitor/control-plane MVP into a more real execution product.

### Proven working now

- execution job bootstrap
- runner advancing multi-step workflows
- explicit artifact delivery to Discord
- honest `PARTIAL_SUCCESS` semantics when some outputs exist but required steps fail
- monitor raw-output leak fixed
- monitor error cron cleanup path validated

### Important bugs fixed

- raw internal `REPORTING HOOK` blocks leaking to Discord
- tasks completing without artifact delivery
- false `SUCCESS` / `COMPLETED` when failed items still existed
- terminal jobs getting stuck in fake `RUNNING` / `HEARTBEAT_DUE`
- partial-failure reconcile not mapping cleanly into blocker truth

### Still true conceptually

The monitor is **not** the product.
The result delivered to the user is the product.

## Acceptance / regression tests

Important tests now include:

```bash
python3 scripts/monitor_delivery_regression_e2e.py
python3 scripts/artifact_delivery_e2e.py
python3 scripts/completion_semantics_regression_e2e.py
python3 scripts/terminal_closeout_regression_e2e.py
```

These cover:

- sanitized monitor delivery
- explicit artifact delivery
- no fake success on failed steps
- terminal auto-closeout into `BLOCKED / PARTIAL_SUCCESS`

## References

- `references/openclaw-native-runbook.md`
- `references/prompt-contract.md`
- `references/live-acceptance-test.md`
- `references/redesign-spec.md`
- `references/adapter-strategy.md`
