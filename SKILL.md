---
name: long-task-control
description: Standardize task control, checkpointing, and status reporting for long-running or multi-stage work. Use when task characteristics indicate non-trivial execution control is needed: the work is likely to take more than one visible turn, includes waiting/polling/background execution, has multiple checkpoints or handoffs, depends on external or asynchronous job systems, produces intermediate artifacts before the final deliverable, or can become blocked and requires proactive status reporting. Do not rely on enumerating task categories; trigger from execution traits. When triggered, first emit the required activation message that explicitly tells the user this task is being executed with the long-task-control skill and how progress will be reported.
---

# Long Task Control

Keep long tasks boring, traceable, and easy to audit. Break work into checkpoints, report only verifiable facts, surface blockers early, and finish with a concrete delivery handoff.

## Core rules

1. Decide whether to activate this skill based on task characteristics, not a memorized list of task types.
2. Once activated, emit the mandatory activation message before starting meaningful execution updates.
3. Create a task record before starting meaningful work.
4. Split the task into numbered checkpoints with clear completion evidence.
5. Report only facts you can verify right now.
6. Prefer identifiers over narratives: task id, job id, PID, file path, URL, timestamp, exit state.
7. If something is blocked, report the exact blocker, what was tried, and the next needed action.
8. When complete, hand off outputs, validation result, and any still-running/background items.
9. Do not claim progress based on hope, estimates, or hidden reasoning.

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

Do not decide by saying "this is a video task" or "this is a RunningHub task". Decide by asking whether the execution itself is long, staged, asynchronous, stateful, or blocker-prone.

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

1. Detect whether task traits require long-task-control.
2. Emit the mandatory activation message.
3. Start task record.
4. Submit or launch work.
5. Emit checkpoint when a verifiable state changes.
6. If waiting, report the live handle (job id / PID) instead of filler text.
7. If blocked, escalate immediately with facts.
8. Validate outputs.
9. Deliver with `COMPLETED` handoff.
