# SetuHaul Rescheduling Service -- documentation

Dock appointment rescheduling for warehouses and drivers.

This directory is a wiki maintained by an LLM: linked markdown pages, nothing outside the
repository. The rules are in [`CLAUDE.md`](CLAUDE.md).

## Start here

1. [[refusal-messages]]

The wiki is new and grows as work touches each area.

---

## concepts

None yet.

## architecture

None yet.

## subsystems

- [[refusal-messages]] -- how a refused request says what to fix
- [[model-calls]] -- why a driver message can fail when the connection test passes

## decisions

None yet.

## reference

None yet.

## operations

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
