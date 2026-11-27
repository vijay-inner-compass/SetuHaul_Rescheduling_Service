# Work log

Append-only. Newest entries at the bottom.

---

## [2026-11-27] setup | docs/ wiki scaffolded

- Categories: concepts - architecture - subsystems - decisions - reference - operations - history
- Baseline commit: `f5e477e`
- Created: `docs/{CLAUDE,index,log}.md`, `raw/`, 4 `.claude/commands/docs-*`,
  and `.claude/scripts/docs-lint.py`

Started in grow mode: one page, [[refusal-messages]].

## [2026-11-27] add | [[model-calls]]

- Written with the connection-test fix.

## [2026-11-27] sync | all documentation brought up to `b5b462f`

- Wiki pages: [[refusal-messages]] (line citation moved) and [[model-calls]] verified.
- Updated README and the current-behaviour documents outside the wiki. Plans, specs, verification records, `Archieve/`,
  the slide decks and the submission PDFs were left as they are.
- Proposed wiki pages for warehouse operations, passes and gate, round-the-clock slots, sign-in.

## [2026-11-27] add | [[warehouse-operations]], [[passes-and-gate]], [[round-the-clock-slots]], [[sign-in]]

- The four pages proposed by the sync, approved by the user. Checked against `b5b462f`.

## [2026-11-28] fix | real-model run statements

- The documents said the one-slot flow had no recorded real-model run. `250d283` (2026-11-26) added English, Hindi and Hinglish
  recordings. Current-behaviour documents corrected; details in `verification/evaluation-plan.md`.
- Decision register entries and other dated records were left as they are.

## [2026-11-28] add | [[guardrails]], [[evals]]

- Requested by the user: all guardrails and evals in one lookup pair. Checked against `8c1a551`.

## [2026-11-28] add | [[driver-notices]]

- Written with the change that queues partner answers and no-load case updates. Checked against the work on top of `b6501b0`.

## [2026-11-28] verify | [[guardrails]], [[evals]]

- Rechecked after merging `main` (`8a5487e`): confirm and cancel intent checks, crash and Redis-outage behaviour, moved line
  citations.
