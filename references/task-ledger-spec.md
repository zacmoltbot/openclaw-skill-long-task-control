# Task Ledger Spec

`task-ledger` 是 `long-task-control` 的半強制（semi-enforced）狀態層。

目的：把原本只存在於訊息中的 long-task status，落到一份可持久化、可檢查、可被 watchdog/cron 掃描的 ledger state file。

這次升級後，monitor cron 的定位也明確改成 **任務續行提醒器 / execution nudge**：

- 當任務還沒完成
- 當 main agent 沒再推進
- 當沒有新 checkpoint
- 當 task 沒有被正確收尾成 `COMPLETED` / `FAILED` / `BLOCKED`

monitor cron 應用低成本 rule engine 主動提醒 main agent 繼續執行，而不是只被動抓 timeout。

## 為什麼需要 ledger

只有文字 checkpoint 時，常見問題是：

- 訊息散落在不同 session / channel，不好追蹤
- agent 說「有在做」，但沒有 durable state
- 長時間沉默時，很難判斷是正常等待、失敗、還是忘了更新
- 後續 compliance checker 沒有結構化輸入可檢查
- monitor cron 不知道該提醒、升級、還是自刪

ledger 的角色就是提供一個簡單、機器可讀的單一事實來源（single source of truth）。

**重要 contract：** owner / main agent 維護 task truth；monitor 只允許更新 supervision metadata。詳細欄位清單與 action contract 請看 `references/monitor-action-spec.md`。

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
  "updated_at": "2026-04-11T15:00:00+08:00",
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
      "monitoring": {
        "nudge_after_sec": 1800,
        "blocked_escalate_after_sec": 1800,
        "renotify_interval_sec": 900,
        "last_nudge_at": null,
        "last_escalated_at": null,
        "cron_owner": "long-task-monitor"
      },
      "validation": [],
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

### External async job fields

對於 RunningHub / queue / remote render 這類外部 async job，task 內要保留 `external_jobs[]`：

```json
{
  "provider": "runninghub",
  "job_id": "rh-123",
  "status": "RUNNING",
  "workflow": "wf-a",
  "app": "app-a",
  "pending_external": true,
  "submitted_at": "2026-04-12T12:00:00+08:00",
  "updated_at": "2026-04-12T12:05:00+08:00",
  "failure_count": 1,
  "switch_count": 0,
  "history": [
    {"at": "...", "state": "SUBMITTED", "summary": "...", "facts": {}},
    {"at": "...", "state": "RUNNING", "summary": "...", "facts": {}}
  ]
}
```

Allowed lifecycle states:
- `SUBMITTED`
- `PENDING`
- `RUNNING`
- `FAILED`
- `RETRYING`
- `SWITCHED_WORKFLOW`
- `COMPLETED`

每個 pending external job 還必須帶最小 `provider_evidence` contract，至少有一個可驗證欄位，例如：
- `provider_job_id`
- `submission_receipt` / `submission_receipt_id`
- `provider_status_handle` / `status_handle`
- `status_url`
- `poll_token`
- `artifact_path` / `artifact_url` / `output_file`
- `provider_response_ref`

Monitor stale detection must consult this structure first. 只有「pending state + provider evidence contract 成立」才可視為 legitimate external wait。若 ledger 只有 owner 自己寫的 pending claim、但缺 provider evidence，monitor 必須先進 `OWNER_RECONCILE` 要 owner 補證據；多次 reconcile 仍補不出來才 escalation / stop cron.

### Reporting delivery fields

`ledger truth`、`monitor supervision`、`user-visible status update` 要分開，但必須被同一套 durable contract 綁住。

每個 task 應有：

- `reporting.delivery_seq`
- `reporting.pending_updates[]`
- `reporting.delivered_updates[]`

`pending_updates[]` entry 建議至少包含：

- `update_id`
- `event_type`: `STEP_COMPLETED | EXTERNAL_JOB_COMPLETED | EXTERNAL_JOB_FAILED | WORKFLOW_SWITCH | BLOCKED_ESCALATE | COMPLETED_HANDOFF`
- `summary`
- `checkpoint`
- `facts`
- `status_block`
- `created_at`
- `required`
- `delivered`
- `delivered_at` / `message_ref`（delivery 完成後）

規則：owner 一旦寫進可見進度點，就必須同時產生 `pending_updates[]`；之後只有在實際 requester-visible delivery 完成後，才可用 `ack-delivery` 將該 obligation 轉入 `delivered_updates[]`。

### Recommended monitor fields

若要讓 monitor cron 做 execution nudge，建議補上：

- `monitoring.nudge_after_sec`
- `monitoring.blocked_escalate_after_sec`
- `monitoring.renotify_interval_sec`
- `monitoring.last_nudge_at`
- `monitoring.owner_query_at`
- `monitoring.owner_response_at`
- `monitoring.owner_response_kind`
  - 對應 `A_IN_PROGRESS_FORGOT_LEDGER | B_BLOCKED | C_COMPLETED | D_NO_REPLY | E_FORGOT_OR_NOT_DOING`
- `monitoring.reconcile_count`
- `monitoring.last_reconcile_at`
- `monitoring.last_resume_request_at`
- `monitoring.recovery_attempt_count`
- `monitoring.last_escalated_at`
- `monitoring.last_action_at`
- `monitoring.last_action_state`
- `monitoring.last_action_reason`
- `monitoring.last_action_kind`
- `monitoring.last_action_payload`
- `monitoring.action_log[]`
- `monitoring.cron_owner`
- `monitoring.cron_state`

## Status semantics

### `PENDING`
尚未真正開始執行，但若已被納入 supervision，monitor 可提醒 main agent 真正啟動或明確取消。

### `RUNNING`
有進行中工作，且應持續產生 checkpoint 或 heartbeat。

### `BLOCKED`
因外部條件、錯誤、缺輸入、缺權限而卡住。這不是繼續無限提醒的狀態；應轉成 `BLOCKED_ESCALATE`，送出 blocker escalation，然後停掉 cron。

### `COMPLETED`
輸出已完成且至少做過一項 validation。monitor 應 `STOP_AND_DELETE`。

### `FAILED`
任務終止，且目前沒有自動恢復計畫。monitor 應 `STOP_AND_DELETE`。

### `ABANDONED`
被人工中止或任務被替代。monitor 應 `STOP_AND_DELETE`。

## Checkpoint vs heartbeat

- `checkpoint`: 有實際進展或新事實，應更新 `last_checkpoint_at`
- `heartbeat`: 只是確認 task 仍被看管，未必代表有新進展

如果只有 heartbeat、長時間沒有 checkpoint，monitor 應先給 `STALE_PROGRESS`，之後進一步升級成 `NUDGE_MAIN_AGENT`。

## Timeout model

建議拆成三層：

1. `expected_interval_sec`
   - 正常情況下多久內應該至少有 heartbeat
2. `timeout_sec`
   - 超過這個時間仍無 checkpoint，視為 `STALE_PROGRESS`
3. `nudge_after_sec`
   - stale progress 持續多久後，要正式推進到 `NUDGE_MAIN_AGENT`
4. `escalate_after_nudges` / `max_nudges`
   - 累積 nudge 後，不是直接宣判 failure；先進入 `OWNER_RECONCILE` 查 owner 真相，再決定 blocked/completed/resume path

### Suggested defaults

- 輕量長任務：`expected_interval_sec=600`, `timeout_sec=1800`, `nudge_after_sec=1800`
- 遠端排隊任務：`expected_interval_sec=900`, `timeout_sec=3600`, `nudge_after_sec=3600`
- 高風險 / 高可見度任務：`expected_interval_sec=300`, `timeout_sec=900`, `nudge_after_sec=900`

## Monitor state machine

### `OK`
最近 checkpoint/heartbeat 正常。

### `HEARTBEAT_DUE`
heartbeat 太久沒更新，但 task 尚未被證明停住。只做輕量提醒。

### `STALE_PROGRESS`
太久沒有 progress checkpoint。這是 pre-gate warning，表示「可能停住」。

### `NUDGE_MAIN_AGENT`
這是第一層 execution nudge；monitor 應先提醒 owner agent 回來續行或收尾。

當以下任一成立時使用：

- `STALE_PROGRESS` 已達 `nudge_after_sec`
- activation 缺失
- task 明顯沒有被繼續推進，也沒有被正確關閉

預期動作：提醒 main agent 回來做以下其中之一：

- 繼續執行
- 補發 checkpoint
- 明確標記 `BLOCKED`
- 明確標記 `FAILED`
- 若已完成則寫 `COMPLETED`

### `OWNER_RECONCILE`
當 stale progress 在 prior nudges 後仍持續時，monitor 不應直接假設 blocked 或 failed，而應先進入 stale -> query owner。

建議分流：

- `A_IN_PROGRESS_FORGOT_LEDGER`: owner 表示有在做，只是忘了更新 ledger -> owner 補 checkpoint / artifacts / next_action，維持 `RUNNING`
- `B_BLOCKED`: owner 表示其實卡住 -> owner 寫 `BLOCKED` truth，monitor 再走 `BLOCKED_ESCALATE`
- `C_COMPLETED`: owner 表示已完成 -> owner 補 validation evidence，寫 `COMPLETED`
- `D_NO_REPLY`: owner 沒回 -> 不可憑空改 task truth；先找外部 evidence（job status, PID, output file, upstream log）
- `E_FORGOT_OR_NOT_DOING`: owner 承認忘了做 / 沒在做 -> 不能只留下 note；必須立刻要求 resume execution / 補做，repo 內由 `task_ledger.py owner-reply --reply E` 直接寫 resume-required checkpoint 並把 `next_action` 改成恢復執行路徑

### `BLOCKED_ESCALATE`
當以下任一成立時使用：

- ledger 已明確 `status=BLOCKED`
- task 缺外部輸入/批准/修復，無法自行安全續行，而且前面的自救路徑（resume / rebuild-safe-step / reconcile / 補做）已嘗試過或明確不成立

預期動作：送 blocker escalation，而不是再發「請繼續做」提醒。送出後應立刻停掉 cron，避免 monitor 空跑。

### `STOP_AND_DELETE`
當以下任一成立時使用：

- `status=COMPLETED`
- `status=FAILED`
- `status=ABANDONED`
- `status=BLOCKED` 且 escalation 已送出
- task record 已消失或不再受監控

預期動作：刪除 monitor cron，避免無限空轉。

## Reminder / blocked / failed / self-delete rules

### 只是提醒

以下情況只需要提醒，不應直接宣判失敗：

- heartbeat 過期
- 沒新 checkpoint，但沒有明確錯誤
- main agent 只是沒有更新 ledger / 訊息

### 視為 blocked

以下情況應由 owner agent 明確寫入 `BLOCKED`：

- 等待 approval
- 等待 credentials
- 等待 user input
- 等待 upstream fix
- 等待不可自動解的依賴

### 視為 failed

以下情況應由 owner agent 明確寫入 `FAILED`：

- 已知步驟失敗且沒有安全自動續行方案
- retry policy 已耗盡且沒有下一步
- 目前能做的只有人工重新設計或重開 task

### 自刪 cron

cron 應自刪，不要無限保留，當：

- terminal state 已成立
- blocker escalation 已發出且 task 不再需要輪詢
- task 被替代 / 取消 / 移除

## Recommended cron / heartbeat design

### Low-cost pre-gate loop

推薦順序：

1. 跑 `monitor_nudge.py`
2. 只有出現 `STALE_PROGRESS` / `NUDGE_MAIN_AGENT` / `BLOCKED_ESCALATE` 時，才進一步觸發 deeper review
3. terminal state 直接停掉 cron

### Example commands

```bash
python3 scripts/monitor_nudge.py --ledger state/long-task-ledger.example.json --only-active
python3 scripts/checkpoint_timeout.py --ledger state/long-task-ledger.example.json
python3 scripts/compliance_check.py --ledger state/long-task-ledger.example.json
```

推薦頻率：每 10-30 分鐘一次。

## Semi-enforced interpretation

這套系統不是 hard block runtime，而是：

- 把 long-task 規格轉成可檢查狀態
- 讓 checker/watchdog 能自動抓出明顯不合規
- 讓 monitor cron 能低成本地決定：提醒、升級、或自刪
- 讓 maintainer 可在 PR review / QA / cron report 中快速定位問題

也就是：**先做到可追蹤、可提醒、可稽核、可收尾，再逐步提高 enforcement 強度。**
