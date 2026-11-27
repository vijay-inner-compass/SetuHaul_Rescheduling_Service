# SetuHaul: FDE Academy submission

**Name:** Vijay Madhusudan Kulkarni

**Repo:** https://github.com/vijay-inner-compass/SetuHaul_Rescheduling_Service

**Live demo link:** not provided

**Stack used:** Python 3.12, FastAPI 0.164.0, Pydantic 2.14.3, LangGraph 1.2.23, SQLite, Redis client 8.1.0, pytest 9.2.0

## 1. Problem Definition Sheet and data audit
* [Problem definition](https://github.com/vijay-inner-compass/SetuHaul_Rescheduling_Service/blob/main/docs/problem-definition.md)
* [Data audit](https://github.com/vijay-inner-compass/SetuHaul_Rescheduling_Service/blob/main/docs/data-audit.md)
* [Data issues with queries](https://github.com/vijay-inner-compass/SetuHaul_Rescheduling_Service/blob/main/docs/verification/data-issues.md)

**Top data issue:** two active appointments are on docks that cannot take their weight (SHP1005, 20,500 kg on D3;
SHP1014, 21,000 kg on D1; both docks are limited to 20,000 kg).

## 2. Decision register
* [Answers to the 13 decisions](https://github.com/vijay-inner-compass/SetuHaul_Rescheduling_Service/blob/main/docs/decision-answers.md)

**How competing requests are ordered:** after hard eligibility checks, the first successful hold commit wins. Priority is
advisory and never takes a held slot.

## 3. Prototype: how to run it
After install (see the README), one command:

```powershell
.\.venv\Scripts\python scripts\run_demo.py --auto-login
```

## 4. Scenario run
**Video:** not yet recorded

**Script:** `python scripts/run_scenario.py`,
[transcript and 23 checks](https://github.com/vijay-inner-compass/SetuHaul_Rescheduling_Service/blob/main/docs/verification/scenario-run.md)

**Which case escalates and why:** DRV014 / SHP1014. The 21,000 kg load needs 75 minutes, no single 60-minute slot works,
so the service opens a `NO_SLOT` case and changes nothing.

## 5. Integrity test
* [Concurrency and integrity proof](https://github.com/vijay-inner-compass/SetuHaul_Rescheduling_Service/blob/main/docs/verification/concurrency-proof.md)

**Result:** 320 requests for one slot (20 rounds of 10 threads and 20 rounds of 6 processes), 40 holds (one per round),
0 double bookings. The contested action is the hold that reserves the slot.

## 6. Client readout
* [One-page readout](https://github.com/vijay-inner-compass/SetuHaul_Rescheduling_Service/blob/main/docs/client-readout.md)

**Optional scheduling tool:** not attempted

## What does not work yet
* Only one recorded real-model run per language (2026-11-26), none since the 2026-11-27 changes.
* No labour or consignee feed: on the supplied data alone no otherwise feasible slot can be offered, and screen-created loads rest on
  figures warehouse staff type.
* A driver is not told why an earlier slot went to someone else.
* Hindi and Hinglish wording is unreviewed.
* No real driver has used it.
