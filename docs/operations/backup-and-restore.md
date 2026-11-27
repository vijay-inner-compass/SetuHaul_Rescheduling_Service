---
title: Backup and restore
type: operation
created: 2026-11-28
updated: 2026-11-28
sources:
  - code: src/setuhaul/db.py
  - code: src/setuhaul/app_settings.py
  - code: docker-compose.yml
verified_at: cebaf09
---

# Backup and restore

A complete SetuHaul backup requires **two** artefacts stored separately:

1. **The SQLite database file** (`SETUHAUL_DB_PATH`, `/app/state/setuhaul.db` in Docker), taken with SQLite's online
   backup API so WAL frames are included consistently.
2. **The settings master key** (`SETUHAUL_SETTINGS_KEY` from `.env`). Encrypted rows in `app_settings` (model API key,
   Telegram bot token and webhook secret, SMTP password, LangSmith key) cannot be decrypted without this exact Fernet key.

> ⚠️ Never copy a live SQLite database with plain `cp` while the service is writing in WAL mode; always use SQLite's
> online backup API (`sqlite3.Connection.backup`). Never store `SETUHAUL_SETTINGS_KEY` in the same unencrypted archive as
> the database.

## 1. Backup

Run against the live database path (`SRC_DB`) and destination (`BACKUP_DB`):

```bash
python -c "import sqlite3, sys; s=sqlite3.connect(sys.argv[1]); d=sqlite3.connect(sys.argv[2]); s.backup(d); d.close(); s.close()" \
  "$SRC_DB" "$BACKUP_DB"
```

Verify the backup file immediately (read-only):

```bash
python -c "import sqlite3, sys; c=sqlite3.connect('file:'+sys.argv[1]+'?mode=ro', uri=True); print('integrity:', c.execute('pragma integrity_check').fetchall()); print('fk:', c.execute('pragma foreign_key_check').fetchall()); c.close()" \
  "$BACKUP_DB"
```

Expected output: `integrity: [('ok',)]` and `fk: []`.

Separately back up `SETUHAUL_SETTINGS_KEY` (and the rest of `.env`) in your secret manager or encrypted host backup
(never print the key value into terminal logs or tickets).

## 2. Restore

1. Stop the application container (`docker compose stop app`) so nothing writes during restore.
2. Restore the database file to `RESTORE_DB` (removing any stale `-wal` and `-shm` side files beside the target):

```bash
rm -f "${RESTORE_DB}-wal" "${RESTORE_DB}-shm"
cp "$BACKUP_DB" "$RESTORE_DB"
```

3. Restore `SETUHAUL_SETTINGS_KEY` into `.env` (with mode `0600`).
4. Verify database integrity, foreign keys, and that encrypted `app_settings` decrypt cleanly with `SETUHAUL_SETTINGS_KEY`:

```bash
python -c "import os, sqlite3, sys; from setuhaul import app_settings; c=sqlite3.connect(sys.argv[1]); print('integrity:', c.execute('pragma integrity_check').fetchall()); print('fk:', c.execute('pragma foreign_key_check').fetchall()); eff=app_settings.effective(c, os.environ); print('unreadable_secrets:', sorted(eff.unreadable)); assert not eff.unreadable; c.close()" \
  "$RESTORE_DB"
```

Expected output: `integrity: [('ok',)]`, `fk: []`, `unreadable_secrets: []`.

5. Start the service (`docker compose up -d app`) and check `/health`, `/ready`, and `/version`.
