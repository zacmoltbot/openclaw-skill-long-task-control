---
name: long-task-control
description: Standardize task control, checkpointing, and status reporting for long-running or multi-stage work. Use when task characteristics indicate non-trivial execution control is needed: the work is likely to take more than one visible turn, includes waiting/polling/background execution, has multiple checkpoints or handoffs, depends on external or asynchronous job systems, produces intermediate artifacts before the final deliverable, or can become blocked and requires proactive status reporting. When triggered, this upgraded version expects a durable task ledger plus a low-cost monitor cron that acts as an execution nudge: if the main agent stops moving, no new checkpoint appears, or supervision goes stale, the monitor should remind the main agent to continue until the task reaches COMPLETED, FAILED, or BLOCKED.
---

# Long Task Control

Keep long tasks boring, traceable, and easy to audit.

This skill is now a **semi-enforced task control system**, not just a formatting guide. The contract has four layers:

1. **User-visible reporting**: activation → task start → checkpoint → blocked → completed
2. **Durable state**: task ledger with `task_id`, workflow, timestamps, blocker, validation, heartbeat
3. **Low-cost monitor cron**: pre-gate rule engine that checks ledger freshness and nudges the main agent when execution stalls
4. **Machine checks**: timeout detector + compliance checker

For actual OpenClaw usage, do not stop at repo-local scripts. Use `scripts/openclaw_ops.py` plus `references/openclaw-native-runbook.md` and `references/prompt-contract.md`.

Preferred default: run **one** OpenClaw-native bootstrap command so the lifecycle becomes the natural entrypoint instead of something the main agent has to remember to stitch together manually:

- emit the activation block in the live requester session
- emit the `TASK START` block for the same task id / workflow
- initialize the ledger with requester-channel metadata
- create a **real OpenClaw cron job** with `openclaw cron add`
- return ready-to-use `record-update` owner commands for STARTED / CHECKPOINT / BLOCKED / COMPLETED
- let the cron send `message.send` nudges / reconcile prompts / blocker escalations
- auto-remove the OpenClaw cron job on `COMPLETED` / `FAILED` / post-escalated `BLOCKED`

## Core rules

1. Decide whether to activate this skill based on execution characteristics, not a memorized list of task types.
2. Once activated, emit the mandatory activation message before meaningful execution updates.
3. Create a task record before meaningful work.
4. For long tasks, prefer a durable ledger entry rather than keeping all state only in chat text.
5. Split the task into numbered checkpoints with clear completion evidence.
6. Report only facts you can verify right now.
7. Prefer identifiers over narratives: task id, job id, PID, file path, URL, timestamp, exit state.
8. If something is blocked, report the exact blocker, what was tried, and the next needed action.
9. If the task is waiting for a long time, heartbeat is acceptable, but do not confuse heartbeat with progress.
10. If heartbeats continue but checkpoints do not, let the monitor escalate from reminder to execution nudge.
11. When complete, hand off outputs, validation result, and any still-running/background items.
12. Do not claim progress based on hope, estimates, or hidden reasoning.

## Long-task detection checklist

Activate this skill when the task shows one or more of these execution traits:

- it will likely take more than one visible response turn to finish cleanly
- it requires waiting, polling, sleep/retry cycles, or background execution
- it has multiple dependent stages where later steps rely on earlier outputs
- it produces intermediate artifacts before the final deliverable
- it depends on remote, queued, asynchronous, or externally managed jobs
- it has a meaningful chance of becoming blocked on approval, credentials, upstream systems, retries, or missing outputs
- it benefits from explicit handoff because the requester must later inspect, download, review, or continue from produced artifacts

Strong activation signals:

- you already know you will need progress updates rather than a single final answer
- you need durable handles such as job ids, PIDs, output paths, URLs, timestamps, or exit codes to keep the task auditable
- silent waiting would make the user lose visibility into the true state of the work

## Mandatory activation message

When this skill is activated, the first user-visible message for the task must clearly state that the task is being executed with the `long-task-control` skill and how updates will work.

Use this standard activation message template before the task record:

```text
ACTIVATED
- skill: long-task-control
- announcement: 目前這個 task 會採用 long-task-control SKILL 執行
- reporting: 我會用 checkpoint / blocker / completed 這類可驗證狀態回報進度；有新事實才更新，不用模糊的「還在跑」敘述
- next: 接著建立 task record，開始第一個可驗證步驟
```

You may add one short task-specific sentence after the template, but do not omit the explicit announcement that `long-task-control` is active.

## Task ledger requirement

For long or blocker-prone work, keep a task ledger entry.

Recommended file:

```text
state/long-task-ledger.json
```

This repo ships an example state file:

```text
state/long-task-ledger.example.json
```

Minimum task fields:

- `task_id`
- `goal`
- `status`
- `activation.announced`
- `workflow[]`
- `current_checkpoint`
- `last_checkpoint_at`
- `heartbeat.expected_interval_sec`
- `heartbeat.timeout_sec`
- `next_action`

See `references/task-ledger-spec.md` for the fuller schema and the monitor-specific fields. See `references/monitor-action-spec.md` for the owner-vs-monitor write contract and the action wiring for `NUDGE_MAIN_AGENT`, `OWNER_RECONCILE`, `BLOCKED_ESCALATE`, and `STOP_AND_DELETE`.

## Checkpoint vs heartbeat

Use both, but do not merge them conceptually.

### Checkpoint

A checkpoint means something actually changed.

Examples:

- request submitted
- remote job id received
- segment 2 finished
- file downloaded
- validation passed

Checkpoint updates should advance:

- `last_checkpoint_at`
- `heartbeat.last_progress_at`

### Heartbeat

A heartbeat means the task is still under supervision, even if no new progress happened.

Examples:

- remote queue still running
- background process still alive
- waiting for approval, but task is still being watched

Heartbeat updates should only advance:

- `heartbeat.last_heartbeat_at`

If heartbeats continue but checkpoints do not, the monitor should first warn, then nudge the main agent to either make progress or explicitly mark `BLOCKED` / `FAILED`.

## Monitor cron purpose

The monitor cron is **not** a generic bug scanner. Its main purpose is to act as a **任務續行提醒器 / execution nudge**.

It exists to answer one cheap question repeatedly:

> Is this task still truly progressing, or did the main agent stop moving without closing the loop?

Use the monitor cron to do low-cost pre-gate checks against ledger timestamps and state, then choose one of these state-machine outcomes. The cron's success condition is not merely "no errors found"; it should keep nudging until the task is explicitly closed as `COMPLETED`, `FAILED`, or `BLOCKED`:

- `OK`
- `HEARTBEAT_DUE`
- `STALE_PROGRESS`
- `NUDGE_MAIN_AGENT`
- `OWNER_RECONCILE`
- `BLOCKED_ESCALATE`
- `STOP_AND_DELETE`

Read `references/task-ledger-spec.md` for the full state machine, `references/monitor-action-spec.md` for action payload / escalation / self-delete semantics, and `references/multi-stage-runbook.md` for operating guidance.

## Monitor state semantics

### `OK`

Use when heartbeat and progress are fresh enough. No reminder needed.

### `HEARTBEAT_DUE`

Use when supervision metadata is stale, but the task is still plausibly healthy. This is a lightweight reminder only: update heartbeat, confirm the task is still under watch, and avoid unnecessary large-model work.

**Smart stale detection:** If the task has a pending external return (RunningHub queue/pending/running) or `progress_at` is still updating, do **not** emit `HEARTBEAT_DUE` — the task is waiting, not stalled.

### `STALE_PROGRESS`

Use when there has been no real checkpoint for too long. This is a pre-gate warning state, not yet a failure verdict. The monitor should surface that execution looks stalled.

**Smart stale detection:** If `progress_at` is updating or an external async return is pending (RunningHub job still queued/running), skip `STALE_PROGRESS` entirely — the agent is working or waiting legitimately.

### Retry-count tracking

The monitor tracks per-step, per-failure-type retry counts in `monitoring.retry_count`:

```json
"retry_count": {
  "implement:TIMEOUT": 2,
  "collect:EXTERNAL_WAIT": 3
}
```

Failure types:
- `TIMEOUT`: no checkpoint within `timeout_sec`
- `EXECUTION_ERROR`: command/API returned a non-zero exit or error status
- `EXTERNAL_WAIT`: external system (RunningHub, remote queue) returned failure or wait-exceeded

**Rule:** Same step + same failure type 3 times → immediate `BLOCKED_ESCALATE` (no wall-clock wait). Retry counters reset when the step advances successfully (new checkpoint with `last_checkpoint_at` updated).

### `NUDGE_MAIN_AGENT`

Use for the first execution nudge: ask the main agent to resume execution, post a checkpoint, or explicitly mark `BLOCKED` / `FAILED` / `COMPLETED`.

### `OWNER_RECONCILE`

Use when stale progress persists after prior nudges. This is the stale -> query owner state.

Expected owner-reconciliation branches:

- `A_IN_PROGRESS_FORGOT_LEDGER`: owner says work was progressing but the ledger was stale -> append missed checkpoint(s), refresh `next_action`, keep `RUNNING`
- `B_BLOCKED`: owner confirms the task is blocked -> write blocker truth and move to `BLOCKED_ESCALATE`
- `C_COMPLETED`: owner confirms the task already finished -> write `COMPLETED` plus validation evidence
- `D_NO_REPLY`: owner does not respond -> do not invent task truth; seek external evidence first
- `E_FORGOT_OR_NOT_DOING`: owner admits the work was forgotten / not being done -> do not only record it; immediately enter the resume-execution / 要求補做 path, update `next_action`, and require a fresh real checkpoint

### `BLOCKED_ESCALATE`

Use later, not early. Enter this state only when the task is already `BLOCKED`, or when evidence shows the task cannot continue safely without external input/approval/fix **after** the recovery path was tried or ruled out. Recovery path means: resume execution, rebuild/restart the stuck step if safe, reconcile missing ledger truth, or require the main agent to補做. Escalate with blocker facts instead of repeatedly nudging.

**Retry-count path (new):** If the same step fails the same way 3 times (per `retry_count`), monitor escalates immediately — no wall-clock wait required.

**Enhanced notification:** The `BLOCKED_ESCALATE` message must include in one shot: (1) which step is stuck, (2) retry history, (3) what was tried, (4) why it is now deemed unrecoverable, (5) recommended next steps for the requester.

**Immediate cron stop:** On `BLOCKED_ESCALATE`, the monitor sets `monitoring.cron_state=DELETE_REQUESTED` and stops — no more空燒. The cron removes itself immediately after delivering the escalation.

### `STOP_AND_DELETE`

Use when the task is terminal and the monitor cron should delete itself. Terminal statuses are:

- `COMPLETED`
- `FAILED`
- `BLOCKED` after escalation was delivered
- `ABANDONED`

## Reminder vs blocked vs failed vs self-delete

### Reminder only

Use `HEARTBEAT_DUE`, `NUDGE_MAIN_AGENT`, or `OWNER_RECONCILE` when:

- the task is still expected to continue
- no terminal error has been proven
- the main problem is silence, stale heartbeat, or missing checkpoint
- a simple resume/checkpoint/update from the main agent would resolve ambiguity

### Mark or treat as `BLOCKED`

Escalate toward `BLOCKED` only after the task has been actively pushed as far as it can go. Before that, prefer self-recovery / owner-remediation paths such as resume, rebuild-safe-step, reconcile missed checkpoints, or requiring the main agent to actually continue execution.

Move to `BLOCKED` when:

- the task needs approval, credentials, user input, upstream recovery, or a missing dependency
- retry/rebuild/resume is unsafe or meaningless without outside action
- the blocker is known and can be described precisely
- repeated nudges / reconcile / remediation attempts still produce no safe progress

### Mark or treat as `FAILED`

Use `FAILED` when:

- the task has definitively failed
- no safe automatic continuation plan exists
- the correct next action is human review, redesign, or explicit restart from scratch

Do **not** use the monitor cron alone to invent a failure. The low-cost monitor should recommend escalation; the owning agent should write the terminal status once failure is verified.

### Self-delete the monitor cron

Delete the cron when:

- the task is `COMPLETED`
- the task is `FAILED`
- the task is `ABANDONED`
- the task is `BLOCKED` and one escalation message has already been sent
- the task record no longer exists or is no longer meant to be supervised

For repo-local pseudo-implementation, let the monitor mark `monitoring.cron_state=DELETE_REQUESTED` and emit an action payload; let the owner/scheduler integration layer perform the real cron deletion.

## Cost-control rule

Follow Edward's cost-control principle: the monitor is a **low-cost pre-gate**, not an excuse to wake a large model on every interval.

Preferred monitor loop:

1. Read the ledger.
2. Compare timestamps and status using deterministic rules.
3. Emit one cheap state outcome.
4. Only if the outcome is `NUDGE_MAIN_AGENT`, `OWNER_RECONCILE`, or `BLOCKED_ESCALATE`, send the smallest useful reminder/escalation.
5. Stop/delete once the task is terminal.

Avoid:

- re-summarizing the whole task every run
- asking a big model to re-diagnose healthy tasks
- repeated verbose reminders with no new facts
- keeping the cron alive after terminal closure

## Compliance expectations

At minimum, this upgraded skill expects checks for these failure modes:

1. no activation message / activation record
2. claiming active work without a task id
3. checkpoint timeout / stale progress
4. vague progress wording without facts
5. task should be `BLOCKED` but remains silent or lacks blocker details

The repo includes baseline helpers for these cases in `scripts/checkpoint_timeout.py`, `scripts/compliance_check.py`, and `scripts/monitor_nudge.py`.

## Start-of-task procedure

At the beginning of a long or multi-stage task, establish:

- `task_id`: your local tracking id for this request
- `goal`: one-sentence target outcome
- `workflow`: ordered checkpoints
- `evidence plan`: what facts will prove each checkpoint finished
- `artifacts`: expected output files, directories, URLs, or job handles

Use this compact start format:

```text
TASK START
- task_id: <local-id>
- goal: <target deliverable>
- workflow:
  1. <checkpoint>
  2. <checkpoint>
  3. <checkpoint>
- expected artifacts:
  - <file/url/job>
- first action: <next concrete step>
```

## Reporting rules

### Always include verifiable facts

Good facts:

- `runninghub_job_id=...`
- `pid=12345`
- `output_file=/tmp/out/final.mp4`
- `size_bytes=104857600`
- `duration_s=92.4`
- `submitted_at=2026-04-10T23:40:00+08:00`
- `poll_result=still running`
- `exit_code=0`

Avoid non-facts:

- “應該快好了”
- “看起來差不多完成”
- “還在跑” without handle/evidence

### Report by state, not by stream-of-consciousness

Use one of these states only:

- `STARTED`
- `CHECKPOINT`
- `BLOCKED`
- `COMPLETED`

## Validation before claiming completion

Before saying a task is done, verify at least one of:

- output file exists and size is non-zero
- media duration/stream info can be read
- checksum or file count matches expectation
- remote job status is succeeded/completed
- downstream delivery actually sent

If validation fails, report `BLOCKED` or a failed checkpoint instead of `COMPLETED`.

## Use the bundled resources

- `references/task-ledger-spec.md`: durable state schema + monitor state machine
- `references/monitor-action-spec.md`: owner-vs-monitor ledger contract + action wiring semantics
- `references/multi-stage-runbook.md`: fuller SOP for planning, polling, retries, handoff, and monitor operation
- `references/openclaw-native-runbook.md`: the real OpenClaw activation -> ledger -> cron -> message.send -> cleanup lifecycle
- `references/prompt-contract.md`: default contract that makes lifecycle bootstrap the preferred OpenClaw-native entrypoint
- `references/failure-examples.md`: non-compliant examples, corrected reporting patterns, and nudge-specific anti-patterns
- `scripts/checkpoint_report.py`: generate consistent status blocks
- `scripts/task_ledger.py`: mutate ledger state; use `supervisor-update` only for supervision metadata; use `owner-reply` to ingest owner reconciliation replies and auto-route to resume / blocked / completed
- `scripts/checkpoint_timeout.py`: detect stale tasks
- `scripts/compliance_check.py`: scan for baseline rule violations
- `scripts/monitor_nudge.py`: evaluate the low-cost execution-nudge state machine and optionally write supervision metadata only
- `scripts/openclaw_ops.py`: OpenClaw-native glue for activation text, `TASK START` text, one-shot bootstrap, ledger init, real `openclaw cron add/rm`, monitor prompt generation, and terminal cleanup
- `scripts/demo_monitor_flow.py`: run a temp-ledger E2E demo for stale -> owner reconcile -> owner reply -> resume / blocked escalate / stop-delete flows
- `scripts/monitor_cron.py`: create/remove pseudo cron registrations and execute one monitor tick with terminal self-cleanup wiring
- `scripts/openclaw_native_e2e.py`: run an OpenClaw-style E2E smoke test with real cron create/remove plus stale -> reconcile -> completed cleanup validation
- `scripts/generic_long_task_e2e.py`: run a task-agnostic E2E proving bootstrap -> updates -> stale nudge -> reconcile -> completed -> cleanup without task-specific patches
- `scripts/user_centered_monitor_e2e.py`: run two required user-centered scenarios: (A) transient/self-recoverable stall -> resume/rebuild/reconcile without blocked escalation; (B) true unrecoverable blocker -> BLOCKED_ESCALATE plus immediate cron cleanup
- `scripts/shampoo_sample_e2e.py`: run the required 30s shampoo sample E2E flow via the same bootstrap entrypoint: activation -> ledger init -> monitor cron install -> checkpoint -> stale/nudge -> owner reconcile -> completed -> cron cleanup

## OpenClaw-native activation and monitor lifecycle

When using this skill inside OpenClaw, follow this exact flow:

1. Prefer the one-shot bootstrap entrypoint: `python3 scripts/openclaw_ops.py --ledger state/long-task-ledger.json bootstrap-task <task_id> ...`. Default monitor check interval is **5 minutes** (previously 10 minutes).
2. That bootstrap should be treated as the default lifecycle entry: it returns the activation block, the `TASK START` block, initializes the ledger, and installs a **real OpenClaw cron monitor** in one operation.
3. Keep writing owner-truth checkpoints with `python3 scripts/openclaw_ops.py --ledger state/long-task-ledger.json record-update <STARTED|CHECKPOINT|BLOCKED|COMPLETED> ...` plus `task_ledger.py owner-reply` when reconcile input arrives.
4. Let the cron agent run the generated monitor prompt: it calls `monitor_nudge.py`, then uses `message.send` only for `NUDGE_MAIN_AGENT`, `OWNER_RECONCILE`, and `BLOCKED_ESCALATE`.
5. On `BLOCKED_ESCALATE` or `STOP_AND_DELETE`, let the cron call `openclaw_ops.py remove-monitor <task_id>` so the real OpenClaw cron job is deleted and the ledger is marked `DELETED`.
6. Only use the split `activation` -> `init-task` -> `install-monitor` sequence when debugging or when custom orchestration really requires phase separation.
7. Use `python3 scripts/generic_long_task_e2e.py`, `python3 scripts/openclaw_native_e2e.py`, and `python3 scripts/shampoo_sample_e2e.py` before handoff when you need smoke tests for generic + OpenClaw-style + shampoo-like flows.

## Minimal operating pattern

1. Detect whether task traits require long-task-control.
2. Emit the mandatory activation message.
3. Create the task record and ledger entry.
4. Submit or launch work.
5. Emit checkpoint when a verifiable state changes.
6. If waiting, update heartbeat but do not fake progress.
7. Run the low-cost monitor; if progress stays stale, nudge the main agent to resume or close the loop.
8. If blocked, escalate immediately with facts and blocker metadata.
9. Validate outputs.
10. Deliver with `COMPLETED` handoff.
11. Stop/delete the monitor once the task is terminal.
