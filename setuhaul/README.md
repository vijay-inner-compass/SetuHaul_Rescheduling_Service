# SetuHaul exception service (prototype)

See DESIGN.md for the architecture and the course concepts behind each file.

Run the tests (16, offline, no API key):
    python -m pytest -q

Versions used: Python 3.11.9, SQLite 3.45.1, pydantic 2.14.1, pytest 9.1.1.
Optional real model: `pip install anthropic`, set ANTHROPIC_API_KEY, pass `agent.AnthropicModel()` to `app.chat` (adapter written, not yet exercised).

Status: the schema and seed data are synthetic, because the supplied 18-table SQLite snapshot was not available.
Adapt `schema.sql`, `seed.py` and `database.py` to it, then run the data audit.
