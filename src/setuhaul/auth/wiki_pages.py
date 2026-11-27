"""The wiki page: how to use the application. Every signed-in role reads all of it.

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
        "Open the application's address. Access is by invitation: an administrator adds your email address first.",
        "Choose “Sign in with Google” and use the invited address.",
        ("Where email sign-in is switched on, use the link in your invitation to set a password (it works once, for 48 "
         "hours), then sign in with email and password."),
        "Forgotten it? Click “Forgot password?”. The link emailed to you works once, for 1 hour.",
        "Where only a backup password is set up, an administrator can open “Administrator password” and sign in with it.",
        "Use “Sign out” at the top right when you finish, especially on a shared computer.",
    )),
    ("Who can open what", (
        "Admin: the Warehouse, Admin and Gate screens, at every facility.",
        "Warehouse authority: the Warehouse screen, for their own facilities only.",
        "Security (gate): the Gate screen, for their own facilities only.",
        "Drivers do not sign in here. They use Telegram.",
    )),
    ("The usual day, from shipment to gate", (
        "The warehouse opens availability, then creates a shipment with a driver and a dock slot.",
        "The driver receives a Dock Access Pass in Telegram and sends it back to confirm.",
        "If the driver is delayed they say so in the chat, and are offered another slot.",
        "At the gate, security checks the pass and checks the truck in.",
        "The warehouse marks unloading complete, and security checks the truck out.",
    )),
)

_WAREHOUSE: tuple[Task, ...] = (
    ("See what needs attention", (
        "The cards at the top count waiting work and licences that are not valid. Click a card to go to it.",
        "The dock board and the Operations panel update by themselves. The other tabs do not: reload the page.",
    )),
    ("Read the dock board", (
        "On the Dock board tab, click the facility's button at the top.",
        ("Each row is a dock and each column an hour. A cell with initials has a booking; the colour says Free, Booked, "
        "Held (not yet confirmed) or Blocked."),
        "Hover over a cell, or click it, to see the dock, time, driver, order and any block reason.",
        "Open “On the bays now” under the board to see what is on each dock at this moment and what comes next.",
    )),
    ("Free a booked slot", (
        "Click the booking's cell on the dock board.",
        "Click “Free this slot” and confirm. The shipment is kept and needs a new slot; the driver is told.",
        "The button is not offered for held slots, slots that have started, unloading, or trucks already inside.",
        "If the screen says the driver could not be reached, phone them.",
    )),
    ("Block a dock", (
        "On the Dock board tab, open “Block a dock” under the board.",
        "Choose the dock and type, enter From (India time), leave To empty to block until reopened, and give a reason.",
        "Click “Check bookings”. Read which bookings are affected and where each would move.",
        "Click “Block and move” (or “Block” if none are affected). Drivers are told their new dock and time.",
        "A booking that fits nowhere is freed: its shipment is kept and needs a slot.",
        "Look for the penalty note: a move that misses the customer's latest arrival says so before you confirm.",
        "Trucks already inside or unloading are listed and left alone.",
    )),
    ("Reopen a blocked dock", (
        "Under “Current and upcoming blocks”, click “Reopen” on the block and confirm.",
        "Only that block ends. Bookings that were already moved stay where they are.",
    )),
    ("Work on a case", (
        "In the Operations panel beside the board, choose “Cases”. A case is a problem the assistant handed to a person.",
        "Click “Claim” so others know you are working on it.",
        "Choose an outcome, write the message the driver will receive, and click “Resolve”.",
        "A recently resolved case can be brought back with “Reopen”.",
    )),
    ("Answer requests and read chats", (
        "In Operations, “Requests” lists drivers asking to deliver elsewhere. Approve or Reject each one.",
        "“Chats” shows the conversations of drivers you may see. To write to a driver, resolve their case.",
        "“Requests” also shows slot requests sent to partner warehouses by email and their answers.",
    )),
    ("Manage projects", (
        "On the Projects tab, fill in “New project” and click “Create”.",
        "Choose a driver in the project's row and click “Allocate”. “Release” removes them.",
    )),
    ("Open availability and create a shipment", (
        ("On the Shipments tab, fill in “1. Open availability” (at most 14 days) and click “Open availability”. "
        "Existing slots are kept."),
        "Fill in “2. New shipment”. The order number is ORD followed by at least six digits.",
        "Pick a planned dock slot and click “Create shipment”. The driver is sent the pass.",
    )),
    ("Change, cancel or re-send a shipment", (
        "Before the driver confirms you can use “Change driver” or “Cancel”.",
        "“Unlock” clears a shipment locked after too many wrong passes.",
        "“Send new pass” replaces a lost pass. “Revoke pass” needs a reason and stops the old one.",
        "When the truck has been unloaded, click “Unloading complete” so security can check it out.",
    )),
    ("Add a driver", (
        "On the Drivers tab, fill in “Add a driver” and click “Add driver”.",
        "An administrator must approve the driver before they can be given work.",
        "“Revoke Telegram” cuts a driver's chat access; “Allow re-link” lets them link again.",
    )),
    ("Delete a driver", (
        "On the Drivers tab, click “Delete” beside the driver.",
        "Read the list of unfinished shipments that will be cancelled and their slots freed.",
        "Type the driver's name and click “Delete driver”. Nothing is erased, and an administrator can restore them.",
        "Warehouse staff can delete only drivers they added.",
    )),
    (ERRORS, (
        "“That dock slot has been taken or has passed.” Reload and choose another slot.",
        "“This load cannot use that slot” is followed by the reason. Choose a slot or dock that fits.",
        "“That driver is not approved, active and licensed.” Check the driver on the Drivers tab.",
        "“Someone else has already claimed this case.” Leave it to them.",
        "“Things have changed since the warning.” Check the new list and confirm again.",
        "“One of this driver's trucks is at a dock or inside the gate.” Finish that visit, then delete.",
        "“This driver has shipments at another warehouse.” Ask an administrator.",
    )),
)

_ADMIN: tuple[Task, ...] = (
    ("Invite and manage users", (
        ("On the Users tab, fill in “Invite a user”: email, name, role and, for warehouse and security, facilities. "
        "Click “Invite”."),
        "“Disable” ends a user's sessions at once; “Re-enable” lets them back.",
        "Open “Change facilities” in a user's row and click “Save facilities”.",
    )),
    ("Approve drivers", (
        "On Pending drivers, click “Approve” or “Reject”.",
        "On Onboarded drivers, “Suspend” stops an approved driver and “Re-approve” brings a rejected one back.",
    )),
    ("Restore a deleted driver", (
        "Open Deleted drivers and click “Restore”.",
        "Cancelled shipments and project allocations do not come back.",
        "Telegram must be allowed to link again from the warehouse Drivers tab.",
    )),
    ("Check who did what", (
        "Open Audit log. Filter by actor, action, outcome or time and click “Filter”.",
        "Times in the Since box need an offset, for example 2026-08-04T09:00:00+05:30.",
    )),
    ("Watch the assistant", (
        "The Model tab shows which model is in use, how long replies take, failures, token use and cost.",
        "“Load from LangSmith” fetches the tracing summary when tracing is set up.",
    )),
    ("Set up invitation and password emails", (
        ("In the Google account that will send the mail, turn on 2-Step Verification and create an app password "
         "(Google Account, then Security, then App passwords). A separate mailbox is safest."),
        ("Signing in to this application with Google does not let it send mail: it still needs the app password. "
         "Never type your normal Google password here."),
        ("On Configuration, under Email, enter the mail server (smtp.gmail.com), port (465), sending user, app password "
         "and from address. Server and port may be left empty for Gmail."),
        "Click “Send test email to me”. It goes to the address you are signed in with; check spam too.",
        "Click “Save”. The app password box should then say it is set. It is never shown again.",
        ("Whoever runs the server adds SETUHAUL_PASSWORD_LOGIN=1 and the site address (SETUHAUL_PUBLIC_URL) to the "
         "server's settings file once, and redeploys. These cannot be changed on screen."),
        "When the Email status says “Sign-in emails are on”, new invitations are emailed.",
        "For people invited before that, click “Send password link” on the Users tab. It also re-sends an invitation.",
        "Changing the mail server, port or sending user means typing the app password again.",
    )),
    ("Change settings", (
        "On Configuration, edit Model, LangSmith, Telegram or Email and click “Save” under that group.",
        "Use “Test connection” or “Test bot” before relying on a change. A new Telegram bot also needs “Register webhook”.",
        "Leave a key field empty to keep the saved key. “Reset to .env” removes saved settings.",
    )),
    (ERRORS, (
        "“That email address has already been invited.” Find the user in the list instead.",
        "“Invitation emails are off” on the Users tab: nothing is sent until Email is set up under Configuration.",
        "“Enter the sending user and the app password first.” The app password has not been entered or saved.",
        "“Failed: SMTP SMTPAuthenticationError” means the sending user or app password is wrong.",
        ("A test that times out or cannot connect usually means the host blocks the mail port. Try port 587, typing the "
         "app password again."),
        "“A test email was just sent.” Wait a minute and try again.",
        "“Choose at least one facility for a warehouse authority.” Tick one and invite again.",
        "“The last administrator cannot be disabled.” Invite another administrator first.",
        "“That licence has expired.” The driver cannot be approved with it.",
        "“Keys cannot be saved” means the server has no valid settings key. Ask whoever runs the server.",
    )),
)

_GATE: tuple[Task, ...] = (
    ("Check a truck in", (
        "On the Gate screen, under “Arriving truck”, type the Dock Access Pass.",
        "Type the vehicle number you see on the truck and the number on the driver's licence.",
        "Compare the licence photo with the driver yourself.",
        "Click “Check pass”. If the checks pass, click “Check in”.",
    )),
    ("Check a truck out", (
        "The warehouse must first mark unloading complete.",
        "Find the truck under “Trucks inside” and click “Check out”, or type the pass and click “Check out by pass”.",
    )),
    (ERRORS, (
        "“This pass is not valid.” Do not admit the truck; ask the driver to contact their warehouse.",
        "“This pass is for another warehouse.” Send the driver to the warehouse on their booking.",
        "“The truck is too early for its slot.” Ask the driver to wait until the pass window opens.",
        "“The pass window has passed.” The warehouse must book again and send a new pass.",
        "“There is no booked slot for this pass.” The driver should contact their warehouse.",
        "“The vehicle number or licence number does not match the pass.” Check what you typed.",
        "“Too many wrong passes or details.” Wait 15 minutes or ask an administrator.",
        "“The warehouse has not marked unloading complete.” Ask them before the truck leaves.",
    )),
)

_DRIVERS: tuple[Task, ...] = (
    ("Before the driver starts (office staff)", (
        "Warehouse: on the Drivers tab, use “Add driver” with the phone number the driver uses in Telegram.",
        "Administrator: on Pending drivers, click “Approve”. An unapproved driver cannot be linked.",
        "Administrator: on Configuration, under Telegram, click “Test bot” to see the bot's name (it starts with @).",
        "Give that bot name to the driver. Drivers cannot see any screen here, so they can only get it from you.",
    )),
    ("Register on the phone (driver)", (
        "Install Telegram and sign up with the same phone number your warehouse has for you.",
        "In Telegram's search, type the bot name your warehouse gave you, open it and press Start.",
        ("The bot asks for your phone number. Tap the button “Share my phone number” below the message box. "
        "Typing the number, or forwarding a contact, does not work."),
        f"You will see: “{telegram.BOUND_REPLY}” You only do this once.",
        "If a shipment is waiting for you, its Dock Access Pass arrives next.",
    )),
    ("Send a message (driver)", (
        "Type in the same chat, like a normal message, and press send. English, Hindi and Hinglish are understood.",
        "Send the Dock Access Pass you received back in the chat to confirm the shipment. Keep it to show at the gate.",
        "Running late: “my truck is 60 minutes late” or “bhai meri gaadi 60 minute late hai”.",
        "One new slot is then held for a few minutes. It is not booked until you reply “Yes”. Reply “No” for the next one.",
        "To keep your old booking: “never mind, keep my original slot”.",
        "To cancel: “please cancel my appointment”. Close to the slot time, a person decides.",
        "To deliver elsewhere: “Please change facility to Gurugram”. The warehouse must approve.",
        "If the assistant cannot sort it out you get a case reference, and a person replies in the same chat.",
    )),
    ("Text to send your driver", (
        "Copy these lines into a message and put the bot name in:",
        "Open Telegram and search for @________ (your bot name).",
        "Press Start, then tap “Share my phone number”.",
        "When your Dock Access Pass arrives, send it back to confirm.",
        "If you are running late, just type it, for example “my truck is 60 minutes late”.",
        "A slot is yours only after you reply “Yes” and see “Your appointment is confirmed”.",
    )),
    (ERRORS, (
        (f"“{telegram.NEUTRAL_REFUSAL}” The number does not match the driver record, the driver is not approved or their "
        "licence is not valid, or their access was revoked. The warehouse checks the Drivers tab; “Allow re-link” lets a "
        "revoked driver share again."),
        "No reply at all: check the bot name, press Start, and ask an administrator to check that the webhook is registered.",
        f"“{say('unsupported_content', 'en')}” Photos, voice notes, stickers and locations are not read.",
        f"“{say('slow_down', 'en', seconds='60')}” Wait, then send it once.",
        f"“{telegram.TOO_LONG_REPLY}” Keep a message under 2000 characters.",
        "“That pass is not valid.” Check the pass and send it again.",
        "“I cannot accept that pass.” The shipment is locked: the warehouse can unlock it or send a new pass.",
        "“Your hold on that slot has lapsed.” Nothing changed; ask for fresh options.",
        "New phone number: no screen changes a driver's phone number, so ask whoever runs the server.",
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
