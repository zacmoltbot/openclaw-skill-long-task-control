---
name: long-task-control
description: Take ownership of a long-running or multi-stage task as one durable execution workflow: decompose it, execute what is concretely executable, persist evidence, resume after interruption, and reconcile the user-facing result from actual outputs. Use when work spans multiple turns or async waits and must survive silence without relying on memory-only status. Explicit trigger patterns: (1) any batch job with 3 or more sequential API calls or exec commands, (2) multi-step image/video generation across multiple prompts or apps, (3) any task that risks partial completion and needs durable progress tracking. If outputs exist even though execution/cleanup was interrupted, prefer success/partial-success reconciliation over surfacing raw control-plane blocker noise first.
---

# Long Task Control

Treat this skill as one user-facing product behavior:

- accept the long task
- bootstrap durable state
- start execution immediately
- keep reporting grounded in observed truth
- persist outputs/evidence on disk
- resume from disk
- reconcile the user-facing result from actual deliverables
- mention interruption/cleanup issues honestly, but do not lead with them when useful outputs already exist

Do not expose internal architecture unless debugging requires it.

## Canonical path

```bash
python3 scripts/openclaw_ops.py --ledger state/long-task-ledger.json bootstrap-task <task_id> \
  --goal "<one sentence goal>" \
  --requester-channel <channel> \
  --workflow "Step 1 title :: shell=<command> :: expect=<artifact>" \
  --workflow "Step 2 title :: shell=<command> :: expect=<artifact>" \
  --workflow "Step 3 title :: shell=<command> :: expect=<artifact>" \
  --next-action "Start step-01"
```

`bootstrap-task` is the single entrypoint. It:
- creates the ledger task
- installs monitor supervision
- derives the execution job
- starts the owned runner loop and keeps driving it until terminal completion or an honest blocked state

If a retry/correction needs a clean slate, start a fresh run:

```bash
python3 scripts/openclaw_ops.py --ledger state/long-task-ledger.json rerun-task <task_id> \
  --reason "<what changed>" \
  --summary "fresh run after correction" \
  --current-checkpoint step-01 \
  --next-action "Resume execution"
```

## User-facing state model

Keep two layers separate:

1. **control-plane state**
   - `RUNNING`, `BLOCKED`, `COMPLETED`, etc.
2. **user-facing outcome state** under `derived.user_facing`
   - `IN_PROGRESS`
   - `SUCCESS`
   - `PARTIAL_SUCCESS`
   - `BLOCKED`
   - `FAILED`

Rule: when useful outputs already exist, reconcile/report that outcome first. Do not surface raw interruption or cleanup noise as the main story unless there is no meaningful result yet.

## Generic executor contract

The default adapter is `generic_manual`.

What it can do now:
- execute `shell=...` steps
- verify expected artifacts via `expect=` / `artifacts=`
- persist item/job state
- bridge step/task truth into the ledger automatically

What it must not do:
- invent automation for vague steps
- auto-complete unsupported work
- report success with no evidence

If a step has no executable semantics, the generic path must block honestly.

## Step syntax for the generic path

Each workflow entry can stay plain text, or embed a concrete execution contract using `:: key=value` parts.

Supported keys in the first redesign slice:
- `shell=<command>`
- `expect=<path>`
- `artifacts=<path1>|<path2>`
- `cwd=<dir>`
- `timeout=<sec>`
- `next_action=<text>`
- `batch_result=<path-to-json-summary>`
- `batch_min_success=<n>`
- `auto_action=deliver_artifacts`
- `deliver_artifacts=<path1>|<path2>`
- `deliver_caption=<user-facing delivery text>`

Example:

```bash
--workflow "Build draft :: shell=python3 build.py --out /tmp/draft.txt :: expect=/tmp/draft.txt"
--workflow "Deliver review pack :: auto_action=deliver_artifacts :: deliver_artifacts=/tmp/draft.txt :: deliver_caption=Here is the generated draft for review"
```

If no `shell=` is present, the step is usually treated as a human gate and will block unless a specialized adapter owns it.

Exception: config-like Discord target normalize / repair / self-heal steps are auto-promoted into a deterministic local repair path (`auto_action=repair_requester_channel`) so they execute directly instead of surfacing fake `BLOCKED` / `OWNER_ACTION_REQUIRED` escalations.

## Monitor role

The monitor is supervision only. It exists to:
- ensure progress is still happening
- flush pending updates
- request reconciliation when outputs/evidence and control-plane state diverge
- escalate blockers when there is no better user-correct explanation
- stop/delete itself on terminal tasks

If outputs exist but execution was interrupted, monitor should prefer reconciliation before blocker escalation.

## Truth rules

- Only observed execution results, artifacts, owner observations, or external evidence count as truth.
- `STEP_PROGRESS` is not `STEP_COMPLETED`.
- `TASK_COMPLETED` requires completion evidence.
- `BLOCKED` does not automatically mean the user got nothing.
- Monitor never fabricates task truth.

## Main references

Read these when needed:
- `references/redesign-spec.md` — user-facing state model and outcome-first reconciliation
- `references/openclaw-native-runbook.md` — operational flow
- `references/live-acceptance-test.md` — acceptance procedure
