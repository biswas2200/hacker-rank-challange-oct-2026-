"""IntentClassifier: does the request text actually ask for deletion?

A request that does not authorize deletion is NOT_APPLICABLE with no steps,
however much its other fields look like a deletion case (AGENTS.md 6.5).
This module holds the keyword classifier, the Gemini classifier and the
ensemble that combines them. The keyword classifier works offline.

The decision order matters. Non-deletion requests in the dataset are written
to look like deletions: "remove me from marketing lists only", "correct it
without removing any of my records", "not a request to erase it". So an
explicit refusal is checked first, then a deletion cue, then the topic of a
non-deletion request.
"""

from __future__ import annotations

import re
from collections import defaultdict
from dataclasses import dataclass
from enum import Enum
from pathlib import Path
from typing import TYPE_CHECKING

from llm.cascade import HIGH, LOW
from models import DeletionRequest

if TYPE_CHECKING:
    from llm.gemini_client import GeminiClient


class Intent(str, Enum):
    DELETE = "DELETE"
    ACCESS = "ACCESS"
    EXPORT = "EXPORT"
    CORRECTION = "CORRECTION"
    OPT_OUT = "OPT_OUT"
    OTHER = "OTHER"


@dataclass(frozen=True)
class IntentResult:
    intent: Intent
    # The phrase that decided it, so `explain` and the eval report can show why.
    matched_cue: str | None

    @property
    def authorizes_deletion(self) -> bool:
        return self.intent is Intent.DELETE


# The person says outright that they do not want deletion.
_REFUSAL_CUES = [
    r"not asking for deletion",
    r"not a request to (erase|delete|remove)",
    r"do not (close|delete|remove)",
    r"don'?t (close|delete|remove)",
    r"without removing",
    r"leave (the|my) (account|profile)",
    r"keep the account",
    r"continue using the service",
    r"still (need|want|use) (the|it|my|receipts)",
]

# Deletion wording across the languages present in the requests: English,
# Spanish, Portuguese, French, Japanese and Korean.
_DELETION_CUES = [
    r"\b(delete|deleted|deleting|deletion)\b",
    r"\b(erase|erased|erasure)\b",
    # "remove that search result", "removed" in follow-ups about an earlier
    # deletion; "remove me from <list>" is a marketing opt-out instead.
    r"\bremov(e|ed|al)\b(?! me from)",
    # Follow-ups that ask to confirm an earlier deletion: "the profile is gone".
    r"\b(profile|account)( page)? is gone\b",
    r"\bclose (my|this|the) account\b",
    r"\b(closed|disconnected) my account\b",
    r"\bwould like it closed\b",
    r"\beliminar\b|\beliminaci[oó]n\b",
    r"\bexclus[aã]o\b",
    r"\bsuppression\b|\bsupprimer\b",
    r"削除",
    r"삭제",
]

# What a non-deletion request is about, checked only after the two lists above.
_TOPIC_CUES: list[tuple[Intent, str]] = [
    (Intent.OPT_OUT, r"promotional emails|marketing"),
    (Intent.EXPORT, r"download|portable format|copy of"),
    (Intent.ACCESS, r"information (you hold|request)|what personal information"),
    (Intent.CORRECTION, r"misspelled|correct it|out of date|change the email|replace it"),
]


def _first_match(patterns: list[str], text: str) -> str | None:
    for pattern in patterns:
        match = re.search(pattern, text)
        if match:
            return match.group(0)
    return None


class KeywordIntentClassifier:
    def classify(self, request: DeletionRequest) -> IntentResult:
        text = request.request_text.lower().replace("’", "'")
        refusal = _first_match(_REFUSAL_CUES, text)
        if refusal is None:
            deletion_cue = _first_match(_DELETION_CUES, text)
            if deletion_cue is not None:
                return IntentResult(Intent.DELETE, deletion_cue)
        for intent, pattern in _TOPIC_CUES:
            match = re.search(pattern, text)
            if match:
                return IntentResult(intent, match.group(0))
        # Neither a deletion nor a known topic: deletion was not authorized.
        return IntentResult(Intent.OTHER, refusal)

    def classify_all(self, requests: list[DeletionRequest]) -> dict[str, IntentResult]:
        return {request.case_id: self.classify(request) for request in requests}


INTENT_PROMPT_PATH = Path(__file__).resolve().parent.parent / "llm" / "prompts" / "intent.md"
INTENT_PROMPT_VERSION = "intent-v2"
INTENT_BATCH_SIZE = 50
# Case ids grouped under their label. The first version asked for one
# {"case_id", "intent"} object per request; Gemini often pretty-printed it,
# and half of the billed output tokens were spaces and line breaks.
INTENT_SCHEMA = {
    "type": "OBJECT",
    "properties": {intent.value: {"type": "ARRAY", "items": {"type": "STRING"}} for intent in Intent},
    "required": [intent.value for intent in Intent],
}


class GeminiIntentClassifier:
    """Labels request texts with Gemini, about 50 texts per call."""

    def __init__(self, gemini: "GeminiClient"):
        self.gemini = gemini

    def classify_all(self, requests: list[DeletionRequest], thinking_level: str) -> dict[str, Intent]:
        # Many requests repeat a text word for word (398 distinct texts in
        # 550 requests). Each text is asked once, under the first case_id that
        # has it, and the label is copied to every request with that text.
        case_ids_by_text: dict[str, list[str]] = {}
        for request in sorted(requests, key=lambda r: r.case_id):
            case_ids_by_text.setdefault(request.request_text, []).append(request.case_id)
        text_by_first_case = {case_ids[0]: text for text, case_ids in case_ids_by_text.items()}
        labels = self._ask(text_by_first_case, thinking_level)
        # Gemini sometimes leaves an id out of a long answer (the first
        # version silently skipped case_39). Those are asked once more.
        missing = {case_id: text for case_id, text in text_by_first_case.items() if case_id not in labels}
        if missing:
            labels.update(self._ask(missing, thinking_level))
        labels_by_case: dict[str, Intent] = {}
        for case_ids in case_ids_by_text.values():
            if case_ids[0] in labels:
                for case_id in case_ids:
                    labels_by_case[case_id] = labels[case_ids[0]]
        return labels_by_case

    def _ask(self, text_by_case_id: dict[str, str], thinking_level: str) -> dict[str, Intent]:
        prompt_head = INTENT_PROMPT_PATH.read_text(encoding="utf-8")
        known_labels = {intent.value for intent in Intent}
        labels: dict[str, Intent] = {}
        ordered = sorted(text_by_case_id.items())
        for start in range(0, len(ordered), INTENT_BATCH_SIZE):
            batch = dict(ordered[start : start + INTENT_BATCH_SIZE])
            listing = "\n".join(f"{case_id}: {text}" for case_id, text in batch.items())
            answer = self.gemini.generate_json(
                purpose="intent",
                prompt_version=INTENT_PROMPT_VERSION,
                prompt=f"{prompt_head}\nRequests:\n{listing}",
                schema=INTENT_SCHEMA,
                thinking_level=thinking_level,
            )
            found: dict[str, set[Intent]] = defaultdict(set)
            for label, case_ids in (answer or {}).items():
                if label in known_labels:
                    for case_id in case_ids:
                        found[case_id].add(Intent(label))
            for case_id, intents in found.items():
                # An id filed under two labels is a contradiction, not an answer.
                if case_id in batch and len(intents) == 1:
                    labels[case_id] = next(iter(intents))
        return labels


class EnsembleIntentClassifier:
    """Keyword rules first, Gemini LOW as a second opinion, Gemini HIGH as tie-breaker.

    The keyword rules are free and right on every known request, so their
    label stands whenever Gemini LOW agrees. A request gets a HIGH-thinking
    look when the two disagree, when Gemini left it out, or when the rules
    found no cue at all (wording or a language they do not know). Then two
    of the opinions decide. If they all differ, HIGH decides, being the most
    careful reader. Every second look is kept in `second_looks` so a person
    can read those requests.

    The keyword rules check every Gemini answer, so the spot check and the
    switch that guard screenshot reads (llm/cascade.py) are not needed here.
    """

    def __init__(self, keyword: KeywordIntentClassifier, gemini: GeminiIntentClassifier | None):
        self.keyword = keyword
        self.gemini = gemini
        self.voted: dict[str, IntentResult] = {}
        self.second_looks: list[str] = []

    def prepare(self, requests: list[DeletionRequest]) -> None:
        """Run the batched Gemini calls once, before cases are audited."""
        if self.gemini is None:
            return
        by_rules = {request.case_id: self.keyword.classify(request) for request in requests}
        quick = self.gemini.classify_all(requests, LOW)
        doubtful = [
            request for request in requests
            if by_rules[request.case_id].matched_cue is None
            or quick.get(request.case_id) is not by_rules[request.case_id].intent
        ]
        careful = self.gemini.classify_all(doubtful, HIGH) if doubtful else {}
        for request in doubtful:
            rules = by_rules[request.case_id]
            rules_vote = rules.intent if rules.matched_cue is not None else None
            votes = [vote for vote in (rules_vote, quick.get(request.case_id), careful.get(request.case_id)) if vote]
            majority = next((vote for vote in votes if votes.count(vote) >= 2), None)
            decided = majority or careful.get(request.case_id) or rules_vote or quick.get(request.case_id) or rules.intent
            summary = (
                f"rules {rules_vote.value if rules_vote else 'no cue'}, "
                f"LOW {quick[request.case_id].value if request.case_id in quick else 'no answer'}, "
                f"HIGH {careful[request.case_id].value if request.case_id in careful else 'no answer'}"
            )
            self.voted[request.case_id] = IntentResult(decided, f"vote: {summary}")
            self.second_looks.append(f"{request.case_id}: {summary} -> {decided.value}")

    def classify(self, request: DeletionRequest) -> IntentResult:
        return self.voted.get(request.case_id) or self.keyword.classify(request)
