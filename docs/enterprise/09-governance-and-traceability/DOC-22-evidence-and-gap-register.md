# DOC-22: Source Evidence Register & Documentation Gap Register

| Field | Value |
|---|---|
| **Document ID** | `DOC-22` |
| **Version** | `1.0.0` |
| **Category** | 9 — Traceability, Evidence & Governance |
| **Repository Commit** | `445c7f965e22e7915238cca5631e68b4462d7b7b` |
| **Validation Status** | Reviewed |

---

## 1. Purpose and Scope
This register catalogues the authoritative Source Evidence (`EV-01`–`EV-16) underpinning all enterprise documentation claims and the complete Documentation Gap & Discrepancy Register (`GAP-01`–`GAP-12) detected by cross-checking existing `docs/` against the codebase.

## 2. Executive Summary
Every material architectural, functional, security, and operational statement in `docs/enterprise/` is anchored to verified repository artifacts at commit `445c7f965e22e7915238cca5631e68b4462d7b7b`. Cross-validation uncovered **11 concrete documentation discrepancies** in existing `docs/` (including a security-critical contradiction on backup key separation, `GAP-08`, an incomplete LLM tool list in `docs/model-contract.md`, `GAP-12`, and drift in migration/test counts) plus **1 commercial/governance information gap** (`GAP-11).

---

## 3. Source Evidence Register (`EV-01`–`EV-16)

| Evidence ID | Artifact / Path | Key Facts Verified |
|---|---|---|
| `EV-01` | `pyproject.toml`, `docs/requirements-lock.txt` | Python 3.12, 10 runtime deps (2 unused OTel), 7 dev deps, 100 locked transitive packages, `mypy --strict`, `ruff`. |
| `EV-02` | `Dockerfile`, `docker-compose.yml`, `deploy/start.py` | Two-stage `.pyc`-only image, non-root `app`, `redis-alpine`, atomic `.partial` seed copy, `/ready` healthcheck. |
| `EV-03` | `data/setuhaul.sqlite3`, `src/setuhaul/config.py` | SHA-256 pinned seed DB (18 tables, 4 views, 2 facilities, 9 docks, 21 shipments, 0 labour/consignee rows); live-use guard. |
| `EV-04` | `migrations/001`–`027` (`27` files), `src/setuhaul/db.py` | WAL mode, `busy_timeout=5000`, `BEGIN IMMEDIATE`, FK checks, 3 shared capacity unique indexes, 6 immutability triggers. |
| `EV-05` | `src/setuhaul/eta.py`, `gps.py`, `travel.py`, `position.py` | ETA precedence (D6), 15m GPS freshness, Haversine `1.3x / 40 km/h`, GPS arrival floor, 30m mismatch, 900s proactive hold. |
| `EV-06` | `src/setuhaul/capacity.py`, `feasibility.py`, `allocation.py`, `option_history.py` | 5-gate feasibility, 5m freshness, fail-closed labour/consignee, FCFS ranking, 15m option TTL, 30d retention. |
| `EV-07` | `src/setuhaul/booking.py`, `cancellation.py`, `release.py`, `assign.py`, `round_clock.py`, `dock_blocks.py` | 180s holds, atomic confirm swap, 2-turn cancel (120m cutoff), 24/7 14d crew-driven horizon, dock block `PLAN_CHANGED`. |
| `EV-08` | `src/setuhaul/facility_change.py`, `partner_booking.py`, `mail_transport.py` | 120m facility change + auto-revert; SMTP/IMAP partner booking with SPF/DKIM/DMARC & bare `APPROVE`/`DECLINE` parser. |
| `EV-09` | `src/setuhaul/dock_pass.py`, `passes.py`, `pass_check.py`, `gate.py` | 128-bit `DAP-...` (SHA-256), 5-attempt chat lockout, 8-step gate refusal order, 10 fails/15m Redis limit, overstay sweep. |
| `EV-10` | `src/setuhaul/conversation.py`, `model_gateway.py`, `prompts.py`, `tools.py`, `language.py`, `memory.py` | LangGraph bounded loop (`6` rounds, `4` tools, `8s`/`30s`, `$0.05), 14 tools, `driver_quote`, canary & claim guards. |
| `EV-11` | `src/setuhaul/escalation.py`, `coordinator.py`, `events.py`, `notifications.py`, `driver_messages.py` | 18 reason codes, dedupe key, 24h reopen, CLI, fsync JSONL fallback, transactional outbox, 5-attempt Telegram backoff. |
| `EV-12` | `src/setuhaul/api.py`, `src/setuhaul/auth/*`, `app_settings.py`, `retention.py` | 77 HTTP routes, OpenAPI disabled, Google OIDC+PKCE, email/password, break-glass admin, RBAC, Fernet config, retention/erasure. |
| `EV-13` | `tests/test_*.py` (`142` files), `pytest --collect-only -q` | **2,127** automated tests collected; 9 active regression traces in `tests/traces/`. |
| `EV-14` | `docs/verification/concurrency-proof.md`, `scale-test.md`, `scenario-run.md`, `auth-roles-run.md` | 100/100 contention runs (0 violations); 244 holds/s SQLite plateau; 23/23 scenario checks; 58/58 evaluator checks. |
| `EV-15` | `docs/decision-register.md` (`D1`–`D107), `docs/operations-questions.md` (`Q1`–`Q20) | 107 recorded decisions; 20 unapproved operational questions (6 `BLOCKER). |
| `EV-16` | `docs/client-readout.md`, `docs/faq.md`, `docs/readiness-detail.md`, `docs/deployment-hostinger.md` | Explicit prototype status, unproven external integrations, unassessed DPDP/GDPR, never deployed. |

---

## 4. Documentation Gap & Discrepancy Register (`GAP-01`–`GAP-12)

| Gap ID | Severity | Area | Existing Documentation Claim | Verified Repository Reality | Recommended Action |
|---|---|---|---|---|---|
| `GAP-01` | Medium | Migration count | `README.md` ("001-023"), `running-and-demonstrating.md` ("022"), `deployment-hostinger.md` ("023") | **27 migrations** (`001`–`027_user_deletions.sql) | Update migration range references in existing docs. |
| `GAP-02` | Low | Test & source count | `README.md` ("~1,970"), `running-and-demonstrating.md` ("1,972 passed", "105 source files") | **2,127 tests** across **142** test files; **108** `.py` files in `src/` | Refresh verification counts after next CI run. |
| `GAP-03` | Low | Scenario evaluator count | `docs/readiness-detail.md` ("66/66 evaluator checks") | `docs/verification/scenario-run.md` records **58 / 58** | Reconcile `readiness-detail.md` to `58/58`. |
| `GAP-04` | Medium | Missing ADR | `docs/decision-register.md` ends at `D107` | Commits `e430ec3`/`f8e106f` added hybrid web user soft-deletion (`027_user_deletions.sql) | Append `D108` to `decision-register.md` (already captured as `ADR-12` in `DOC-11). |
| `GAP-05` | Medium | Password-login toggle | `running-and-demonstrating.md` ("switch stays in `.env`") | Moved to **Admin > Configuration > Email** (`D107`, commit `445c7f9) | Remove outdated sentence at line 274. |
| `GAP-06` | Medium | Chat visibility rule | `running-and-demonstrating.md` describes superseded `D86` project-window visibility | Replaced by `D104` per-load message tagging (`021_chat_message_loads.sql) | Update Known Limitations section to reflect `D104`. |
| `GAP-07` | Medium | Demo console capabilities | `running-and-demonstrating.md` ("no Google sign-in", "rule-based stand-in") | Demo supports Google sign-in from `.env` and has **no** rule-based stand-in (`D92) | Update demo section in `running-and-demonstrating.md`. |
| `GAP-08` | **High** | Backup key separation | `deployment-hostinger.md` ("backup copies both DB and `.env`") | `operations/backup-and-restore.md` explicitly forbids storing `SETUHAUL_SETTINGS_KEY` with unencrypted DB | Align `deployment-hostinger.md` with `backup-and-restore.md` key-separation rule. |
| `GAP-09` | Medium | Retention & IP lockouts | `readiness-detail.md` ("no retention/erasure"); `faq.md` ("no general web sign-in rate limit"); `decision-register.md` (D84 "re-enable users not built"); `evaluation-plan.md` ("5/5 traces") | `retention.py` + `024_auth_ip_lockouts.sql` + `check_auth_rate_limit` + user re-enable + 9 active traces exist | Refresh stale statements in `readiness-detail.md`, `faq.md`, `decision-register.md`, `evaluation-plan.md`. |
| `GAP-10` | Low | SQL rollback comment typo | `migrations/024`, `025`, `026` reference `schema_migrations WHERE version=...` | Actual column is `filename` (`src/setuhaul/db.py) | Fix SQL comments in future cleanup or note in runbook. |
| `GAP-11` | **High** | Commercial & governance data | Not present in repository | Missing business sponsor, ROI/budget, SLA targets, DPDP/GDPR legal sign-off, UAT sign-off | Resolve via Stakeholder Questionnaire (`DOC-23). |
| `GAP-12` | Medium | Incomplete LLM tool catalogue | `docs/model-contract.md` lists 13 tools under "What the model can call" | `src/setuhaul/tools.py` registers **14 tools**; `model-contract.md` omits `request_facility_change` | Add `request_facility_change` to `docs/model-contract.md` (full 14-tool table now documented in `DOC-08). |
