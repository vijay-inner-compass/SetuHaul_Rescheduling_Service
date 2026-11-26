# Admin Tabs and Model Governance Implementation Plan

> **For agentic workers:** REQUIRED SUB-SKILL: Use superpowers:subagent-driven-development (recommended) or superpowers:executing-plans to implement this plan task-by-task. Steps use checkbox (`- [ ]`) syntax for tracking.

**Goal:** Split the Admin screen into tabs and add a report-only Model tab fed by per-call records, per-message records and LangSmith.

**Architecture:** A recorder wraps the model gateway at the single place the conversation obtains it (`_process_trusted` in `api.py`, shared by webhook, Telegram and demo) and saves rows after the turn. The Model tab is server-rendered from those rows; the LangSmith panel is a separate admin JSON route loaded on demand.

**Tech Stack:** Python 3.12, FastAPI, SQLite migrations, plain JavaScript, `langsmith==0.15.0` (already installed), pytest.

**Spec:** `docs/superpowers/specs/2026-11-26-admin-tabs-model-governance-design.md`

## Global Constraints

- Branch `feat/admin-tabs-model`. Run tools as `.\.venv\Scripts\python -m pytest|ruff check .|mypy src`.
- Report only: no screen action changes a limit, price, model or tracing setting.
- The new tables and the tab hold no message text, prompt, driver id or API key.
- Recording never fails a driver's message and never changes the exception the conversation sees.
- No cost is shown as 0 when the price is not set: store NULL, show `price not set`.
- Tabs, in order: Users, Pending drivers, Onboarded drivers, Audit log, Model (keys `users`, `pending`, `decided`, `audit`, `model`).
- CSP stays: no inline script or style; no `innerHTML` and the other banned calls in `test_js_assets_do_not_use_innerhtml`. Escape all text with `esc`.
- No new dependency. Comment the code line by line, as the surrounding files do.

## Review Focus

1. A working database without migration 014 (an existing Telegram or service database): messages still get replies, and the Model tab says to upgrade; the rest of Admin works. (Task 2, Task 3)
2. Saving the rows fails (database locked): the reply is unchanged. (Task 2)
3. The turn raises after calling the model: calls are kept with turn status `ERROR`, and the exception still propagates. (Task 2)
4. LangSmith is slow, refuses the key or errors: a short message, with no key or error text on screen. (Task 4)
5. The demo clock is frozen in August 2026 while LangSmith stamps real time: the LangSmith window must use real time. (Task 4)

---

### Task 1: Tabs on the Admin screen

**Files:**
- Create: `src/setuhaul/auth/assets/tabs.js`
- Modify: `src/setuhaul/auth/assets/warehouse.js` (remove the `// Tabs:` block), `admin.js`, `src/setuhaul/auth/pages.py` (`ASSET_TYPES`, admin sections, `render_admin`, `render_warehouse`)
- Test: `tests/test_auth_pages.py`, `tests/test_live_chat.py`

**Interfaces:**
- Produces: `tabs.js` acts on any page with `.tabs a[data-tab]` and `[data-panel]`; the default is the first tab; panels are looked up inside `show()`. `pages._tabs(items: tuple[tuple[str, str], ...]) -> str` renders the `<nav class="tabs" aria-label="Sections">`.

- [ ] **Step 1: Write the failing tests**

```python
def test_admin_page_has_one_panel_per_tab(world: World) -> None:
    text = world.client("admin").get("/admin").text
    keys = ["users", "pending", "decided", "audit"]
    assert re.findall(r'data-tab="(\w+)"', text) == keys
    assert re.findall(r'<section id="\w+" data-panel="(\w+)"', text) == keys
    assert text.index("/app/assets/tabs.js") < text.index("/app/assets/admin.js")
    assert 'action="/admin#audit"' in text
```

In `test_live_chat.py`, point `test_tabs_still_hide_a_section_the_live_refresh_replaced` at `tabs.js` (assert on the whole file, same ordering assertion). Add `tabs.js` to `ASSETS` and to the `types` dict in `test_auth_pages.py`.

- [ ] **Step 2:** Run `pytest tests/test_auth_pages.py tests/test_live_chat.py -q`. Expected: FAIL (no `data-tab` on admin, no `tabs.js`).
- [ ] **Step 3:** Move the tab block into `tabs.js` (default = first tab's `data-tab`). Load it before `warehouse.js` / `admin.js`. Give the four admin sections `data-panel`, wrap the admin body in `<div class="wh">` so the existing tab styles apply. Form action and the **Clear filters** link become `/admin#audit`.
- [ ] **Step 4:** Run the same command. Expected: PASS.
- [ ] **Step 5:** Commit `feat: tabs on the admin screen`.

### Task 2: Record model calls and messages

**Files:**
- Create: `migrations/014_model_calls.sql`, `src/setuhaul/model_usage.py`, `tests/test_model_usage.py`
- Modify: `src/setuhaul/api.py:476-505` (`_process_trusted`)

**Interfaces:**
- Produces tables:
  - `model_turns(turn_id TEXT PRIMARY KEY, ts TEXT, ts_epoch REAL, duration_ms REAL, status TEXT, reason_code TEXT)`
  - `model_calls(call_id TEXT PRIMARY KEY, turn_id TEXT REFERENCES model_turns, ts TEXT, ts_epoch REAL, provider TEXT, model_id TEXT, duration_ms REAL, outcome TEXT, input_tokens INTEGER, output_tokens INTEGER, cost_usd REAL NULL)`, `outcome` checked against `OK, TIMEOUT, TRANSIENT, REFUSED, NOT_CONFIGURED, ERROR`; both tables indexed on `ts_epoch`.
- Produces:
  - `describe_model(gateway: ModelGateway, policy: ModelPolicy) -> tuple[str, str]`: `StubGateway` gives `("stand-in", "scripted stand-in")`, `UnconfiguredGateway` gives `("", "")`, otherwise `(policy.provider, policy.model_id)`.
  - `TurnRecorder(clock: Clock, timer: Callable[[], float] = time.perf_counter)` with `wrap(gateway: ModelGateway) -> ModelGateway` and `save(conn: sqlite3.Connection, status: str, reason_code: str | None) -> None`.
- `save` writes one turn and its calls inside `immediate(conn)`, writes nothing if there were no calls, and catches `sqlite3.Error`, logging `model_usage.save_failed`.

- [ ] **Step 1: Write the failing tests** (unit tests use `ScriptedGateway` and `complete_with_retry`; end-to-end ones use `build_demo_app(tmp_path, live_factory=StubGateway)` and `_driver(app)`):

```python
def test_a_retry_is_two_rows(db) -> None:
    # steps: ModelTimeout(), ModelResponse(text="x", input_tokens=10, output_tokens=5); default policy
    assert rows(db) == [("TIMEOUT", 0, 0, None), ("OK", 10, 5, None)]  # outcome, in, out, cost

def test_each_error_has_an_outcome_and_is_re_raised(db) -> None:
    # parametrise: TransientModelError->TRANSIENT, PermanentModelError->REFUSED,
    # ModelUnavailable("MODEL_NOT_CONFIGURED")->NOT_CONFIGURED, ValueError->ERROR
    # assert pytest.raises(the same instance) and the stored outcome

def test_cost_is_stored_only_when_priced(db) -> None:
    # price_in_per_mtok=1.0, price_out_per_mtok=2.0, 1_000_000 in and out -> cost_usd == 3.0; unpriced -> None

def test_a_demo_message_records_a_turn_and_its_calls(tmp_path) -> None:
    # send "my truck is 60 minutes late": one turn with status == the reply's status, >=1 calls with that turn_id,
    # provider "stand-in"; no column of either table contains "late" or "DRV006"

def test_reply_survives_a_database_without_the_tables(tmp_path) -> None:
    # drop both tables, send the message: http_status 200 and a normal reply

def test_save_failure_is_swallowed(db) -> None:
    # call save() on a read-only connection: returns None, no exception

def test_a_failing_turn_is_recorded_and_still_raises(tmp_path, monkeypatch) -> None:
    # monkeypatch setuhaul.api.handle_message to call services.gateway.complete once then raise RuntimeError:
    # the request raises, and model_turns has status "ERROR" with one call
```

- [ ] **Step 2:** Run `pytest tests/test_model_usage.py -v`. Expected: FAIL (module missing).
- [ ] **Step 3:** Implement. In `_process_trusted`, create a recorder, pass `gateway=recorder.wrap(deps.gateway_factory())`, and call `save` after `handle_message` succeeds (reply status and `escalation_reason`) and in the `except BaseException` branch (`"ERROR"`, `None`) before re-raising.
- [ ] **Step 4:** Run `pytest tests/test_model_usage.py tests/test_demo_driver.py tests/test_api.py -q`. Expected: PASS.
- [ ] **Step 5:** Commit `feat: record each model call and driver message`.

### Task 3: The Model tab

**Files:**
- Create: `src/setuhaul/model_report.py`, `tests/test_model_tab.py`
- Modify: `src/setuhaul/auth/pages.py` (`_model_section`, `render_admin`, admin route)

**Interfaces:**
- Consumes: the Task 2 tables and `describe_model`.
- Produces:
  - `percentile(values: list[float], p: float) -> float | None`: nearest rank, `sorted[ceil(p*n)-1]`; typical is `p=0.5`, 95th is `p=0.95`.
  - `call_totals(conn, since_epoch: float | None) -> CallTotals(calls, failed, p50_ms, p95_ms, input_tokens, output_tokens, cost_usd: float | None, unpriced: int)`; `failed` is outcome other than `OK`; `unpriced` counts OK calls with NULL cost.
  - `message_totals(conn) -> MessageTotals(messages, p50_ms, p95_ms, avg_calls, avg_input_tokens, avg_output_tokens, avg_cost_usd: float | None)`
  - `recent_turns(conn, limit: int = 50)` and `recent_calls(conn, limit: int = 50)`, newest first (`ts_epoch desc, rowid desc`).
  - `render_admin(conn, me, query, deps: AppDeps) -> str`

**Copy:** `No model configured`, `scripted stand-in`, `price not set`, `(some calls unpriced)`, `No model calls recorded yet.`, `This database has no model records yet. Apply the migrations, then restart.` Health line: `Last call <outcome> at <ts>; <n>% of calls failed`. Limits listed: call timeout, retries, calls per message, tool runs per message, message deadline, cost budget per message, output length, input and output price per million tokens, LangSmith tracing (`tracing_status().reason`).

- [ ] **Step 1: Write the failing tests**

```python
def test_percentile_nearest_rank() -> None:
    assert percentile([], 0.5) is None
    assert percentile([4, 1, 3, 2], 0.5) == 2 and percentile([4, 1, 3, 2], 0.95) == 4

def test_totals_from_known_rows(db) -> None:
    # 3 calls in the last 24 h (one TIMEOUT), 1 older; known durations, tokens, one priced 0.5
    # assert every CallTotals field for since=now-86400 and since=None, and MessageTotals averages

def test_model_tab_empty_state(world: World) -> None:
    text = world.client("admin").get("/admin").text
    assert 'data-tab="model"' in text and "No model calls recorded yet." in text and "No model configured" in text

def test_model_tab_shows_price_not_set_and_never_a_zero_cost(world: World) -> None:
    # insert an OK call with cost NULL: "price not set" in text, "$0" not in text

def test_admin_page_works_without_the_tables(world: World) -> None:
    # drop both tables: status 200, Users still rendered, upgrade message shown

def test_model_names_are_escaped(world: World) -> None:
    # model_id "<script>alert(1)</script>" is escaped
```

Update `test_admin_page_has_one_panel_per_tab` to include `model`. The existing admin-only test covers access.

- [ ] **Step 2:** Run `pytest tests/test_model_tab.py -v`. Expected: FAIL.
- [ ] **Step 3:** Implement. Show times in seconds with one decimal, cost as `$0.0000`. Catch `sqlite3.OperationalError` for missing tables inside `_model_section` only. "Last 24 h" uses `deps.clock.now()`.
- [ ] **Step 4:** Run `pytest tests/test_model_tab.py tests/test_auth_pages.py -q`. Expected: PASS.
- [ ] **Step 5:** Commit `feat: report-only model tab`.

### Task 4: LangSmith panel

**Files:**
- Create: `src/setuhaul/langsmith_report.py`, `src/setuhaul/auth/assets/model.js`, `tests/test_langsmith_report.py`
- Modify: `src/setuhaul/auth/admin_routes.py` (route), `pages.py` (panel markup, `ASSET_TYPES`, scripts), `tests/test_auth_pages.py` (asset lists)

**Interfaces:**
- Consumes: `tracing_status` from `langsmith_trace`, `percentile` from `model_report`.
- Produces:
  - `load_summary(env: Mapping[str, str] | None = None, client_factory: Callable[..., Any] | None = None, now: Callable[[], datetime] = lambda: datetime.now(UTC), timer: Callable[[], float] = time.monotonic) -> dict[str, Any]` returning `{"state": "off"|"ok"|"error", "message": str, "runs": int, "failed": int, "p50_ms": float|None, "p95_ms": float|None, "latest": str|None, "capped": bool}`.
  - `GET /v1/admin/model/langsmith`, `Depends(require_role("ADMIN"))`.

**Values:** client built with `api_key`, optional `api_url`, `timeout_ms=5000`; `list_runs(project_name=status.project, is_root=True, start_time=now()-timedelta(hours=24), select=["start_time", "end_time", "error", "status"], limit=500)`; `capped` when 500 rows came back. Cache an `ok` or `error` result for 60 seconds per (key, project, endpoint). `off` message is `tracing_status().reason`; `error` message is `Could not read from LangSmith (<exception class name>).`

- [ ] **Step 1: Write the failing tests** (stand-in client, no network):

```python
def test_off_builds_no_client() -> None:          # env {} -> state "off", factory never called
def test_summary_from_runs() -> None:             # 3 runs, one with error: runs 3, failed 1, p50/p95, latest
def test_asks_for_no_inputs_or_outputs() -> None: # captured select == the four names; limit 500
def test_window_is_real_time() -> None:           # captured start_time == fixed now - 24 h
def test_errors_are_quiet() -> None:              # list_runs raises TimeoutError("key ls_secret"): state "error", "ls_secret" not in str(result)
def test_cached_for_sixty_seconds() -> None:      # two calls at timer 0 and 59 -> one list_runs; at 61 -> two
def test_route_is_admin_only(world: World) -> None:  # admin 200; wh_a 403; anonymous 401
```

- [ ] **Step 2:** Run `pytest tests/test_langsmith_report.py -v`. Expected: FAIL.
- [ ] **Step 3:** Implement. The panel has a **Load from LangSmith** button and the note: `LangSmith traces the conversation loop, not the provider request: it has no reliable token or cost figures, and a handled model failure can appear as a successful run.` `model.js` fetches on click and writes with `textContent`; on a failed request it says `Could not load. Sign in again.`
- [ ] **Step 4:** Run `pytest tests/test_langsmith_report.py tests/test_auth_pages.py -q`. Expected: PASS.
- [ ] **Step 5:** Commit `feat: LangSmith panel on the model tab`.

### Task 5: Documentation and verification

**Files:**
- Modify: `docs/running-and-demonstrating.md` (sections 3 and 7a), `docs/decision-register.md` (one new row, next free number)

- [ ] **Step 1:** Document the tabs, the Model tab, migration 014 (with the existing upgrade command, needed for older databases including the Telegram one), the price settings and the LangSmith limits.
- [ ] **Step 2:** Run `pytest -q`, `ruff check .`, `mypy src`. Expected: all pass.
- [ ] **Step 3:** Browser check on a spare port with a scratch app (`build_demo_app` + `StubGateway`, admin session, LangSmith variables unset): each tab shows only itself, a driver message appears on the Model tab, **Load from LangSmith** says it is switched off, **Filter** and **Clear filters** stay on Audit log, no console errors.
- [ ] **Step 4:** Commit `docs: admin tabs and model tab`.
