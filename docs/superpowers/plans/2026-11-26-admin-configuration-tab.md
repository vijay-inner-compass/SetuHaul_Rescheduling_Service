# Admin Configuration Tab Implementation Plan

> **For agentic workers:** REQUIRED SUB-SKILL: Use superpowers:subagent-driven-development (recommended) or superpowers:executing-plans to implement this plan task-by-task. Steps use checkbox (`- [ ]`) syntax for tracking.

**Goal:** An admin changes model, LangSmith and Telegram settings on the live service from the Admin screen, with no restart and no server login.

**Architecture:** A table of saved settings is laid over the process environment, keyed by the same variable names. A `SettingsSource` on `AppDeps` hands each request the settings in force; when it is `None` (tests today, the web demo) behaviour is unchanged. Secrets are encrypted with Fernet using `SETUHAUL_SETTINGS_KEY`.

**Tech Stack:** Python 3.12, FastAPI, SQLite, `cryptography` (new), plain JavaScript.

**Spec:** [docs/superpowers/specs/2026-11-26-admin-configuration-tab-design.md](../specs/2026-11-26-admin-configuration-tab-design.md)

## Before Task 1

`feat/admin-tabs-model` (tabs, Model tab, migration 014) and `feat/forgeci-deploy` (deployment files) must both be merged into `main`. Branch from that `main`. Code names below were read from `feat/admin-tabs-model` at `c2dbe9b`; if it changed, follow the merged code.

## Global Constraints

- Editable names, and no others: `SETUHAUL_MODEL_PROVIDER`, `SETUHAUL_MODEL_ID`, `SETUHAUL_MODEL_API_KEY`, `SETUHAUL_OPENAI_REASONING_EFFORT`, `SETUHAUL_MODEL_PRICE_IN_PER_MTOK`, `SETUHAUL_MODEL_PRICE_OUT_PER_MTOK`, `SETUHAUL_LANGSMITH_ENABLED`, `SETUHAUL_LANGSMITH`, `LANGSMITH_API_KEY`, `LANGSMITH_PROJECT`, `SETUHAUL_LANGSMITH_HIDE_TEXT`, `SETUHAUL_TELEGRAM_BOT_TOKEN`, `SETUHAUL_TELEGRAM_WEBHOOK_SECRET`.
- Secret names (encrypted): `SETUHAUL_MODEL_API_KEY`, `LANGSMITH_API_KEY`, `SETUHAUL_TELEGRAM_BOT_TOKEN`, `SETUHAUL_TELEGRAM_WEBHOOK_SECRET`.
- A secret is never returned by an API, put in a page, logged or audited. Shown only as "saved, ends in 1a2b" (the webhook secret: "saved", no characters).
- Base URL and LangSmith endpoint are not editable.
- A saved secret that cannot be decrypted is never replaced by the `.env` one.
- A broken saved setting never stops start-up.
- Routes: `require_role("ADMIN")`; the existing same-origin check applies to every POST.
- Tests use no network. Gates after every task: `python -m pytest -q`, `python -m ruff check .`, `python -m mypy src`.
- Add each new route to `ROLE_GUARDED` in `tests/test_auth_role_matrix.py` with `ADMIN`, in the task that adds it.

## Review Focus

1. A key pasted with spaces or a trailing newline: saved stripped (test in Task 5).
2. `SETUHAUL_SETTINGS_KEY` malformed: service starts, tab says it is invalid, secret fields disabled (Tasks 1, 7).
3. Database not yet migrated (no `app_settings` table): `.env` values are used, nothing crashes (Task 1).
4. Price of `abc`, `-1`, `nan` or `inf`: refused with 422, nothing saved (Task 5).
5. Telegram reset or unreadable while notices are queued: they stay queued, none marked failed (Task 4).

---

### Task 1: Saved settings and encryption

**Files:**
- Create: `migrations/015_app_settings.sql`, `src/setuhaul/app_settings.py`, `tests/test_app_settings.py`
- Modify: `pyproject.toml`, `docs/requirements-lock.txt`

**Interfaces:**
- Produces:
  - `EDITABLE: frozenset[str]`, `SECRETS: frozenset[str]`, `SECTIONS: dict[str, tuple[str, ...]]` with keys `"model"`, `"langsmith"`, `"telegram"`
  - `master_key_problem(env: Mapping[str, str]) -> str | None` returns `"missing"`, `"invalid"` or `None`
  - `save(conn, values: Mapping[str, str], *, env: Mapping[str, str], updated_by: str, now: datetime) -> None` (caller owns the transaction)
  - `reset(conn, names: Iterable[str]) -> None`
  - `effective(conn, env: Mapping[str, str]) -> Effective` where `Effective(env: dict[str, str], source: dict[str, str], unreadable: frozenset[str])`; `source[name]` is `"saved"`, `"env"` or `"unset"`
- Table: `app_settings(name text primary key, value text not null, encrypted integer not null, updated_by text not null, updated_at text not null)`

- [ ] **Step 1: Add the dependency.** `pip install cryptography`, pin the installed version as `cryptography==X.Y.Z` in `pyproject.toml` and add it and its new dependencies to `docs/requirements-lock.txt`.
- [ ] **Step 2: Write failing tests** (database from `make_working_db(tmp_path / "s.sqlite3")`, `KEY = Fernet.generate_key().decode()`):
  - `test_a_saved_value_wins_and_reset_returns_to_env`: save `SETUHAUL_MODEL_ID="b"` over env `"a"`: effective `"b"`, source `"saved"`; after `reset`: `"a"`, `"env"`.
  - `test_a_secret_is_not_in_the_database_in_clear`: save `SETUHAUL_MODEL_API_KEY="sk-live-1234"`; `b"sk-live-1234"` not in the bytes of the database file or its `-wal`.
  - `test_an_unreadable_secret_is_empty_not_env`: save with `KEY`, read with another key and env key `"sk-env"`: effective value `""`, name in `unreadable`.
  - `test_master_key_problems`: `{}` gives `"missing"`, `"abc"` gives `"invalid"`; saving a secret in either case raises `ValueError`; saving `SETUHAUL_MODEL_ID` succeeds.
  - `test_a_name_that_is_not_editable_is_refused`: `SETUHAUL_DB_PATH` raises `ValueError`; a row for it inserted by hand is ignored by `effective`.
  - `test_without_the_table_env_is_used`: drop the table; `effective(...).env` equals the input.
  - `test_a_saved_empty_value_overrides_env`: saved `SETUHAUL_OPENAI_REASONING_EFFORT=""` over `"none"` gives `""`.
- [ ] **Step 3: Run** `python -m pytest -q tests/test_app_settings.py`. Expected: FAIL (no module).
- [ ] **Step 4: Implement** the migration and module.
- [ ] **Step 5: Run the gates.** Expected: all pass.
- [ ] **Step 6: Commit** `feat: store editable settings, secrets encrypted`.

### Task 2: Model settings apply from the next message

**Files:**
- Create: `src/setuhaul/runtime_settings.py`, `tests/test_runtime_model.py`
- Modify: `src/setuhaul/model_gateway.py` (`load_model_policy`, `build_gateway`, `OpenAIAdapter`), `src/setuhaul/api.py` (`AppDeps`, the turn, `create_app_from_env`), `src/setuhaul/auth/pages.py` (`_model_in_use`, `_model_limits`, `_model_section`)

**Interfaces:**
- Consumes: `app_settings.effective`.
- Produces:
  - `load_model_policy(env: Mapping[str, str] | None = None) -> ModelPolicy`
  - `build_gateway(policy, env: Mapping[str, str] | None = None) -> ModelGateway`
  - `OpenAIAdapter(..., reasoning_effort: str | None = None)`; `None` keeps reading the environment
  - `class SettingsSource: __init__(self, base_env: Mapping[str, str], stub: bool = False)`; `effective(conn) -> Effective`; `model(conn) -> tuple[ModelGateway, ModelPolicy]`
  - `AppDeps.settings: SettingsSource | None = None`
  - `model_in_force(deps, conn) -> tuple[ModelGateway, ModelPolicy]` in `api.py`: from `deps.settings`, else `(deps.gateway_factory(), deps.policy)`

`model()` returns the stub when `stub`, `UnconfiguredGateway()` when there is no provider, the key is unreadable or the provider is unknown. The saved key is used only with the provider in force (Task 5 enforces how it is saved). `UnconfiguredGateway` stays importable without a cycle.

- [ ] **Step 1: Write failing tests**, using the `env` fixture style of `tests/test_api.py` and an adapter transport stub:
  - `test_a_saved_model_id_is_used_by_the_next_message`: post a message, save a new `SETUHAUL_MODEL_ID`, post another; the two captured request bodies name the old then the new model, with the same app object.
  - `test_saved_key_prices_and_reasoning_effort_reach_the_call`
  - `test_an_unreadable_key_takes_the_safe_outage_path`: reply status is the same as with no model configured; no request is made.
  - `test_the_model_tab_shows_what_is_in_force`: after saving, `/admin` contains the new model ID and price.
  - `test_without_a_settings_source_nothing_changes`
- [ ] **Step 2: Run** them. Expected: FAIL.
- [ ] **Step 3: Implement.** `create_app_from_env` always builds `SettingsSource(os.environ, stub=...)`; the start-up values it still passes (`policy`, `gateway_factory`) remain the fallback. A bad provider in `.env` must not raise at start-up.
- [ ] **Step 4: Run the gates.** Expected: all pass.
- [ ] **Step 5: Commit** `feat: apply saved model settings without a restart`.

### Task 3: LangSmith follows the saved settings

**Files:**
- Modify: `src/setuhaul/conversation.py` (`ConversationServices`), `src/setuhaul/model_gateway.py` (`run_bounded_loop`), `src/setuhaul/api.py`, `src/setuhaul/auth/admin_routes.py` (LangSmith route), `src/setuhaul/auth/pages.py`
- Test: `tests/test_runtime_langsmith.py`

**Interfaces:**
- Produces: `ConversationServices.trace_env: Mapping[str, str] | None = None`; `run_bounded_loop(..., trace_env: Mapping[str, str] | None = None)` passes it to `build_tracer`. `load_summary(env)` and `tracing_status(env)` already take a mapping.

- [ ] **Step 1: Write failing tests:**
  - `test_the_turn_traces_with_the_settings_in_force`: replace `build_tracer` with a recorder returning `None`; after saving on, key and project, the recorded env has them.
  - `test_saved_off_overrides_env_on`: env fully on, saved `SETUHAUL_LANGSMITH_ENABLED="false"`: `tracing_status(effective).enabled is False`.
  - `test_the_panel_and_limits_use_the_settings_in_force` (route with `load_summary` replaced).
- [ ] **Step 2: Run** them. Expected: FAIL.
- [ ] **Step 3: Implement.**
- [ ] **Step 4: Run the gates**, including `tests/test_langsmith_trace.py`. Expected: all pass.
- [ ] **Step 5: Commit** `feat: LangSmith follows saved settings`.

### Task 4: Telegram follows the saved settings

**Files:**
- Modify: `src/setuhaul/runtime_settings.py`, `src/setuhaul/auth/telegram_routes.py`, `src/setuhaul/jobs/send_driver_notifications.py`, `src/setuhaul/api.py` (lifespan, `create_app_from_env`)
- Test: `tests/test_runtime_telegram.py`

**Interfaces:**
- Produces:
  - `SettingsSource(..., telegram_factory: Callable[[str], TelegramClient] | None = None)`; `telegram(conn) -> tuple[bytes, TelegramClient | None]`: `(b"", None)` unless both values are readable and the secret has at least `MIN_TELEGRAM_SECRET_LEN` (16) characters; one client cached per token
  - `telegram_in_force(deps) -> tuple[bytes, TelegramClient | None]`
  - `DriverNoticeRunner(..., client_source: Callable[[], TelegramClient | None] | None = None)`; with a source, `client` may be `None`, and a pass with no client returns `None` without touching the queue

With `deps.settings`, the lifespan always starts the maintenance and notice runners.

- [ ] **Step 1: Write failing tests** (`FakeTelegramClient` through `telegram_factory`):
  - `test_a_bot_saved_after_start_up_receives_the_webhook`: 503 `TELEGRAM_NOT_CONFIGURED` before saving; 401 for a wrong secret and 200 for the saved one after.
  - `test_replies_and_queued_notices_use_the_new_bot`
  - `test_queued_notices_wait_while_there_is_no_bot`: after `run_once()` with no client, the notice rows are unchanged.
  - `test_without_a_settings_source_nothing_changes`
- [ ] **Step 2: Run** them. Expected: FAIL.
- [ ] **Step 3: Implement.** The webhook route reads the secret and client once per request.
- [ ] **Step 4: Run the gates.** Expected: all pass.
- [ ] **Step 5: Commit** `feat: Telegram follows saved settings`.

### Task 5: Admin API to read, save and reset

**Files:**
- Create: `src/setuhaul/auth/settings_routes.py` (`register_settings_routes(app: FastAPI, deps: AppDeps) -> None`), `tests/test_settings_api.py`
- Modify: `src/setuhaul/api.py` (register), `tests/test_auth_role_matrix.py`

**Interfaces:**
- Routes: `GET /v1/admin/settings`; `POST /v1/admin/settings/{section}`; `POST /v1/admin/settings/{section}/reset`.
- Bodies (all fields strings, `extra="forbid"`; an omitted field is left as it is):
  - model: `provider` (`anthropic`|`openai`), `model_id` (1 to 200), `api_key`, `reasoning_effort` (0 to 20), `price_in`, `price_out` (finite, 0 or more)
  - langsmith: `enabled` (`on`|`off`), `hide_text` (`on`|`off`), `api_key`, `project` (1 to 100), `confirm`
  - telegram: `bot_token`, `webhook_secret` (16 to 256 of `A-Za-z0-9_-`), `generate_secret` (`yes`)
- GET returns, per name: `source`, `value` for ordinary settings, `set` and `last4` for keys; plus `master_key` (`ok`|`missing`|`invalid`), `unreadable`, `live` (false when `deps.settings` is `None`).
- Refusals: 409 `SETTINGS_NOT_AVAILABLE` (no source), 422 `INVALID_REQUEST`, 422 `KEY_REQUIRED_FOR_PROVIDER`, 422 `SETTINGS_KEY_UNAVAILABLE`, 422 `CONFIRMATION_REQUIRED`, 422 `TELEGRAM_PAIR_REQUIRED`, 404 unknown section.
- Rules:
  - Changing provider needs `api_key` in the same save unless the new provider is the one in the base environment, in which case the saved key is removed.
  - `enabled="on"` needs `confirm="SEND_TO_LANGSMITH"`; it saves `SETUHAUL_LANGSMITH="approved"` and `..._ENABLED="true"`. `off` saves `..._ENABLED="false"`. The first save of the section saves `HIDE_TEXT="1"` unless `hide_text="off"`.
  - Telegram needs both a token and a secret in force after the save; `generate_secret` uses `secrets.token_urlsafe(32)`.
- Audit: `SETTINGS_SAVE` / `SETTINGS_RESET`, `target_type="settings"`, `target_id=section`; detail maps name to `{"from": old, "to": new}` for ordinary settings and to `"set"`, `"changed"` or `"removed"` for keys. Save and audit in one `immediate(conn)` transaction.

- [ ] **Step 1: Write failing tests**, signing in with `tests/auth_helpers.py`:
  - `test_only_an_admin_from_this_site_can_read_or_change` (anonymous 401, warehouse 403, other `Origin` 403)
  - `test_no_secret_comes_back_or_reaches_the_audit_log`: save all four secrets; none of the strings occurs in any response, `audit_log` row or `caplog.text`.
  - `test_a_pasted_key_is_stripped`: `"  sk-x \n"` is in force as `"sk-x"`.
  - `test_bad_prices_are_refused` parametrised `abc`, `-1`, `nan`, `inf`: 422, nothing saved.
  - `test_provider_change_rules` (both branches)
  - `test_langsmith_needs_confirmation_and_hides_text_by_default`
  - `test_telegram_pair_and_generated_secret`
  - `test_without_a_master_key_ordinary_settings_still_save`
  - `test_reset_restores_env`
- [ ] **Step 2: Run** them. Expected: FAIL (404).
- [ ] **Step 3: Implement**, following `with_conn` in `admin_routes.py`. Errors must not echo the request body.
- [ ] **Step 4: Run the gates.** Expected: all pass.
- [ ] **Step 5: Commit** `feat: admin API for settings`.

### Task 6: Test connection, test bot, register webhook

**Files:**
- Create: `src/setuhaul/settings_checks.py`, `tests/test_settings_checks.py`
- Modify: `src/setuhaul/auth/settings_routes.py`, `src/setuhaul/app_settings.py`, `tests/test_auth_role_matrix.py`

**Interfaces:**
- `check_model(env: Mapping[str, str], transport: httpx.BaseTransport | None = None) -> tuple[bool, str]`: one call with the single message "Reply with the word OK." and no tools.
- `check_bot(token: str, http: httpx.Client) -> tuple[bool, str]`: `getMe`; the message is the bot's username.
- `register_webhook(token: str, secret: str, public_url: str, http: httpx.Client) -> tuple[bool, str]`: `setWebhook` with `url=<public_url>/v1/telegram/webhook` and `secret_token`, then `getWebhookInfo`; message gives address, waiting updates, last error.
- Routes: `POST /v1/admin/settings/model/test` (model body; blank fields mean the value in force), `POST .../telegram/test` (`bot_token` optional), `POST .../telegram/register-webhook`. Reply `{"ok": bool, "message": str}`.
- `app_settings.webhook_fingerprint(token, secret, url) -> str` (SHA-256), stored on success as the non-editable row `telegram_webhook_registered`; GET gains `webhook`: `registered`, `needs_registering` or `no_public_url`.

A provider change in the test body follows the key rule of Task 5: a saved key is never sent to the other provider. Audit actions `SETTINGS_TEST` and `TELEGRAM_WEBHOOK_REGISTER`.

- [ ] **Step 1: Write failing tests** with `httpx.MockTransport`:
  - `test_checks_save_nothing`
  - `test_provider_errors_are_shown_without_the_key` (a 401 body that echoes the key)
  - `test_telegram_errors_never_contain_the_token` (transport raising `ConnectError` with the URL)
  - `test_a_saved_key_is_not_sent_to_another_provider`
  - `test_register_uses_the_public_url_and_marks_it_registered`; changing the secret turns it to `needs_registering`
  - `test_register_without_a_public_url_is_refused`: 422 `PUBLIC_URL_NOT_SET`
- [ ] **Step 2: Run** them. Expected: FAIL.
- [ ] **Step 3: Implement.** Report exception class names only for transport errors, and replace any occurrence of a secret in a provider message before returning it.
- [ ] **Step 4: Run the gates.** Expected: all pass.
- [ ] **Step 5: Commit** `feat: test connection and register the webhook from admin`.

### Task 7: Configuration tab and documentation

**Files:**
- Create: `src/setuhaul/auth/assets/settings.js`, `tests/test_settings_page.py`
- Modify: `src/setuhaul/auth/pages.py` (`ASSET_TYPES`, `render_admin`, new `_configuration_section(conn, deps) -> str`), `.env.example`, `docker-compose.yml` (comment), `docs/deployment-hostinger.md`, `docs/running-and-demonstrating.md`, `tests/test_auth_pages.py`

**Screen** (tab `("config", "Configuration")` after Model): three blocks, Model, LangSmith, Telegram, as drawn in the spec. Each value shows its source: `saved here`, `.env` or `not set`. On/off settings are `<select>` elements, because `common.js` submits a checkbox whether or not it is ticked. Key fields are `type="password"`, always empty, with "saved, ends in XXXX" beside them.

**Copy:** LangSmith confirm: "Conversation data will be sent to LangSmith from this server. Continue?". Missing master key: "Keys cannot be saved: SETUHAUL_SETTINGS_KEY is not set in .env." Invalid: "... is not a valid key." Unreadable: "A saved key cannot be read. Enter it again or reset to .env." No source: "Changes are for the live service. This demo reads .env." Different bot note: "Each driver has to open a new bot and press Start before it can message them."

- [ ] **Step 1: Write failing tests:** `test_admin_sees_the_tab_with_sources`; `test_no_secret_is_in_the_page`; `test_key_fields_are_disabled_with_a_bad_master_key`; `test_demo_shows_the_tab_read_only`; `test_settings_js_is_served`; and update the five-tab assertion to six.
- [ ] **Step 2: Run** them. Expected: FAIL.
- [ ] **Step 3: Implement** the section and `settings.js` (test buttons show the message with `textContent`; LangSmith save asks for confirmation and adds `confirm`; Generate sends `generate_secret`).
- [ ] **Step 4: Document** `SETUHAUL_SETTINGS_KEY`, generated with `python -c "from cryptography.fernet import Fernet; print(Fernet.generate_key().decode())"`; saved settings override `.env`; losing the key means re-entering keys; backups hold both.
- [ ] **Step 5: Run the gates**, then build the image and check it starts.
- [ ] **Step 6: Check in a browser** on localhost with test values: save and reset each section, reload stays on the tab, no key visible in page source or network responses.
- [ ] **Step 7: Commit** `feat: Configuration tab on the admin screen`.
