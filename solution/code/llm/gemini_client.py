"""GeminiClient: the only place the pipeline talks to a language model.

Gemini runs on Vertex AI with Application Default Credentials, so there is
no API key anywhere. Every call:
  * asks for JSON that matches a schema, at temperature 0;
  * can ask for a thinking level (LOW for the cheap first pass, HIGH for the
    careful second look; see llm/cascade.py);
  * is looked up in the ResponseCache first and saved there after;
  * is recorded in the UsageTracker (tokens, live call or cache hit).

With use_llm=False (--no-llm) the client only reads the cache. A miss
returns None and the caller falls back to its non-model path. With
read_cache=False (--fresh) every call goes to Gemini, as in a real run on new
data; the answers are still saved so `explain` can show them later.
"""

from __future__ import annotations

import json
import time
from dataclasses import dataclass, field

from config import Settings
from llm.cache import ResponseCache, cache_key

MAX_ATTEMPTS = 4


@dataclass
class CallRecord:
    purpose: str  # "intent" or "media"
    thinking_level: str  # "LOW", "HIGH" or "default"
    model: str
    from_cache: bool
    input_tokens: int
    output_tokens: int
    # Gemini's hidden reasoning tokens; billed like output tokens.
    thinking_tokens: int


@dataclass
class UsageTracker:
    calls: list[CallRecord] = field(default_factory=list)
    # Filled by the cascade so the report can say why HIGH calls were made.
    cascade_notes: list[str] = field(default_factory=list)

    def record(self, call: CallRecord) -> None:
        self.calls.append(call)

    def to_markdown(self, settings: Settings, request_count: int) -> str:
        """usage_report.md: the model answers the final run used, and their cost.

        A cached answer counts with the tokens of the call that produced it,
        because that call is what the output depends on. The table also says
        how many of those calls ran live in this run.
        """
        lines = [
            "# Token usage and cost",
            "",
            f"Final full-dataset run that produced output.csv ({request_count} requests).",
            "",
            "| Provider | Model | Purpose | Thinking | Model calls | Live this run | From cache | Input tokens | Output tokens | Thinking tokens | Total tokens | Cost (USD) |",
            "| --- | --- | --- | --- | --- | --- | --- | --- | --- | --- | --- | --- |",
        ]
        groups = sorted({(c.purpose, c.thinking_level) for c in self.calls}) + [("all", "all")]
        overall = None
        for purpose, level in groups:
            calls = [c for c in self.calls if purpose == "all" or (c.purpose, c.thinking_level) == (purpose, level)]
            if not calls:
                continue
            input_tokens = sum(c.input_tokens for c in calls)
            output_tokens = sum(c.output_tokens for c in calls)
            thinking = sum(c.thinking_tokens for c in calls)
            cost = (input_tokens * settings.input_price_per_million
                    + (output_tokens + thinking) * settings.output_price_per_million) / 1_000_000
            label, level_label = ("**overall**", "all") if purpose == "all" else (purpose, level)
            lines.append(
                f"| Google Vertex AI | {settings.gemini_model} | {label} | {level_label} | {len(calls)} | "
                f"{sum(not c.from_cache for c in calls)} | {sum(c.from_cache for c in calls)} | "
                f"{input_tokens:,} | {output_tokens:,} | {thinking:,} | {input_tokens + output_tokens + thinking:,} | ${cost:.4f} |"
            )
            overall = (input_tokens + output_tokens + thinking, cost)
        if overall:
            total_tokens, cost = overall
            lines += [
                "",
                f"- Total tokens: {total_tokens:,}; average tokens per request: {total_tokens / request_count:,.1f}",
                f"- Estimated total cost: ${cost:.4f}; estimated cost per request: ${cost / request_count:.6f}",
                f"- One provider and one model ({settings.gemini_model}), so the overall row is also the per-model total.",
            ]
        lines += [
            "",
            f"Prices: ${settings.input_price_per_million} per 1M input tokens and "
            f"${settings.output_price_per_million} per 1M output tokens (Vertex AI introductory rate for "
            f"{settings.gemini_model}, valid through 2026-12-31). Thinking tokens are billed as output.",
            "",
            "Gemini does two jobs, each cheap first and careful only when needed (code/llm/cascade.md):",
            "",
            "- `media`: reads the 11 admin-console screenshots. Every screenshot is read with LOW thinking;",
            "  code checks send doubtful reads, plus a sample of the passed ones, to a HIGH-thinking read.",
            "- `intent`: a second opinion on every request text (each distinct text once, about 50 per call,",
            "  LOW thinking). Requests where it disagrees with the keyword rules get a HIGH-thinking vote.",
        ]
        if self.cascade_notes:
            lines += ["", "## Second looks in this run", ""] + [f"- {note}" for note in self.cascade_notes]
        return "\n".join(lines) + "\n"


class GeminiClient:
    def __init__(self, settings: Settings, usage: UsageTracker):
        self.settings = settings
        self.usage = usage
        self.cache = ResponseCache(settings.llm_cache_dir)
        self._client = None  # created on the first live call only

    def generate_json(
        self,
        purpose: str,
        prompt_version: str,
        prompt: str,
        schema: dict,
        binary_part: tuple[bytes, str] | None = None,
        thinking_level: str | None = None,
        attempt: int = 1,
    ) -> dict | list | None:
        """Ask Gemini for JSON matching `schema`; None if offline and not cached.

        binary_part is (file bytes, mime type) for an image or PDF.
        thinking_level is "LOW" or "HIGH"; None keeps the model's default.
        attempt > 1 asks the same question again as a new call.
        """
        key = cache_key(
            self.settings.gemini_model, prompt_version, prompt, json.dumps(schema, sort_keys=True),
            *(binary_part if binary_part else ()),
            # LOW and HIGH answers get different keys, so a careful second
            # look can never be handed the cheap answer it is meant to check.
            *((f"thinking={thinking_level}",) if thinking_level else ()),
            *((f"attempt={attempt}",) if attempt > 1 else ()),
        )
        level_label = thinking_level or "default"
        cached = self.cache.get(key) if self.settings.read_cache else None
        if cached is not None:
            self._record(purpose, level_label, cached["usage"], from_cache=True)
            return cached["response"]
        if not self.settings.use_llm:
            return None

        response_text, usage = self._call_with_retries(prompt, schema, binary_part, thinking_level)
        parsed = json.loads(response_text)
        self.cache.put(
            key,
            {"model": self.settings.gemini_model, "purpose": purpose, "prompt_version": prompt_version,
             "thinking_level": level_label, "response": parsed, "usage": usage},
        )
        self._record(purpose, level_label, usage, from_cache=False)
        return parsed

    def _record(self, purpose: str, thinking_level: str, usage: dict, from_cache: bool) -> None:
        self.usage.record(
            CallRecord(purpose, thinking_level, self.settings.gemini_model, from_cache,
                       usage["input_tokens"], usage["output_tokens"], usage["thinking_tokens"])
        )

    def _call_with_retries(self, prompt: str, schema: dict, binary_part, thinking_level: str | None) -> tuple[str, dict]:
        from google import genai
        from google.genai import types

        if self._client is None:
            self._client = genai.Client(
                vertexai=True,
                project=self.settings.google_cloud_project,
                location=self.settings.google_cloud_location,
            )
        contents: list = [prompt]
        if binary_part:
            data, mime_type = binary_part
            contents.insert(0, types.Part.from_bytes(data=data, mime_type=mime_type))
        config = types.GenerateContentConfig(
            temperature=0, response_mime_type="application/json", response_schema=schema
        )
        if thinking_level:
            config.thinking_config = types.ThinkingConfig(thinking_level=thinking_level)
        for attempt in range(1, MAX_ATTEMPTS + 1):
            try:
                response = self._client.models.generate_content(
                    model=self.settings.gemini_model, contents=contents, config=config
                )
                metadata = response.usage_metadata
                usage = {
                    "input_tokens": metadata.prompt_token_count or 0,
                    "output_tokens": metadata.candidates_token_count or 0,
                    "thinking_tokens": metadata.thoughts_token_count or 0,
                }
                return response.text, usage
            except Exception:
                # Quota and transient server errors pass with a short wait;
                # anything still failing after the last attempt is raised.
                if attempt == MAX_ATTEMPTS:
                    raise
                time.sleep(2**attempt)
        raise RuntimeError("unreachable")
