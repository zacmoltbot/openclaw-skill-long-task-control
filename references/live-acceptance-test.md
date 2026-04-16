# Live acceptance test

Validate the skill as a real user-facing path, not just a monitor demo.

## What this proves

- `bootstrap-task` is the one canonical entrypoint
- bootstrap starts execution immediately
- the generic executor can complete a real multi-step task without a specialized adapter
- progress and reporting survive interruption because state lives on disk
- terminal completion is evidence-backed and drives cleanup

## Step-by-step

```bash
cd /tmp/openclaw-skill-long-task-control
export LTC_ACCEPT_ROOT=/tmp/ltc-live-acceptance
rm -rf "$LTC_ACCEPT_ROOT"
mkdir -p "$LTC_ACCEPT_ROOT/out"
export LTC_LEDGER="$LTC_ACCEPT_ROOT/ledger.json"
export LTC_JOBS="$LTC_ACCEPT_ROOT/jobs"
export LTC_OUT="$LTC_ACCEPT_ROOT/out"
```

Bootstrap a generic long task whose steps are executable through the generic path:

```bash
python3 scripts/openclaw_ops.py --ledger "$LTC_LEDGER" bootstrap-task acceptance-demo \
  --goal "Inspect inputs, build draft artifact, validate, and hand off" \
  --owner main-agent \
  --channel discord \
  --requester-channel acceptance-demo-channel \
  --workflow "Inspect inputs :: shell=printf 'inspected\n' > $LTC_OUT/inspected.txt :: expect=$LTC_OUT/inspected.txt" \
  --workflow "Build draft artifact :: shell=cat $LTC_OUT/inspected.txt > $LTC_OUT/draft.txt && printf 'draft\n' >> $LTC_OUT/draft.txt :: expect=$LTC_OUT/draft.txt" \
  --workflow "Validate and hand off :: shell=grep -q draft $LTC_OUT/draft.txt && cp $LTC_OUT/draft.txt $LTC_OUT/validated.txt :: expect=$LTC_OUT/validated.txt" \
  --next-action "Start step-01 and publish the first observed update" \
  --message-ref "discord:msg:acceptance-demo" \
  --jobs-root "$LTC_JOBS" \
  --disabled
```

Expected after bootstrap:
- ledger exists
- job spec exists
- the owned runner loop already executed through the full workflow
- the task is terminal (`COMPLETED` for this demo), not mid-run waiting for another manual tick

Inspect durable state:

```bash
python3 scripts/runner_engine.py --jobs-root "$LTC_JOBS" status acceptance-demo-job
python3 scripts/openclaw_ops.py --ledger "$LTC_LEDGER" preview-tick acceptance-demo
```

Inspect final durable state:

```bash
python3 scripts/runner_engine.py --jobs-root "$LTC_JOBS" status acceptance-demo-job
python3 scripts/openclaw_ops.py --ledger "$LTC_LEDGER" preview-tick acceptance-demo
```

Expected at the end:
- job status is `COMPLETED`
- ledger task status is `COMPLETED`
- pending user updates contain all step completions plus task completion
- preview state is `STOP_AND_DELETE`
- output artifacts exist under `$LTC_OUT/`

Fast smoke:

```bash
python3 scripts/skill_live_acceptance_e2e.py
```
