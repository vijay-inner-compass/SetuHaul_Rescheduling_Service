# DOC-17: Operations Runbook & Troubleshooting Guide

| Field | Value |
|---|---|
| **Document ID** | `DOC-17` |
| **Version** | `1.0.0` |
| **Category** | 6 — Deployment & Operations |
| **Repository Commit** | `445c7f965e22e7915238cca5631e68b4462d7b7b` |
| **Validation Status** | Reviewed |

---

## 1. Purpose and Scope
This Operations Runbook and Troubleshooting Guide documents day-2 operational procedures, health/liveness checks, backup/restore steps, coordinator CLI operations, failure recovery, and explicitly flags missing operational procedures.

## 2. Executive Summary
Core operational primitives are built into the application (`/health`, `/ready`, `/version`, `/v1/admin/runners`, `python -m setuhaul.coordinator`, online SQLite backup verification, and fsync JSONL escalation fallback). However, **no production deployment, monitoring/alerting stack, restore drill, or secret-rotation runbook has been executed** ( /).

---

## 3. Health, Liveness & Background Runner Monitoring

| Check | Endpoint / Command | Expected Healthy Output | Failure Interpretation |
|---|---|---|---|
| **Liveness** | `GET /health` | `200 {"status": "ok"}` | Process hung or crashed. |
| **Readiness** | `GET /ready` | `200 {"status": "ready"}` (or `200` with `degraded: ["cache"]` if Redis down) | `503` = SQLite unreachable or `schema_migrations` unreadable. |
| **Build Version** | `GET /version` | `200 {"commit": "<sha>"}` | `"unknown"` if `GIT_COMMIT` build-arg omitted. |
| **Runner Heartbeats** | `GET /v1/admin/runners` (`ADMIN` session) | Recent `last_run_at` for `maintenance_runner` (~30s), `driver_notice_runner` (~5s), `partner_mail_runner` (~30s in live mode). | Stale timestamp indicates crashed/blocked daemon thread. *(Caveat `TD-09`: `partner_mail_runner` updates timestamp even if an inner step failed.)* |
| **Fallback Escalation File** | Check `/app/state/escalations-fallback.jsonl` & `.rejected` | Absent or empty | Non-empty `.jsonl` = DB outage occurred; `.rejected` = malformed/FK-violating lines requiring manual coordinator review. |

---

## 4. Standard Operational Procedures

### 4.1 Backup & Restore
- **Backup**: Always use SQLite online backup API (`sqlite3.Connection.backup), **never `cp`** while WAL mode is active. Verify backup with `PRAGMA integrity_check;` and `PRAGMA foreign_key_check;`.
- **Key Separation Policy (`GAP-08)**: Follow `docs/operations/backup-and-restore.md` — **store `SETUHAUL_SETTINGS_KEY` / `.env` separately** from the unencrypted DB archive.
- **Restore**: Not present in repository.
 1. Stop `setuhaul-app` container.
 2. Remove stale `setuhaul.db-wal` and `setuhaul.db-shm` files.
 3. Copy verified backup to `/app/state/setuhaul.db` and restore `.env`.
 4. Verify `PRAGMA integrity_check;`, `PRAGMA foreign_key_check;`, and confirm `app_settings.effective` returns empty `unreadable` list.
 5. Restart container and verify `/health`, `/ready`, `/version`.

### 4.2 Break-Glass Admin Password Management
```bash
python scripts/set_admin_password.py <admin_email>
```
Refuses to run against `data/setuhaul.sqlite3`. Clears locked break-glass state; requires `SETUHAUL_LOCAL_ADMIN=1` in environment.

### 4.3 Coordinator CLI Operations
```bash
python -m setuhaul.coordinator --db /app/state/setuhaul.db list
python -m setuhaul.coordinator --db /app/state/setuhaul.db show <CASE_ID>
python -m setuhaul.coordinator --db /app/state/setuhaul.db claim <CASE_ID> --as <name>
python -m setuhaul.coordinator --db /app/state/setuhaul.db resolve <CASE_ID> --as <name> --code <CODE> --note "<note>"
python -m setuhaul.coordinator --db /app/state/setuhaul.db reopen <CASE_ID> --as <name>
```

### 4.4 Email Connectivity Verification
```bash
python scripts/check_email_config.py
```
Or use **Admin > Configuration > Email > Send test email** (`POST /v1/admin/settings/email/test`, rate-limited 1/60s).

---

## 5. Troubleshooting Guide

| Symptom / Error Code | Likely Cause | Diagnostic Check | Resolution |
|---|---|---|---|
| `403 CROSS_SITE_REQUEST` on web forms | Reverse proxy rewrites `Host` header without `SETUHAUL_PUBLIC_URL` set. | Inspect `SETUHAUL_PUBLIC_URL` and proxy headers. | Set `SETUHAUL_PUBLIC_URL=https://<domain>` (no trailing slash) and restart. |
| `503` on `/auth/login` or `/v1/telegram/webhook` | Missing/short secrets (`SESSION_SECRET < 32B` or `TELEGRAM_WEBHOOK_SECRET < 16` chars). | Check `/v1/admin/settings` and container env. | Provision valid secrets and restart (or save Telegram settings via Admin UI). |
| `503 STATE_STORE_UNAVAILABLE` on `/auth/callback` or `/v1/gate/*` | Redis unreachable (both flows fail closed by design). | `GET /ready` (shows `degraded: ["cache"]) & check `redis` container. | Restore Redis connectivity. |
| Every driver turn escalates `MODEL_UNAVAILABLE` | No LLM provider/model/key configured, or provider returning 4xx/5xx/timeout. | Check Admin > Model & Configuration > Test model. | Configure valid `SETUHAUL_MODEL_PROVIDER`/`ID`/`API_KEY` or check provider status. |
| Supplied loads always escalate `INCOMPLETE_FEASIBILITY_DATA` | Seed DB has 0 `labour_windows` and 0 `consignee_commitments` (`BR-04). | Check Warehouse > Dock Board > Round-the-clock slots & `consignee_commitments`. | Enter `facility_crews` for 24/7 labour and supply consignee commitment records. |
| Saved secrets unreadable after restart | `SETUHAUL_SETTINGS_KEY` changed or lost (`app_settings.py). | Inspect Admin > Configuration banner (`unreadable` keys). | Restore original Fernet key from vault, or re-enter secrets in Admin > Configuration. |
| `409 PLAN_CHANGED` when blocking a dock | Another booking/hold changed between preview and commit (`dock_blocks.py). | Re-run preview in UI. | Review updated displacement plan and re-confirm. |
| `LOCK_TIMEOUT` in logs | SQLite write lock held $> 5000\text{ ms}$ (excessive concurrency or stray external writer). | Check active processes & scale metrics. | Ensure single Uvicorn worker; if sustained $>1\%$, trigger PostgreSQL migration (`DOC-20). |

---

## 6. Missing Operational Procedures
1. **Monitoring, Metrics & Alerting**: No Prometheus/OTel exporter, dashboards, or PagerDuty/Slack alert rules (`TD-04).
2. **Schema-Rebuilding Migration Rollback**: Migrations `002`, `017`, `020`, `023` rebuild tables; rollback requires full pre-deploy backup restore (not automated).
3. **Secret Rotation Runbook**: No documented zero-downtime rotation procedure for `SETUHAUL_SETTINGS_KEY`, `SETUHAUL_SESSION_SECRET`, `SETUHAUL_WEBHOOK_SECRET`, or `SETUHAUL_COORDINATOR_TOKENS`.
4. **Incident Response & Data Breach Runbook**: Not present in repository.
5. **Downstream Outbox Consumer / Purge**: `WAREHOUSE_PLANNER`/`CONSIGNEE`/`OPERATIONS` rows accumulate `PENDING` indefinitely (`TD-03).

## 7. Open Questions
- Open Questions: Target RTO/RPO, on-call ownership, and alerting channel (`DOC-23).
