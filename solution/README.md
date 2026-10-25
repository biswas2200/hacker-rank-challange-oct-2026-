# Ghost Data Still Exists: deletion-audit agent

Northstar Shop's deletion workflow said "Complete" while a customer's data still sat in a
search index, a replica, a backup and a sync job. This agent checks every deletion request
against the evidence and decides whether the data is really gone. For each request it:

1. decides whether the text actually asks for deletion (otherwise `NOT_APPLICABLE`);
2. finds the person, including old emails and phone numbers from the change history;
3. finds every record that belongs to them in databases, logs, PDFs, screenshots, exports
   and backups, using only evidence captured at or before `audit_at`;
4. works out each record's real state at `audit_at` (claims in workflow logs are not proof);
5. applies `deletion_rules.csv` (delete, redact, anonymize, retain, or keep a tombstone);
6. writes the findings, an ordered fix plan and the checks that prove the final state.

## Setup

Tested with Python 3.14.

```bash
python -m venv .venv
source .venv/bin/activate          # Windows: .venv\Scripts\activate
pip install -r requirements.txt
```

Put the challenge `dataset/` folder next to `code/` (the repository root). Paths are resolved
from `code/config.py`, so the program works from any folder.

### Gemini access (only for live model calls)

Gemini runs on Vertex AI with Application Default Credentials. There is no API key.

```bash
gcloud auth application-default login
gcloud config set project <your-gcp-project>   # or set GOOGLE_CLOUD_PROJECT
```

Optional settings are listed in `.env.example` (model, location, prices). Without Google
credentials, add `--no-llm`: the saved model answers in `code/llm/cache/` replay the final
run exactly.

## Run

```bash
python code/main.py run              # audit all 518 requests -> output.csv + code/evaluation/usage_report.md
python code/main.py validate         # check output.csv against the output contract
python code/main.py eval             # score samples + custom cases -> code/evaluation/evaluation_report.md
python code/main.py explain case_11  # every intermediate result for one case
python code/main.py index-report     # how many records each evidence file gave
```

| Flag | Meaning |
| --- | --- |
| `--no-llm` | Never call Gemini; use saved answers and the keyword rules. Offline and free. |
| `--fresh` | Ignore saved answers and ask Gemini again, the way a run on new data would. |

`output.csv` is written at the repository root with the columns
`case_id,deletion_status,steps_json`, one row per request, sorted by case number.

## How it works

```text
deletion_requests.csv
  -> intent        core/intent.py         keyword rules, Gemini second opinion, vote on disagreement
  -> identity      core/identity.py       current + historical emails, phones, user_id, email hashes
  -> evidence      loaders/ + core/index.py   SQL, JSON, logs, text, PDF, screenshots -> one index
  -> linking       core/linking.py + core/workflow_matching.py + core/episodes.py
  -> state         core/state.py          what is true at audit_at (replica drift, restores, syncs)
  -> policy        core/policy.py         deletion_rules.csv -> required treatment per record
  -> plan          core/planner.py        findings, remediation in safe order, verification
  -> output        output/writer.py + output/validator.py
```

Gemini (`gemini-3.6-flash`) does two jobs, each cheap first and careful only when needed:

- **Screenshots** have no text layer, so Gemini copies their records into JSON. Each
  screenshot is read with LOW thinking first. Plain-code checks send doubtful reads to a
  HIGH-thinking read. A spot check re-reads every 10th passed read, and a switch turns the
  cheap pass off if more than half of the reads get flagged.
- **Request intent**: Gemini gives a second opinion on every request text. When it disagrees
  with the keyword rules, a HIGH-thinking read breaks the tie.

`code/llm/cascade.md` explains this design and the measurements behind it. Prompts live in
`code/llm/prompts/`.

## Reports

- `code/evaluation/evaluation_report.md`: status, step, field and sequence accuracy on the 32
  public samples, 3 custom cases (`code/evaluation/custom_cases.py`), and failures found and
  fixed.
- `code/evaluation/usage_report.md`: model calls, tokens and cost of the final run.

## Layout

```text
code/main.py            command-line entry point
code/config.py          paths, model and prices (no secrets)
code/models.py          data classes shared by every stage
code/repository.py      reads the CSV files in dataset/
code/core/              intent, identity, linking, state, policy, planning
code/loaders/           one loader per evidence format
code/llm/               Gemini client, cascade, prompts, saved answers
code/output/            output.csv writer and contract validator
code/evaluation/        evaluator, custom cases, reports
```
