"""Evaluation cases we wrote, on top of the 32 public samples.

Each case takes a public sample request that the agent already answers
exactly right and changes one thing about it: the request text or the email.
The person, the dates and the evidence stay the same, so the expected answer
is known without inventing a new truth. The sample a case is based on is left
out of that run, so the person's request history (RequestEpisodes) and the
deletion batch timing (WorkflowWindowMatcher) do not change.
"""

from __future__ import annotations

from dataclasses import dataclass


@dataclass(frozen=True)
class CustomCase:
    case_id: str
    based_on: str  # the public sample case it replaces
    tests: str  # what the case checks, in one sentence
    request_text: str | None = None  # None keeps the sample's text
    email: str | None = None  # None keeps the sample's email
    # None: the sample's own answer is expected (status and steps).
    expected_status: str | None = None


CUSTOM_CASES = [
    CustomCase(
        case_id="custom_01",
        based_on="case_01",
        tests=(
            "A request that uses the word 'delete' but does not ask for deletion. The keyword rules "
            "say DELETE, so the Gemini vote has to overrule them."
        ),
        request_text=(
            "I am not asking you to delete anything yet. Before I decide, please send me a copy "
            "of the personal data you hold about me."
        ),
        expected_status="NOT_APPLICABLE",
    ),
    CustomCase(
        case_id="custom_02",
        based_on="case_12",
        tests=(
            "The request comes from the person's other email address in user_audit_log, so the "
            "identity has to be resolved through the change history."
        ),
        email="minjun.kim90@gmail.com",
    ),
    CustomCase(
        case_id="custom_03",
        based_on="case_06",
        tests=(
            "A deletion request in German, a language the keyword rules do not cover, so only "
            "Gemini can tell that it asks for removal."
        ),
        request_text=(
            "Mein Konto ist schon weg, aber mein altes Profil erscheint noch in der Suche. "
            "Bitte entfernen Sie auch dieses Suchergebnis."
        ),
    ),
]
