# Final execution summary — Celstream BD Live PoC

14 November 2026. Every figure below was read back from the live Apollo and HubSpot APIs
(`bdlive/verify.py`; raw output in `logs/verify_final.json`). Overall status: **demonstration-ready,
with three open items** (sting.ly incomplete; 1 company + 3 contacts awaiting a delete decision).

## Runs

| Run | Trigger | Scope | Status | Notes |
|---|---|---|---|---|
| 1 | CLI | 1 new job | success | First end-to-end record |
| 2 | CLI | 3 new jobs | partial* | 3 companies had no contacts. Originally recorded as success because of a status bug; corrected |
| 3 | Dashboard | 3 new jobs + retry | partial | sting.ly has no decision-makers |
| 4 | CLI `--reprocess-existing` | Same 7 jobs re-fetched | partial | Idempotency test, 0 records created |
| 5 | Dashboard | 2 new jobs + retry | partial | Final run. sting.ly still has no decision-makers |

## Final verified state

| Item | Result |
|---|---|
| TheirStack jobs retrieved | 9 real postings, no duplicates |
| OpenAI analyses | 9/9 succeeded (`gpt-6-luna`, prompt v3), 0 failed, total cost $0.011 |
| Capability scores | 9 job-level capability scores (58–100) and 9 company opportunity scores: 7 High (83.33), 2 Medium (66.66) |
| Apollo accounts | 8 valid accounts, each with `Celstream BD` and the correct `Priority: High/Medium` label, plus 1 rejected (Clay) |
| Apollo contacts | 14 valid contacts, all linked to the correct account, plus 3 rejected |
| HubSpot companies | 8 valid companies with all 12 `celstream_*` properties filled, plus 1 rejected. Exactly 1 company per domain |
| HubSpot contacts | 14 valid contacts, all associated with their company; exactly 1 record per Apollo person ID. Plus 3 rejected |
| HubSpot properties | 9 created through the API with the existing token (7 company, 2 contact). Existing properties were not modified |

## Duplicate prevention (run 4)

The same 7 jobs were re-fetched by exact TheirStack job ID and every stage was re-run.

- **Created 0:** jobs, companies and contacts.
- **Updated:** 7 companies and 12 contacts on each platform.
- **Unchanged:**
  - Apollo totals (7 accounts / 12 contacts) and HubSpot portal totals (8 companies / 14 contacts).
  - Every Apollo and HubSpot ID.
  - All associations.
  - OpenAI cost: $0, because already-analysed jobs are not sent again.

In run 5, the platform totals grew by exactly what the run created: Apollo +2 accounts and +5
contacts, HubSpot +2 companies and +5 contacts.

## Missing, incomplete or rejected records

| Record | Status | Reason | Action |
|---|---|---|---|
| Stingray (sting.ly) | partial | Apollo has no decision-maker roles for this domain. The company exists in both CRMs with 0 contacts | Retried automatically on each run |
| Ahmed Abdelazeez, "Capability Owner \| Middleware Platform" (Coretura) | rejected | Title matches no `apollo.yaml` role; it was picked by an "owner" keyword bug, since fixed. Johan Lunden (CEO) has replaced it | **Awaiting decision:** delete in Apollo and HubSpot |
| Clay (insideclay.com): account/company plus 2 contacts | rejected | TheirStack attached Clay, the B2B GTM platform, to `insideclay.com`. Apollo identifies that domain as "CLAY Health Club + Spa" (NYC) | **Awaiting decision:** delete in Apollo and HubSpot |

Rejected records are excluded from dashboard counts, retries and future syncs. Nothing has been
deleted externally.

## Remaining limitations

- **Company identity:** depends on the domain TheirStack provides; the Clay case shows it can be
  wrong. There is no automatic check yet that the Apollo organisation matches the hiring company.
- **Last names:** Apollo returns only the initial for some people (e.g. "Robin D").
- **Apollo scores:** Apollo has no custom fields for scores, so score and priority are stored as
  labels. The full score detail is in HubSpot and the dashboard.
- **Scoring:** most companies score 83.33 because each has a single fresh job with 3+ capability
  matches. That is how the unchanged `scoring.yaml` rules behave on this data.
- **OpenAI token limit:** the configured 2,000-token output limit is too small for `gpt-6-luna`.
  The app escalates once to 6,000 tokens per process, which costs one extra call per run.
- **Tests:** 8 smoke tests run against in-memory fakes. The live proof is the runs above.
