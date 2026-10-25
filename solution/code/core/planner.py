"""StepPlanner: turns policy Decisions into the ordered steps_json of one case.

Ordering follows AGENTS.md 6.3/6.5: phase first (FINDING, REMEDIATION,
VERIFICATION), then the operation's position in the contract table, then
system_id, then resource_id. Because the table lists DISABLE_DATA_FLOW and
BLOCK_RESTORE before the record operations, sorting alone already stops
recreation paths before records are changed.

Which steps appear:
  * COMPLETE: one cited verification per relevant resource.
  * INCOMPLETE: a cited finding per violation, its remediation, then a
    verification for every relevant resource (the samples verify compliant
    resources too, like the deleted account in case_11). Future work has an
    empty evidence_id.
"""

from __future__ import annotations

from core.policy import Decision
from models import OPERATION_PHASE_AND_STATE, DeletionStatus, Operation, Phase, Step

_PHASE_ORDER = list(Phase)
_OPERATION_ORDER = list(Operation)


def step_sort_key(step: Step) -> tuple:
    return (_PHASE_ORDER.index(step.phase), _OPERATION_ORDER.index(step.operation), step.system_id, step.resource_id)


def _step(operation: Operation, decision: Decision, evidence_id: str) -> Step:
    phase, state = OPERATION_PHASE_AND_STATE[operation]
    return Step(
        step_no=0,  # numbered after sorting
        phase=phase,
        system_id=decision.state.system_id,
        resource_id=decision.state.resource_id,
        operation=operation,
        state=state,
        evidence_id=evidence_id,
    )


class StepPlanner:
    def plan(self, decisions: list[Decision]) -> tuple[DeletionStatus, list[Step]]:
        violations = [d for d in decisions if not d.is_compliant]
        if violations:
            status = DeletionStatus.INCOMPLETE
            steps = [_step(d.finding, d, d.state.evidence_id or "") for d in violations]
            steps += [_step(d.remediation, d, "") for d in violations]
            steps += [_step(d.verification, d, "") for d in decisions]
        else:
            status = DeletionStatus.COMPLETE
            steps = [
                _step(d.verification, d, d.state.evidence_id or d.state.latest_activity_id or "")
                for d in decisions
            ]
        ordered = sorted(steps, key=step_sort_key)
        numbered = [
            Step(position, s.phase, s.system_id, s.resource_id, s.operation, s.state, s.evidence_id)
            for position, s in enumerate(ordered, start=1)
        ]
        return status, numbered
