---
name: long-task-control
description: Standardize task control, checkpointing, and status reporting for long-running or multi-stage work. Use when task characteristics indicate non-trivial execution control is needed: the work is likely to take more than one visible turn, includes waiting/polling/background execution, has multiple checkpoints or handoffs, depends on external or asynchronous job systems, produces intermediate artifacts before the final deliverable, or can become blocked and requires proactive status reporting. When triggered, this upgraded version also expects a durable task ledger, checkpoint timeout tracking, and watchdog/compliance visibility. Do not rely on enumerating task categories; trigger from execution traits. When triggered, first emit the required activation message that explicitly tells the user this task is being executed with the long-task-control skill and how progress will be reported.
---

# Long Task Control

Keep long tasks boring, traceable, and easy to audit.

This skill is now a **semi-enforced task control system**, not just a formatting guide. The contract has three layers:

1. **User-visible reporting**: activation → task start → checkpoint → blocked → completed
2. **Durable state**: task ledger with `task_id`, workflow, timestamps, blocker, validation, heartbeat
3. **Machine checks**: timeout detector + compliance checker

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
10. When complete, hand off outputs, validation result, and any still-running/background items.
11. Do not claim progress based on hope, estimates, or hidden reasoning.

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

See `references/task-ledger-spec.md` for the fuller schema and state semantics.

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

If heartbeats continue but checkpoints do not, watchdog should still be able to flag `STALE_PROGRESS`.

## Timeout / watchdog rules

Treat silent long tasks as observable risk.

### Recommended timing model

- `heartbeat.expected_interval_sec`: how often supervision should be refreshed
- `heartbeat.timeout_sec`: maximum allowed age of the latest progress checkpoint

Suggested defaults:

- light long task: `600 / 1800`
- remote async task: `900 / 3600`
- high-visibility task: `300 / 900`

### Watchdog states

The timeout detector may label tasks with:

- `OK`
- `HEARTBEAT_DUE`
- `STALE_PROGRESS`
- `BLOCKED_SILENT`
- `MISSING_ACTIVATION`
- `COMPLETED_NO_VALIDATION`

Interpretation:

- `HEARTBEAT_DUE`: supervision metadata is stale
- `STALE_PROGRESS`: no meaningful checkpoint for too long
- `BLOCKED_SILENT`: task is effectively blocked or marked blocked without usable blocker payload
- `MISSING_ACTIVATION`: the task is active but the required activation record is absent

## Compliance expectations

At minimum, this upgraded skill expects checks for these failure modes:

1. no activation message / activation record
2. claiming active work without a task id
3. checkpoint timeout / stale progress
4. vague progress wording without facts
5. task should be `BLOCKED` but remains silent or lacks blocker details

The repo includes a baseline checker for these cases in `scripts/compliance_check.py`.

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

## Fixed report formats

### Generic checkpoint update

```text
CHECKPOINT
- task_id: <local-id>
- checkpoint: <n>/<total> <name>
- state: running|done|blocked
- verified facts:
  - <key>=<value>
  - <key>=<value>
- outputs:
  - <path-or-url>
- next: <single concrete next action>
```

### Blocker report

```text
BLOCKED
- task_id: <local-id>
- checkpoint: <name>
- blocker: <specific failure or missing dependency>
- verified facts:
  - <key>=<value>
- tried:
  - <attempt 1>
- need:
  - <approval/input/retry decision>
- safe next step: <what can be done once unblocked>
```

### Completion handoff

```text
COMPLETED
- task_id: <local-id>
- goal: <delivered outcome>
- completed checkpoints:
  - <checkpoint>
- output artifacts:
  - <path-or-url>
- validation:
  - <command/check and result>
- background items still running:
  - none | <pid/job>
- handoff: <what the requester can now review/use>
```

## Validation before claiming completion

Before saying a task is done, verify at least one of:

- output file exists and size is non-zero
- media duration/stream info can be read
- checksum or file count matches expectation
- remote job status is succeeded/completed
- downstream delivery actually sent

If validation fails, report `BLOCKED` or a failed checkpoint instead of `COMPLETED`.

## Use the bundled resources

- `references/task-ledger-spec.md`: durable state schema + watchdog model
- `references/multi-stage-runbook.md`: fuller SOP for planning, polling, retries, and handoff
- `references/failure-examples.md`: explicit non-compliant examples and corrected reporting patterns
- `scripts/checkpoint_report.py`: generate consistent status blocks
- `scripts/task_ledger.py`: mutate ledger state
- `scripts/checkpoint_timeout.py`: detect stale tasks
- `scripts/compliance_check.py`: scan for baseline rule violations

## Minimal operating pattern

1. Detect whether task traits require long-task-control.
2. Emit the mandatory activation message.
3. Create the task record and ledger entry.
4. Submit or launch work.
5. Emit checkpoint when a verifiable state changes.
6. If waiting, update heartbeat but do not fake progress.
7. If blocked, escalate immediately with facts and blocker metadata.
8. Run timeout/compliance checks if the task spans time or handoffs.
9. Validate outputs.
10. Deliver with `COMPLETED` handoff.
