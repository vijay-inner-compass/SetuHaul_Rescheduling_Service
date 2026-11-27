---
title: Model calls and the connection test
type: subsystem
created: 2026-11-27
updated: 2026-11-27
sources:
  - code: src/setuhaul/model_gateway.py
  - code: src/setuhaul/settings_checks.py
verified_at: 4f78e1a
---

# Model calls and the connection test

Driver messages call the model with booking tools through `complete_with_retry` in `src/setuhaul/model_gateway.py`.
A rejected request is `MODEL_PERMANENT_FAILURE` and is not retried. The case and the call record keep only a code;
the provider's reason is in the server log as `model_call.refused`.

The connection test on Admin > Configuration is `check_model` in `src/setuhaul/settings_checks.py`.

> ⚠️ The connection test must send a tool. Some reasoning models answer a plain question but refuse function tools
> unless `SETUHAUL_OPENAI_REASONING_EFFORT` is `none`, so a test without tools passes while every driver message fails.

> ⚠️ The provider's error text goes to the log only, with key-like strings hidden. The model usage tables hold
> numbers and codes by design; do not add message text to them.
