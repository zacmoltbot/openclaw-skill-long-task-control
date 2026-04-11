# Failure Examples and Anti-patterns

Use this reference when you need concrete examples of what **not** to do with `long-task-control`.

Each anti-pattern below shows:

- why the behavior is non-compliant
- a **wrong example**
- a **correct example** using the skill's reporting contract

## 1) Missing activation message

### Why this is wrong

If the skill is active, the first user-visible message must explicitly say that `long-task-control` is being used. Skipping this hides the execution mode change and makes later structured updates feel arbitrary.

### Wrong example

```text
我先幫你跑這個，等等回來更新。
```

### Correct example

```text
ACTIVATED
- skill: long-task-control
- announcement: 目前這個 task 會採用 long-task-control SKILL 執行
- reporting: 我會用 checkpoint / blocker / completed 這類可驗證狀態回報進度；有新事實才更新，不用模糊的「還在跑」敘述
- next: 接著建立 task record，開始第一個可驗證步驟
```

## 2) Claiming work is in progress without a task id

### Why this is wrong

Saying "I'm working on it" without a `task_id` makes the task non-auditable. The requester cannot tell which workflow, artifact set, or checkpoint series you are referring to.

### Wrong example

```text
CHECKPOINT
- state: running
- verified facts:
  - render started
- next: keep going
```

### Correct example

```text
TASK START
- task_id: video-20260411-a
- goal: 產出可交付的 stitched video
- workflow:
  1. submit segment renders
  2. collect outputs
  3. stitch final video
  4. validate final file
- expected artifacts:
  - /tmp/video-20260411-a/final.mp4
- first action: submit seg01 render request
```

## 3) Going silent halfway through a long task

### Why this is wrong

If the task is still waiting on a real handle such as a job id or PID, the user should not be left guessing whether the task stalled, failed, or was abandoned.

### Wrong example

```text
CHECKPOINT
- task_id: rh-20260411-001
- checkpoint: 1/4 submitted
- state: done
- verified facts:
  - remote_job_id: rh_123
- next: poll status
```

...then no further update for a long period, despite the job still running.

### Correct example

```text
CHECKPOINT
- task_id: rh-20260411-001
- workflow_type: remote-job
- stage: polling
- verified facts:
  - provider: RunningHub
  - remote_job_id: rh_123
  - latest_status: running
  - last_checked_at: 2026-04-11T00:30:00+08:00
- next: poll again after the current wait interval
```

## 4) Reporting the plan as if it were progress

### Why this is wrong

A plan is not a completed checkpoint. Do not present intended future actions as evidence that work has already advanced.

### Wrong example

```text
CHECKPOINT
- task_id: longtask-20260411-a
- checkpoint: 2/4 validate output
- state: running
- verified facts:
  - 之後會用 ffprobe 驗證
  - 接著會下載檔案
- next: continue
```

### Correct example

```text
CHECKPOINT
- task_id: longtask-20260411-a
- checkpoint: 2/4 download output
- state: done
- verified facts:
  - output_file=/tmp/longtask-20260411-a/final.mp4
  - size_bytes=104857600
- outputs:
  - /tmp/longtask-20260411-a/final.mp4
- next: run ffprobe validation
```

## 5) Mixing old progress with a new topic or new request

### Why this is wrong

Do not reuse stale status from an earlier task, earlier run, or different deliverable. Each distinct request needs its own fresh `task_id` and evidence trail.

### Wrong example

```text
CHECKPOINT
- task_id: video-20260410-a
- checkpoint: 3/4 stitch final video
- state: done
- verified facts:
  - final.mp4 already exists
- next: deliver the newly requested subtitle-burned version
```

### Correct example

```text
TASK START
- task_id: video-20260411-subtitle-a
- goal: 產出燒錄字幕的新版本 final_subbed.mp4
- workflow:
  1. prepare subtitle file
  2. run subtitle burn-in
  3. validate new output
- expected artifacts:
  - /tmp/video-20260411-subtitle-a/final_subbed.mp4
- first action: confirm subtitle input path and start ffmpeg burn-in
```

## 6) Staying silent even though there is no blocker

### Why this is wrong

"Not blocked" does not justify silence. If meaningful state changed, report that state change. The user should not need to ask whether a checkpoint finished.

### Wrong example

```text
(Agent notices the download finished and validation passed, but says nothing until much later.)
```

### Correct example

```text
CHECKPOINT
- task_id: pkg-20260411-a
- checkpoint: 3/4 validate package
- state: done
- verified facts:
  - output_file=/tmp/pkg-20260411-a/build.zip
  - size_bytes=2843312
  - sha256=abc123...
  - validation=unzip -t passed
- outputs:
  - /tmp/pkg-20260411-a/build.zip
- next: publish completion handoff
```

## 7) Saying “still working” with no evidence

### Why this is wrong

A filler update is not a checkpoint. A valid status update needs a concrete state transition or at least current verifiable handles.

### Wrong example

```text
還在跑，等我一下。
```

### Correct example

```text
CHECKPOINT
- task_id: transcode-20260411-a
- workflow_type: local-process
- stage: transcoding
- verified facts:
  - pid: 8421
  - output_file: /tmp/transcode-20260411-a/final.mp4
  - last_observed_state: process running
- next: wait for process exit, then validate output
```

## 8) Running task exists only in chat, not in ledger

### Why this is wrong

If the upgraded skill is using semi-enforced task control, a long-running task should have durable state. Without a ledger entry, watchdog and compliance tooling cannot observe the task.

### Wrong example

```text
ACTIVATED
- skill: long-task-control
...

CHECKPOINT
- task_id: repo-upgrade-20260411-a
- state: running
- verified facts:
  - branch=main
```

But there is no matching ledger task for `repo-upgrade-20260411-a`.

### Correct example

```bash
python3 scripts/task_ledger.py \
  --ledger state/long-task-ledger.json \
  init repo-upgrade-20260411-a \
  --goal "Upgrade repo to semi-enforced task control" \
  --activation-announced \
  --next-action "Inspect repo status"
```

Then continue with visible `TASK START` / `CHECKPOINT` updates.

## 9) Cron detects stale progress but never nudges the main agent

### Why this is wrong

If the monitor only records that something is stale, but never pushes the owner agent to resume or close the loop, it fails its purpose as an execution nudge.

### Wrong example

```text
monitor result: STALE_PROGRESS
monitor result: STALE_PROGRESS
monitor result: STALE_PROGRESS
```

...with no reminder, no escalation, and no terminal closure.

### Correct example

```text
monitor result: NUDGE_MAIN_AGENT
reason: no new checkpoint for 2400s; main agent should resume, post checkpoint, or mark BLOCKED/FAILED
action: send_execution_nudge
```

## 10) Cron keeps running after terminal status

### Why this is wrong

A terminal task does not need perpetual monitoring. Keeping the cron alive wastes cost and creates noisy false reminders.

### Wrong example

```text
status=COMPLETED
monitor result: HEARTBEAT_DUE
```

### Correct example

```text
status=COMPLETED
monitor result: STOP_AND_DELETE
action: delete_monitor_cron
```

## 11) Skipping owner reconciliation after repeated stale nudges

### Why this is wrong

Repeated stale progress does not always mean the task is truly blocked. The owner may have continued working but forgotten to update the ledger, or may admit the work was forgotten and needs to be resumed immediately. Jumping straight from stale nudges to blocker escalation loses that distinction.

### Wrong example

```text
state=NUDGE_MAIN_AGENT
nudge_count=2
monitor result: BLOCKED_ESCALATE
reason: too many nudges
```

### Correct example

```text
state=OWNER_RECONCILE
reason: stale progress persists after prior nudges
owner branch handling:
- A in progress but forgot ledger -> append missing checkpoint(s)
- B blocked -> BLOCKED_ESCALATE
- C completed -> write COMPLETED + validation
- D no reply -> seek external evidence
- E forgot/not doing -> require resume execution now
```

## 12) Treating blocked tasks as ordinary reminders

### Why this is wrong

Once a task is truly blocked, the right move is blocker escalation, not more generic nudges.

### Wrong example

```text
status=BLOCKED
monitor result: NUDGE_MAIN_AGENT
action: ask agent to keep going
```

### Correct example

```text
status=BLOCKED
monitor result: BLOCKED_ESCALATE
reason: waiting for user approval to access private repo
action: send_blocked_escalation_then_delete_cron
```

## Quick review checklist

Before sending any progress update or monitor decision, ask:

- Did I already send the activation message?
- Does this update include the correct `task_id`?
- Am I reporting a verified state change instead of a plan?
- Am I accidentally reusing evidence from an older task?
- If work is still ongoing, did I include the current handle or output evidence?
- If a checkpoint finished, did I report it instead of staying silent?
- If progress is stale, am I nudging the main agent instead of passively repeating `STALE_PROGRESS`?
- After repeated nudges, did I run `OWNER_RECONCILE` before assuming the task is blocked?
- If the task is terminal, did I stop/delete the monitor?
