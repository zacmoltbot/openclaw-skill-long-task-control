---
name: long-task-control
description: Standardize task control, checkpointing, and status reporting for long-running or multi-stage work. Use when a task will take minutes to hours, involves polling/waiting/handoffs, produces intermediate outputs, or depends on external job systems such as RunningHub, long video generation, render queues, uploads, downloads, transcoding, or multi-segment stitching. Apply it to keep updates factual, checkpoint-based, and easy to verify.
---

# Long Task Control

Keep long tasks boring, traceable, and easy to audit. Break work into checkpoints, report only verifiable facts, surface blockers early, and finish with a concrete delivery handoff.

## Core rules

1. Create a task record before starting meaningful work.
2. Split the task into numbered checkpoints with clear completion evidence.
3. Report only facts you can verify right now.
4. Prefer identifiers over narratives: task id, job id, PID, file path, URL, timestamp, exit state.
5. If something is blocked, report the exact blocker, what was tried, and the next needed action.
6. When complete, hand off outputs, validation result, and any still-running/background items.
7. Do not claim progress based on hope, estimates, or hidden reasoning.

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

## Checkpoint design

A good checkpoint is observable. Prefer checkpoints such as:

- request submitted
- remote job id received
- input uploaded
- segment 1 rendered
- segment 2 rendered
- stitch process started
- output file written
- output file validated
- delivery sent

For each checkpoint, keep three things explicit:

- `state`: pending | running | blocked | done
- `evidence`: command output, job id, PID, file path, file size, duration, checksum, URL, exit code
- `next`: the next concrete action

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
- “可能還要幾分鐘” unless the time came from the platform and you label it as an estimate from that platform

### Report by state, not by stream-of-consciousness

Use one of these states only:

- `STARTED`
- `CHECKPOINT`
- `BLOCKED`
- `COMPLETED`

### Escalate blockers proactively

If blocked, do not wait silently. Report when:

- an approval or credential is missing
- a remote job is stuck or failed
- retries are exhausted
- an expected output file was not produced
- a validation check failed
- a dependency or upstream system is unavailable

## Fixed report formats

### 1) Generic checkpoint update

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

### 2) RunningHub / remote job format

Use for RunningHub, hosted renderers, inference queues, cloud jobs, or any async remote task.

```text
CHECKPOINT
- task_id: <local-id>
- workflow_type: remote-job
- stage: submitted|polling|downloaded|validated|failed
- verified facts:
  - provider: RunningHub
  - remote_job_id: <job-id>
  - submitted_at: <timestamp>
  - latest_status: <queued|running|succeeded|failed>
  - output_file: <path-if-downloaded>
- next: <poll again|download output|inspect failure|deliver result>
```

### 3) Long video generation / multi-segment render format

Use for long video generation, chapter-by-chapter renders, or multiple clips generated separately.

```text
CHECKPOINT
- task_id: <local-id>
- workflow_type: multi-segment-video
- segment_status:
  - seg01: <pending|running|done|failed> [job_id=<id>] [file=<path>]
  - seg02: <pending|running|done|failed> [job_id=<id>] [file=<path>]
  - seg03: <pending|running|done|failed> [job_id=<id>] [file=<path>]
- verified facts:
  - completed_segments: <n>/<total>
  - latest_artifact: <path-or-url>
- next: <render next segment|retry failed segment|start stitch>
```

### 4) Stitch / merge / transcode format

Use for ffmpeg joins, transcodes, subtitles burn-in, packaging, archive creation, or any local long-running process.

```text
CHECKPOINT
- task_id: <local-id>
- workflow_type: local-process
- stage: stitching|transcoding|muxing|packaging|validating
- verified facts:
  - pid: <process-id>
  - command: <short command summary>
  - input_count: <n>
  - output_file: <path>
  - exit_code: <code-if-finished>
- next: <wait for process|validate output|deliver>
```

### 5) Blocker report

```text
BLOCKED
- task_id: <local-id>
- checkpoint: <name>
- blocker: <specific failure or missing dependency>
- verified facts:
  - <key>=<value>
- tried:
  - <attempt 1>
  - <attempt 2>
- need:
  - <approval/input/retry decision>
- safe next step: <what can be done once unblocked>
```

### 6) Completion handoff

```text
COMPLETED
- task_id: <local-id>
- goal: <delivered outcome>
- completed checkpoints:
  - <checkpoint>
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

- Read `references/multi-stage-runbook.md` when you need a fuller SOP for planning, polling, retries, and handoff.
- Run `scripts/checkpoint_report.py` to generate consistent checkpoint/update blocks without rewriting the format each time.

## Minimal operating pattern

1. Start task record.
2. Submit or launch work.
3. Emit checkpoint when a verifiable state changes.
4. If waiting, report the live handle (job id / PID) instead of filler text.
5. If blocked, escalate immediately with facts.
6. Validate outputs.
7. Deliver with `COMPLETED` handoff.
