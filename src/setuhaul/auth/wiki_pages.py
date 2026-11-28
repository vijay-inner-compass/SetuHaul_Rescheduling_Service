"""The wiki page: how to use the production application. Every signed-in role reads all of it.

The text lives here so that it changes in the same commit as the screen it describes. Buttons, tabs and messages are
quoted as the screens have them.
"""

# Lazy annotations, like the rest of the codebase.
from __future__ import annotations

from fastapi.responses import HTMLResponse

from setuhaul.auth import telegram
from setuhaul.auth.pages import _layout, _respond, _tabs, esc
from setuhaul.auth.users import User
from setuhaul.language import say

ERRORS = "If you see an error"

Task = tuple[str, tuple[str, ...]]

_START: tuple[Task, ...] = (
    ("Sign in", (
        "Open your production application address. Access is by invitation: an administrator adds your email address first.",
        "Choose “Sign in with Google” and sign in with your invited email address.",
        ("Where email sign-in is switched on, open the link in your invitation email to set a password (it works once, for 48 "
         "hours), then sign in with your email and password."),
        "Forgotten your password? Click “Forgot password?”. The reset link emailed to you works once, for 1 hour.",
        "Where a backup administrator password is configured, an administrator can open “Administrator password” and sign in with it.",
        "Use “Sign out” at the top right when you finish, especially on a shared workstation.",
    )),
    ("Who can open what", (
        "Admin: the Warehouse, Admin, Gate and Wiki screens, across every facility.",
        "Warehouse authority: the Warehouse and Wiki screens, for their assigned facilities only.",
        "Security (gate): the Gate and Wiki screens, for their assigned facilities only.",
        "Drivers do not sign in to the web app. They communicate through the production Telegram bot.",
    )),
    ("The usual day, from shipment to gate", (
        "Step 1 — Open availability: the warehouse opens dock slots and labour (manually under Shipments → “1. Open availability” or automatically under Dock board → “Round-the-clock slots”).",
        "Step 2 — Create shipment: the warehouse creates a shipment under Shipments → “2. New shipment” with a driver, vehicle, and planned dock slot.",
        "Step 3 — Driver confirms: the driver receives a Dock Access Pass on Telegram and replies with the code to confirm.",
        "Step 4 — Changes or delays: if the driver is delayed, wants to cancel, or requests another warehouse, they message the Telegram bot; warehouse staff can also free slots, change warehouses, allocate slots, or handle escalated cases from /warehouse.",
        "Step 5 — Gate check-in: at the gate, security checks the Dock Access Pass, vehicle number, and driver licence on /gate and checks the truck in.",
        "Step 6 — Unload and check-out: warehouse staff click “Unloading complete” on the Shipments tab, and security checks the truck out on /gate.",
    )),
)

_WAREHOUSE: tuple[Task, ...] = (
    ("See what needs attention", (
        "The summary cards at the top count waiting work (open cases, pending requests, unconfirmed shipments) and invalid licences. Click a card to jump straight to it.",
        "The Dock board and the Operations panel refresh automatically every few seconds. Other tabs (Shipments, Drivers, Projects) do not auto-refresh: reload the page to see updates.",
    )),
    ("Read the dock board", (
        "On the Dock board tab, click the facility button (or pick from the Facility list) at the top.",
        "The board always displays a full 24-hour day (00:00 to 23:00 IST) with the current hour highlighted. To inspect another date, choose it under “Day” and click “Show”.",
        ("Each row is a dock and each column is an hour. A cell with initials has a booking; the colour indicates Free, Booked, "
         "Held (not yet confirmed), Blocked, or No slot (no slot opened for that hour yet)."),
        "Hover over or click any cell to inspect its dock, time window, driver name, order number, and any block reason.",
        "Open “On the bays now” under the board to see what is occupying each dock right now and which booking is next.",
    )),
    ("Create slots day and night", (
        "Warehouses are open 24 hours. On the Dock board tab, open “Round-the-clock slots” under the board.",
        "Enter the number of Crews (how many docks can unload at the same time) and click “Save”.",
        "1-hour slots for every active dock are created automatically for the next 14 days and topped up continuously. Any labour windows already recorded are preserved.",
    )),
    ("Free a booked slot", (
        "Click the “×” button in the top-right corner of a freeable booked cell on the dock board (or click the cell and click “Free this slot”).",
        "Read and confirm the warning prompt. The booking is cancelled, the dock slot turns Free, any live chat hold is cleared, and the driver is notified on Telegram.",
        "The shipment itself is kept (shown as “Confirmed (no slot)” and “No slot allocated” on the Shipments tab) so you or the driver can book a new slot.",
        "This works for both upcoming bookings and past no-show bookings where the truck never arrived. It is not offered for held slots, partner facilities, unloading, or trucks already inside the gate.",
        "If the screen warns that the driver could not be reached on Telegram, phone them.",
    )),
    ("Block a dock", (
        "On the Dock board tab, open “Block a dock” under the board.",
        "Choose the dock and block type, enter From (India time), leave To empty to block until reopened, and enter a clear reason.",
        "Click “Check bookings”. Review every affected booking and where the system plans to move it.",
        "Click “Block and move” (or “Block” if no bookings are affected). Affected drivers are automatically notified of their new dock and time.",
        "Any booking that fits nowhere else is freed: its shipment is kept and waits for a new slot.",
        "Watch for penalty warnings: if a move would miss the customer's latest arrival time, the preview flags it before you confirm.",
        "Trucks already inside or unloading are listed and left untouched.",
    )),
    ("Reopen a blocked dock", (
        "Under “Current and upcoming blocks”, click “Reopen” on the block and confirm.",
        "Only that block ends early. Bookings that were already moved to other docks remain where they were moved.",
    )),
    ("Work on a case", (
        "In the Operations panel beside the dock board, open “Cases”. A case is an issue the assistant handed off to a person.",
        "Click “Claim” so other staff see you are handling it.",
        "Choose an Outcome, write a clear “Message to the driver”, and click “Resolve”.",
        "“Assigned a slot” opens the “Slot to book” picker (up to 200 upcoming open slots at the shipment's destination warehouse). Resolving books that slot, updates the dock board, and sends the dock and time to the driver.",
        "“Cancelled by operations” cancels the shipment's active booking, frees the dock slot immediately, clears any live hold, and notifies the driver (refused if the truck is already on-site or unloading).",
        "Other outcomes (“Advised the driver”, “No action needed”, “Other”) close the case and send your message without changing the booking.",
        "If a case has no shipment attached, the picker displays “No shipment linked to this case” — allocate a slot from the Shipments tab if needed and close the case with “Advised the driver”.",
        "A recently resolved case can be reopened with “Reopen”. Reopening a case resolved with “Assigned a slot” or “Cancelled by operations” warns that the previous slot change is already saved and will not be undone automatically.",
    )),
    ("Answer requests and read chats", (
        "In Operations, “Requests” lists drivers asking to deliver to a different warehouse. Click “Approve” or “Reject”.",
        "Approving a warehouse change immediately frees the old dock slot, closes any pending partner requests, and revokes the old gate pass. If a slot is available at the new warehouse, it is held for 15 minutes for the driver to confirm; if none is available or the hold lapses, the load stays assigned to the new warehouse so staff can allocate a slot later.",
        "“Chats” shows driver conversations about loads bound for your facilities. Messages not yet tied to a load are visible only to administrators.",
        "To message a driver directly, select them in “Chats”, type under the conversation, and click “Send”.",
        "If delivery to Telegram fails, it is shown in the thread; once retries stop, click “Resend” to queue it again.",
        "“Requests” also lists partner-warehouse slot requests sent by email and their current status.",
    )),
    ("Manage projects", (
        "On the Projects tab, fill in “New project” and click “Create”.",
        "Select a driver in the project row and click “Allocate”. Click “Release” to remove them from the project.",
    )),
    ("Open availability and create a shipment", (
        "On the Shipments tab, fill in “1. Open availability”: select Facility, First date and Last date (at most 14 days), one or more Docks, From and To times (use 00:00 to 00:00 for all day), Slot minutes (default 60), Crews, and Equipment available.",
        "Click “Open availability”. Existing slots and labour windows are preserved; new slots are added immediately.",
        "Fill in “2. New shipment”: enter Order number (ORD followed by at least six digits), select an eligible Driver and matching Vehicle, and enter Origin name, Origin city, Destination facility, Customer, Product, Load (kg), Dock type, Refrigerated, Priority, Unload minutes, and Planned departure (must be before the slot start).",
        "Choose an open “Planned dock slot” and click “Create shipment”. Feasibility rules verify dock type, refrigeration, weight, unload duration and labour; on success the slot turns Booked and the Dock Access Pass is sent to the driver on Telegram.",
    )),
    ("Change warehouse, allocate a slot, cancel or re-send a shipment", (
        "Before the driver confirms their pass, you can use “Change driver” to reassign the shipment to another eligible driver and vehicle.",
        "To move any active shipment (not yet on-site) to another non-partner warehouse, choose the target facility in its row, click “Change warehouse”, and confirm. The old dock slot is freed immediately, the old gate pass is revoked without locking out an unconfirmed driver, pending requests are closed, and the driver is notified.",
        "To book or replace a dock slot on any active shipment (not yet on-site), choose a compatible open slot in its row and click “Allocate slot” (automatically filtered to compatible dock types such as Reefer or Heavy, up to 200 upcoming slots, and prompts before replacing an existing booking). The driver is notified and a fresh Dock Access Pass is sent.",
        "To cancel any active shipment (waiting, locked, or confirmed — as long as the truck is not at the gate or dock), click “Cancel” and confirm. Its slot is freed and its pass is cancelled.",
        "“Unlock” resets a shipment that was locked after too many wrong pass attempts.",
        "“Send new pass” issues and sends a replacement pass (shown only when a slot is booked). “Revoke pass” requires a reason and invalidates the current pass.",
        "When unloading finishes, click “Unloading complete” so security can check the truck out at the gate.",
    )),
    ("Add a driver", (
        "On the Drivers tab, fill in “Add a driver” using the exact phone number the driver uses in Telegram, then click “Add driver”.",
        "An administrator must approve the driver before they can link Telegram or be assigned shipments.",
        "“Revoke Telegram” cuts a driver's chat access immediately; “Allow re-link” lets them link their phone number again.",
    )),
    ("Delete a driver", (
        "On the Drivers tab, click “Delete” beside the driver.",
        "Review the list of unfinished shipments that will be cancelled and whose slots will be freed.",
        "Type the driver's exact name and click “Delete driver”. History is preserved, and an administrator can restore the driver later.",
        "Warehouse authorities can delete only drivers they added.",
    )),
    (ERRORS, (
        "“That dock slot has been taken or has passed.” Reload the page and choose another open slot.",
        "“This load cannot use that slot” is followed by the exact reason (dock type, refrigeration, weight, labour, or arrival time). Choose a compatible dock or time.",
        "“That driver is not approved, active and licensed.” Check the driver's status and licence expiry on the Drivers tab.",
        "“The shipment is already assigned to that warehouse.” Choose a different destination warehouse.",
        "“The truck is already at the facility, so its booking cannot be changed.” Complete the current visit first.",
        "“That shipment is already finished or cancelled.” Reload the page to see its current status.",
        "“Someone else has already claimed this case.” Coordinate with the staff member who claimed it.",
        "“Things have changed since the warning.” Review the updated booking list and confirm again.",
        "“One of this driver's trucks is at a dock or inside the gate.” Complete that visit before deleting the driver.",
        "“This driver has shipments at another warehouse.” Ask an administrator to handle the deletion.",
    )),
)

_ADMIN: tuple[Task, ...] = (
    ("Invite and manage users", (
        ("On the Users tab, fill in “Invite a user”: email, name, role and, for warehouse and security roles, assigned facilities. "
         "Click “Invite”."),
        "“Disable” ends a user's active sessions immediately; “Re-enable” restores their access.",
        "Open “Change facilities” in a user's row, update their facilities, and click “Save facilities”.",
    )),
    ("Approve drivers", (
        "On Pending drivers, verify details and click “Approve” or “Reject”.",
        "On Onboarded drivers, “Suspend” stops an approved driver and “Re-approve” restores a rejected or suspended driver.",
    )),
    ("Restore a deleted driver", (
        "Open Deleted drivers and click “Restore”.",
        "Previously cancelled shipments and project allocations do not come back.",
        "Use “Allow re-link” on the warehouse Drivers tab so the driver can link Telegram again.",
    )),
    ("Check who did what", (
        "Open Audit log. Filter by actor, action, outcome or time and click “Filter”.",
        "Times in the Since box require a timezone offset, for example 2026-08-04T09:00:00+05:30.",
    )),
    ("Watch the assistant", (
        "The Model tab displays the active model, reply latency, failure counts, token usage, and estimated cost.",
        "“Load from LangSmith” fetches the tracing summary when LangSmith tracing is configured.",
    )),
    ("Set up invitation and password emails", (
        ("In the Google account that will send mail, turn on 2-Step Verification and create an app password "
         "(Google Account → Security → App passwords). Using a dedicated mailbox is safest."),
        ("Signing in to this application with Google does not allow it to send email: it still requires an app password. "
         "Never enter your regular Google password here."),
        ("On Configuration, under Email, enter the mail server (smtp.gmail.com), port (465), sending user, app password, "
         "and from address. Server and port may be left empty for Gmail."),
        "Click “Send test email to me”. It sends to your signed-in address; check spam if it does not appear.",
        "Click “Save”. The app password field confirms it is saved and never displays the secret again.",
        "Set “Email and password sign-in” to “On” and click “Save”. Google sign-in continues working alongside it.",
        ("Your server operator sets the public site address (SETUHAUL_PUBLIC_URL) in the production environment once and "
         "redeploys. It cannot be edited from the screen."),
        "Once Email status shows “Sign-in emails are on”, new user invitations are emailed automatically.",
        "For users invited earlier, click “Send password link” on the Users tab to email or re-send their link.",
        "Changing the mail server, port, or sending user requires re-entering the app password.",
    )),
    ("Change settings", (
        "On Configuration, update Model, LangSmith, Telegram, or Email settings and click “Save” in that section.",
        "Always run “Test connection” or “Test bot” before relying on a change. Switching to a new Telegram bot also requires clicking “Register webhook”.",
        "Leave any secret/key field empty to keep the currently saved value. “Reset to .env” reverts saved overrides back to server environment defaults.",
    )),
    (ERRORS, (
        "“That email address has already been invited.” Find the user in the Users table instead.",
        "“Invitation emails are off” on the Users tab: nothing is emailed until Email is configured under Configuration.",
        "“Enter the sending user and the app password first.” Fill in both fields before testing or saving.",
        "“Failed: SMTP SMTPAuthenticationError” means the sending user or app password is invalid.",
        ("A test that times out or cannot connect usually means the host blocks port 465. Try port 587 and re-enter the "
         "app password."),
        "“A test email was just sent.” Wait a minute before sending another.",
        "“Choose at least one facility for a warehouse authority.” Tick at least one facility and invite again.",
        "“The last administrator cannot be disabled.” Invite another administrator first.",
        "“That licence has expired.” Update the licence before approving the driver.",
        "“Keys cannot be saved” means the server has no valid encryption key configured. Contact your server operator.",
    )),
)

_GATE: tuple[Task, ...] = (
    ("Check a truck in", (
        "On the Gate screen, under “Arriving truck”, type the driver's Dock Access Pass code.",
        "Type the vehicle registration number seen on the truck and the driver's licence number.",
        "Visually verify the licence photo against the driver.",
        "Click “Check pass”. If every check succeeds, click “Check in”.",
    )),
    ("Check a truck out", (
        "Wait until warehouse staff mark unloading complete on the Shipments tab.",
        "Locate the truck under “Trucks inside” and click “Check out”, or enter its pass code and click “Check out by pass”.",
    )),
    (ERRORS, (
        "“This pass is not valid.” Do not admit the truck; ask the driver to contact their warehouse.",
        "“This pass is for another warehouse.” Direct the driver to the warehouse shown on their booking.",
        "“The truck is too early for its slot.” Ask the driver to wait until the check-in window opens.",
        "“The pass window has passed.” The warehouse must allocate a new slot and send a new pass.",
        "“There is no booked slot for this pass.” The driver should contact their warehouse for a slot.",
        "“The vehicle number or licence number does not match the pass.” Double-check what was typed.",
        "“Too many wrong passes or details.” Wait 15 minutes or ask an administrator.",
        "“The warehouse has not marked unloading complete.” Ask warehouse staff to mark it complete before check-out.",
    )),
)

_DRIVERS: tuple[Task, ...] = (
    ("Before the driver starts (office staff)", (
        "Warehouse: on the Drivers tab, use “Add driver” with the exact phone number the driver uses on Telegram.",
        "Administrator: on Pending drivers, click “Approve”. An unapproved driver cannot link Telegram.",
        "Administrator: on Configuration, under Telegram, click “Test bot” to confirm the production bot username (starts with @).",
        "Share that bot username with the driver. Drivers do not sign in to the web app.",
    )),
    ("Register on the phone (driver)", (
        "Install Telegram and sign in with the same phone number registered by your warehouse.",
        "Search for the bot username your warehouse gave you, open the chat, and press Start.",
        ("When the bot asks for your phone number, tap the “Share my phone number” button below the text box. "
         "Typing the number or forwarding a contact does not work."),
        f"You will see: “{telegram.BOUND_REPLY}” You only need to do this once.",
        "If a shipment is waiting for you, your Dock Access Pass arrives right after.",
    )),
    ("Send a message (driver)", (
        "Type in the same Telegram chat like a normal message and send. English, Hindi and Hinglish are understood.",
        "Reply with the Dock Access Pass code you received to confirm your shipment. Keep the code to show at the gate.",
        "Running late: “my truck is 60 minutes late” or “bhai meri gaadi 60 minute late hai”.",
        "One feasible slot is held for 3 minutes. It is not booked until you reply “Yes”. Reply “No” to see the next option.",
        "To keep your original booking: “never mind, keep my original slot”.",
        "To cancel: “please cancel my appointment”, then reply “Yes” when asked. This frees your dock slot and closes any pending warehouse-change request; close to the slot time, operations decides.",
        "To deliver to another warehouse: “Please change facility to Gurugram”. Once approved, your old slot is freed immediately and a new slot is offered (reply “Yes” to confirm and receive your new Dock Access Pass).",
        "If the assistant cannot handle the request automatically, you receive a case reference and operations replies in the same chat.",
    )),
    ("Text to send your driver", (
        "Copy these lines into a message and fill in your production bot username:",
        "Open Telegram and search for @________ (your bot name).",
        "Press Start, then tap “Share my phone number”.",
        "When your Dock Access Pass arrives, send it back to confirm.",
        "If you are running late, just type it, for example “my truck is 60 minutes late”.",
        "A slot is yours only after you reply “Yes” and see “Your appointment is confirmed”.",
    )),
    (ERRORS, (
        (f"“{telegram.NEUTRAL_REFUSAL}” The phone number does not match the driver record, the driver is not approved or their "
         "licence is expired, or Telegram access was revoked. Warehouse checks the Drivers tab; “Allow re-link” lets a "
         "revoked driver link again."),
        "No reply at all: verify the bot username, press Start, and ask an administrator to check that the Telegram webhook is registered.",
        f"“{say('unsupported_content', 'en')}” Photos, voice notes, stickers and locations are not processed.",
        f"“{say('slow_down', 'en', seconds='60')}” Wait a minute, then send once.",
        f"“{telegram.TOO_LONG_REPLY}” Keep messages under 2000 characters.",
        "“That pass is not valid.” Check the code and send it again.",
        "“I cannot accept that pass.” Confirmation is locked: ask the warehouse to unlock it or send a new pass.",
        "“Your hold on that slot has lapsed.” Your original booking is unchanged; ask for fresh options.",
        "New phone number: no screen edits a driver's phone number — ask your server operator.",
    )),
)

# (tab key, tab label, tasks)
TOPICS: tuple[tuple[str, str, tuple[Task, ...]], ...] = (
    ("start", "Getting started", _START),
    ("warehouse", "Warehouse", _WAREHOUSE),
    ("admin", "Admin", _ADMIN),
    ("gate", "Gate", _GATE),
    ("drivers", "Driver chat", _DRIVERS),
)

# The screens each role may open, for the menu: it never offers a page that would refuse them.
_SCREENS = (("/warehouse", "Warehouse", {"WAREHOUSE_AUTHORITY", "ADMIN"}), ("/admin", "Admin", {"ADMIN"}),
            ("/gate", "Gate", {"SECURITY", "ADMIN"}))


def _task(heading: str, steps: tuple[str, ...]) -> str:
    # Error messages are a list to look things up in; everything else is done in order.
    tag = "ul" if heading == ERRORS else "ol"
    return f"<h3>{esc(heading)}</h3><{tag}>{''.join(f'<li>{esc(s)}</li>' for s in steps)}</{tag}>"


def render_wiki(user: User) -> HTMLResponse:
    sections = "".join(
        f'<section id="{key}" data-panel="{key}"><h2>{esc(label)}</h2>{"".join(_task(*t) for t in tasks)}</section>'
        for key, label, tasks in TOPICS)
    tabs = _tabs(tuple((key, label) for key, label, _ in TOPICS))
    nav = " ".join([f'<a href="{path}">{label}</a>' for path, label, roles in _SCREENS if user.role in roles]
                   + ['<a href="/wiki">Wiki</a>'])
    return _respond(_layout("Wiki", f'<div class="wh wiki">{tabs}{sections}</div>', ("common.js", "tabs.js"), user, nav))
