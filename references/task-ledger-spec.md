# Task Ledger Spec

這版重新把 long-task-control 拆成三層，避免 script 用半套資料自己腦補：

1. **Observed Truth**：只能由 owner / agent / external observation 回填
2. **Derived State**：只能由 deterministic script 從 observed truth 推導
3. **Control Actions**：monitor / owner / user-facing delivery 根據 derived state 採取動作

核心原則：**沒有先拿到真實觀察，就不准 script 先判 OK / pending / stalled。**

---

## 1. Task schema（v2）

```json
{
  "version": 2,
  "tasks": [
    {
      "task_id": "example-task",
      "goal": "...",
      "status": "RUNNING",
      "workflow": [
        {"id": "step-01", "title": "collect input", "state": "DONE"},
        {"id": "step-02", "title": "download result", "state": "RUNNING"}
      ],
      "current_checkpoint": "step-02",
      "observations": [],
      "observed": {
        "steps": {},
        "task_completion": null,
        "block": null,
        "owner": {},
        "external_jobs": {},
        "downloads": {}
      },
      "derived": {
        "workflow": [],
        "current_step": "step-02",
        "step_states": {},
        "pending_external": false,
        "truth_state": "CONSISTENT",
        "inconsistencies": []
      },
      "heartbeat": {},
      "monitoring": {},
      "reporting": {}
    }
  ]
}
```

---

## 2. Observed Truth（只能被 observation/owner 寫）

這些欄位是**真相層**，script 不可推測補完：

- `observations[]`
  - append-only observation log
- `observed.steps[step_id]`
  - `last_progress_at`
  - `last_progress_summary`
  - `progress_facts`
  - `completed_at`
  - `completion_summary`
  - `completion_facts`
  - `blocked_at` / `block_summary` / `block_facts`
  - `failed_at` / `failure_summary` / `failure_facts`
- `observed.task_completion`
  - task 真正完成的 observed truth
- `observed.block`
  - task 真正 blocked 的 observed truth
- `observed.owner`
  - `last_reply_kind`
  - `last_reply_at`
- `observed.external_jobs[provider:job_id]`
  - provider 真實回應
  - `status`
  - `observed_at`
  - `provider_evidence`
  - `summary`
  - `facts`
- `observed.downloads[download_id]`
  - 下載是否真的完成 / 完整 / 損壞

### Provider evidence contract

若 external job status 是：
- `SUBMITTED`
- `PENDING`
- `RUNNING`
- `RETRYING`
- `SWITCHED_WORKFLOW`

則必須至少有一個可驗證 evidence：
- `provider_job_id`
- `submission_receipt` / `submission_receipt_id`
- `provider_status_handle` / `status_handle`
- `status_url`
- `poll_token`
- `artifact_path` / `artifact_url` / `output_file`
- `provider_response_ref`

沒有 evidence 的 pending claim，不算 legitimate external wait。

---

## 3. Event semantics（不再混用 CHECKPOINT）

舊問題：`CHECKPOINT` 同時表示「有進展」與「step 做完」。

新規則：

- `STARTED`
  - task 初始化
- `STEP_PROGRESS`
  - 觀察到 step 有進展，但**不代表完成**
- `STEP_COMPLETED`
  - 明確觀察到 step 完成
- `TASK_COMPLETED`
  - 明確觀察到 task 完成
- `BLOCKED_CONFIRMED`
  - 明確觀察到 task/step blocked
- `OWNER_RESUMED`
  - owner 確認已恢復執行
- `OWNER_REPLY_RECORDED`
  - owner branch reply 被記錄
- `EXTERNAL_OBSERVED`
  - provider / external system 真實狀態回填
- `DOWNLOAD_OBSERVED`
  - download completeness truth 回填
- `HEARTBEAT`
  - supervision heartbeat，不代表 progress

### 最重要的語意約束

- **workflow step DONE 只能由 `STEP_COMPLETED` 驅動**
- `STEP_PROGRESS` 只會把 step 視為 `RUNNING`
- `TASK_COMPLETED` 只能在 completion truth 存在時成立
- `BLOCKED_CONFIRMED` 只能由 observed blocker truth 驅動

---

## 4. Derived State（只准 script 推導）

`derived.*` 是 deterministic projection，不是 owner 主觀填寫：

- `derived.workflow`
- `derived.current_step`
- `derived.step_states`
- `derived.pending_external`
- `derived.suspicious_external_jobs`
- `derived.external_failures`
- `derived.truth_state`
  - `CONSISTENT`
  - `INCONSISTENT`
- `derived.inconsistencies[]`
- `derived.last_observed_progress_at`
- `derived.status`

### Consistency examples

以下都要先判 `INCONSISTENT`，不能先掉到 `OK`：

- task completion truth 存在，但 workflow 還有未完成 step
- task completion truth 存在，但 external job 仍 legitimate pending
- external job 標成 pending，但缺 provider evidence
- download observed 為 incomplete/corrupt
- step completion 之後又出現更晚的 progress truth

---

## 5. Control Actions

### Monitor 負責

- 讀 observed truth + derived state
- 若 truth inconsistent → `TRUTH_INCONSISTENT` / `OWNER_RECONCILE`
- 若 stale 但 truth consistent → `NUDGE_MAIN_AGENT`
- 若 blocked truth 已確認且 retry-first contract 已滿足 → `BLOCKED_ESCALATE`
- terminal → `STOP_AND_DELETE`

### Owner / executor 負責

- 真的去觀察外部世界
- 回填 observed truth
- 寫 `STEP_PROGRESS` / `STEP_COMPLETED` / `TASK_COMPLETED` / `BLOCKED_CONFIRMED`
- 在收到 resume/reconcile request 後，**先補 observed truth，再讓 script 投影 derived state**

### User-facing delivery 負責

- 將 `reporting.pending_updates[]` 送到 requester channel
- `STEP_COMPLETED` / external completion/failure / blocked escalate / completed handoff 才是 user-visible update obligation

---

## 6. Owner-resume contract

monitor 用 `resume_requests[]` 要求的是：
- 請 owner 回來**觀察**
- 補 observed truth
- 必要時恢復 execution

不是要求 owner 直接亂寫狀態。

`monitoring.resume_requests[]` entry 至少包含：
- `resume_token`
- `request_kind`
- `current_step`
- `reason`
- `next_action`
- `requested_at`
- `delivery`
- `acknowledged_at`
- `resume_outcome`
- `acknowledged_checkpoint`

---

## 7. Retry-first contract

retry-first 必須建立在**observed failure truth** 上，而不是 monitor 猜：

可接受 failure type 例子：
- `DOWNLOAD_TIMEOUT`
- `DOWNLOAD_INCOMPLETE`
- `TRANSIENT_NETWORK`
- `EXECUTION_ERROR`
- `EXTERNAL_WAIT`

來源只能是：
- `BLOCKED_CONFIRMED.facts.failure_type`
- `EXTERNAL_OBSERVED.facts.failure_type`
- `DOWNLOAD_OBSERVED.facts.failure_type`

monitor 只讀 `monitoring.retry_count` 做 deterministic escalation policy，不負責 invent failure type。

---

## 8. CLI mapping

- `task_ledger.py checkpoint --event-type STEP_PROGRESS`
- `task_ledger.py checkpoint --event-type STEP_COMPLETED`
- `task_ledger.py checkpoint --event-type TASK_COMPLETED`
- `task_ledger.py block`
- `task_ledger.py external-job`
- `task_ledger.py download-observed`
- `task_ledger.py owner-reply`
- `task_ledger.py supervisor-update`

`supervisor-update` 仍然只能寫 supervision metadata，不能碰 truth。
