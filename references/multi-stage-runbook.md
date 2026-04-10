# Multi-stage Task Runbook

Use this runbook when a task has remote jobs, long waits, multiple renders, stitching, uploads/downloads, or more than one checkpoint.

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

### 5. Handle failures

If a step fails:

- name the exact failed checkpoint
- capture the error text / code / status if available
- say what was tried
- say whether retry is safe
- ask for the minimum needed decision only if human input is required

### 6. Validate before handoff

For media tasks, prefer:

- file exists
- file size > 0
- ffprobe/mediainfo can read duration and streams
- expected segment count matches
- stitched output opens cleanly

For remote jobs:

- provider status says succeeded/completed
- output URL or downloaded file exists

### 7. Complete with handoff

A completion handoff should answer:

- what is done
- where the outputs are
- what validation was performed
- whether anything is still running or pending cleanup

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
- dump huge raw logs when 3-5 facts would do

## Suggested naming

Task id patterns:

- `rh-20260410-001`
- `video-stitch-20260410-a`
- `longtask-<date>-<short-suffix>`

Output naming patterns:

- include task id or timestamp
- keep segment numbering zero-padded: `seg01`, `seg02`, `seg03`
- keep final outputs obvious: `final.mp4`, `final_v2.mp4`
