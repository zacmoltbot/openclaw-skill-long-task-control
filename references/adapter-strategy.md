# Adapter strategy

## Why adapters exist

`long-task-control` should not hardcode RunningHub, GitHub, browser automation, or any single task domain into the runner.

The execution plane must stay generic.
Adapters translate domain-specific execution semantics into the generic runner contract.

## Important product rule

Adapters do **not** auto-appear.

The system should ship with:

1. one durable generic adapter (`generic_manual`)
2. zero or more specialized adapters added incrementally from real usage

This prevents the design trap of trying to pre-model every possible long-running task domain up front.

## Layering

- `runner_engine.py`: serial job execution, persistence, progress
- `executor_engine.py`: single-item execution loop
- `adapters/base.py`: contract
- `adapters/generic_manual.py`: baseline adapter
- `adapters/<domain>.py`: optional specialization

## Current adapter roadmap

### Generic baseline
- `generic_manual`
  - explicit human-gated fallback for arbitrary multi-step work when no real domain adapter exists
  - safe by default: block honestly instead of auto-completing real work

### First specialized adapter
- `runninghub_matrix`
  - because it is the current pain point and a good test for single-concurrency external orchestration

### Future examples
- `github_batch`
- `browser_batch`
- `research_pipeline`
- `artifact_review`

Add them only when real demand justifies them.

## OpenProse compatibility

OpenProse should orchestrate workflows, while adapters remain the domain-specific bridge for the durable execution engine.

That means:
- OpenProse does not replace adapters
- adapters do not replace OpenProse
- the runner remains the durable stateful execution core
