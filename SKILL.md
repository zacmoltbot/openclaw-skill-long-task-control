---
name: long-task-control
description: Standardize long-running durable task execution with a strict truth/control split. Use when work spans multiple turns, depends on external async jobs, needs proactive supervision, or must survive interruption/silence without fabricating state. Especially use for: (1) 3+ sequential API/exec steps, (2) multi-stage image/video/content generation, (3) tasks with partial-completion risk that need durable progress tracking, resume, and reconciliation.
---

# Long Task Control

## Quickstart

### Good fit

用 LTC 處理這些任務：
- 3+ sequential API calls / exec commands / workflow stages
- multi-stage image / video / content generation
- external async/provider job 需要等待、輪詢、下載、reconcile
- 任務會跨多 turn，不能只靠 session memory 記住狀態
- partial completion 風險高，需要 durable progress tracking / resume / monitor

### Bad fit

不要把這些任務硬塞進 LTC：
- 單次查資料、一次性問答、短 summarization
- 短 research / scouting / brainstorming
- 1-2 步就能完成的小修補
- 沒有可觀察 progress signal 的模糊工作
- 其實只需要一般 agent turn，不需要 durable execution ownership 的任務

### Canonical flow

大多數任務沿著這條路徑走：

1. `init` 建立 task
2. 開始做事時寫 `STEP_PROGRESS`
3. 某一步真的完成時寫 `STEP_COMPLETED`
4. 整個任務真的完成時寫 `TASK_COMPLETED`
5. 有 external/provider job 時寫 `external-job`
6. 有 artifact/download 真正落地時寫 `download-observed`
7. owner 有觀察/回覆時寫 `owner-reply`
8. monitor 用 `monitor_nudge.py --apply-supervision` 做 supervision，不直接腦補 task truth

### Do not do this

- 不要手改 `derived.*`
- 不要把 `STEP_PROGRESS` 當成 `STEP_COMPLETED`
- 不要沒有 observed truth 就宣告 success / completed
- 不要把 monitor/control state 當 ground truth
- 不要把 owner guess / optimistic assumption 寫成 observed fact
- 不要為了讓 monitor 安靜而補假 state

這版的重點不是多一個 monitor，而是把 architecture 拆乾淨：

1. **Observed Truth**：owner / executor / external observation 回填真相
2. **Derived State**：script 從 truth 做 deterministic projection
3. **Control Actions**：monitor / owner / user-facing delivery 根據 derived state 執行動作

## Root rule

- 沒有 observed truth，就不要判 `OK`
- `STEP_PROGRESS` 不等於 `STEP_COMPLETED`
- `TASK_COMPLETED` 只能由 completion truth 驅動
- monitor 不能用矛盾欄位腦補「agent 還在做 / external pending」

## Event model

使用這些事件，不再混用 `CHECKPOINT`：

- `STARTED`
- `STEP_PROGRESS`
- `STEP_COMPLETED`
- `TASK_COMPLETED`
- `BLOCKED_CONFIRMED`
- `OWNER_RESUMED`
- `OWNER_REPLY_RECORDED`
- `EXTERNAL_OBSERVED`
- `DOWNLOAD_OBSERVED`
- `HEARTBEAT`

## Architecture summary

### Observed truth

由 owner / executor / external observation 回填：
- `observations[]`
- `observed.steps.*`
- `observed.task_completion`
- `observed.block`
- `observed.owner`
- `observed.external_jobs`
- `observed.downloads`

### Derived state

由 `task_ledger.py project_task()` / `monitor_nudge.py` 推導：
- `derived.workflow`
- `derived.current_step`
- `derived.step_states`
- `derived.pending_external`
- `derived.truth_state`
- `derived.inconsistencies`

### Control actions

- `TRUTH_INCONSISTENT`
- `OWNER_RECONCILE`
- `NUDGE_MAIN_AGENT`
- `BLOCKED_ESCALATE`
- `STOP_AND_DELETE`

## Monitor precedence

1. terminal → `STOP_AND_DELETE`
2. truth inconsistent / suspicious external claim → `TRUTH_INCONSISTENT` or `OWNER_RECONCILE`
3. blocked confirmed → retry-first check → `BLOCKED_ESCALATE`
4. no material progress delta / heartbeat → `NUDGE_MAIN_AGENT` / `STALE_PROGRESS` / `HEARTBEAT_DUE`
5. only then `OK`

## Progress-based supervision contract

- LTC **不是** wall-clock task TTL；不是「20 分鐘到就停任務」
- monitor 只在 **長時間沒有實質 progress delta** 時介入
- progress delta 可來自：
  - 新的 `STEP_PROGRESS` / `STEP_COMPLETED` / `TASK_COMPLETED`
  - 新 artifact / download observation
  - external/provider job status 變化
  - executor health 的成功心跳（例如 `executor_health.last_success_at`）
- 只要長任務仍持續產生這些 progress signal，就算跑幾小時也不應被當成 stop-loss 目標
- `timeout_sec` / `nudge_after_sec` 在這版語義上代表 **progress idle threshold**，不是 hard kill deadline

## Owner-resume contract

monitor 用 `sessions_send` 要 owner 做的是：
- 先觀察
- 補真實資料
- 再 resume / 完成 / blocked

不要直接寫半成品 state 去和 deterministic projection 打架。

## Retry-first contract

`DOWNLOAD_TIMEOUT` / `DOWNLOAD_INCOMPLETE` / `TRANSIENT_NETWORK` / `EXECUTION_ERROR` / `EXTERNAL_WAIT`
必須先來自 observed truth，之後 monitor 才能用 retry counter 決定 retry 或 escalate。

## Primary commands

```bash
python3 scripts/task_ledger.py --ledger state/long-task-ledger.json init <task_id> ...
python3 scripts/task_ledger.py --ledger state/long-task-ledger.json checkpoint <task_id> --event-type STEP_PROGRESS ...
python3 scripts/task_ledger.py --ledger state/long-task-ledger.json checkpoint <task_id> --event-type STEP_COMPLETED ...
python3 scripts/task_ledger.py --ledger state/long-task-ledger.json checkpoint <task_id> --event-type TASK_COMPLETED ...
python3 scripts/task_ledger.py --ledger state/long-task-ledger.json block <task_id> ...
python3 scripts/task_ledger.py --ledger state/long-task-ledger.json external-job <task_id> ...
python3 scripts/task_ledger.py --ledger state/long-task-ledger.json download-observed <task_id> ...
python3 scripts/task_ledger.py --ledger state/long-task-ledger.json owner-reply <task_id> --reply <A|B|C|D|E> ...
python3 scripts/monitor_nudge.py --ledger state/long-task-ledger.json --apply-supervision
```

## References

- `references/task-ledger-spec.md`
- `references/monitor-action-spec.md`
- `scripts/task_ledger.py`
- `scripts/monitor_nudge.py`
- `scripts/openclaw_ops.py`

## Why this version exists

因為舊版把 deterministic script 與 agent judgement 混在一起，導致：
- `CHECKPOINT` 同時表示 progress / completed
- monitor 用半套 truth 判 OK
- external pending claim 沒證據也被當真
- owner resume 後寫的狀態和 script projection 打架

這版是直接把根因拆開重做，不再 patch 舊混線模型。
