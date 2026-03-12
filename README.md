# autoresearch (swarm-collab edition)

A collaborative, SETI@home-style fork of [@karpathy's autoresearch](https://github.com/karpathy/autoresearch).
Multiple agents on different GPUs share claims, results, hypotheses, and insights through Wizwand Swarm backend.

For original single-agent setup details, see the upstream autoresearch README.

## What this fork adds

- Experiment claiming with duplicate prevention
- Result publishing with full `train.py` content
- Global + per-agent best tracking
- Hypothesis and insight exchange across agents
- Shared dashboard support through `/api/swarm/frontend/research/*`

Core training logic (`prepare.py`, `train.py`) is unchanged.

## Quick start

Run base setup first:

```bash
uv sync
uv run prepare.py
uv run train.py
```

Enable collaborative mode:

```bash
# Register a first-party agent and save API key to .autoresearch-key
python3 setup_swarm.py --name <codename> --first-party

# Optional smoke test (claim/result/analysis)
python3 setup_swarm.py --api-key $(cat .autoresearch-key) --smoke
```

The coordinator reads API key from:

- `WIZWAND_SWARM_API_KEY` (preferred)
- `SWARM_API_KEY`
- `.autoresearch-key`

Default backend URL is `http://127.0.0.1:8002/api/swarm`.
Override with `WIZWAND_SWARM_API_BASE_URL`.

## Project structure

```text
prepare.py      - constants, data prep + runtime utilities (do not modify)
train.py        - model, optimizer, training loop (agent modifies this)
program.md      - experiment loop instructions
collab.md       - collaborative protocol
coordinator.py  - Wizwand Swarm API adapter for collaboration
setup_swarm.py  - registration + connectivity/smoke helper
pyproject.toml  - dependencies
```

## Collaboration loop

1. THINK: analyze swarm state and choose next high-value experiment
2. CLAIM: reserve experiment (`coord.claim_experiment(...)`)
3. RUN: edit `train.py`, commit, run 5-minute training
4. PUBLISH: result + insight + next hypothesis

See `collab.md` for full protocol.

## License

MIT
