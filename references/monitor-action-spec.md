# Monitor Action Spec

這版 monitor contract 的重點只有一句：

> **monitor 是 supervisor，不是 truth author。**

它只能：
- 讀 observed truth
- 推 derived state
- 發 control action

它不能：
- 猜 step 已完成
- 猜 external wait 合法
- 猜 failure type
- 用半套 truth 判 OK

---

## 1. Ownership boundary

### Owner / executor 可寫

- `observations[]`
- `observed.*`
- `status`
- `workflow`
- `current_checkpoint`
- `checkpoints[]`
- `last_checkpoint_at`
- `blocker`
- `validation`
- `artifacts`
- `next_action`
- `notes`
- `reporting.*`

### Monitor 可寫

只限 supervision metadata：
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
- `monitoring.owner_query_at`
- `monitoring.owner_response_at`
- `monitoring.owner_response_kind`
- `monitoring.reconcile_count`
- `monitoring.last_reconcile_at`
- `monitoring.last_resume_request_at`
- `monitoring.retry_count`
- `monitoring.resume_requests[]`

---

## 2. Monitor precedence

### P0: terminal cleanup
- `COMPLETED | FAILED | ABANDONED` → `STOP_AND_DELETE`
- `BLOCKED` 且 escalation 已送出 → `STOP_AND_DELETE`

### P1: truth consistency gate
先檢查：
- `derived.truth_state == INCONSISTENT`
- `derived.inconsistencies[]` 非空
- `derived.suspicious_external_jobs[]` 非空

若成立：
- `TRUTH_INCONSISTENT` 或 `OWNER_RECONCILE`
- **不可**先掉到 `OK`
- **不可**說「agent is working / external pending」

### P2: observed blocked truth
- 若 `status=BLOCKED`
- 且 blocker truth 已存在
- 再看 observed failure type 是否屬於 retry-first

### P3: stale / heartbeat
只在 truth consistent 且沒有 legitimate pending external 時處理：
- `NUDGE_MAIN_AGENT`
- `STALE_PROGRESS`
- `HEARTBEAT_DUE`

### P4: OK
只有當：
- truth consistent
- 無 suspicious external claim
- 無 blocked truth needing escalation
- progress/heartbeat 在門檻內，或 legitimate external wait 成立

才能 `OK`

---

## 3. State semantics

### `OK`
只代表：
- observed truth 足夠
- derived state 一致
- 目前不需要 supervisor action

### `TRUTH_INCONSISTENT`
代表：
- observation 不完整或矛盾
- monitor 必須要求 owner/executor 先 reconcile truth
- 不准靠 script 猜成 pending/OK

### `OWNER_RECONCILE`
代表：
- owner 必須回來觀察與補 truth
- 尤其是 external evidence / owner branch / resume truth

### `NUDGE_MAIN_AGENT`
代表：
- stale 但 truth consistent
- 要求 owner resume execution 並回填 observed truth

### `BLOCKED_ESCALATE`
代表：
- blocked truth 已確認
- retry-first contract 已滿足，或屬於非 transient blocker
- 該對 requester 做 blocker escalation

### `STOP_AND_DELETE`
代表：
- monitor 任務完成，刪 cron

---

## 4. Owner-resume contract

monitor 發 `sessions_send` 的含義是：

1. 回來觀察真實狀況
2. 補 observed truth
3. 必要時 resume execution
4. 再讓 deterministic script 投影 derived state

**不是**叫 owner 直接改 `derived` / `watchdog` / 半成品 status。

### Branch handling

- `A_IN_PROGRESS_FORGOT_LEDGER`
  - owner 回填實際 progress truth
  - 可寫 `OWNER_RESUMED` + `STEP_PROGRESS`
- `B_BLOCKED`
  - owner 確認 blocker truth
  - 寫 `BLOCKED_CONFIRMED`
- `C_COMPLETED`
  - owner 確認完成 truth
  - 寫 `TASK_COMPLETED`
- `D_NO_REPLY`
  - 不改 task truth
  - 要求去找 external observation
- `E_FORGOT_OR_NOT_DOING`
  - owner 明確 resumed
  - 寫 `OWNER_RESUMED`，然後補 progress truth

---

## 5. Retry-first contract

monitor 不得從模糊 stall 直接猜：
- `DOWNLOAD_TIMEOUT`
- `DOWNLOAD_INCOMPLETE`
- `TRANSIENT_NETWORK`

failure type 必須先由 observed truth 回填。

然後 script 才能依 `monitoring.retry_count` 做：
- retry 1
- retry 2
- escalate

這個切分點是這次 redesign 的核心。

---

## 6. Delivery wiring

- `NUDGE_MAIN_AGENT` / `OWNER_RECONCILE` / `TRUTH_INCONSISTENT`
  - `sessions_send` 給 owner
- `BLOCKED_ESCALATE`
  - `message.send` 給 requester
- `STEP_COMPLETED` / external completion/failure / workflow switch / completed handoff
  - 走 `reporting.pending_updates[]` + passive delivery push

---

## 7. Why this is a redesign, not patch

這次不是多補一條 if/else，而是整個 decision architecture 改成：

- 先有 observed truth model
- 再有 derived projection
- 最後才有 control action

所以 monitor 不再直接讀混雜欄位做猜測，也不再把 progress/completion/event semantics 混在一起。
