# DOC-19: User Manual & Administrator Manual

| Field | Value |
|---|---|
| **Document ID** | `DOC-19` |
| **Version** | `1.0.0` |
| **Category** | 7 — Guides & Manuals |
| **Repository Commit** | `445c7f965e22e7915238cca5631e68b4462d7b7b` |
| **Validation Status** | Verified against `src/setuhaul/auth/wiki_pages.py`, `pages.py`, `gate_pages.py`, and `README.md` |

---

## 1. Purpose and Scope
This manual provides role-specific operating instructions for **Truck Drivers** (Telegram), **Warehouse Authorities** (`/warehouse`), **Gate Security Guards** (`/gate`), and **System Administrators** (`/admin`). In-app help is also available to all signed-in staff at `/wiki` (`[Verified]`).

## 2. Executive Summary
Each operational role has a dedicated, server-enforced surface: drivers interact exclusively via Telegram (no web login), warehouse staff manage dock schedules/exceptions/shipments for their assigned facilities, gate guards validate passes and check trucks in/out, and administrators manage users, drivers, audit logs, model telemetry, and runtime configuration (`[Verified]`).

---

## 3. User Manual — Truck Drivers (`ACT-01`, Telegram) `[Verified]`
1. **First-Time Binding**: Open the SetuHaul Telegram bot in a private chat and tap **Share my phone number** (`request_contact` button). Forwarded contacts are rejected. Your phone number must match an approved, active driver profile with a valid licence.
2. **Confirming a New Shipment (Dock Access Pass)**: When a warehouse assigns a new shipment, the bot sends a `DAP-...` code. Reply with that code once to unlock the shipment in chat (5 wrong guesses lock the shipment and alert operations). Keep the `DAP-...` code ready for the security gate.
3. **Reporting a Delay, Breakdown, or Early Arrival**: Message the bot in English, Hindi, or Hinglish (e.g., *"Running 60 mins late"* or *"Tyre puncture, 45 min repair + 30 min drive"*).
   - If a feasible slot is available, the bot **automatically holds** the best slot for **3 minutes** and asks you to reply **Yes** to confirm or **No** for another option.
   - Replying **Yes** confirms the new slot and cancels your old slot.
   - Replying **No** releases the hold and holds the next feasible slot.
4. **Cancelling an Appointment**: Ask to cancel; the bot previews the cancellation and asks for confirmation. Reply **Yes** within 10 minutes. Cancellations within 120 minutes of slot start or with consignee penalties are handed to operations.
5. **Changing Facility**: Ask to move to another facility. The destination warehouse must approve; if approved, confirm the held slot within 15 minutes or your shipment reverts to the original facility.

---

## 4. User Manual — Warehouse Authority (`ACT-02`, `/warehouse`) `[Verified]`
Sign in at `/` via Google or invited Email & Password. You see only your assigned facilities.
1. **Dock Board (`#board`)**: Select facility and date. Click any booked/held slot to inspect details or **Free this slot** (only future, non-unloading slots). The board refreshes automatically every 3 seconds.
2. **Round-the-Clock 14-Day Slots**: Expand **Round-the-clock slots**, enter your facility's crew count ($\ge 1$), and save. The system automatically maintains 1-hour slots and labour windows 14 days ahead.
3. **Blocking a Dock**: Expand **Block a dock**, choose dock/type/window, and click **Preview** to review priority-ordered booking moves/frees. Click **Confirm block** to commit (re-preview if `PLAN_CHANGED` appears). Click **Reopen** when the dock returns to service.
4. **Operations Sidebar**:
   - **Cases**: Claim open escalations, then Resolve (selecting `ASSIGNED_SLOT` lets you pick an open slot to book atomically).
   - **Chats**: View facility-scoped driver transcripts, send direct operations messages, or resend failed Telegram notices.
   - **Requests**: Approve/reject incoming **Facility Change** requests and monitor **Partner Warehouse** email requests.
5. **Shipments (`#shipments`)**: Open availability (if not using 24/7 auto-slots), create a shipment (`ORD` + $\ge 6$ digits, driver, slot) — the bot delivers the Dock Access Pass automatically. Use row actions to reassign driver (while unconfirmed), reissue/revoke pass, unlock after 5 failed guesses, or mark **Unloading complete** on checked-in trucks.
6. **Projects (`#projects`) & Drivers (`#drivers`)**: Create customer contract projects, allocate/release drivers, onboard new drivers (pending Admin approval), revoke/allow-relink Telegram bindings, or soft-delete drivers you added.

---

## 5. User Manual — Gate Security Guard (`ACT-03`, `/gate`) `[Verified]`
Sign in at `/` (auto-redirects to `/gate`).
1. **Validate Arriving Truck**: Scan (QR/barcode camera or keyboard-wedge scanner) or type the driver's `DAP-...` code, vehicle registration, and driving licence number, then click **Validate**.
2. **Check In**: Review the validated shipment, dock, and time window, then click **Check in** (records arrival as `EARLY`, `ON_TIME`, or `LATE`).
3. **Check Out**: Once Warehouse marks unloading complete, click **Check out** next to the truck in **Trucks inside** (or enter the `DAP-...` code in the check-out form).
4. **Lockout Notice**: 10 failed pass/detail checks within 15 minutes temporarily rate-limit validation.

---

## 6. Administrator Manual (`ACT-04`, `/admin`) `[Verified]`
Sign in at `/` via Google, Email/Password, or Break-Glass Local Admin (if enabled).
1. **Users (`#users`)**: Invite new `ADMIN`, `WAREHOUSE_AUTHORITY`, or `SECURITY` users and assign facilities. Disable/re-enable users (takes effect immediately on next request), update facility bindings, send invite/reset password links, or delete users (unreferenced users are hard-deleted; historically referenced users are soft-deleted while freeing their email). Note: you cannot disable yourself or the last active admin.
2. **Drivers (`#pending`, `#drivers`, `#deleted-drivers`)**: Approve or reject newly onboarded drivers, suspend/re-approve existing drivers, and restore soft-deleted drivers.
3. **Audit Log (`#audit`)**: Filter append-only system events by actor, action, outcome, and `since` timestamp (up to 500 rows; credentials are scrubbed).
4. **Model Telemetry (`#model`)**: Review local turn/call latency, outcomes, tokens, and estimated USD cost; click to load on-demand 24h LangSmith summary (when enabled).
5. **Configuration (`#configuration`)**: Override **Model**, **LangSmith** (requires typing `SEND_TO_LANGSMITH`), **Telegram** (generate secret, test bot, register webhook), and **Email** (SMTP settings, toggle email/password sign-in on/off, send test email). Secrets are Fernet-encrypted via `SETUHAUL_SETTINGS_KEY`. Use **Reset to .env** per section to revert overrides.
6. **Accessing Gate**: Admins have access to `/gate` by navigating directly to `/gate` (note `TD-11`: top nav bar omits `/gate` link).

## 7. Source Evidence & Open Questions
- Evidence: `src/setuhaul/auth/wiki_pages.py`, `pages.py`, `gate_pages.py`, `README.md`.
- Open Questions: Should driver instructions be translated into native-reviewed Hindi/Hinglish PDF/WhatsApp cards prior to pilot? (`DOC-23`)
