# 8. The demonstration console

Files: `demo_ui.py`, `demo/index.html`, `scripts/run_demo.py`. Decision D64.

## What it is

A localhost-only web page that drives the **real service**. It has three panels: a driver console (chat), a dock board
(dock-by-hour grid) and an operations panel (cases, history, notification outbox). It is a demonstration aid, not a
production surface.

## How it stays honest

* **Same code path.** Each chat message goes through the same `process` function as the webhook: duplicate handling, rate limit,
  conversation, tools. Only the signature step is skipped, because the page stands in for the messaging channel.
* **Simulated clock.** The snapshot is dated 4 Aug 2026, so the demo runs on a `FakeClock` starting at 09:35. Each message
  advances it 20 seconds. Buttons add 1 minute or 3 minutes (and run the expiry sweep) so a 180-second hold can be watched
  lapsing.
* **Synthetic data.** `seed_demo_evidence` adds labour windows and customer commitments labelled `DEMO_FIXTURE`, clears seeded
  chat history and provisions identities. **Reset** rebuilds the database, empties the cache and rewinds the clock.
* **People, not ids.** Drivers appear by name; loads by order reference and customer; bookings on the board as "driver · order".
* **Local only.** Every route returns 403 `DEMO_IS_LOCAL_ONLY` unless the caller is the same machine. The server binds to
  127.0.0.1. The page loads nothing from outside, and all rendered content is escaped.
* **One assistant: the configured model.** The demo has no rule-based stand-in. The chat uses the provider, model id and key in
  `.env` (the model name is shown in the title bar); use synthetic data only. With none configured the chat is off (409
  `NO_MODEL_CONFIGURED`, a banner, disabled message box) while the board, planner and operations panels keep working.

## Routes (`/demo/api/...`)

`config`, `drivers`, `chat`, `driver/{id}/state`, `board`, `planner-booking`, `clock`, `reset`, `cases`,
`cases/{id}/claim`, `cases/{id}/resolve`, `activity`, `deliver`.

* **planner-booking** books a slot as the warehouse planner using a spare load, so you can create a real conflict with a driver's
  hold. The shared unique index decides the winner.
* **deliver** sends queued notices to a stand-in receiver that accepts everything, to show the outbox draining.

## Run it

```powershell
python scripts/run_demo.py
```

Opens `http://127.0.0.1:8800/demo`. See the [demonstration guide](../demonstration-guide.md) for what to do on the page.

## Edge cases and failure scenarios

| Situation | Result | Test |
|---|---|---|
| Request from another machine | 403 | `test_demo_routes_refuse_anyone_but_this_machine` |
| Demo not enabled | routes do not exist | `test_demo_routes_do_not_exist_when_the_demo_is_off` |
| Hostile text in names or messages | escaped | `test_the_page_escapes_everything_it_renders` |
| Hold lapses as time moves | driver told | `test_a_hold_lapses_when_time_moves_on_and_the_driver_is_told` |
| Planner and driver want one slot | one wins, the other conflicts | `test_planner_booking_takes_a_slot_and_a_second_attempt_conflicts` |
| A driver cannot be served | becomes a case a coordinator can work | `test_a_driver_who_cannot_be_served_becomes_a_case_a_coordinator_can_work` |
| No model configured | chat refused with `NO_MODEL_CONFIGURED`; nothing created; board still works | `test_without_a_configured_model_the_chat_is_off_and_there_is_no_stand_in` |
| Old assistant switch | gone from the page and the API | `test_the_page_has_no_rule_based_assistant_switch`, `test_the_model_switch_endpoint_no_longer_exists` |
| Reset | schedule, clock and chats restored | `test_reset_restores_the_schedule_the_clock_and_clears_conversations` |
| Bad input | rejected cleanly | `test_bad_input_is_rejected_cleanly` |
