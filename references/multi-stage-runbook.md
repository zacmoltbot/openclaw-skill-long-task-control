# Multi-stage Task Runbook

Use this runbook when a task has remote jobs, long waits, multiple renders, stitching, uploads/downloads, or more than one checkpoint.

## Key monitor principle

The monitor cron is not just a timeout alarm. Treat it as an **execution nudge loop**:

- if the task is healthy, do nothing cheap (`OK`)
- if supervision is old, issue a light reminder (`HEARTBEAT_DUE`)
- if progress is stale, warn first (`STALE_PROGRESS`)
- if the main agent appears stopped, nudge it to resume or close the loop (`NUDGE_MAIN_AGENT`)
- if the task is truly blocked, escalate once (`BLOCKED_ESCALATE`)
- if the task is terminal, delete the cron (`STOP_AND_DELETE`)

## SOP

### 1. Intake

Capture:

- requested deliverable
- source inputs
- constraints (format, quality, deadline, cost sensitivity)
- likely execution model: remote job, local process, or hybrid

Create a local `task_id` immediately.

### 2. Plan checkpoints

Choose 3-7 checkpoints. Prefer observable transitions:

1. request prepared
2. job submitted / process started
3. intermediate output produced
4. final output produced
5. output validated
6. delivered

Avoid vague checkpoints like “working on it”.

### 3. Launch work

Record whichever handle exists first:

- remote job id
- local PID
- output directory
- generated log file

Also create/update the durable ledger entry so the task exists outside chat text:

- `task_id`
- current checkpoint
- `last_checkpoint_at`
- heartbeat timing
- blocker state if any

If the task may take a while, send a `STARTED` update right after submission/start, not after it finishes.

### 4. Poll and wait sanely

When waiting on a remote job or background process:

- report the job id or PID
- report the last known state
- avoid noisy non-updates
- only emit a new checkpoint when something verifiable changed

Examples of meaningful changes:

- queued -> running
- running -> succeeded
- segment 2 finished
- file downloaded
- validation passed

### 5. Use the monitor cron as a continuation guardrail

The monitor cron should repeatedly ask: **does the main agent need a push to continue?**

Recommended behavior:

1. run a deterministic ledger check first
2. avoid big-model analysis for healthy tasks
3. if only heartbeat is due, send a cheap reminder
4. if progress is stale, warn once and prepare to nudge
5. let the monitor update supervision metadata only; do not let it rewrite task truth such as `status`, `checkpoints`, `blocker`, or `next_action`
6. if stale progress persists, keep nudging the main agent until the task is resumed or explicitly closed; each nudge should ask it to do one of:
   - resume execution
   - emit a real checkpoint
   - mark `BLOCKED`
   - mark `FAILED`
   - mark `COMPLETED`
6. if blocked is confirmed, escalate once and stop monitoring
7. if terminal, delete the cron immediately

### 6. Handle failures

If a step fails:

- name the exact failed checkpoint
- capture the error text / code / status if available
- say what was tried
- say whether retry is safe
- ask for the minimum needed decision only if human input is required

Important distinction:

- **stale progress** is not automatically failure
- **blocked** means continuation depends on outside action
- **failed** means there is no safe continuation plan right now

### 7. Validate before handoff

For media tasks, prefer:

- file exists
- file size > 0
- ffprobe/mediainfo can read duration and streams
- expected segment count matches
- stitched output opens cleanly

For remote jobs:

- provider status says succeeded/completed
- output URL or downloaded file exists

### 8. Complete with handoff

A completion handoff should answer:

- what is done
- where the outputs are
- what validation was performed
- whether anything is still running or pending cleanup

After completion, make sure the task becomes terminal in the ledger so the monitor can choose `STOP_AND_DELETE`.

## State-machine cheat sheet

### `OK`
- progress fresh enough
- heartbeat fresh enough
- no action

### `HEARTBEAT_DUE`
- no heartbeat within expected interval
- reminder only

### `STALE_PROGRESS`
- no checkpoint within timeout window
- warning, still pre-gate

### `NUDGE_MAIN_AGENT`
- stale progress persists
- activation missing
- or main agent appears to have stopped without a terminal update

### `BLOCKED_ESCALATE`
- task already `BLOCKED`
- or evidence shows safe continuation requires external action

### `STOP_AND_DELETE`
- task terminal
- escalation sent for blocked task
- or task no longer needs supervision

## Task archetypes

### RunningHub / hosted generation

Typical chain:

1. submit request
2. receive job id
3. poll status
4. download artifact
5. validate artifact
6. deliver

Report fields:

- provider
- remote_job_id
- latest_status
- output_file or output_url
- validation result

### Long video generation with many segments

Typical chain:

1. split prompt/script/chapters
2. render each segment
3. confirm each segment file exists
4. stitch segments
5. validate final output
6. deliver

Report fields:

- completed segments / total segments
- failed segment ids if any
- per-segment job ids or files
- stitch PID if stitching started
- final output path

### Multi-step local processing

Typical chain:

1. input prepared
2. process launched
3. process still running (PID known)
4. output written
5. validation passed
6. deliver

Report fields:

- pid
- short command summary
- output file path
- exit code
- validation result

## Anti-patterns

Do not:

- claim completion before validation
- write speculative ETAs as facts
- hide blockers until asked
- say “still working” without a job id, PID, or changed state
- let the monitor cron run forever after terminal status
- keep nudging when the correct action is blocker escalation
- wake a large model on every cron tick for healthy tasks

## Suggested naming

Task id patterns:

- `rh-20260410-001`
- `video-stitch-20260410-a`
- `longtask-<date>-<short-suffix>`

Output naming patterns:

- include task id or timestamp
- keep segment numbering zero-padded: `seg01`, `seg02`, `seg03`
- keep final outputs obvious: `final.mp4`, `final_v2.mp4`
