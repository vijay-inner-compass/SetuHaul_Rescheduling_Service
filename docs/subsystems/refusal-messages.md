---
title: Refusal messages
type: subsystem
created: 2026-11-27
updated: 2026-11-27
sources:
  - code: src/setuhaul/auth/refusals.py
  - code: src/setuhaul/api.py
  - code: src/setuhaul/auth/shipments.py
  - code: src/setuhaul/auth/shipment_routes.py
  - code: src/setuhaul/auth/assets/common.js
verified_at: b5b462f
---

# Refusal messages

How a refused request tells the user what to fix. A refusal carries a `reason_code`, and may carry a `message`
and `problems` (a list of `field` and `problem`).

## Where the words come from

- Malformed bodies: the handler at `src/setuhaul/api.py:439` turns validation errors into `problems` through
  `src/setuhaul/auth/refusals.py:45`.
- Hand-written checks raise `InvalidRequest` (`src/setuhaul/auth/refusals.py:28`). The route that catches it
  must add `details_of(exc)` to its answer.
- Date rules of "Open availability": `src/setuhaul/auth/shipments.py:72`, one sentence per mistake.
- The page: `src/setuhaul/auth/assets/common.js:115` names fields by their on-screen labels when it is given the
  form. Otherwise it shows the server's `message`, then its own sentence for the code.

> ⚠️ Never repeat anything the caller sent in a message: values may be passwords or tokens, and unknown field
> names are caller-chosen too. Word problems from the error type and our own limits, not from pydantic's text.

> ⚠️ A route that rejects with only `exc.code` silently drops the field names. A new route that can raise
> `InvalidRequest` must pass `details_of(exc)`.

> ⚠️ A script that calls `window.SH.reason` must pass the form as the third argument, or the user sees names
> derived from the field instead of the screen's labels.

## Not covered

The password set and forgot routes and the signed channel routes (driver messages, GPS, Telegram) answer with
the code alone.
