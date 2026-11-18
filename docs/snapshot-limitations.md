# What the Snapshot Cannot Prove

- **Live labour and equipment capacity.** No table holds crew, forklift or shift data.
- **Consignee commitments.** No latest-arrival windows, penalties or contract terms.
- **Production traffic.** 21 shipments, 106 slots and 2 facilities on one day (2026-08-04) say nothing about the brief's daily volume or 30-minute spikes. Phase 11 uses a generated, labelled scale fixture.
- **Concurrency behaviour.** A static file has no contending writers; Phase 7 creates them.
- **Real model behaviour.** There are no live-model transcripts; Phase 10 collects them.
- **Hold behaviour.** No hold state exists in the data.
- **Phone/identity provisioning.** `drivers.phone` has a unique index, but there is no webhook, session or channel mapping. The phone format is `+91-9000010001`, which needs E.164 normalisation in Phase 3.
- **Statistical success.** Outcome rates from this data would not be evidence of operational success. Thresholds are reported with numerator and denominator and not extrapolated.
- **Freshness.** The snapshot is a point in time; stale-state handling is tested with injected clocks, not observed.
