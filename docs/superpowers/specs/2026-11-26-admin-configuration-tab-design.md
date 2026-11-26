# Admin Configuration tab

Date: 2026-11-26. Status: approved 2026-11-26. Size: a project.

## Intent

On the live server, changing the model ID or a key means logging in over SSH, editing `.env` and restarting. An admin should be able
to change model, LangSmith and Telegram settings from the Admin screen, with effect from the next driver message.

## Goals

- A **Configuration** tab on the Admin screen, for admins only.
- Editable: model provider, model ID, model API key, reasoning effort, input and output price; LangSmith on or off, API key, project,
  hide message text; Telegram bot token and webhook secret, with webhook registration from the screen.
- API keys, the bot token and the webhook secret stored encrypted. A saved value overrides `.env`; **Reset** returns to `.env`.
- A change applies from the next driver message, with no restart.

## Non-goals and anti-goals

- Not editable: database, Redis, public URL, session secret, Google sign-in, partner mail, signed-channel webhook secret and coordinator tokens.
- Not editable: the provider base URL and the LangSmith endpoint. Changing an address could send a saved key somewhere else.
- A saved key is never shown, returned by an API, logged or audited. Only "saved, ends in 1a2b".
- A broken saved setting must never stop the service starting, or silently use a different key from the one chosen.
- The model limits (timeout, retries, cost budget and so on) stay fixed.
- No master-key rotation, change history screen or second password prompt.

## Constraints

- Builds on `feat/admin-tabs-model` (tabs, Model tab, LangSmith panel, migration 014). Start after it is merged into `main`.
- One application process with SQLite, as deployed.
- Admin changes already pass the same-origin check; these routes use it and `require_role("ADMIN")`.

## Approaches

| Approach | Trade-off | Decision |
|---|---|---|
| A. Database table, keys encrypted with a master key in `.env` | keys unreadable in the database, easy to audit; one new library, and a lost master key means re-entering keys | **Chosen** |
| B. Settings file on the data volume | less work; keys in plain text, including in backups | Rejected |
| C. Admin rewrites the server's `.env` | the container cannot reach it without extra access, and still needs a restart | Ruled out |

## Design

**Storage.** Migration `015_app_settings.sql`: a table with one row per saved setting (name, value, whether it is encrypted, who
changed it, when). Keys are encrypted with `cryptography` (Fernet), a new pinned dependency. The master key is
`SETUHAUL_SETTINGS_KEY` in `.env`, set once.

**Effective settings.** One function returns, for each editable setting, the saved value if there is one and otherwise the
environment value. Every reader uses it: the model gateway and policy for each message, the Model tab ("in use", limits and prices),
the LangSmith tracer and the LangSmith panel. Today these read start-up values or the environment directly.

**Rules.**

- Changing the provider needs a key for that provider, entered in the same save, unless it is the provider named in `.env`.
- A saved key belongs to the provider it was saved for and is never sent to the other one.
- Without a valid master key the other settings can still be edited; key fields are disabled and the tab says why.
- If a saved key cannot be decrypted, the model is treated as unavailable (drivers get the existing safe hand-off) and LangSmith is
  off. The tab says so. There is no fall back to the `.env` key.

**Test connection.** Sends one fixed harmless prompt with the values in the form (a blank key field means the key in force) and shows
success or the provider's error without any key. Nothing is saved, and no driver data is sent. Saving does not require a test.

**LangSmith.** Switching it on asks the admin to confirm that conversation data will be sent to LangSmith. The confirmed save counts as
the owner's approval (`SETUHAUL_LANGSMITH=approved` and `..._ENABLED=true`). "Hide message text" starts ticked. Switching it off
overrides an `.env` that has it on.

**Telegram.** Bot token and webhook secret, saved together (the route stays off unless both are usable; the secret needs at least 16
characters from `A-Z a-z 0-9 _ -`). **Generate** makes a secret on the server, so nobody has to see or type one.

- **Test bot** asks Telegram who the token belongs to and shows the bot's username. Nothing is saved.
- **Register webhook** tells Telegram to call `<SETUHAUL_PUBLIC_URL>/v1/telegram/webhook` with the saved secret, then shows Telegram's
  view of it (address, waiting updates, last error). It is disabled when the public URL is not set. This replaces the manual command.
- A save does not register by itself. After changing the token or secret the tab says the webhook needs registering.
- Today the sender of queued driver notices starts only if a bot is configured at start-up. It will always run and use the bot in force,
  doing nothing when there is none. The webhook route likewise reads the secret in force.
- Saving empty values is not possible; **Reset to .env** removes the saved pair.

**Audit.** Each save, reset, test and webhook registration records who and which settings. Old and new values are recorded for ordinary settings only;
for keys, only "set", "changed" or "removed".

**Screen.**

```
Users | Pending drivers | Onboarded drivers | Audit log | Model | [Configuration]

Model                                        from
  Provider        [ openai        v ]        saved here
  Model ID        [ ................ ]       saved here
  API key         saved, ends 1a2b  [Replace]
  Reasoning       [ none ]                   .env
  Price in / out  [      ] [      ]          not set
  [Test connection]  [Save]  [Reset to .env]

LangSmith
  [ ] Send traces to LangSmith     [x] Hide message text
  API key / Project ...
  [Save]  [Reset to .env]

Telegram
  Bot token       saved, ends 9xQ2  [Replace]
  Webhook secret  saved             [Generate]
  Webhook         registered, no errors
  [Test bot]  [Save]  [Register webhook]  [Reset to .env]
```

Each section saves and resets on its own.

## Limits to know

- The server backup copies both the database and `.env`, so a full backup holds the keys and the means to decrypt them. Encryption
  protects against a leaked database, not a leaked backup.
- Anyone with an admin account can replace the keys or turn tracing on. Both are audited.
- Switching to a different bot: each driver has to open the new bot and press Start before it can message them.
- The web demo is not deployed; it keeps reading `.env`, and the tab there says changes are for the live service.

## Tests

Saved value wins and reset restores `.env`; the next message uses the new model without a restart; no key in any response, page, log or
audit row; database holds no plain key; missing or wrong master key; provider change rules; non-admin and cross-site refused; LangSmith
confirm, off overriding `.env`; test connection and test bot save nothing; a bot saved after start-up receives the webhook and sends
queued notices (stand-in client, no network); webhook registration uses the public URL and shows errors without the token; existing behaviour unchanged with an empty table. Tab checked in a browser.

Also update `.env.example`, `docker-compose` notes and the deployment guide with `SETUHAUL_SETTINGS_KEY`.

## Calls I made (check these)

- Provider base URL and LangSmith endpoint stay in `.env`.
- No fall back to `.env` when a saved key cannot be read.
- Saving is allowed without a passing test.
- No second password prompt before changing a key.
- Registering the webhook is a separate button, not automatic on save.

## Left to the builder

Module names, exact table columns, form validation limits and page layout details.
