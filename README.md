# SetuHaul Conversational Rescheduling Service

Phase 0 scaffold. See `docs/` for decisions and the phased plan gates.

```powershell
py -3.12 -m venv .venv
.\.venv\Scripts\python -m pip install -e ".[dev]"
.\.venv\Scripts\python -m pytest -v
.\.venv\Scripts\python -m ruff check .
.\.venv\Scripts\python -m mypy src
```

Copy `.env.example` to `.env` for local settings. Never commit secrets.
