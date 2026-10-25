"""EscalationPolicy: read cheap first, read carefully only when needed.

Gemini can answer with LOW thinking (about a quarter of the cost) or HIGH
thinking (slower, careful). Measured on the 11 screenshots (2026-10-25):

  * HIGH on every screenshot: the same output.csv in 3 of 3 runs, ~$0.14.
  * LOW on every screenshot: ~$0.04, but 2 of 4 runs lost a real finding
    (case_50: a name and email put on a fake "user_20" record).
  * The model's own "I am unsure" list caught 0 of 3 wrong reads.
  * Two plain-code checks caught 3 of 3 (ImageLoader.needs_second_look).

So each item is read with LOW first, and code checks decide which reads get
a HIGH second look. A HIGH read can slip the same way (case_50, 1 of 4 runs),
so a HIGH read that fails the checks is read once more. Two more nets cover
mistakes the checks do not know yet:

  1. Spot check: the 1st, 11th, 21st... read that passed the checks is read
     with HIGH too, and the answers are compared. The pick goes by position
     in the run, not by chance, so a re-run picks the same items. Position
     says nothing about how good a read is, so the sample stays fair.
  2. Switch: once more than half of the reads so far were flagged (after at
     least 4), the cheap pass stops and the rest go straight to HIGH. Two
     reads cost more than one HIGH read once about 73% get flagged (LOW
     ~$0.0035 + HIGH ~$0.013 per screenshot), and a high flag rate means
     the quick read is struggling with these screens anyway.

code/llm/cascade.md explains the design and the measurements behind it.
"""

from __future__ import annotations

from dataclasses import dataclass, field

LOW = "LOW"
HIGH = "HIGH"


@dataclass
class EscalationPolicy:
    spot_check_every: int = 10
    switch_share: float = 0.5
    min_reads_before_switch: int = 4
    # What happened in this run, in the order items were read.
    cheap_reads: int = 0
    passed_reads: int = 0
    flagged: list[tuple[str, list[str]]] = field(default_factory=list)
    spot_checked: list[str] = field(default_factory=list)
    spot_check_differences: list[str] = field(default_factory=list)
    high_only: list[str] = field(default_factory=list)
    # (item, doubts left) when a careful read also failed the checks and was retried.
    careful_retries: list[tuple[str, list[str]]] = field(default_factory=list)

    def cheap_pass_stopped(self) -> bool:
        if self.cheap_reads < self.min_reads_before_switch:
            return False
        return len(self.flagged) / self.cheap_reads > self.switch_share

    def take_spot_check(self) -> bool:
        """Count one read that passed the checks; True when it is due for a spot check."""
        self.passed_reads += 1
        return (self.passed_reads - 1) % self.spot_check_every == 0

    def notes(self, what: str) -> list[str]:
        """Plain sentences for usage_report.md about the second looks in this run."""
        if not self.cheap_reads and not self.high_only:
            return []
        notes = [f"{what}: {self.cheap_reads} read with LOW thinking first; "
                 f"{len(self.flagged)} flagged by the checks and read again with HIGH."]
        notes += [f"{what} {item} flagged: {'; '.join(reasons)}." for item, reasons in self.flagged]
        for item, remaining in self.careful_retries:
            outcome = (f"still doubtful ({'; '.join(remaining)}), needs a person to look"
                       if remaining else "the second careful read passed and was used")
            notes.append(f"{what} {item}: the careful read made the same slip, so it was read carefully "
                         f"once more; {outcome}.")
        if self.spot_checked:
            differed = ", ".join(self.spot_check_differences) or "none"
            notes.append(f"{what}: spot-checked with HIGH (passed the checks): {', '.join(self.spot_checked)}; "
                         f"LOW and HIGH answers differed for: {differed}.")
        if self.high_only:
            notes.append(f"{what}: more than {self.switch_share:.0%} of reads were flagged, so the cheap pass "
                         f"stopped; read with HIGH only: {', '.join(self.high_only)}.")
        else:
            notes.append(f"{what}: the cheap pass never switched off "
                         f"(it would at more than {self.switch_share:.0%} flagged).")
        return notes
