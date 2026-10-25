"""Evaluator: scores predicted rows against known answers.

The metrics follow the problem statement's list:
  * status accuracy: deletion_status matches;
  * step exact match (by position): step i equals expected step i in every field;
  * step precision / recall / F1 (order ignored): how many expected steps we
    produced anywhere in the array, and how many of ours were expected;
  * field accuracy: per field, how often position i agrees;
  * sequence accuracy: the whole steps array is identical.
step_no is left out of the order-free comparison, because it only restates
the position.
"""

from __future__ import annotations

from collections import Counter
from dataclasses import dataclass, field

FIELDS = ["step_no", "phase", "system_id", "resource_id", "operation", "state", "evidence_id"]
_ORDER_FREE_FIELDS = [f for f in FIELDS if f != "step_no"]


@dataclass
class Metrics:
    cases: int = 0
    status_matches: int = 0
    sequence_matches: int = 0
    positions: int = 0
    positional_step_matches: int = 0
    field_matches: Counter = field(default_factory=Counter)
    expected_steps: int = 0
    predicted_steps: int = 0
    shared_steps: int = 0
    failing_cases: list[str] = field(default_factory=list)

    @staticmethod
    def _ratio(part: int, whole: int) -> float:
        return part / whole if whole else 0.0

    @property
    def precision(self) -> float:
        return self._ratio(self.shared_steps, self.predicted_steps)

    @property
    def recall(self) -> float:
        return self._ratio(self.shared_steps, self.expected_steps)

    @property
    def f1(self) -> float:
        p, r = self.precision, self.recall
        return 2 * p * r / (p + r) if p + r else 0.0


def _step_key(step: dict) -> tuple:
    return tuple(step[f] for f in _ORDER_FREE_FIELDS)


class Evaluator:
    def compare(
        self,
        predicted: dict[str, tuple[str, list[dict]]],
        expected: dict[str, tuple[str, list[dict]]],
    ) -> Metrics:
        metrics = Metrics()
        for case_id, (expected_status, expected_steps) in expected.items():
            predicted_status, predicted_steps = predicted.get(case_id, ("MISSING", []))
            metrics.cases += 1
            metrics.status_matches += predicted_status == expected_status
            same_sequence = predicted_steps == expected_steps
            metrics.sequence_matches += same_sequence
            if not (same_sequence and predicted_status == expected_status):
                metrics.failing_cases.append(case_id)

            for position in range(max(len(expected_steps), len(predicted_steps))):
                got = predicted_steps[position] if position < len(predicted_steps) else {}
                want = expected_steps[position] if position < len(expected_steps) else {}
                metrics.positions += 1
                agreeing = [f for f in FIELDS if f in got and got.get(f) == want.get(f)]
                metrics.field_matches.update(agreeing)
                metrics.positional_step_matches += len(agreeing) == len(FIELDS)

            expected_keys = Counter(_step_key(s) for s in expected_steps)
            predicted_keys = Counter(_step_key(s) for s in predicted_steps)
            metrics.expected_steps += sum(expected_keys.values())
            metrics.predicted_steps += sum(predicted_keys.values())
            metrics.shared_steps += sum((expected_keys & predicted_keys).values())
        return metrics

    def to_markdown(self, metrics: Metrics, title: str) -> str:
        pct = lambda part, whole: f"{100 * Metrics._ratio(part, whole):.1f}%"
        lines = [
            f"### {title}",
            "",
            "| Metric | Value |",
            "| --- | --- |",
            f"| Cases | {metrics.cases} |",
            f"| Status accuracy | {metrics.status_matches}/{metrics.cases} ({pct(metrics.status_matches, metrics.cases)}) |",
            f"| Sequence accuracy (whole array identical) | {metrics.sequence_matches}/{metrics.cases} ({pct(metrics.sequence_matches, metrics.cases)}) |",
            f"| Step exact match by position | {metrics.positional_step_matches}/{metrics.positions} ({pct(metrics.positional_step_matches, metrics.positions)}) |",
            f"| Step precision (order ignored) | {metrics.shared_steps}/{metrics.predicted_steps} ({100 * metrics.precision:.1f}%) |",
            f"| Step recall (order ignored) | {metrics.shared_steps}/{metrics.expected_steps} ({100 * metrics.recall:.1f}%) |",
            f"| Step F1 (order ignored) | {100 * metrics.f1:.1f}% |",
            "",
            "| Field | Agreement by position |",
            "| --- | --- |",
        ]
        lines += [
            f"| {f} | {metrics.field_matches[f]}/{metrics.positions} ({pct(metrics.field_matches[f], metrics.positions)}) |"
            for f in FIELDS
        ]
        return "\n".join(lines) + "\n"
