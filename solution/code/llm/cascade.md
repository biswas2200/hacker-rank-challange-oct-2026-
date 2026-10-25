# Cheap first, careful when needed

Gemini does two jobs in this agent:

1. **Reading the 11 admin-console screenshots** (`loaders/image_loader.py`). An evidence file is
   read once when it arrives, so this cost grows with new evidence, not with new cases.
2. **A second opinion on every request text** (`core/intent.py`). Every new request pays this again.

Before this design, one fresh run cost about **$0.27** ($0.124 intent, about $0.14 screenshots).
The goal was to cut that without losing a single finding, because one missed record is the
$2.4M bug this agent exists to catch.

## How it works

### Screenshots

```python
quick = read(image, thinking="LOW")
reasons = needs_second_look(quick)               # plain code, no model
if reasons:
    answer = careful_read(image)                 # HIGH; once more if it slips the same way
elif spot_check_due():                           # the 1st, 11th, 21st... read that passed
    answer = read(image, thinking="HIGH")        # compared with the quick read
else:
    answer = quick
# After 4 reads, if more than half were flagged, the rest go straight to HIGH.
```

The checks (`ImageLoader.needs_second_look`) each stand for a mistake we saw:

| Check | Mistake it catches |
| --- | --- |
| Personal data that only a non-record carries | case_50: the name and email went onto "user_20" instead of `analytics_7438`. Non-records are dropped, so the exposed data would be lost. |
| The screen is about a known record but does not list it | case_46: the page is about `user_17`, the catalog name of `tombstone_17`, and the quick read left it out. |
| No records at all | An empty read of a screen that always shows something. |

Two more nets cover mistakes the checks do not know yet:

- **Spot check (second net).** Every 10th read that passed the checks is read with HIGH too.
  The pick goes by position in the run, not by chance, so a re-run picks the same screenshot.
  Position says nothing about how good a read is, so the sample stays fair. If the two answers
  differ, the HIGH answer is used and the difference is reported in `usage_report.md`. Over
  many runs, this is the measured miss rate of the checks.
- **Switch (third net).** Paying for a quick read and a careful read costs more than one careful
  read once about 73% of reads get flagged (LOW ~$0.0035 + HIGH ~$0.013 per screenshot). The
  switch fires earlier, at more than 50% flagged after at least 4 reads. A flag rate that high
  also means the quick read is struggling with these screens (a new layout, say).

A careful read can slip too: in one run HIGH made the same case_50 mistake. So a HIGH answer
that fails the checks is read with HIGH once more. The first answer that passes is used; if
both fail, the one with fewer doubts is kept and the run's notes say a person should look.

### Request intent

```text
keyword rules (free)
  -> Gemini LOW: each distinct text once, about 50 per call, answer grouped by label
  -> Gemini HIGH only where the rules and LOW disagree, LOW gave no answer, or the rules found no cue
  -> two of the three opinions decide; if all differ, HIGH decides
```

The keyword rules check every Gemini answer, so the spot check and the switch are not needed
here. Ids that Gemini leaves out of an answer are asked once more (the first version silently
skipped `case_39`).

The answer format matters more than it looks. The first version asked for one
`{"case_id": ..., "intent": ...}` object per request. Gemini often pretty-printed it, and
half of the billed output tokens were spaces and line breaks. The grouped answer
`{"DELETE": ["case_01", ...], "OPT_OUT": [...]}` costs about 10 tokens per request instead
of 14 to 28.

### Saved answers

Each thinking level and each retry has its own cache key, so a careful read can never be
handed the quick answer it is meant to check. Saved answers let an auditor replay a run and
see why a case got its answer (`--no-llm`, `explain`). They are not proof of correctness: a
wrong saved answer stays wrong. `--fresh` ignores them and asks Gemini again, the way a real
run on new data would.

## What we measured

All numbers are live Vertex AI calls to `gemini-3.6-flash` on 2026-10-25. Output is compared
with the run that used HIGH everywhere.

| Screenshot setting | Cost per pass of 11 | Same output.csv |
| --- | --- | --- |
| HIGH on every screenshot | $0.126 - $0.161 | 3 of 3 runs, but HIGH slipped on case_50 in a 4th run |
| LOW on every screenshot | $0.035 - $0.040 | 2 of 4 runs (case_50 lost its PII finding) |
| MINIMAL thinking | $0.033 | lost findings |
| LOW with lower image resolution | $0.035 - $0.048 | 1 of 4 runs; saves under $0.007 |
| Model's own "unsure" list as the trigger | about LOW | caught 0 of 3 wrong reads |
| Code checks as the trigger, 12 saved cheap runs replayed | $0.038 - $0.106 | 12 of 12 |

| Intent setting | Cost for 550 requests | Notes |
| --- | --- | --- |
| v1: one object per request, default thinking | $0.124 | skipped case_39; half the output was whitespace |
| v2: grouped answer, LOW, every request new | $0.048 - $0.054 | 0 disagreements with the rules |
| v2 with each distinct text once (398 of 550) | $0.037 | used in the final run |

The final run's own numbers are in `evaluation/usage_report.md`.

## Where it can still go wrong

- The checks know only the mistakes we have seen. The spot check measures how often a new
  kind slips through, but with 11 screenshots it samples one or two per run.
- If the keyword rules and Gemini LOW make the same mistake, no vote happens.
- Gemini's answers change from run to run. The output is stable across our replays, but a
  new screen layout could still produce a run where both careful reads slip.

## What we would do with more time

1. **Make the screenshot task easier for a cheap read.** Ask Gemini to copy the page header
   (title, ids, badge, name, email) separately from the table rows, and attach header values to
   records in code. Then LOW would not need the attribution reasoning HIGH is paying for.
2. **An adaptive spot-check rate.** Check more often after a spot check finds a difference,
   less often after a long clean streak.
3. **Track the miss rate across runs** and alert when it rises.
4. **A small hand-labelled set of screenshots** to score extraction directly, instead of only
   through changes in output.csv.
5. **Short batching windows for live traffic.** When requests arrive one at a time, group them
   for a few minutes, so each intent call still covers many requests.
6. **Recompute the switch point when prices change** (the introductory rate doubles in 2027).
