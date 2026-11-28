# SetuHaul Rescheduling Service — Enterprise Documentation Suite

| Field | Value |
|---|---|
| **Repository** | `SetuHaul_Rescheduling_Service` |
| **Baseline Commit** | `445c7f965e22e7915238cca5631e68b4462d7b7b` |
| **Generated Date** | `2026-11-28` |
| **System Status** | Working prototype (verified on Windows; not production-deployed) |
| **Existing Docs Preservation** | All pre-existing files under `docs/` were preserved untouched |

---

## 1. Documentation Applicability Matrix & Master Index

Every category from the Enterprise Software Documentation Intelligence framework was assessed against the repository before generation:

| Category | Doc ID | Document | Applicability | Status |
|---|---|---|---|---|
| **Knowledge Model** | `KM-00` | [Software Knowledge Model (`knowledge-model.json)](knowledge-model/knowledge-model.json) & [Guide] | **Applicable** | Complete |
| **1 — Business & Product** | `DOC-01` | [Business Case & BRS](01-business/DOC-01-business-case-and-brs.md) | **Applicable** (financials) | Complete |
| | `DOC-02` | [PRD & Business Capability Map](01-business/DOC-02-prd-and-capability-map.md) | **Applicable** | Complete |
| | `DOC-03` | [Business Process Specification & Business Rules Catalogue](01-business/DOC-03-business-processes-and-rules.md) | **Applicable** | Complete |
| **2 — Requirements & UX** | `DOC-04` | [SRS & FRS](02-requirements/DOC-04-srs-and-frs.md) | **Applicable** | Complete |
| | `DOC-05` | [Use Cases, User Stories, Acceptance Criteria & Feature Catalogue](02-requirements/DOC-05-use-cases-user-stories-and-features.md) | **Applicable** | Complete |
| | `DOC-06` | [UI Specification](02-requirements/DOC-06-ui-specification.md) | **Applicable** (SSR Web UI + 11 JS/CSS assets) | Complete |
| **3 — Architecture & Design** | `DOC-07` | [Software Architecture Document & HLD](03-architecture/DOC-07-software-architecture-and-hld.md) | **Applicable** | Complete |
| | `DOC-08` | [Low-Level Design (LLD)](03-architecture/DOC-08-low-level-design.md) | **Applicable** | Complete |
| | `DOC-09` | [Integration Architecture & API Specification](03-architecture/DOC-09-integration-and-api-specification.md) | **Applicable** (77 HTTP routes, 6 external integrations) | Complete |
| | `DOC-10` | [Data Architecture Specification](03-architecture/DOC-10-data-architecture.md) | **Applicable** (18 base tables + 27 migrations) | Complete |
| | `DOC-11` | [Architecture Decision Records (ADRs)](03-architecture/DOC-11-architecture-decision-records.md) | **Applicable** (D1–D107 + ADR-12) | Complete |
| **4 — Security, Privacy & Governance** | `DOC-12` | [Security Architecture & Threat Model](04-security/DOC-12-security-architecture-and-threat-model.md) | **Applicable** | Complete |
| | `DOC-13` | [Privacy Specification, Risk Register, SBOM & Governance Assessment](04-security/DOC-13-privacy-risk-sbom-and-governance.md) | **Applicable** (100 locked pkgs; DPDP/GDPR) | Complete |
| **5 — Quality & Testing** | `DOC-14` | [Test Strategy & Test Plan](05-testing/DOC-14-test-strategy-and-plan.md) | **Applicable** (2,127 pytest tests) | Complete |
| | `DOC-15` | [Functional Test Cases, Integration Spec, Regression & UAT](05-testing/DOC-15-test-cases-regression-and-uat.md) | **Applicable** (UAT execution) | Complete |
| **6 — Deployment & Operations** | `DOC-16` | [Deployment Architecture, Installation, Config & CI/CD](06-operations/DOC-16-deployment-installation-and-config.md) | **Applicable** (Docker/Compose/Hostinger) | Complete |
| | `DOC-17` | [Operations Runbook & Troubleshooting Guide](06-operations/DOC-17-operations-runbook-and-troubleshooting.md) | **Applicable** | Complete |
| **7 — Guides & Manuals** | `DOC-18` | [Developer Onboarding & Codebase Navigation Guide](07-manuals/DOC-18-developer-onboarding-and-codebase-guide.md) | **Applicable** | Complete |
| | `DOC-19` | [User Manual & Administrator Manual](07-manuals/DOC-19-user-and-administrator-manual.md) | **Applicable** | Complete |
| **8 — Tech Debt & Modernization** | `DOC-20` | [Technical Debt Register, Maintainability & Modernization Roadmap](08-modernization/DOC-20-tech-debt-maintainability-and-modernization.md) | **Applicable** (12 TD items) | Complete |
| **9 — Traceability & Governance** | `DOC-21` | [Bidirectional Requirements Traceability Matrix](09-governance-and-traceability/DOC-21-requirements-traceability-matrix.md) | **Applicable** | Complete |
| | `DOC-22` | [Source Evidence Register & Documentation Gap Register](09-governance-and-traceability/DOC-22-evidence-and-gap-register.md) | **Applicable** (16 EV + 12 GAP items) | Complete |
| | `DOC-23` | [Stakeholder Clarification Questionnaire](09-governance-and-traceability/DOC-23-stakeholder-clarification-questionnaire.md) | **Applicable** (6 BLOCKER + 12 enterprise Qs) | Complete |
| | `DOC-24` | [Documentation Quality Assessment & Continuous Docs](09-governance-and-traceability/DOC-24-documentation-quality-and-continuous-docs.md) | **Applicable** | Complete |

*Not Applicable (Explicitly Excluded)*: Native mobile app UI specs (drivers use Telegram), multi-region Kubernetes/Terraform IaC specs (single-container Docker Compose target), stored-procedure catalogues (SQLite uses Python services + 6 immutability triggers only).

---

## 2. Editable Architecture Diagrams (`diagrams/)
1. [01-c4-context.mmd](diagrams/01-c4-context.mmd) — C4 System Context
2. [02-c4-container.mmd](diagrams/02-c4-container.mmd) — C4 Container Architecture
3. [03-c4-component.mmd](diagrams/03-c4-component.mmd) — C4 Component (Scheduling & AI Engine)
4. [04-seq-driver-reschedule.mmd](diagrams/04-seq-driver-reschedule.mmd) — Sequence: Driver Delay -> Auto-Hold -> Confirm
5. [05-seq-dock-access-pass-gate.mmd](diagrams/05-seq-dock-access-pass-gate.mmd) — Sequence: Dock Access Pass & Gate Check-In/Out
6. [06-seq-partner-and-facility-change.mmd](diagrams/06-seq-partner-and-facility-change.mmd) — Sequence: Facility Change & Partner Email
7. [07-erd-core-data-model.mmd](diagrams/07-erd-core-data-model.mmd) — Entity Relationship Diagram
8. [08-state-machines.mmd](diagrams/08-state-machines.mmd) — Domain State Machines
9. [09-data-flow-and-security-trust-boundaries.mmd](diagrams/09-data-flow-and-security-trust-boundaries.mmd) — Data Flow & Security Trust Boundaries
10. [10-deployment-and-integration.mmd](diagrams/10-deployment-and-integration.mmd) — Deployment & Integration Topology
11. [11-business-process-rescheduling.mmd](diagrams/11-business-process-rescheduling.mmd) — End-to-End Business Process
12. [12-class-domain-core.mmd](diagrams/12-class-domain-core.mmd) — Core Domain & Gateway Class Diagram

---

## 3. Continuous Documentation Tooling (`tools/)
- [`tools/impact_check.py`](tools/impact_check.py): Read-only CLI that compares `git diff` against `knowledge-model.json` to list impacted enterprise documents for incremental updates.
