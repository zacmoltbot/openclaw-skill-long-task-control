# Long-task-control redesign spec: user-facing state model + outcome-first reconciliation

## 1. Product point of view

The user should experience this skill as a durable task owner, not as a bundle of monitor/control-plane sub-systems.

From the user's point of view, the product contract is:

1. accept a long or multi-stage task
2. keep making real progress when it can
3. preserve evidence and outputs on disk
4. recover after interruption
5. report useful progress in simple language
6. reconcile final user-facing result from actual outputs/evidence
7. mention internal interruption/cleanup issues honestly, but not as the headline when deliverables already exist

The monitor exists to protect this product contract, not to replace it.

---

## 2. Core redesign

### Old mental model (too control-plane-first)

The old system leaned toward:
- internal state correctness first
- blocker/interruption semantics first
- monitor decisions dominating the user story
- completion judged primarily from control-plane cleanliness

That produces a bad UX failure mode:
- output exists
- interruption/cleanup happened
- user sees blocker/interruption noise first
- system looks more failed than the actual task result deserves

### New mental model

Separate **control-plane state** from **user-facing outcome state**.

- **Control-plane state** answers: what happened inside execution/monitoring?
- **User-facing outcome state** answers: what should the user believe they got?

The system must keep both.
The user-facing layer should normally lead the explanation.

---

## 3. Redesigned user-facing state model

Each task now has two different semantic layers:

### A. Control-plane state

This is the existing operational truth:
- `RUNNING`
- `BLOCKED`
- `COMPLETED`
- `FAILED`
- interruption / retry / escalation / cleanup details

This is still required for honesty and debugging.

### B. User-facing outcome state

New derived model under `derived.user_facing`:

- `IN_PROGRESS`
  - no meaningful deliverable yet
  - task is still legitimately underway
- `SUCCESS`
  - user-visible deliverable exists and completion truth is established
- `PARTIAL_SUCCESS`
  - some real outputs exist, but the task did not finish cleanly or some intended work remains/unconfirmed
- `BLOCKED`
  - task cannot currently continue and there is not yet enough successful result to foreground
- `FAILED`
  - task failed without meaningful user-facing deliverable

Each outcome includes:
- `headline`
- `artifacts`
- `done_steps`
- `remaining_steps`
- `control_plane_status`
- `honesty_notes`

This lets the system say things like:
- “2 outputs were produced; cleanup was interrupted”
- instead of only: “EXECUTION_INTERRUPTED / BLOCKED_ESCALATE”

---

## 4. Outcome-first reconciliation

Outcome-first reconciliation means:

1. inspect actual deliverable evidence first
   - recorded artifacts
   - discovered expected artifacts on disk
   - validation evidence
   - external completion evidence
   - explicit owner completion truth
2. derive the best honest user-facing result
3. only then explain control-plane defects/interruption noise

### Decision rule

If useful outputs exist, interruption noise should **not** automatically dominate the top-level result.

Examples:

#### Case A: clean completion
- outputs exist
- completion truth exists
- user-facing outcome = `SUCCESS`

#### Case B: outputs exist, execution interrupted before clean handoff
- outputs exist
- task/control-plane ended `BLOCKED`
- user-facing outcome = `PARTIAL_SUCCESS`
- monitor should request result reconciliation, not jump straight to blocker-noise escalation

#### Case C: no outputs, interrupted before meaningful work landed
- no outputs
- task/control-plane blocked/interrupted
- user-facing outcome = `BLOCKED` or `FAILED`

This keeps the system honest without being product-confusing.

---

## 5. Monitor redesign

The monitor is no longer framed as the thing that defines the product state.

Its job is:
- ensure progress is still happening
- deliver queued updates
- notice stale or contradictory truth
- request reconciliation when result/evidence and control-plane state diverge
- escalate only when there is no better user-correct explanation
- clean itself up on terminal tasks

### New monitor rule

If `derived.user_facing.outcome_status == PARTIAL_SUCCESS`, monitor should prefer:
- `OWNER_RECONCILE`

before:
- `BLOCKED_ESCALATE`

because the task story is now:
- “some useful result already exists; reconcile/report that first”

not:
- “system is blocked; user should first see internal interruption semantics”

---

## 6. First implementation slice in this pass

This pass intentionally implements the smallest coherent slice of the redesign:

1. derive `derived.user_facing`
2. auto-discover workflow-declared expected artifacts that already exist on disk
3. classify blocked+artifact cases as `PARTIAL_SUCCESS`
4. teach monitor to prefer reconciliation over blocker escalation in those cases
5. expose the user-facing outcome via monitor preview
6. add a regression proving: artifact exists + execution interrupted => user-facing partial success + reconcile-first monitor behavior

This slice does **not** solve every result-reconciliation problem yet.
It solves the most confusing one first.

---

## 7. What this redesign does not claim yet

Still not done in this pass:
- richer success scoring across multiple outputs/validations
- domain-specific result reconciliation for external providers
- a polished user-facing message renderer that converts `derived.user_facing` directly into final chat copy
- deeper partial-success semantics across multi-item batch jobs
- automatic promotion from `PARTIAL_SUCCESS` to `SUCCESS` after owner validation without explicit reconciliation writeback
- full end-to-end cleanup semantics for all adapters

---

## 8. Acceptance target for this redesign slice

The regression case to preserve:

1. workflow step starts
2. real output artifact is produced
3. control-plane execution is interrupted before clean completion
4. task control-plane state becomes `BLOCKED`
5. derived user-facing state becomes `PARTIAL_SUCCESS`
6. monitor chooses reconciliation-first behavior (`OWNER_RECONCILE`), not blocker escalation first
7. preview/reporting surfaces the user-facing outcome and artifacts

If that holds, the system is materially more user-correct than the prior design.
