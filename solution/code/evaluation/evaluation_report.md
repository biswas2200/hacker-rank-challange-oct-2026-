# Evaluation report

Written by `python code/main.py eval`. Sections 1-3 are measured by running the agent;
section 4 is our own account of what went wrong and what we changed.

## 1. Public samples: status, step, field and sequence accuracy

The 32 cases in `dataset/sample_output.csv`.

| Metric | Value |
| --- | --- |
| Cases | 32 |
| Status accuracy | 32/32 (100.0%) |
| Sequence accuracy (whole array identical) | 25/32 (78.1%) |
| Step exact match by position | 179/193 (92.7%) |
| Step precision (order ignored) | 185/186 (99.5%) |
| Step recall (order ignored) | 185/193 (95.9%) |
| Step F1 (order ignored) | 97.6% |

| Field | Agreement by position |
| --- | --- |
| step_no | 186/193 (96.4%) |
| phase | 186/193 (96.4%) |
| system_id | 180/193 (93.3%) |
| resource_id | 179/193 (92.7%) |
| operation | 180/193 (93.3%) |
| state | 180/193 (93.3%) |
| evidence_id | 186/193 (96.4%) |

Samples that are not an exact match, and why:

- case_04: missed account_004 (appears in no dataset file), analytics_901 (not linked)
- case_18: missed audit_record_6645 (appears in no dataset file)
- case_20: missed audit_record_5569 (appears in no dataset file)
- case_21: missed analytics_5335 (not linked)
- case_23: missed account_410 (not linked); extra account_7015
- case_25: missed analytics_1895 (not linked)
- case_26: missed analytics_4566 (not linked)

## 2. Custom evaluation cases (3 of 3 passed)

Each case changes one thing about a public sample the agent already answers exactly, so the
expected answer is known (code/evaluation/custom_cases.py).

| Case | Based on | Expected | Got | Steps identical | Intent decided by | Result |
| --- | --- | --- | --- | --- | --- | --- |
| custom_01 | case_01 | NOT_APPLICABLE (0 steps) | NOT_APPLICABLE (0 steps) | yes | EXPORT (vote: rules DELETE, LOW EXPORT, HIGH EXPORT) | pass |
| custom_02 | case_12 | INCOMPLETE (5 steps) | INCOMPLETE (5 steps) | yes | DELETE (deleting) | pass |
| custom_03 | case_06 | INCOMPLETE (4 steps) | INCOMPLETE (4 steps) | yes | DELETE (vote: rules no cue, LOW DELETE, HIGH DELETE) | pass |

- **custom_01**: A request that uses the word 'delete' but does not ask for deletion. The keyword rules say DELETE, so the Gemini vote has to overrule them.
  - email: `amina.elmasri@gmail.com`
  - request text: "I am not asking you to delete anything yet. Before I decide, please send me a copy of the personal data you hold about me."
- **custom_02**: The request comes from the person's other email address in user_audit_log, so the identity has to be resolved through the change history.
  - email: `minjun.kim90@gmail.com`
  - request text: "I changed the email on this account before deleting it. Please check that nothing tied to the old address was left behind, even in systems that do not display the email."
- **custom_03**: A deletion request in German, a language the keyword rules do not cover, so only Gemini can tell that it asks for removal.
  - email: `jonas.berg@outlook.com`
  - request text: "Mein Konto ist schon weg, aber mein altes Profil erscheint noch in der Suche. Bitte entfernen Sie auch dieses Suchergebnis."

## 3. Full dataset run

- output.csv: 518 rows (COMPLETE 156, INCOMPLETE 352, NOT_APPLICABLE 10)
- Contract validator (`python code/main.py validate`): 0 issue(s)
- The hidden answers are not available, so the full run is checked against the contract, not scored.

## 4. Failures found and improvements made

Each item: what went wrong, how we found it, and what we changed.

1. **Deleted records name nobody.** A correctly deleted account or an anonymized analytics row
   has no email left, yet the sample answers still verify it. Found with `link-check`
   (expected records per sample vs linked records). Change: `WorkflowWindowMatcher` links
   records through the deletion workflow's own activity in a window around `reported_at`.
   Expected sample records found went from 98 to 103 of 111.
2. **Evidence from an earlier audit leaked into a later one.** Some people file twice
   (case_61 then case_07). Change: `RequestEpisodes` lets each request use only evidence
   after the same person's previous audit.
3. **Non-deletion requests written to look like deletions** ("remove me from marketing lists
   only", "not a request to erase it"). Change: refusal cues are checked before deletion
   cues, then the topic decides. All 10 non-deletion requests are found. custom_01 shows the
   gap that is left: "I am not asking you to delete anything yet" fools the rules, and the
   Gemini vote has to catch it.
4. **Fake records from screenshots.** Gemini sometimes named a person (`user_20`) or a
   storage bucket (`northstar-customer-data`) as a record's parent, and the linker audited
   those names as records: 2 made-up steps in output.csv. Found while measuring cost, when a
   fresh Gemini run changed case_50. Change: parents pass the same resource-id check as
   records, and the validator now rejects any `resource_id` that does not look like one.
5. **COMPLETE checks with no evidence file.** Some records are proven only by activity rows.
   Change: those checks cite the record's latest `activity_id`.
6. **Screenshot reads change from run to run.** With the same settings, 5 of 11 screenshots
   gave different record lists. LOW thinking lost case_50's personal-data finding in 2 of 4
   runs, and HIGH made the same slip in 1 of 4. The model's own "unsure" flag caught 0 of 3
   wrong reads. Change: a cheap-first cascade where plain-code checks decide the careful
   second look, with a retry, a spot check and a switch (`code/llm/cascade.md`). Final run:
   the same output.csv as reading every screenshot carefully.
7. **Paying for a second opinion that changed nothing.** Gemini's intent label was used for 0
   of 550 requests, half of its output tokens were JSON whitespace, and it silently skipped
   case_39. Change: answers grouped by label, LOW thinking, each distinct text asked once,
   missing ids asked again, and a HIGH vote only on disagreement. Intent cost went from
   $0.124 to $0.037, and a full run from about $0.27 to $0.15.
8. **Known failures we left.** Seven samples are not an exact match (section 1). Three
   expected records (`account_004`, `audit_record_6645`, `audit_record_5569`) appear in no
   dataset file, so no linker can find them. When several requests share a `reported_at`,
   pairing deleted records to requests is a guess (marked "paired guess" in `explain`). We
   chose not to tune rules to these seven cases.
9. **One false alarm in the final run.** Gemini copied a file's own name into the `name`
   field on the object-storage screen (evidence_46), so the personal-data check flagged it and
   paid for one careful read (~$0.025). Next step: ignore a `name` equal to the record's own id.
