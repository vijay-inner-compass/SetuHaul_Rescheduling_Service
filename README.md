# SetuHaul Conversational Rescheduling Service

A prototype that lets a truck driver report a delay, breakdown or early arrival in English, Hindi or Hinglish, and then
safely get a new dock slot. A model interprets the message and calls narrow tools; deterministic Python services own
identity, ETA derivation, feasibility, holds, expiry, confirmation, cancellation, idempotency, notifications and escalation.
SQLite is the system of record for the prototype.

> **Status:** all twelve phases are built and verified on Windows. The prototype has **not** been run against a real model,
> and its Hindi/Hinglish wording has not been reviewed by a native speaker. Start with `docs/client-readout.md`.

## Quick start (Windows PowerShell; Python 3.12)
```powershell
py -3.12 -m venv .venv
.\.venv\Scripts\python -m pip install -e ".[dev]"
.\.venv\Scripts\python -m pytest -q          # 494 tests
.\.venv\Scripts\python -m ruff check .
.\.venv\Scripts\python -m mypy src
```
On Linux/macOS use `python3.12 -m venv .venv` and `.venv/bin/python`. Dependencies are pinned in `pyproject.toml`; the full
resolved set is in `docs/requirements-lock.txt`.

## Run the evidence
| Command | What it shows |
|---|---|
| `python scripts/audit_data.py` | read-only audit of the supplied snapshot (integrity, timestamps, one shipment trace) |
| `python scripts/replay_traces.py` | replays the recorded traces through the deterministic layers (no model) and scores them |
| `python scripts/run_scenario.py` | the required end-to-end scenario, 23 checks, writes `docs/verification/scenario-run.md` |
| `python scripts/run_integrity_proof.py` | 20x thread, process and mixed contention plus property tests; writes `concurrency-proof.md` (about 8 minutes) |
| `python scripts/run_scale_profile.py` | generated 360-load fixture, spike and burst, SQLite saturation; writes `scale-test.md` data |
| `python scripts/run_live_model_scenario.py` | one real-model scenario. **Blocked until you set a provider and key** (see below) |
| `python scripts/run_demo.py` | demonstration console in the browser at `http://127.0.0.1:8800/demo` (driver chat, dock board, operations; localhost only, simulated clock) |

## Run the service
```powershell
$env:SETUHAUL_DB_PATH = "data\working.sqlite3"            # a migrated copy, see below
$env:SETUHAUL_WEBHOOK_SECRET = "<shared secret with the messaging provider>"
$env:SETUHAUL_COORDINATOR_TOKENS = '{"<token>": "asha"}'
$env:SETUHAUL_REDIS_URL = "redis://localhost:6379/0"
python -m uvicorn --factory setuhaul.api:create_app_from_env --port 8000
```
Make the working database from the pristine seed: `python -c "from pathlib import Path; from setuhaul.db import make_working_db; make_working_db(Path('data/working.sqlite3'))"`.
`data/setuhaul.sqlite3` is hash-pinned and never written; migrations (`migrations/`) are applied to copies.
Coordinators can also use the CLI: `python -m setuhaul.coordinator --db data/working.sqlite3 list|show|claim|resolve|reopen`.

Model configuration comes from the environment only: `SETUHAUL_MODEL_PROVIDER`, `SETUHAUL_MODEL_ID`, `SETUHAUL_MODEL_API_KEY`
(`SETUHAUL_MODEL_PROVIDER` is `openai` or `anthropic`; optionally a git-ignored `.env`, see `.env.example`). With none set, every turn takes the safe "cannot check bookings right now"
path and opens an escalation case. Optional LangSmith tracing is off unless `SETUHAUL_LANGSMITH=approved` and `LANGSMITH_API_KEY` are set (scrubbed first; see `.env.example`). Load-test-only switches (`SETUHAUL_USE_STUB_MODEL`, `fakeredis://`, `SETUHAUL_FAKE_NOW`) are not for production.

## Layout
```
src/setuhaul/   config, clock, db, identity, rate_limit, memory, eta, feasibility, capacity, allocation, option_history,
                booking (holds, confirmation), cancellation, events, notifications, escalation, coordinator, model_gateway,
                prompts, tools, language, conversation, evaluation, integrity, proof, scale_fixture, stub_model, api, jobs/
migrations/     001-008, applied in filename order to working copies
tests/          494 tests, traces/ (sanitised regression baselines), scenario_flow.py (the scenario)
scripts/        audit, replay, scenario, integrity proof, scale profile, live-model run, trace recording
load/           Locust profile
docs/           problem definition, data audit, decision register, operations questions, model contract, client readout,
                verification/ (every phase's evidence)
Archieve/       the earlier flat prototype, the brief PDF and the supplied data package, moved here by the owner (not part of this build;
                the build's database is the hash-pinned copy at data/setuhaul.sqlite3)
```

## The rules the code enforces
- Identity comes from a verified channel or a server-made session, never from the model or the message.
- A slot is *shown*, *held*, *confirmed*, *cancelled*, *expired* or *released*, and the wording follows the stored state.
- Holds live in the same table and unique index as bookings, so a planner, another driver or a scheduling tool collides with them
  on the database constraint. First valid hold wins; at most one hold per shipment.
- The original appointment ends only inside the transaction that confirms its replacement.
- Missing labour, consignee or fresh facility data means "cannot offer", never "assume available".
- Confirmation, hold, release and cancellation wording is rendered from committed rows, not written by the model.
- Anything the service cannot do safely becomes an escalation case with an owner, a lifecycle and a full context.

## Where to read next
`docs/client-readout.md` (what is proven and what is not), `docs/decision-register.md` (every decision and default),
`docs/operations-questions.md` (what operations must answer), `docs/model-contract.md`, `docs/verification/`.
