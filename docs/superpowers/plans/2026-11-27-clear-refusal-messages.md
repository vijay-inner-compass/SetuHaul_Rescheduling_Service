# Clear Refusal Messages Implementation Plan

**Goal:** A refused request says which field or date rule is wrong.

**Spec:** `docs/superpowers/specs/2026-11-27-clear-refusal-messages-design.md`

**Execution:** native, in this session ("Yes, build it").

## Constraints
- No new validation rules; reason and status codes unchanged.
- A message never repeats a typed value (dates in `INVALID_DATES` excepted).
- Signed channel routes (driver messages, GPS, Telegram) are not screens and stay as they are.
- Finish with ruff, mypy, `pytest -q`.

## Review Focus
1. An unknown field name from the caller is not echoed back (test).
2. A secret that fails a settings check is not echoed back (test).
3. A body that is not JSON still gets a sentence (test).
4. A field without an on-screen label falls back to its readable name (browser check).

## Task 1: Dates
`shipments.py`: `BadDates(AuthError)` with `.message`, code `INVALID_DATES`; `shipment_routes._message` returns it.
Test: `tests/test_refusal_messages.py::test_each_date_mistake_has_its_own_sentence`.

## Task 2: Fields
New `auth/refusals.py`: `InvalidRequest(AuthError)(*problems: tuple[str, str])`,
`problems_from(errors) -> list[tuple[str, str]]`, `details(exc_or_problems) -> {"message", "problems": [{"field", "problem"}]}`.
Used by the `RequestValidationError` handler (`api.py`), `settings_routes.parse_body`, `onboarding`, `projects`,
and the reject paths in `admin_routes` and `settings_routes`.

## Task 3: Page
`common.js`: `reason(status, data, form)` uses on-screen labels and marks the first bad field; `warehouse.js` passes its form.
