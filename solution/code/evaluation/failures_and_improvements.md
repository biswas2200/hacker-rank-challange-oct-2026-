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
