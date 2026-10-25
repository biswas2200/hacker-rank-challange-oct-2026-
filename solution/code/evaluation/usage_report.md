# Token usage and cost

Final full-dataset run that produced output.csv (518 requests).

| Provider | Model | Purpose | Thinking | Model calls | Live this run | From cache | Input tokens | Output tokens | Thinking tokens | Total tokens | Cost (USD) |
| --- | --- | --- | --- | --- | --- | --- | --- | --- | --- | --- | --- |
| Google Vertex AI | gemini-3.6-flash | intent | LOW | 8 | 8 | 0 | 20,425 | 3,720 | 2,029 | 26,174 | $0.0369 |
| Google Vertex AI | gemini-3.6-flash | media | HIGH | 3 | 3 | 0 | 5,479 | 1,468 | 17,800 | 24,747 | $0.0764 |
| Google Vertex AI | gemini-3.6-flash | media | LOW | 11 | 11 | 0 | 20,101 | 5,075 | 1,036 | 26,212 | $0.0380 |
| Google Vertex AI | gemini-3.6-flash | **overall** | all | 22 | 22 | 0 | 46,005 | 10,263 | 20,865 | 77,133 | $0.1512 |

- Total tokens: 77,133; average tokens per request: 148.9
- Estimated total cost: $0.1512; estimated cost per request: $0.000292
- One provider and one model (gemini-3.6-flash), so the overall row is also the per-model total.

Prices: $0.75 per 1M input tokens and $3.75 per 1M output tokens (Vertex AI introductory rate for gemini-3.6-flash, valid through 2026-12-31). Thinking tokens are billed as output.

Gemini does two jobs, each cheap first and careful only when needed (code/llm/cascade.md):

- `media`: reads the 11 admin-console screenshots. Every screenshot is read with LOW thinking;
  code checks send doubtful reads, plus a sample of the passed ones, to a HIGH-thinking read.
- `intent`: a second opinion on every request text (each distinct text once, about 50 per call,
  LOW thinking). Requests where it disagrees with the keyword rules get a HIGH-thinking vote.

## Second looks in this run

- Screenshots: 11 read with LOW thinking first; 2 flagged by the checks and read again with HIGH.
- Screenshots evidence_42 flagged: screen is about user_17 (tombstone_17) but does not list it.
- Screenshots evidence_46 flagged: personal data only on delivery_confirmation_20.pdf, which is not a record; personal data only on return_label_4821.pdf, which is not a record; personal data only on return_label_4876.pdf, which is not a record; personal data only on invoice_NS-10493.pdf, which is not a record; personal data only on parcel_damage_7713.jpg, which is not a record; personal data only on customer_export_2026-02-10.zip, which is not a record; personal data only on customer_export_2026-02-12.zip, which is not a record.
- Screenshots: spot-checked with HIGH (passed the checks): evidence_04; LOW and HIGH answers differed for: none.
- Screenshots: the cheap pass never switched off (it would at more than 50% flagged).
