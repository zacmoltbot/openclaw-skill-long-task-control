# long-task-control

`long-task-control` 已從單純 documentation skill，升級成 **semi-enforced task control system**。

這次重點不只是 monitor cron 可以抓問題，而是把它明確設計成 **任務續行提醒器 / execution nudge**：

- 當 task 還沒結束，但 main agent 停住了
- 當只有 heartbeat、沒有新 checkpoint
- 當 supervision 斷掉、任務狀態開始模糊

monitor cron 應主動提醒 main agent 繼續做，直到任務進入 terminal state：

- `COMPLETED`
- `FAILED`
- `BLOCKED`
- `ABANDONED`

其中 `BLOCKED` 不是繼續無限提醒，而是應升級成 blocker escalation，之後 cron 自刪。

## 這次升級後，repo 提供什麼？

### 已實作

- `references/task-ledger-spec.md`
  - 定義 ledger schema、status semantics、checkpoint vs heartbeat、monitor state machine
- `references/monitor-action-spec.md`
  - 定義 owner vs monitor 的 ledger ownership contract、action payload、何時 escalation / 刪 cron
- `references/multi-stage-runbook.md`
  - 定義 multi-stage task 與 monitor cron 的 operating model
- `references/failure-examples.md`
  - 列出 long-task-control 常見違規與 nudge anti-pattern
- `state/long-task-ledger.example.json`
  - 提供可直接餵給 scripts 的 example ledger
- `scripts/task_ledger.py`
  - 初始化 task、寫 checkpoint、標記 blocked、更新 heartbeat、ingest owner reply 並自動分流到 resume / blocked / completed
  - 新增 `ack-delivery`：把 user-visible reporting obligation 從 `reporting.pending_updates[]` 正式結案
- `scripts/checkpoint_timeout.py`
  - 掃描 timeout / stale progress / heartbeat due / missing activation
- `scripts/compliance_check.py`
  - 檢查 activation、task_id、vague progress、blocked silence、completion validation
- `scripts/monitor_nudge.py`
  - 用 deterministic rule engine 評估 execution-nudge state，決定提醒 / 升級 / 自刪；可選擇寫回 supervision metadata 與 action payload
- `scripts/task_ledger.py supervisor-update`
  - 提供 monitor 專用的 supervision-only 更新入口，不允許覆寫 task truth
- `scripts/demo_monitor_flow.py`
  - 用 temp ledger 跑可測的 E2E demo：stale -> OWNER_RECONCILE -> owner reply -> resume execution / BLOCKED_ESCALATE / STOP_AND_DELETE
- `scripts/monitor_cron.py`
  - pseudo cron wrapper：建立/列出/移除 monitor registration，並可執行單次 monitor tick；若遇到 `BLOCKED_ESCALATE` 或 `STOP_AND_DELETE` 會做 terminal cleanup
- `scripts/openclaw_ops.py`
  - OpenClaw-native glue：產生 activation block、`TASK START` block、初始化 ledger、建立/移除真實 `openclaw cron` monitor job、生成 cron agent prompt、預覽 nudge/reconcile/escalate 訊息
  - 新增 `bootstrap-task` one-shot entrypoint：把 activation -> task start -> ledger init -> monitor install 綁成單一預設 lifecycle，避免 main agent 靠記憶手動 bootstrap
  - 新增 `record-update` owner wrapper：把 execution truth 的 checkpoint/block/completed 與 ledger append 綁成同一條命令，避免 main agent 做了事卻忘了寫 ledger
  - 每次可見進度點都會回傳 `pending_user_update` + `ack_delivery_command`，強制 main agent 把 requester-visible delivery 補齊
- `references/openclaw-native-runbook.md`
  - 明確說明 bootstrap-task / activation -> ledger init -> OpenClaw cron monitor -> message.send nudge/reconcile/escalate -> terminal cleanup 的實際操作流程
- `references/prompt-contract.md`
  - 定義 agent 載入 skill 後的預設 prompt contract：典型長任務應直接走 bootstrap lifecycle，而不是事後靠人工補 bootstrap
- `scripts/openclaw_native_e2e.py`
  - 跑 OpenClaw-style E2E smoke test：真實建立 cron job、驗證 stale -> reconcile -> completed -> cron delete lifecycle
- `scripts/shampoo_sample_e2e.py`
  - 以『30s 洗髮精廣告』sample 跑完整 E2E：activation -> ledger init -> monitor cron install -> checkpoint -> stale/nudge -> owner reconcile -> completed -> cron cleanup
- `scripts/checkpoint_report.py`
  - 產出 user-visible status block

## Why this matters

沒有 ledger 的 long task 常見失敗：

- agent 說「還在跑」，但沒有 task handle
- 任務中途沉默，外界無法判斷是正常等待還是卡死
- 沒有 activation message，使用者根本不知道已切到 long-task mode
- 明明應標 `BLOCKED`，卻沒有 blocker payload
- 宣稱 `COMPLETED`，卻沒有 validation evidence
- cron 一直跑，但沒有真的提醒 main agent 回來把事做完

這版的目標就是讓上述問題可以被看見、被檢查、被提醒，並在 terminal state 自動收尾。

## System model

```text
user-visible reporting
  └─ checkpoint_report.py

machine-readable state
  └─ task ledger JSON
      ├─ task_ledger.py
      ├─ checkpoint_timeout.py
      ├─ compliance_check.py
      └─ monitor_nudge.py
```

## Monitor cron = execution nudge

monitor cron 的核心問題不是「有沒有 bug」，而是：

> 這個 task 還在真的往前嗎？還是只是沒有被正式結案、main agent 也沒再碰它？

所以 monitor cron 應優先做 **low-cost pre-gate**：

1. 讀 ledger
2. 檢查 `status`
3. 檢查 `last_checkpoint_at` / `last_progress_at`
4. 檢查 `last_heartbeat_at`
5. 套用簡單 rule engine
6. 只在必要時發 reminder / escalation

不要每次 cron 都叫大模型重新看一遍整個任務。

## State machine

monitor cron 應輸出以下狀態：

### `OK`
- heartbeat/progress 都新鮮
- 不需要提醒

### `HEARTBEAT_DUE`
- 太久沒 heartbeat
- 但還沒證明 task 卡住
- 只要輕量提醒 main agent 更新 supervision 即可

### `STALE_PROGRESS`
- 太久沒新 checkpoint
- 表示 task 可能停住或被遺忘
- 先標成 pre-gate warning

### `NUDGE_MAIN_AGENT`
- 第一次 execution nudge
- 要求 main agent 回來繼續做 / 補 checkpoint / 明確標 terminal status

### `OWNER_RECONCILE`
- stale progress 在 prior nudges 後仍持續
- 進入 stale -> query owner 流程
- owner 回覆後要立刻分流：
  - A 有在做只是忘了更新 ledger -> 補 ledger，維持 `RUNNING`
  - B 卡住 -> `BLOCKED_ESCALATE`
  - C 已完成 -> 補 validation 並寫 `COMPLETED`
  - D 沒回 -> 先找外部 evidence，不可憑空改 task truth
  - E owner 承認忘了做 / 沒在做 -> 不是只記錄，而是立刻進入「要求補做 / resume execution」路徑

### `BLOCKED_ESCALATE`
- task 已經 `BLOCKED`
- 或 task 的現況顯示沒有外部決策/輸入就無法續行
- 這時應送 blocker escalation，不是一直提醒「快做」

### `STOP_AND_DELETE`
- task 已 `COMPLETED`
- task 已 `FAILED`
- task 已 `ABANDONED`
- task 已 `BLOCKED` 且 escalation 已送出
- monitor cron 應停止並刪掉自己

## 什麼情況只是提醒？什麼情況算 blocked/failed？什麼情況自刪 cron？

### 只是提醒

適用：

- 沒新 checkpoint，但還沒有明確錯誤
- 太久沒 heartbeat
- main agent 看起來只是停住、忘了更新、或沒有把 task 關掉

這時輸出：

- `HEARTBEAT_DUE`
- `NUDGE_MAIN_AGENT`
- `OWNER_RECONCILE`

### 算 `BLOCKED`

適用：

- 缺 approval / credentials / upstream fix / user input / dependency
- 現在不能安全繼續
- blocker 可以具體描述

這時輸出：

- `BLOCKED_ESCALATE`

並在 escalation 後停止 cron。

### 算 `FAILED`

適用：

- 任務已明確失敗
- 沒有合理自動續行方案
- 應等待人工重設或重新規劃

低成本 monitor 不應自行「發明失敗」，而應提示 owner agent 將 task 明確標為 `FAILED`。一旦 ledger 寫成 `FAILED`，cron 進入 `STOP_AND_DELETE`。

### 自刪 cron

適用：

- `COMPLETED`
- `FAILED`
- `ABANDONED`
- `BLOCKED` escalation 已送出
- task record 已不存在或不再需要 supervision

## Cost-control principle

依 Edward 的 cost-control 原則，這個 monitor 必須是：

- deterministic
- cheap
- pre-gate
- 最少輸出
- 只在必要時才觸發更重的提醒或人工介入

也就是：

- **先看 timestamps / status / blocker payload**
- **不要盲目一直跑大模型**
- **不要對健康任務反覆產生 verbose summary**
- **不要讓 cron 在 terminal task 上空轉**

## OpenClaw-native quick start

### 1) 預設用 one-shot bootstrap 直接進 lifecycle

```bash
python3 scripts/openclaw_ops.py --ledger state/long-task-ledger.json bootstrap-task <task_id> \
  --goal "<one sentence goal>" \
  --requester-channel <discord-channel-id> \
  --workflow "Inspect inputs" \
  --workflow "Implement" \
  --workflow "Validate and handoff" \
  --next-action "Start checkpoint 1" \
  --message-ref "discord:msg:<activation-message-id>"
```

這個 entrypoint 會一次回傳：

- activation block
- `TASK START` block
- ledger init 結果
- monitor cron install 結果
- 後續 `record-update` 建議命令

### 2) 用 owner wrapper 持續寫 execution truth

```bash
python3 scripts/openclaw_ops.py --ledger state/long-task-ledger.json record-update STARTED <task_id> \
  --summary "Started checkpoint 1" \
  --current-checkpoint step-01 \
  --next-action "Run checkpoint 2" \
  --fact job_id=abc123
```

### 3) 預覽 monitor tick 會怎麼提醒

```bash
python3 scripts/openclaw_ops.py --ledger state/long-task-ledger.json preview-tick <task_id>
```

### 4) 跑 smoke tests

```bash
python3 scripts/generic_long_task_e2e.py
python3 scripts/openclaw_native_e2e.py
python3 scripts/shampoo_sample_e2e.py
```

更完整操作請看 `references/openclaw-native-runbook.md` 與 `references/prompt-contract.md`。

## Example commands

### Evaluate monitor state

```bash
python3 scripts/monitor_nudge.py \
  --ledger state/long-task-ledger.example.json
```

### Evaluate + write supervision metadata only

```bash
python3 scripts/monitor_nudge.py \
  --ledger state/long-task-ledger.example.json \
  --apply-supervision
```

### Only inspect active tasks

```bash
python3 scripts/monitor_nudge.py \
  --ledger state/long-task-ledger.example.json \
  --only-active
```

### Owner reply ingestion

```bash
python3 scripts/task_ledger.py \
  --ledger state/long-task-ledger.example.json \
  owner-reply repo-upgrade-20260411-a \
  --reply E \
  --summary "Owner admitted the task was forgotten; resume now" \
  --next-action "Resume execution immediately and post the next real checkpoint"
```

分流結果：

- `A` / `A_IN_PROGRESS_FORGOT_LEDGER` -> 補 checkpoint，維持 `RUNNING`
- `B` / `B_BLOCKED` -> 寫 `BLOCKED` truth，下一次 monitor 走 `BLOCKED_ESCALATE`
- `C` / `C_COMPLETED` -> 寫 `COMPLETED` + validation，下一次 monitor 走 `STOP_AND_DELETE`
- `D` / `D_NO_REPLY` -> 只記錄 owner 沒回，保留 `OWNER_RECONCILE`，要求先找外部 evidence
- `E` / `E_FORGOT_OR_NOT_DOING` -> **不是只記錄**；會立刻寫入 resume-required checkpoint，維持 `RUNNING`，把 `next_action` 改成恢復執行路徑

### Supervision-only metadata update

```bash
python3 scripts/task_ledger.py \
  --ledger state/long-task-ledger.example.json \
  supervisor-update repo-upgrade-20260411-a \
  --watchdog-state NUDGE_MAIN_AGENT \
  --monitoring last_action_state=NUDGE_MAIN_AGENT
```

### Timeout scan

```bash
python3 scripts/checkpoint_timeout.py \
  --ledger state/long-task-ledger.example.json
```

### Compliance scan

```bash
python3 scripts/compliance_check.py \
  --ledger state/long-task-ledger.example.json
```

這個 checker 現在也會抓 `missing_user_visible_update`：如果 owner 只更新 ledger、沒有把 `reporting.pending_updates[]` 真正送給 requester 並 `ack-delivery`，就會報錯。

### Demo test

```bash
python3 scripts/demo_monitor_flow.py
```

這個 demo 會建立 temp ledger，驗證：

- stale task 會先進入 `OWNER_RECONCILE`，並產生 owner-query payload
- `owner-reply --reply E` 會把「忘了做 / 沒在做」自動改成 **resume-required** 路徑，而不是只留 note
- `owner-reply --reply B` 會寫 `BLOCKED` truth，下一次 monitor 走 `BLOCKED_ESCALATE`
- `owner-reply --reply C` 會寫 `COMPLETED` + validation，下一次 monitor 走 `STOP_AND_DELETE`
- monitor 仍只會改 supervision metadata，不會偷偷改 task truth

### 30s 洗髮精廣告 sample E2E

```bash
python3 scripts/shampoo_sample_e2e.py
```

這個 sample 現在是 repo 內的唯一驗收路徑，會實測證明：

- activation message 已定稿並可輸出
- `task_ledger.py init` 真的建立 ledger task
- `monitor_cron.py install` 真的建立 monitor cron registration
- checkpoint 真的更新 `current_checkpoint` / `last_checkpoint_at`
- 第一次 stale 會進入 `NUDGE_MAIN_AGENT`
- 持續 stale 會進入 `OWNER_RECONCILE`
- owner reply `E` 會把任務拉回 resume-required path，而不是只記 note
- owner reply `C` + validation 會把任務收成 `COMPLETED`
- terminal monitor tick 會產生 `STOP_AND_DELETE`，並真的刪掉 cron registration

## Suggested cron pattern

```bash
*/10 * * * * cd /repo && python3 scripts/monitor_nudge.py --ledger state/long-task-ledger.json --only-active
*/30 * * * * cd /repo && python3 scripts/checkpoint_timeout.py --ledger state/long-task-ledger.json
*/30 * * * * cd /repo && python3 scripts/compliance_check.py --ledger state/long-task-ledger.json
```

建議順序：

1. `monitor_nudge.py` 做最便宜的 pre-gate
2. 若只是 `HEARTBEAT_DUE`，發最小提醒即可
3. 若進入 `NUDGE_MAIN_AGENT`，提醒 owner agent 繼續做、補 checkpoint、或明確寫成 `COMPLETED` / `FAILED` / `BLOCKED`
4. 若進入 `OWNER_RECONCILE`，立刻 query owner；根據 A/B/C/D/E 分流補 ledger、升級 blocked、寫 completed、找外部 evidence，或直接要求 resume execution
5. 若進入 `BLOCKED_ESCALATE`，送 blocker escalation，並把 cron 標成 `DELETE_REQUESTED`
6. 若進入 `STOP_AND_DELETE`，停用 cron / watcher
7. 只有需要 deeper audit 時，再跑其他 checker

## Files

```text
.
├── README.md
├── SKILL.md
├── references/
│   ├── failure-examples.md
│   ├── monitor-action-spec.md
│   ├── multi-stage-runbook.md
│   └── task-ledger-spec.md
├── scripts/
│   ├── checkpoint_report.py
│   ├── checkpoint_timeout.py
│   ├── compliance_check.py
│   ├── monitor_nudge.py
│   └── task_ledger.py
└── state/
    └── long-task-ledger.example.json
```

## Summary

這次升級後，`long-task-control` 不再只是「怎麼寫 progress update」的說明，而是：

- 有 **ledger** 可追蹤狀態
- 有 **monitor cron / execution nudge** 可提醒 main agent 繼續做
- 有明確的 **ledger ownership contract**：owner 寫 task truth，monitor 只寫 supervision metadata
- 有 **state machine** 可區分 reminder / stale / blocked / terminal
- 有 repo 內可跑的 **E2E demo wiring**：`stale -> OWNER_RECONCILE -> owner reply -> resume / BLOCKED_ESCALATE / STOP_AND_DELETE`
- 有 **low-cost pre-gate** 設計，避免盲目一直燒大模型
- 有 **self-delete rule**，任務終結就停掉 cron

如果你要把 agent 的長任務執行，從「口頭回報」升級成「可續行提醒、可稽核、可收尾」的 operational discipline，這個 repo 現在比較完整了。
