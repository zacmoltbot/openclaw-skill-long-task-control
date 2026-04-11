# Task Ledger Spec

`task-ledger` 是 `long-task-control` 的半強制（semi-enforced）狀態層。

目的：把原本只存在於訊息中的 long-task status，落到一份可持久化、可檢查、可被 watchdog/cron 掃描的 ledger state file。

## 為什麼需要 ledger

只有文字 checkpoint 時，常見問題是：

- 訊息散落在不同 session / channel，不好追蹤
- agent 說「有在做」，但沒有 durable state
- 長時間沉默時，很難判斷是正常等待、失敗、還是忘了更新
- 後續 compliance checker 沒有結構化輸入可檢查

ledger 的角色就是提供一個簡單、機器可讀的單一事實來源（single source of truth）。

## 建議檔案位置

```text
state/long-task-ledger.json
```

也可每個 task 一檔：

```text
state/tasks/<task_id>.json
```

本 repo 先提供單一 ledger example，方便 script 直接操作。

## Ledger schema

```json
{
  "version": 1,
  "updated_at": "2026-04-11T14:30:00+08:00",
  "tasks": [
    {
      "task_id": "repo-upgrade-20260411-a",
      "skill": "long-task-control",
      "goal": "Upgrade repo to semi-enforced task control system",
      "status": "RUNNING",
      "channel": "discord",
      "owner": "coding_agent",
      "activation": {
        "announced": true,
        "announced_at": "2026-04-11T14:00:00+08:00",
        "message_ref": "discord:msg:123456"
      },
      "workflow": [
        {
          "id": "inspect",
          "title": "Inspect repo/workdir/status",
          "state": "DONE"
        },
        {
          "id": "implement",
          "title": "Implement ledger + watchdog tools",
          "state": "RUNNING"
        },
        {
          "id": "docs",
          "title": "Update README and SKILL",
          "state": "PENDING"
        }
      ],
      "current_checkpoint": "implement",
      "checkpoints": [
        {
          "at": "2026-04-11T14:05:00+08:00",
          "kind": "STARTED",
          "summary": "Repo inspection started",
          "facts": {
            "branch": "main"
          }
        },
        {
          "at": "2026-04-11T14:20:00+08:00",
          "kind": "CHECKPOINT",
          "summary": "Designed task ledger schema",
          "facts": {
            "reference_file": "references/task-ledger-spec.md"
          }
        }
      ],
      "last_checkpoint_at": "2026-04-11T14:20:00+08:00",
      "heartbeat": {
        "expected_interval_sec": 900,
        "timeout_sec": 1800,
        "last_progress_at": "2026-04-11T14:20:00+08:00",
        "last_heartbeat_at": "2026-04-11T14:25:00+08:00",
        "watchdog_state": "OK"
      },
      "blocker": null,
      "artifacts": [
        "references/task-ledger-spec.md"
      ],
      "next_action": "Implement helper scripts",
      "notes": []
    }
  ]
}
```

## Required task fields

每個 task 至少要有：

- `task_id`
- `goal`
- `status`: `PENDING | RUNNING | BLOCKED | COMPLETED | FAILED | ABANDONED`
- `activation.announced`
- `workflow[]`
- `current_checkpoint`
- `last_checkpoint_at`
- `heartbeat.timeout_sec`
- `next_action`

## Status semantics

### `PENDING`
尚未真正開始執行。

### `RUNNING`
有進行中工作，且應持續產生 checkpoint 或 heartbeat。

### `BLOCKED`
因外部條件、錯誤、缺輸入、缺權限而卡住。

### `COMPLETED`
輸出已完成且至少做過一項 validation。

### `FAILED`
任務終止，且目前沒有自動恢復計畫。

### `ABANDONED`
被人工中止或任務被替代。

## Checkpoint vs heartbeat

- `checkpoint`: 有實際進展或新事實，應更新 `last_checkpoint_at`
- `heartbeat`: 只是確認 task 仍被看管，未必代表有新進展

如果只有 heartbeat、長時間沒有 checkpoint，watchdog 應仍可標示 `STALE_PROGRESS`。

## Timeout model

建議拆成兩層：

1. `expected_interval_sec`
   - 正常情況下多久內應該至少有 heartbeat 或 checkpoint
2. `timeout_sec`
   - 超過這個時間仍無 checkpoint，應升級為提醒、警告、或 `BLOCKED/STALE`

### Suggested defaults

- 輕量長任務：`expected_interval_sec=600`, `timeout_sec=1800`
- 遠端排隊任務：`expected_interval_sec=900`, `timeout_sec=3600`
- 高風險 / 高可見度任務：`expected_interval_sec=300`, `timeout_sec=900`

## Watchdog states

watchdog 可輸出以下狀態：

- `OK`: 最近 checkpoint/heartbeat 正常
- `HEARTBEAT_DUE`: 該發 heartbeat 了
- `STALE_PROGRESS`: 太久沒有 progress checkpoint
- `BLOCKED_SILENT`: task 應該標成 blocked，但 ledger 長時間無 blocker 記錄且沒進度
- `MISSING_ACTIVATION`: task 已 RUNNING 但 activation 沒記錄
- `COMPLETED_NO_VALIDATION`: 任務完成但沒有 validation evidence

## Recommended cron / heartbeat design

### Heartbeat loop

適合 agent session 自己維護：

- 每 5-15 分鐘看一次 active tasks
- 若無新進展但仍有被看管，更新 `last_heartbeat_at`
- 若發現 timeout，標記 watchdog state 並產出提醒

### Cron / watchdog scan

適合外部 scheduler：

```bash
python3 scripts/checkpoint_timeout.py --ledger state/long-task-ledger.example.json
python3 scripts/compliance_check.py --ledger state/long-task-ledger.example.json
```

推薦頻率：每 10-30 分鐘一次。

## Semi-enforced interpretation

這套系統不是 hard block runtime，而是：

- 把 long-task 規格轉成可檢查狀態
- 讓 checker/watchdog 能自動抓出明顯不合規
- 讓 maintainer 可在 PR review / QA / cron report 中快速定位問題

也就是：**先做到可追蹤、可提醒、可稽核，再逐步提高 enforcement 強度。**
