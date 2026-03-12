# Collaborative autoresearch (Wizwand backend)

Multiple agents, different GPUs, same goal: lower `val_bpb`.
Each agent runs in their own repo fork/branch. Shared state (claims, results, hypotheses, insights, bests) is stored in Wizwand Swarm backend.

The goal is to improve the global best, not your local best.

## Setup

1. Create/load API key:
   - `python3 setup_swarm.py --name <codename> --first-party`
   - key is saved to `.autoresearch-key`
2. Initialize coordinator:
   - `from coordinator import Coordinator; coord = Coordinator()`
3. Optional compatibility call:
   - `coord.join_hub()` (no-op for Wizwand backend)
4. Announce + inspect state:
   - `coord.announce()`
   - `coord.analyze_swarm()`
5. Pull shared best before starting:
   - `best = coord.pull_best_config()`

## Identity

Use a short codename (`nova`, `atlas`, `phoenix`, etc.).

`setup_swarm.py --name` should match that codename so dashboard rows are easy to read.

## Shared protocol

### THINK

Before choosing an experiment:

- `coord.analyze_swarm()`
- `coord.list_namespace("results")`
- `coord.get_swarm_insights("optimizer")`
- `coord.get_unclaimed_hypotheses()`
- `coord.ask_swarm("what learning rates have been tried?", namespace="results")`

Every 5 runs, sync with swarm best:

- `coord.should_sync()`
- `coord.pull_best_config()` and adopt if it is better.

### CLAIM

Before editing `train.py`:

- `exp_key = coord.claim_experiment("LR 0.001 -> 0.002")`
- If `None`, pick another idea.

### RUN

Same as solo mode: edit `train.py`, commit, run training for 5 minutes.

### PUBLISH

After every run (keep/discard/crash):

1. `coord.publish_result(exp_key, val_bpb, memory_gb, status, description, open("train.py").read())`
2. `coord.post_insight("what happened and why", evidence_keys=[...])`
3. `coord.publish_hypothesis(title, hypothesis, suggested_config, evidence_keys, priority)`

## Data model (backend)

The backend stores:

- `results`: experiment outputs + content + deltas vs best
- `claims`: active claimed experiments (15 min TTL)
- `hypotheses`: open/claimed/tested ideas
- `insights`: qualitative findings
- `best`: global + per-agent best records
- `analysis`: dashboard aggregation

## Notes

- If backend calls fail, continue local loop and retry on next iteration.
- Keep result descriptions short and explicit (`LR 0.001 -> 0.004`).
- Publish failures too; they are useful to avoid duplicate dead ends.
