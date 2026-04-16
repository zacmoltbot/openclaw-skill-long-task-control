# Execution Plane v2

## Goal

Turn `long-task-control` from a supervision-heavy framework into a **durable long-task execution system** that can:

- run serial long jobs without relying on the main chat session to "remember"
- persist progress and artifacts to disk
- resume after interruption
- support multiple task domains through adapters
- remain compatible with the current monitor / truth / reconcile model
- later interoperate with OpenProse without being replaced by it

---

## Core design

Separate the repo into three layers:

### 1. Control plane (already exists)

Existing strengths in the repo:

- task ledger / observed truth
- derived state projection
- monitoring / stale detection
- blocked escalation
- delivery push / owner reconcile
- OpenClaw-native glue (`openclaw_ops.py`)

This layer should continue to own:

- truth contracts
- supervision contracts
- reminder / escalation / cleanup decisions

### 2. Execution plane (missing; must be added)

New layer to add:

- serial runner
- item executor
- queue progression
- checkpoint persistence
- resume logic
- artifact ledger updates
- progress emission hooks

This layer should own:

- "what runs next"
- "did the step succeed / fail / block"
- "what artifacts were produced"
- "which queue item is current"

### 3. Adapter layer (new)

Adapters should translate domain-specific work into the execution plane contract.

Examples:

- `generic_manual`
- `runninghub_matrix`
- `github_batch`
- `browser_batch`
- `research_pipeline`

Adapters are **not automatic**. The system must start with a **generic adapter** and later add specialized ones incrementally.

---

## Why adapters must exist

Long-running tasks differ in:

- external submission model
- observation model
- artifact model
- retry policy
- human gate conditions

A single hardcoded runner becomes brittle fast.

But we also should not predesign 20 adapters up front.

Correct strategy:

1. define one stable adapter contract
2. implement one `generic_manual` adapter that works for broad multi-stage tasks
3. implement one concrete domain adapter (`runninghub_matrix`) as the first specialization
4. add more adapters only when real demand appears

---

## Adapter contract (proposed)

Each adapter must implement a deterministic interface:

- `plan(job_spec, state) -> execution plan`
- `prepare(item, context) -> prepared item`
- `submit(item, state) -> submission result`
- `observe(item, state) -> observed truth update`
- `collect(item, state) -> artifacts / completion facts`
- `finalize(item, state) -> final status update`

Optional hooks:

- `can_resume(item_state) -> bool`
- `suggest_retry(item_state) -> retry policy`
- `is_human_gate(item_state) -> bool`
- `format_progress_update(item_state) -> user-visible block`

---

## Generic adapter

A required baseline adapter must exist.

### `generic_manual`

Purpose:

- support arbitrary long-running multi-stage tasks where the steps are known but the domain is not yet specialized
- let the runner persist state and advance deterministic checkpoints even without a domain-specific plugin

Input shape:

- ordered steps
- optional per-step command / prompt / note
- expected artifacts
- completion conditions

Behavior:

- treat generic execution as a human-gated fallback, not a real executor
- never auto-emit completion truth for a real workflow step unless the item explicitly declares synthetic/demo semantics
- on ordinary real work, stop with owner-action-required so the owner can either execute/observe for real and write truth, or switch to a domain adapter
- allow manual observation and resume after explicit owner action

This adapter is the fallback path that keeps the system honest before domain adapters accumulate.

---

## Job model

Proposed files under `state/jobs/<job_id>/`:

```text
state/jobs/<job_id>/
├── job.json
├── progress.jsonl
├── artifacts.json
├── updates.json
└── locks/
```

### `job.json`

```json
{
  "job_id": "runninghub-matrix-2026-04-14",
  "kind": "batch",
  "adapter": "runninghub_matrix",
  "mode": "serial",
  "status": "running",
  "current_index": 0,
  "items": [],
  "completed": [],
  "failed": [],
  "blocked_reason": null,
  "created_at": "...",
  "updated_at": "..."
}
```

---

## Runner engine

New modules:

- `scripts/job_models.py`
- `scripts/runner_engine.py`
- `scripts/executor_engine.py`
- `scripts/artifact_registry.py`
- `scripts/adapters/base.py`
- `scripts/adapters/generic_manual.py`
- `scripts/adapters/runninghub_matrix.py`

### `runner_engine.py`

Responsibilities:

- load job state
- pick next runnable item
- ensure serial execution lock
- call adapter hooks in order
- persist step/item result
- append progress update
- continue until blocked / done / human gate

### `executor_engine.py`

Responsibilities:

- execute a single current item
- no monitoring logic
- no owner reconcile logic
- produce deterministic result object for the runner

---

## Progress reporting

A core rule:

- completion of an item must create a user-visible update automatically
- this must not depend on the main chat session remembering to send it

So the execution plane should append structured updates into the existing reporting layer, and the existing monitor / delivery layer can deliver them.

This preserves compatibility with the current repo strengths.

---

## OpenProse compatibility

OpenProse should be an orchestration layer, not the durable execution engine.

Recommended relationship:

- OpenProse describes workflow structure
- `long-task-control` executes durable serial jobs and persists state

Future integration options:

1. a `.prose` workflow invokes `runner_engine.py`
2. the runner can export/import an OpenProse-compatible task plan
3. a dedicated `openprose_bridge.py` translates between `.prose` state and `job.json`

This keeps the system usable even when OpenProse is disabled.

---

## Minimum viable implementation order

### Phase 1

- add `job_models.py`
- add `runner_engine.py`
- add `executor_engine.py`
- add `adapters/base.py`
- add `adapters/generic_manual.py`
- define minimal `job.json` schema
- wire progress updates into existing reporting contract
- add `execution_bridge.py` to sync execution-plane item results into the existing `task_ledger.py` truth/reporting flow

### Phase 2

- add `adapters/runninghub_matrix.py`
- add serial external wait / observe / collect loop
- add artifact registry
- add resume / retry policy hooks

### Phase 3

- add OpenProse bridge
- add more adapters only when driven by real tasks

---

## Practical answer to the adapter question

Adapters will **not** auto-appear.

What should happen instead:

- the repo ships with one **generic** adapter from day 1
- domain-specific adapters are added over time as real recurring workloads appear
- the generic adapter guarantees the system remains useful before the adapter library grows

That is the right product posture for a long-task-control system.
