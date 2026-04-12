# Monitor Action Spec

這份 reference 定義 monitor / owner 的責任邊界，以及 `monitor_nudge.py` 現在已接上的 action wiring。

## 1. Ledger ownership contract

### Owner / main agent 負責寫 task truth

另外，owner 也負責 **user-visible delivery truth**：`reporting.pending_updates[]` / `reporting.delivered_updates[]`。這不是 monitor 的職責。

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
- `monitoring.reconcile_count`
- `monitoring.last_reconcile_at`
- `monitoring.last_resume_request_at`
- `monitoring.recovery_attempt_count`
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

**Delivery 方式（已更新）：** 不再發 Discord，而是用 `sessions_send` 直接喚醒 owner agent（main session: `agent:main:discord:channel:1484432523781083197`）。

payload contract（仍保留供 preview-tick / format_notification 使用，實際 delivery 由 cron_prompt 決定）：

```json
{
  "kind": "NUDGE_MAIN_AGENT",
  "deliver_to": "main-agent",
  "delivery": "sessions_send",
  "session_key": "agent:main:discord:channel:1484432523781083197",
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

sessions_send 發送的實際訊息格式（由 cron_prompt 構造）：
```
【long-task-control / execution nudge】
task_id=<task_id>
Task stalled at step=<current_step>.
請 main agent 立刻回來續行，先自救：
  - resume / rebuild-safe-step / reconcile 缺漏 checkpoint
  - 若真的無法自救，回報 BLOCKED
next_action=<next_action>
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

**Delivery 方式（已更新）：** 不再發 Discord，而是用 `sessions_send` 直接喚醒 owner agent（main session: `agent:main:discord:channel:1484432523781083197`）。

payload contract（仍保留供 preview-tick / format_notification 使用）：

```json
{
  "kind": "OWNER_RECONCILE",
  "deliver_to": "main-agent",
  "delivery": "sessions_send",
  "session_key": "agent:main:discord:channel:1484432523781083197",
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

sessions_send 發送的實際訊息格式（由 cron_prompt 構造）：
```
【long-task-control / owner reconcile】
task_id=<task_id>
任務處於落後狀態，請儘快回報你所處的 branch：
  - A_IN_PROGRESS_FORGOT_LEDGER：還在跑但忘了更新 ledger，請補 checkpoint
  - B_BLOCKED：任務被 block，請寫 BLOCKED checkpoint
  - C_COMPLETED：任務已完成，請寫 COMPLETED checkpoint
  - D_NO_REPLY：無法確認，先找外部 evidence
  - E_FORGOT_OR_NOT_DOING：忘了或沒在做，請立刻補做
reason=<reason>
```

monitor side effects：

- `monitoring.owner_query_at = now`
- `heartbeat.watchdog_state = OWNER_RECONCILE`
- append `monitoring.action_log[]`

owner next step：

- query owner immediately
- 若原因是 `pending external claim lacks provider evidence`，先補 `external_jobs[].provider_evidence`（真實 provider job id / receipt / status handle / artifact handle 等），不要只回一句「還在跑」
- branch A: 補 ledger，補 checkpoint / artifacts / next_action
- branch B: 寫 `BLOCKED` truth，交給 monitor 送 `BLOCKED_ESCALATE`
- branch C: 寫 `COMPLETED` + validation
- branch D: 先找外部 evidence，不可虛構 checkpoint
- branch E: 立刻恢復執行，不是只做紀錄

若 monitor 已多次要求 reconcile，但 owner 仍補不出 provider evidence，monitor 需將其視為 weak/fake external pending claim，升級 `BLOCKED_ESCALATE` 並要求 cleanup，避免 cron 無限空等。

### `BLOCKED_ESCALATE`

適用（偏晚、終局）：

- ledger 已 `status=BLOCKED`
- 或 owner reconciliation 已確認 task 需要外部輸入/批准/修復，且 resume / rebuild-safe-step / reconcile / 補做 已經嘗試過或明確不可能

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
- `monitoring.cron_state = DELETE_REQUESTED`（立即停止 cron，no more空燒）
- `heartbeat.watchdog_state = BLOCKED_ESCALATE`
- append `monitoring.action_log[]`
- **新增：** `monitoring.retry_count[step_id:failure_type]` 在每次 STALE/BLOCKED 評估時遞增；同一 step + 同一 failure type 累計 3 次 → 立即 BLOCKED_ESCALATE，不需要再等牆時鐘

owner next step：

- 對 requester / human / owner channel 發 blocker escalation，**一次交代清楚**：哪個 step 卡住、retry 次數（retry_count）、嘗試了什麼、為什麼現在判定失敗、建議下一步
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
- **【新】** retry-count 機制：同一個 step 同一 failure type 失敗 3 次 → 立即 BLOCKED_ESCALATE（不等牆時鐘 60 分鐘）

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
- **【新】** retry-count tracking：`monitoring.retry_count` dict，per-step per-failure-type；`DOWNLOAD_TIMEOUT` / `DOWNLOAD_INCOMPLETE` / `TRANSIENT_NETWORK` / `EXECUTION_ERROR` / `TIMEOUT` / `EXTERNAL_WAIT` 這類 transient failure 必須先走 retry-first：第 1、2 次只准 nudge owner retry/resume，第 3 次同樣失敗才可 BLOCKED_ESCALATE
- **【新】** smart stale detection：外部 task 回傳 pending（RunningHub queue/pending）或 progress_at 仍在更新中 → 不觸發 STALE_PROGRESS / HEARTBEAT_DUE
- **【新】** GAP-1 step stall detection：current step 的 workflow sub-state 達到 terminal（DONE/COMPLETED/FAILED/BLOCKED）但 task 沒有推進到下一個 step，也沒有新 checkpoint → 馬上 NUDGE_MAIN_AGENT（第一個檢查），下一個 tick 直接 BLOCKED_ESCALATE（一發升高，不重複 nudge）；狀態必須持久化在 `monitoring.gap1_nudged_steps[]`，不可只靠 prompt 記憶；適用於完全無 external jobs 的 task
- **【新】** 5 分鐘 monitoring interval（從 10 分鐘改為 5 分鐘）
- **【新】** BLOCKED_ESCALATE notification 一次交代清楚：step / retry 次數 / 嘗試了什麼 / 為什麼失敗 / 建議下一步
- **【新】** 新 nudge delivery 架構：NUDGE_MAIN_AGENT / OWNER_RECONCILE 用 sessions_send 直接喚醒 owner agent（不走 Discord）；BLOCKED_ESCALATE / milestone 才發 Discord
- **【新】** Delivery push 先於狀態評估執行，確保 user update 不漏接
- demo E2E test 驗證 stale -> owner query -> owner reply -> resume / blocked / completed routing

## 6. Owner reply ingestion

repo 內現在提供：

```bash
python3 scripts/task_ledger.py --ledger state/long-task-ledger.json owner-reply <task_id> --reply <A|B|C|D|E> [...args]
```

自動分流規則：

- `A_IN_PROGRESS_FORGOT_LEDGER`
  - append checkpoint
  - `status=RUNNING`
  - 清掉 blocker
  - `owner_response_kind=A_IN_PROGRESS_FORGOT_LEDGER`
- `B_BLOCKED`
  - 要求 `--reason` + `--safe-next-step`
  - owner 直接寫 `BLOCKED` truth
  - 下一次 monitor 自動走 `BLOCKED_ESCALATE`
- `C_COMPLETED`
  - owner 直接寫 `COMPLETED` + validation/artifacts
  - 下一次 monitor 自動走 `STOP_AND_DELETE`
- `D_NO_REPLY`
  - 只記錄 `owner_response_kind=D_NO_REPLY`，保留 reconciliation 狀態
  - 明確要求先找外部 evidence，不可亂改 truth
- `E_FORGOT_OR_NOT_DOING`
  - append 一筆 **resume-required checkpoint**
  - `status=RUNNING`
  - `next_action` 強制改成恢復執行路徑（可自訂；未提供時用預設 resume wording）
  - 這是 repo 內可執行規則，不是文案

### 被動 Delivery Push（重要）

每次 monitor tick（5 分鐘）都必須主動檢查 ledger 中 `reporting.pending_updates[]` 是否有 `delivered=false` 的項目。若有，**monitor 會馬上用 `message.send` 推到 Discord**，不需要等 main agent 主動來問。 Delivery push 在**每次 tick 都先於狀態評估執行**，不受 notify flag 限制。

具體流程（每次 tick）：
1. 執行 `preview-tick`，解析 `pending_user_updates_deliverable_count`
2. 若 `> 0`，對每一筆 `delivered=false` 的更新：
   - 用 `message.send --channel <requester_channel> --message "<update['status_block']>"` 發到 Discord
   - 若發送成功，執行 `ack-delivery <task_id> <update_id> --delivered-via message.send` 更新 ledger `delivered=true`
3. 若 message.send 失敗但訊息已出現在 channel，視為 delivered=true，仍更新 ledger

這個機制補足了「你不來問，我就不報」的缺口。所有 `pending_user_update` 都會被 monitor 被動送達 Discord。

### 新 nudge delivery 架構（已實作）

**核心問題：** Monitor 原本發 NUDGE/RECONCILE 到 Discord，但 owner agent 在 main session 不會一直盯著 Discord；Discord 訊息也無法喚醒 owner agent；重複 Discord nudge 還會 spam user。

**新架構：**
- `NUDGE_MAIN_AGENT` / `OWNER_RECONCILE` → `sessions_send` 直接喚醒 owner agent（main session: `agent:main:discord:channel:1484432523781083197`），不发 Discord
- `BLOCKED_ESCALATE` / `COMPLETED` / milestone checkpoints → 才用 `message.send` 發 Discord 通知 user
- GAP-1 fix：同一個 step 已 NUDGE 過，next tick 直接 BLOCKED_ESCALATE，不再重複 NUDGE

**實際行為：**
1. Task stall detected → NUDGE_MAIN_AGENT → sessions_send 喚醒 owner agent
2. Next tick: same step still stalled → BLOCKED_ESCALATE → message.send 發 Discord（一次交代清楚 blocker）
3. Owner agent 收到 sessions_send 被喚醒 → 可以及時干預，不需要等 Discord 被動看到

### 尚未實作（部分已補足）

- ~~真實對 Discord / message bus 送提醒（目前由 cron agent 调用 message.send 處理）~~ → **已補足**：被動 delivery push 機制由 monitor cron 主動執行，不再依賴 main agent 主動觸發；NUDGE/RECONCILE 改用 sessions_send 直接喚醒 owner agent
- 真實安裝 / 刪除 crontab entry（透過 openclaw cron add/rm 處理）
- 多 owner routing / retry queue
- cross-process locking（retry_count 目前存在 ledger supervision metadata 中，跨 cron tick 持久化）

所以目前狀態是：**repo 內已可測 end-to-end wiring（含 sessions_send nudge / 被動 delivery push / GAP-1 one-shot escalate / retry-count tracking / smart stale / owner reply ingestion / auto-routing）；系統 cron deletion 仍保留為 integration layer。**
