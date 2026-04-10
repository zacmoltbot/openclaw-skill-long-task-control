# long-task-control

`long-task-control` 是一個給長時間、非同步、或多階段任務用的 AgentSkill。

它的目的不是幫你「加速任務」，而是幫你把任務**管清楚**：開始時先明確宣告已啟用 `long-task-control`、定義 checkpoint，執行中只回報可驗證事實，遇到卡住時立即升級，完成時附上驗證與交付資訊。這樣使用者不會只看到模糊的「還在跑」，而是能一眼知道現在做到哪、憑什麼這樣判斷、下一步是什麼。

## 它解決什麼問題？

長任務常見痛點：

- 任務跑很久，但更新只有一句「still working」
- 中途有多個階段，卻沒有一致的進度格式
- 任務被外部系統卡住時，沒有及時回報 blocker
- 已經產出結果，但沒有做驗證就宣稱完成
- 多人交接時，看不懂現在到底進行到哪裡

這個 skill 的做法是把任務拆成可觀察的 checkpoint，並要求每次更新都附上**可驗證事實**。

## 核心設計原則

1. **先判斷 task 特徵，再決定是否啟用 skill**  
   不靠列舉任務類型；而是看這個任務是否長、會等待、會分段、會產生中間產物、會卡住、或需要可稽核的進度控制。

2. **一旦啟用，就先宣告 skill 已啟動**  
   第一個 user-visible message 必須清楚告知：目前這個 task 採用 `long-task-control` SKILL 執行，後續會用 checkpoint / blocker / completed 的方式回報。

3. **先建 task record，再開始做事**  
   一開始就定義 `task_id`、目標、workflow、預期產物。

4. **用 checkpoint 管流程，不用流水帳敘事**  
   任務進度以明確階段表示，而不是大量思考過程或模糊描述。

5. **只報可驗證事實（verifiable facts）**  
   例如 job id、PID、檔案路徑、檔案大小、exit code、timestamp、remote status。

6. **被卡住就明講，不要靜默等待**  
   缺權限、遠端失敗、輸出不存在、驗證失敗，都應立即用 `BLOCKED` 回報。

7. **完成前先驗證，完成時可交接**  
   `COMPLETED` 必須附上輸出位置、驗證結果、還有沒有背景工作尚未收尾。

## 運作流程

這個 skill 把更新狀態標準化成 4 種：

### 1) `STARTED`
任務開始時建立追蹤紀錄。

用途：
- 宣告任務目標
- 列出 workflow / checkpoints
- 說明預期輸出
- 明確指出第一個 action

### 2) `CHECKPOINT`
當有**可觀察狀態變化**時回報。

典型例子：
- request 已送出
- 收到 remote job id
- segment 2 render 完成
- 輸出檔下載完成
- stitch process 已啟動
- 驗證通過

不是每隔幾分鐘就發一次，而是**有新事實才更新**。

### 3) `BLOCKED`
只要被卡住，就立刻報告：

- 卡在哪個 checkpoint
- 具體 blocker 是什麼
- 已經試過什麼
- 現在需要什麼決策 / 權限 / 輸入
- 一旦解鎖後的安全下一步是什麼

### 4) `COMPLETED`
完成時不是只說「好了」，而是交付：

- 完成了哪些 checkpoints
- 產物在哪裡
- 做了哪些驗證
- 是否還有 background item 仍在跑
- 請求方現在可以如何使用 / 檢查成果

## 什麼叫「可驗證事實」？

可驗證事實是指**此刻可以被觀察、重跑、或查證的資訊**，例如：

- `remote_job_id=rh_123456`
- `pid=8421`
- `output_file=/tmp/final.mp4`
- `size_bytes=104857600`
- `submitted_at=2026-04-10T23:40:00+08:00`
- `latest_status=running`
- `exit_code=0`

不算可驗證事實的說法：

- 「應該快好了」
- 「看起來差不多」
- 「我覺得快完成了」
- 未標示來源的 ETA

這個 skill 的重點就是：**狀態更新要建立在證據上，不建立在猜測上。**

## Long-task detection checklist

判斷是否要套用這個 skill，請看**任務特徵**，不要只看任務名目。

只要出現以下一項以上，就應高度考慮啟用：

- 任務不太可能在單一可見回合內乾淨結束
- 過程需要等待、polling、retry、sleep、background execution
- 任務有多個相依階段，後一步依賴前一步輸出
- 過程會先產生 intermediate artifacts，再產出 final deliverable
- 任務依賴 remote / queued / async / externally managed job system
- 任務容易卡在 approval、credentials、upstream failure、missing output、validation failure
- 請求方之後需要依據 job id、PID、檔案路徑、URL、timestamp、exit code 來追蹤或交接

一句話：**不是因為它叫做影片任務、RunningHub 任務、轉檔任務才啟用；而是因為它的執行型態具有 long / staged / async / blocker-prone 特徵。**

## Mandatory activation message

一旦決定啟用 skill，第一個 user-visible message 必須先發這段標準訊息（可在後面補一行 task-specific 補充）：

```text
ACTIVATED
- skill: long-task-control
- announcement: 目前這個 task 會採用 long-task-control SKILL 執行
- reporting: 我會用 checkpoint / blocker / completed 這類可驗證狀態回報進度；有新事實才更新，不用模糊的「還在跑」敘述
- next: 接著建立 task record，開始第一個可驗證步驟
```

這段訊息的重點不是形式，而是**先告知使用者執行模式已切換**，避免使用者不知道後面為什麼會看到結構化 checkpoint 更新。

## 明確違規例子（failure examples / anti-patterns）

這個 repo 現在也把「**哪些做法算違規**」明確寫出來，不只講原則，也提供 **錯誤示範 vs 正確示範**。

目前覆蓋的典型失敗模式包含：

- 沒有先發 activation message 就直接開始做
- 沒有 `task_id` 卻宣稱任務已在進行
- 任務做到一半失聯，不再回報可驗證狀態
- 把計畫、打算、下一步，誤報成已經發生的進度
- 把舊任務的證據混進新題材 / 新 deliverable
- 明明沒有 blocker、甚至已有新進度，卻仍然沉默

這些例子收錄在 `references/failure-examples.md`，目的不是增加格式負擔，而是讓 skill 使用者能快速對照：**什麼叫不合規、什麼才是可交接的正確回報**。

## 適用場景

### RunningHub / 其他 remote async job
例如：送出生成工作、拿到 job id、輪詢狀態、下載產物、驗證結果。

### 長影片 / 多段生成
例如：長影片拆成 `seg01`、`seg02`、`seg03` 逐段生成，再做 stitch / merge / transcode。

### 多階段任務
例如：
- upload → process → download → validate
- prepare inputs → run batch → collect outputs → package
- long local process + remote dependency 的 hybrid workflow

一句話：**只要任務會跑很久、會等待、會分段、會交接，而且這些特徵已經明顯可見，這個 skill 就適合。**

## 最小使用範例

### 1. 任務開始

```text
TASK START
- task_id: video-20260410-a
- goal: 產出可交付的最終影片
- workflow:
  1. submit render jobs
  2. wait for segment outputs
  3. stitch final video
  4. validate final file
- expected artifacts:
  - /tmp/video-20260410-a/final.mp4
- first action: submit seg01 render request
```

### 2. 中途 checkpoint

```text
CHECKPOINT
- task_id: video-20260410-a
- checkpoint: 2/4 segment renders
- state: running
- verified facts:
  - seg01_job_id=rh_001
  - seg01_file=/tmp/video-20260410-a/seg01.mp4
  - seg02_job_id=rh_002
  - seg02_status=running
- outputs:
  - /tmp/video-20260410-a/seg01.mp4
- next: poll seg02 status
```

### 3. 被卡住

```text
BLOCKED
- task_id: video-20260410-a
- checkpoint: stitch final video
- blocker: seg03 output file missing
- verified facts:
  - seg03_job_id=rh_003
  - provider_status=failed
- tried:
  - retried poll once after 5 minutes
- need:
  - decide whether to rerun seg03
- safe next step: rerun seg03 and resume stitch after output exists
```

### 4. 完成交付

```text
COMPLETED
- task_id: video-20260410-a
- goal: final stitched video delivered
- completed checkpoints:
  - submit render jobs
  - collect all segment outputs
  - stitch final video
  - validate final file
- output artifacts:
  - /tmp/video-20260410-a/final.mp4
- validation:
  - ffprobe read duration successfully
  - file size > 0
- background items still running:
  - none
- handoff: final.mp4 is ready for review
```

## Repo 內檔案結構

```text
.
├── README.md
├── SKILL.md
├── references/
│   ├── failure-examples.md
│   └── multi-stage-runbook.md
└── scripts/
    └── checkpoint_report.py
```

- `SKILL.md`：skill 主說明與固定格式
- `references/multi-stage-runbook.md`：更完整的多階段任務 SOP
- `references/failure-examples.md`：常見違規例子與「錯誤示範 vs 正確示範」
- `scripts/checkpoint_report.py`：用 CLI 快速產生一致格式的狀態區塊

## 腳本範例

```bash
python3 scripts/checkpoint_report.py CHECKPOINT video-20260410-a \
  --checkpoint "2/4 segment renders" \
  --fact seg01_job_id=rh_001 \
  --fact seg02_status=running \
  --output /tmp/video-20260410-a/seg01.mp4 \
  --next "poll seg02 status"
```

## 總結

這個 repo 提供的不是某個特定平台的 API wrapper，而是一套**長任務控制與進度回報規格**：

- 開始時先定義流程
- 中途只回報可驗證的進度變化
- 卡住時立即升級
- 完成前先驗證
- 完成後提供可交接的 handoff

如果你希望代理在 RunningHub、長影片生成、render queue、轉檔、下載/上傳、或任何 multi-stage workflow 中表現得更穩、更透明、更好交接，這個 skill 就是用來做這件事的。
