# SetuHaul Rescheduling Service -- documentation

Dock appointment rescheduling for warehouses and drivers.

This directory is a wiki maintained by an LLM: linked markdown pages, nothing outside the
repository. The rules are in [`CLAUDE.md`](CLAUDE.md).

## Start here

1. [[warehouse-operations]]
2. [[passes-and-gate]]
3. [[refusal-messages]]

The wiki is new and grows as work touches each area.

---

## concepts

None yet.

## architecture

None yet.

## subsystems

- [[refusal-messages]] -- how a refused request says what to fix
- [[model-calls]] -- why a driver message can fail when the connection test passes
- [[warehouse-operations]] -- what staff can do to a booking or a driver, and how the driver is told
- [[passes-and-gate]] -- how a Dock Access Pass stays true as bookings change
- [[round-the-clock-slots]] -- 24-hour opening and slots created ahead
- [[sign-in]] -- web sessions and one-time password links
- [[driver-notices]] -- what reaches a driver's phone, and how to add to it

## decisions

None yet.

## reference

None yet.

## operations

- [[backup-and-restore]] -- consistent SQLite + `SETUHAUL_SETTINGS_KEY` backup and restore procedure

None yet.

## history

None yet.

---

## Using this wiki

```
/docs-sync     right after a deploy or release -- reads only what changed since verified_at
/docs-lint     integrity check (orphans, broken links, broken sources, stale, unverified)
/docs-query    ask the docs. If it is not there, the answer says so
/docs-status   commits not yet reflected, stale pages, recent work
```

Every page carries `verified_at`, the commit it was checked against. Change code, run
`/docs-sync`.
