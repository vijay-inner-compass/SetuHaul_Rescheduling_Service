# Software Knowledge Model — Schema & Usage Guide

| Field | Value |
|---|---|
| **Document ID** | `KM-00` |
| **Version** | `1.0.0` |
| **Commit Reference** | `445c7f965e22e7915238cca5631e68b4462d7b7b` |
| **Validation Status** | Verified against repository source |

## 1. Purpose
[`knowledge-model.json`](knowledge-model.json) is the structured, machine-readable single source of truth extracted during Phase 1 & Phase 2 reverse-engineering of `SetuHaul_Rescheduling_Service`. Every enterprise document under `docs/enterprise/` references its stable identifiers to guarantee cross-document consistency and enable automated impact analysis during continuous documentation regeneration (Phase 8).

## 2. Identifier Taxonomy

| Prefix | Domain | Example |
|---|---|---|
| `BC-` | Business Capability | `BC-01` Conversational Driver Rescheduling |
| `ACT-` | Actor / Role | `ACT-02` Warehouse Authority |
| `BR-` | Business Rule / Invariant | `BR-06` FCFS Hold Allocation & Contention |
| `FR-` | Functional Requirement | `FR-04` Auto-Hold Acquisition, Confirmation, Decline & Expiry |
| `NFR-` | Non-Functional Requirement | `NFR-01` Concurrency & Integrity |
| `CMP-` | Software Component | `CMP-03` ETA, GPS & Feasibility Engine |
| `TD-` | Technical Debt Item | `TD-02` Divergent 'truck on site' predicates |
| `RSK-` | Risk Register Item | `RSK-01` Unvalidated live-LLM behavior |
| `GAP-` | Documentation Gap / Discrepancy | `GAP-01` Migration count drift |
| `DOC-` | Enterprise Document | `DOC-07` Software Architecture Document & HLD |

## 3. Evidence Classification Standard
- **`Verified`**: Directly supported by source code, SQL migrations, test suites, or recorded verification artifacts.
- **`Inferred`**: Reasonably derived from repository structure, configuration, or conventions.
- **`Unverified`**: Implemented or claimed, but not yet validated against real external systems or production workloads.
- **`Missing`**: Not present in the repository; requires stakeholder clarification (see [`DOC-23`](../09-governance-and-traceability/DOC-23-stakeholder-clarification-questionnaire.md)).

## 4. Continuous Documentation Integration
Run `python docs/enterprise/tools/impact_check.py <base_commit>` to map changed source paths via `components[].paths` -> `components[].affected_docs` and identify which enterprise documents require incremental review or regeneration.
