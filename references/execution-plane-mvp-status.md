# Execution plane MVP status

## Implemented now

- `job_models.py`: serial job / item / artifact / failure model, plus bridge/execution metadata and local lock files
- `runner_engine.py`: job init, status, serial run-loop, single-writer lock skeleton
- `executor_engine.py`: preview and run-next for one item, plus persisted `RUNNING` item resume skeleton
- `adapters/base.py`: adapter contract
- `adapters/generic_manual.py`: explicit human-gated fallback adapter
- `adapters/runninghub_matrix.py`: first specialized adapter skeleton
- `execution_bridge.py`: bridge from execution-plane item events into `task_ledger.py`
- `execution_plane_mvp_e2e.py`: end-to-end demo proving bridge + pending updates + resume path

## What is working now

- generic manual jobs can be initialized, but by default they stop at an honest human gate unless a step explicitly opts into synthetic/demo semantics
- execution-plane state is persisted under `state/jobs/<job_id>/`
- the runner/executor auto-sync item `started / completed / blocked / task-completed` into the existing ledger/reporting flow
- `STEP_COMPLETED` and `TASK_COMPLETED` are only emitted when an adapter actually returns completed/task-completed semantics; human-gated generic steps do not auto-finalize
- one local worker lock file prevents double-running the same job in parallel
- a persisted `RUNNING` item can be resumed by the executor MVP path (`RUNNING -> RETRY -> execute`)

## What is still missing

- stale lock recovery / lease-based locking / distributed locking
- richer resume semantics than `RUNNING -> RETRY` skeleton
- native adapter-specific submit / observe / collect loops for RunningHub
- monitor_nudge retry-first policy directly consuming execution-plane retry telemetry
- OpenProse bridge

## Product meaning

The repo has moved from:
- supervision-oriented long-task control only

to:
- supervision + an actual execution-plane MVP integrated with the same task ledger truth/reporting system

It is still an MVP, but it is now truthful, testable, and connected end-to-end.
