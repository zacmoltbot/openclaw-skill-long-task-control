# long-task-control

`long-task-control` 已從單純 documentation skill，升級成 **semi-enforced task control system**。

它現在不只提供回報格式建議，還補上三個核心能力：

1. **Task ledger**：讓長任務有 durable state，可追蹤 `task_id`、checkpoint、heartbeat、blocker、validation
2. **Checkpoint timeout + watchdog**：讓「太久沒進展 / 沒回報」可以被提醒、標記、掃描
3. **Compliance checker**：把常見違規模式變成可檢查規則，而不只是口頭規範

重點不是把 agent 變成重型 workflow engine，而是用最小成本把 long task 從「口頭說有在做」升級成「可觀察、可稽核、可提醒」。

## 這次升級後，repo 提供什麼？

### 已實作

- `references/task-ledger-spec.md`
  - 定義 ledger schema、status semantics、checkpoint vs heartbeat、watchdog states
- `state/long-task-ledger.example.json`
  - 提供可直接餵給 scripts 的 example ledger
- `scripts/task_ledger.py`
  - 初始化 task、寫 checkpoint、標記 blocked、更新 heartbeat
- `scripts/checkpoint_timeout.py`
  - 掃描 timeout / stale progress / heartbeat due / missing activation
- `scripts/compliance_check.py`
  - 檢查 activation、task_id、vague progress、blocked silence、completion validation
- `scripts/checkpoint_report.py`
  - 仍保留作為 user-visible status block generator

### 設計規格 / operating model

- semi-enforced enforcement model
- watchdog / heartbeat / cron 的分工
- timeout default 建議值
- blocker escalation 與 stale-progress interpretation

## Why this matters

沒有 ledger 的 long task 常見失敗：

- agent 說「還在跑」，但沒有 task handle
- 任務中途沉默，外界無法判斷是正常等待還是卡死
- 沒有 activation message，使用者根本不知道已切到 long-task mode
- 明明應該標 `BLOCKED`，卻沒有 blocker payload
- 宣稱 `COMPLETED`，卻沒有 validation evidence

這版的目標就是讓上述問題可以被看見、被檢查、被提醒。

## System model

```text
user-visible reporting
  └─ checkpoint_report.py

machine-readable state
  └─ task ledger JSON
      ├─ task_ledger.py
      ├─ checkpoint_timeout.py
      └─ compliance_check.py
```

### Reporting layer

給使用者看的仍然是：

- `ACTIVATED`
- `TASK START`
- `CHECKPOINT`
- `BLOCKED`
- `COMPLETED`

### State layer

給 agent / maintainer / cron / QA 掃描的是 ledger：

- `task_id`
- `status`
- `workflow`
- `checkpoints[]`
- `last_checkpoint_at`
- `heartbeat.last_progress_at`
- `heartbeat.last_heartbeat_at`
- `activation.announced`
- `blocker`
- `validation`

## Task ledger concept

Task ledger 是這次升級的核心。

它把 long-task-control 從「訊息格式標準」提升成「有狀態、能掃描、可提醒」的系統。

### Recommended state file

```text
state/long-task-ledger.json
```

本 repo 附的是：

```text
state/long-task-ledger.example.json
```

### Minimal required fields

每個 task 至少應有：

- `task_id`
- `goal`
- `status`
- `activation.announced`
- `workflow[]`
- `current_checkpoint`
- `last_checkpoint_at`
- `heartbeat.timeout_sec`
- `next_action`

完整 schema 見：

- `references/task-ledger-spec.md`

## Timeout / watchdog design

這版把 timeout 拆成兩種時鐘：

### 1) heartbeat clock
表示 agent 是否還有持續看管這個 task。

欄位：

- `heartbeat.last_heartbeat_at`
- `heartbeat.expected_interval_sec`

如果超過 `expected_interval_sec`，watchdog 可標示：

- `HEARTBEAT_DUE`

### 2) progress clock
表示 task 是否真的有新進展。

欄位：

- `last_checkpoint_at`
- `heartbeat.last_progress_at`
- `heartbeat.timeout_sec`

如果只有 heartbeat、太久沒有新 checkpoint，就算 agent 還活著，也應標：

- `STALE_PROGRESS`

### 其他 watchdog states

- `MISSING_ACTIVATION`
- `BLOCKED_SILENT`
- `COMPLETED_NO_VALIDATION`
- `OK`

## Compliance checks currently covered

目前 checker 至少會抓以下幾類：

1. **沒有 activation message**
   - active task 但 `activation.announced=false`
2. **沒有 task id 卻說在做**
   - active task 缺 `task_id`
3. **太久沒 checkpoint**
   - 由 timeout detector 根據 `timeout_sec` 判定
4. **模糊進度語句**
   - 例如 `still working` / `還在跑` / `快好了`，但沒有 facts
5. **應 BLOCKED 卻沉默**
   - `status=BLOCKED` 但沒有 blocker payload

這些規則目前是 **semi-enforced**：

- 可在 cron / QA / PR review 中自動掃描
- 可設 `--fail-on` / `--fail-on-severity` 讓 CI 非零退出
- 但不直接攔截 agent runtime

## Files

```text
.
├── README.md
├── SKILL.md
├── references/
│   ├── failure-examples.md
│   ├── multi-stage-runbook.md
│   └── task-ledger-spec.md
├── scripts/
│   ├── checkpoint_report.py
│   ├── checkpoint_timeout.py
│   ├── compliance_check.py
│   └── task_ledger.py
└── state/
    └── long-task-ledger.example.json
```

## Quick start

### 1) Initialize a task in the ledger

```bash
python3 scripts/task_ledger.py \
  --ledger state/long-task-ledger.example.json \
  init repo-upgrade-20260411-a \
  --goal "Upgrade repo to semi-enforced long-task control" \
  --channel discord \
  --owner coding_agent \
  --activation-announced \
  --message-ref discord:msg:123456 \
  --workflow "Inspect repo/workdir/status" \
  --workflow "Implement ledger + scripts" \
  --workflow "Update README and SKILL" \
  --next-action "Start repo inspection"
```

### 2) Append a checkpoint

```bash
python3 scripts/task_ledger.py \
  --ledger state/long-task-ledger.example.json \
  checkpoint repo-upgrade-20260411-a \
  --summary "Repo inspection completed" \
  --status RUNNING \
  --current-checkpoint step-02 \
  --fact branch=main \
  --fact clean_status=true \
  --next-action "Implement ledger helper scripts"
```

### 3) Mark blocked

```bash
python3 scripts/task_ledger.py \
  --ledger state/long-task-ledger.example.json \
  block render-20260411-b \
  --reason "seg03 upstream job failed" \
  --need "Decide whether to rerun seg03" \
  --safe-next-step "Rerun seg03 then resume stitch" \
  --current-checkpoint collect \
  --next-action "Wait for rerun decision"
```

### 4) Heartbeat without fake progress

```bash
python3 scripts/task_ledger.py \
  --ledger state/long-task-ledger.example.json \
  heartbeat repo-upgrade-20260411-a \
  --watchdog-state OK \
  --note "Still under active supervision; no new checkpoint yet"
```

### 5) Run timeout detector

```bash
python3 scripts/checkpoint_timeout.py \
  --ledger state/long-task-ledger.example.json
```

Fail CI/cron when stale progress exists:

```bash
python3 scripts/checkpoint_timeout.py \
  --ledger state/long-task-ledger.example.json \
  --fail-on STALE_PROGRESS \
  --fail-on MISSING_ACTIVATION
```

### 6) Run compliance checker

```bash
python3 scripts/compliance_check.py \
  --ledger state/long-task-ledger.example.json
```

Fail on warnings or errors:

```bash
python3 scripts/compliance_check.py \
  --ledger state/long-task-ledger.example.json \
  --fail-on-severity warn
```

## Heartbeat / cron operating pattern

### In-session heartbeat

適合 agent 自己持續看管：

- 每 5-15 分鐘掃 active tasks
- 若仍在等待但沒有新事實，可只更新 heartbeat
- 若 checkpoint timeout，立即標記 watchdog state 並提醒

### External cron

適合獨立 scheduler / CI / host cron：

```bash
*/15 * * * * cd /repo && python3 scripts/checkpoint_timeout.py --ledger state/long-task-ledger.example.json
*/15 * * * * cd /repo && python3 scripts/compliance_check.py --ledger state/long-task-ledger.example.json
```

## Relationship to SKILL.md

- `SKILL.md`：給 agent 的 operational contract
- `README.md`：給 repo 訪客、maintainer、reviewer 看的首頁說明
- `references/*`：給 implementer 的 deeper design / failure cases
- `scripts/*`：把規格最小化落地成可執行工具

## Scope boundary

這個 repo 仍然不是完整 workflow orchestrator。

它**有做**：

- task tracking
- checkpoint/status normalization
- timeout/watchdog detection
- compliance scanning
- example state + helper scripts

它**沒有做**：

- distributed job queue
- locking / concurrency control
- event bus
- auto-remediation engine
- provider-specific integrations

這是刻意的：先讓 long tasks 有最低限度的可治理性，再決定是否往更重的 orchestration 演進。

## Summary

這次升級後，`long-task-control` 不再只是「怎麼寫 progress update 比較好」的說明文件，而是：

- 有 **ledger** 可追蹤狀態
- 有 **watchdog/timeout model** 可抓沉默與停滯
- 有 **compliance checker** 可抓常見違規
- 有 **scripts + example state** 可直接落地

如果你想把 agent 的長任務執行，從「口頭回報」升級成「semi-enforced operational discipline」，這個 repo 現在已經可以作為最小可行基底。
