# Monitor Action Spec

這份 reference 定義 monitor / owner 的責任邊界，以及 `monitor_nudge.py` 現在已接上的 action wiring。

## 1. Ledger ownership contract

### Owner / main agent 負責寫 task truth

只有 owner / main agent 可以更新這些 **task truth** 欄位：

- `status`
- `goal`
- `workflow`
- `current_checkpoint`
- `checkpoints[]`
- `last_checkpoint_at`
- `blocker`
- `validation`
- `artifacts`
- `next_action`
- `notes`
- `activation.*`

簡單講：

- 任務是不是完成 / 失敗 / blocked
- checkpoint 有沒有真的往前
- blocker 內容是什麼
- validation 證據是什麼

這些都只能由 owner / main agent 寫入，因為它們是 single source of truth。

### Monitor 只更新 supervision metadata

monitor 允許更新的欄位只限於 supervision metadata：

- `heartbeat.watchdog_state`
- `monitoring.nudge_count`
- `monitoring.last_nudge_at`
- `monitoring.last_escalated_at`
- `monitoring.last_action_at`
- `monitoring.last_action_state`
- `monitoring.last_action_reason`
- `monitoring.last_action_kind`
- `monitoring.last_action_payload`
- `monitoring.action_log[]`
- `monitoring.cron_state`

monitor **不得**自行：

- 把 `RUNNING` 改成 `BLOCKED`
- 把 task 寫成 `FAILED`
- 補 fake checkpoint
- 改 `next_action` 來偽裝 owner decision

Repo 內目前由兩個機制保證這件事：

1. `scripts/task_ledger.py supervisor-update` 只接受 supervision keys
2. `scripts/monitor_nudge.py --apply-supervision` 會在寫回後檢查 diff，若碰到非 supervision 欄位會直接報錯

## 2. Action outcomes

`monitor_nudge.py` 會先做 deterministic decision，然後為需要外部動作的狀態附上 `action_payload`。

### `NUDGE_MAIN_AGENT`

適用：

- active task 長時間沒新 checkpoint
- activation 缺失
- monitor 判斷 owner/main agent 應回來續行或關閉 task

payload contract：

```json
{
  "kind": "NUDGE_MAIN_AGENT",
  "deliver_to": "main-agent",
  "channel": "discord",
  "title": "Execution nudge for <task_id>",
  "message": "Resume execution, post a real checkpoint, or write terminal truth",
  "facts": {
    "task_id": "...",
    "status": "RUNNING",
    "reason": "...",
    "next_action": "..."
  }
}
```

monitor side effects：

- `monitoring.nudge_count += 1`（僅當真的送出 nudge）
- `monitoring.last_nudge_at = now`
- `heartbeat.watchdog_state = NUDGE_MAIN_AGENT`
- append `monitoring.action_log[]`

owner next step：

- 繼續執行
- 寫 checkpoint
- 或明確寫入 `COMPLETED` / `FAILED` / `BLOCKED`

### `OWNER_RECONCILE`

適用：

- active task stale progress 在 prior nudges 後仍持續
- monitor 需要先 query owner，避免把「忘了更新 ledger」誤判成 blocked/failed

payload contract：

```json
{
  "kind": "OWNER_RECONCILE",
  "deliver_to": "main-agent",
  "channel": "discord",
  "title": "Owner reconciliation for <task_id>",
  "message": "Query the owner now and reconcile task truth",
  "facts": {
    "task_id": "...",
    "status": "RUNNING",
    "reason": "...",
    "branches": {
      "A_IN_PROGRESS_FORGOT_LEDGER": "append missing checkpoints and keep RUNNING",
      "B_BLOCKED": "write blocker truth and escalate",
      "C_COMPLETED": "write COMPLETED with validation evidence",
      "D_NO_REPLY": "seek external evidence before changing task truth",
      "E_FORGOT_OR_NOT_DOING": "immediately require resume execution /補做"
    }
  }
}
```

monitor side effects：

- `monitoring.owner_query_at = now`
- `heartbeat.watchdog_state = OWNER_RECONCILE`
- append `monitoring.action_log[]`

owner next step：

- query owner immediately
- branch A: 補 ledger，補 checkpoint / artifacts / next_action
- branch B: 寫 `BLOCKED` truth，交給 monitor 送 `BLOCKED_ESCALATE`
- branch C: 寫 `COMPLETED` + validation
- branch D: 先找外部 evidence，不可虛構 checkpoint
- branch E: 立刻恢復執行，不是只做紀錄

### `BLOCKED_ESCALATE`

適用：

- ledger 已 `status=BLOCKED`
- 或 owner reconciliation 已確認 task 需要外部輸入/批准/修復

payload contract：

```json
{
  "kind": "BLOCKED_ESCALATE",
  "deliver_to": "main-agent",
  "channel": "discord",
  "title": "Blocked escalation for <task_id>",
  "message": "Escalate blocker facts, then stop/delete the monitor cron",
  "facts": {
    "task_id": "...",
    "status": "BLOCKED",
    "reason": "...",
    "blocker": {
      "reason": "...",
      "need": ["..."],
      "safe_next_step": "..."
    }
  }
}
```

monitor side effects：

- `monitoring.last_escalated_at = now`
- `monitoring.cron_state = DELETE_REQUESTED`
- `heartbeat.watchdog_state = BLOCKED_ESCALATE`
- append `monitoring.action_log[]`

owner next step：

- 對 requester / human / owner channel 發 blocker escalation
- 補齊需要的人類決策
- 若 blocker 消除，再由 owner 決定是否重啟 task / 新增 checkpoint

### `STOP_AND_DELETE`

適用：

- `COMPLETED`
- `FAILED`
- `ABANDONED`
- `BLOCKED` 且 escalation 已送過

payload contract：

```json
{
  "kind": "STOP_AND_DELETE",
  "deliver_to": "long-task-monitor",
  "title": "Stop monitor for <task_id>",
  "message": "Delete/disable the monitor cron now"
}
```

monitor side effects：

- `monitoring.cron_state = DELETE_REQUESTED`
- `heartbeat.watchdog_state = STOP_AND_DELETE`
- append `monitoring.action_log[]`

owner / scheduler next step：

- 停用 cron / watcher
- 避免 terminal task 持續空轉

## 3. Escalation policy

### 只是提醒，不 escalation

- `HEARTBEAT_DUE`
- 初次 `STALE_PROGRESS`
- 初次 `NUDGE_MAIN_AGENT`
- active task 還可能只是 supervision 落後

### 先升級成 `OWNER_RECONCILE`

- `nudge_count` 已到 `escalate_after_nudges`
- `nudge_count` 已超過 `max_nudges`
- monitor 需要 owner 回答：到底是有在做、卡住、做完、沒回、還是根本沒做

理由：避免 monitor 把「owner 忘了更新」和「task 真 blocked」混成同一種 escalation。

### 再升級成 `BLOCKED_ESCALATE`

- task 已明確 `BLOCKED`
- 或 owner reconciliation 已經確認 continuation 需要外部決策 / approval / input / fix

## 4. 何時刪 cron

刪 cron 的條件：

- terminal status 成立
- blocked escalation 已送出
- task 已不再需要 supervision

現在 repo 內的 pseudo-implementation 是：

- monitor 不直接刪系統 cron
- monitor 會把 `monitoring.cron_state=DELETE_REQUESTED` 寫回 ledger
- 並產出 `STOP_AND_DELETE` / `BLOCKED_ESCALATE` payload

這讓 scheduler / owner agent / wrapper script 可以安全接手真正的 cron removal。

## 5. Current implementation boundary

### 已實作

- deterministic decision engine
- supervision-only ledger writes
- `action_payload` 產生
- `DELETE_REQUESTED` marker
- demo E2E test 驗證三種 action path

### 尚未實作

- 真實對 Discord / message bus 送提醒
- 真實安裝 / 刪除 crontab entry
- 多 owner routing / retry queue
- cross-process locking

所以目前狀態是：**repo 內已可測 end-to-end wiring；外部通知與系統 cron deletion 仍保留為 integration layer。**
