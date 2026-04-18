---
name: long-task-control
description: Standardize long-running task control with a strict truth/control split. Use when work spans multiple turns, depends on external async jobs, needs proactive supervision, or must survive owner silence without fabricating state.
---

# Long Task Control

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
