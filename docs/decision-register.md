# Decision Register

| ID | Date | Decision | Status | Rationale |
|---|---|---|---|---|
| D1 | 2026-11-18 | Python 3.12, `pathlib`, exact dependency pins (`pyproject.toml`, full lock in `docs/requirements-lock.txt`). | Applied | Plan Global Constraints. |
| D2 | 2026-11-18 | `tzdata` pinned as a runtime dependency. | Applied | Windows has no system IANA database; `Asia/Kolkata` failed to resolve without it (found by the Phase 0 timezone test). |
| D3 | 2026-11-18 | Supplied DB `SetuHaul_Data_Package/setuhaul_freight_operations.db` is copied unchanged to `data/setuhaul.sqlite3`. SHA-256 `C02806E6...F90700` is pinned in `config.py` and checked by a test. | Applied | Plan path differs from the supplied filename; original left untouched. |
| D4 | 2026-11-18 | Repo root is `C:\GitHub\SetuHaul_Rescheduling_Service` (plan's `setuhaul/` root). A pre-existing folder `setuhaul/` (earlier flat prototype with DESIGN.md) is left untouched and excluded from Ruff/pytest. | Needs your awareness | Not overwritten without asking. It does not collide with `src/setuhaul` because pytest uses `pythonpath=src`. |
| D5 | 2026-11-18 | Local `git init` on branch `main`; Phase 0 commits go there. No remote, no push. | Applied | Directory was not a git repo; plan requires commits. |
| D6 | 2026-11-18 | ETA baseline rule: "N minutes late" = active shipment's planned ETA + N. "Repair takes N minutes" needs travel info or returns ClarificationNeeded. | Pending Phase 2 | Plan Review Focus. |
| D7 | 2026-11-18 | Questions Q1-Q16 in `operations-questions.md`; unanswered ones are explicit defaults or blockers. | Pending approval | Phase 0 gate. |
| D8 | 2026-11-18 | Timestamp contract: ISO-8601 with explicit offset, normalised to aware Asia/Kolkata; naive/malformed values rejected; never string-compared. Snapshot verified uniform (+05:30, 34 columns). | Applied (Phase 1) | `docs/data-audit.md` |
| D9 | 2026-11-18 | Phase 5 must rebuild `appointments` to add `HELD` (CHECK cannot be altered in SQLite). | Pending Phase 5 | Found in Phase 1 schema audit. |
| D10 | 2026-11-18 | Missing labour/consignee data returns `INCOMPLETE_FEASIBILITY_DATA` rather than assuming availability. | Pending Phase 4 | No such tables in snapshot. |
