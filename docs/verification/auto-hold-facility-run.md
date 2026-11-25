# Auto-hold and facility change: verification run (2026-11-25)

Demo app on this computer, rule-based stand-in model, demo clock. **No real model, Telegram, partner email or Google
sign-in was used.**

## Gates
```
ruff check src tests    All checks passed!
mypy src                Success: no issues found in 71 source files
pytest tests -q         1305 passed
```
Evidence scripts `show_phase3/4/5/6/8/9.py`, `run_scenario.py` (23/23) and `replay_traces.py` (6/6 traces, 58/58 checks) ran
with exit code 0. `run_integrity_proof.py`, `run_scale_profile.py` and `run_live_model_scenario.py` were **not** run.

## Smoke: delay, No, change request, approve, Yes (driver Manoj Sharma)
```
DRIVER: I will be about two hours late
  REPLY: I am holding 13:00-14:00 at dock D1 on 04 Aug 2026, SetuHaul Jaipur Distribution Centre, for you until 09:38. It is
         not booked yet. Reply Yes to confirm, or No for the next slot. (plus the GPS note)
DRIVER: No
  REPLY: I am holding 13:00-14:00 at dock D2 ... until 09:38. It is not booked yet.
DRIVER: Please change facility to Gurugram
  REPLY: I have asked SetuHaul Gurugram Cross-Dock to take this load. Your destination has not changed yet ...
WAREHOUSE sees: ORD-260804-006 SetuHaul Jaipur Distribution Centre -> SetuHaul Gurugram Cross-Dock
APPROVE: APPROVED, outcome HELD
  DRIVER IS TOLD: The move to SetuHaul Gurugram Cross-Dock is approved. I am holding 16:00-17:00 at dock D1 ... until 09:51.
DRIVER: Yes, confirm
  REPLY: Your appointment is confirmed: dock D1, 16:00-17:00 on 04 Aug 2026. Reference APT-H-... Warehouse: SetuHaul Gurugram Cross-Dock.
```

## Browser check: found and fixed
| Found | Fix | Proof |
|---|---|---|
| A closer-truck GPS ping still sent a numbered 1/2/3 menu (found reading the code) | One slot is held for 15 minutes and offered with Yes / No | `tests/test_edge_cases.py` |
| Approve and Reject (and every other action button inside a refreshed section) did nothing after the warehouse screen refreshed itself | One click listener on the document | reproduced and re-checked in the browser; `tests/test_live_chat.py` |
| After an approved partner move, the hold at the old warehouse stayed live and "Yes" could book it | The hold is released when the partner is asked | `tests/test_facility_follow_up.py` |
| The driver card listed the old booking under the new warehouse's name | The booking says which warehouse it is at | `tests/test_facility_follow_up.py` |

## Not verified
Real model wording and tool choice; Telegram; real partner email; Hindi and Hinglish by a native speaker; phone-width layout
of the new requests table; other browsers.
