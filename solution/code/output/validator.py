"""OutputValidator: checks an output.csv against the contract before submitting.

Each rule below comes from AGENTS.md 6.3-6.6. The validator reads the file
back from disk, so it checks what the grader will actually see.
"""

from __future__ import annotations

import csv
import json
from pathlib import Path

from core.planner import step_sort_key
from loaders.pdf_loader import RESOURCE_ID_PATTERN
from models import OPERATION_PHASE_AND_STATE, DeletionRequest, Operation, Phase, Step
from output.writer import OUTPUT_COLUMNS, case_number_order

STEP_FIELDS = {"step_no", "phase", "system_id", "resource_id", "operation", "state", "evidence_id"}


class OutputValidator:
    def __init__(self, citable_ids: set[str], capture_time_by_file: dict, audit_at_by_case: dict):
        # Every file_id, activity_id and audit_id participants can see.
        self.citable_ids = citable_ids
        self.capture_time_by_file = capture_time_by_file
        self.audit_at_by_case = audit_at_by_case

    def validate(self, path: Path, requests: list[DeletionRequest]) -> list[str]:
        issues: list[str] = []
        with path.open(newline="", encoding="utf-8") as handle:
            reader = csv.DictReader(handle)
            if reader.fieldnames != OUTPUT_COLUMNS:
                return [f"columns are {reader.fieldnames}, expected {OUTPUT_COLUMNS}"]
            rows = list(reader)

        expected_ids = [r.case_id for r in requests]
        found_ids = [row["case_id"] for row in rows]
        if len(rows) != len(requests):
            issues.append(f"{len(rows)} rows, expected {len(requests)}")
        if set(found_ids) != set(expected_ids):
            issues.append(f"case_id set differs: missing {sorted(set(expected_ids) - set(found_ids))[:5]}")
        if found_ids != sorted(found_ids, key=case_number_order):
            issues.append("rows are not sorted by case_id")
        for row in rows:
            issues.extend(self._validate_row(row))
        return issues

    def _validate_row(self, row: dict) -> list[str]:
        case_id, status = row["case_id"], row["deletion_status"]
        problems: list[str] = []
        try:
            raw_steps = json.loads(row["steps_json"])
        except json.JSONDecodeError as error:
            return [f"{case_id}: steps_json is not valid JSON ({error})"]
        if not isinstance(raw_steps, list):
            return [f"{case_id}: steps_json is not an array"]
        if status not in {"COMPLETE", "INCOMPLETE", "NOT_APPLICABLE"}:
            problems.append(f"{case_id}: unknown status {status}")
        if status == "NOT_APPLICABLE" and raw_steps:
            problems.append(f"{case_id}: NOT_APPLICABLE must have []")
        if status in {"COMPLETE", "INCOMPLETE"} and not raw_steps:
            problems.append(f"{case_id}: {status} has no steps")

        steps: list[Step] = []
        for position, raw in enumerate(raw_steps, start=1):
            where = f"{case_id} step {position}"
            if set(raw) != STEP_FIELDS:
                problems.append(f"{where}: fields {sorted(raw)}")
                continue
            if raw["step_no"] != position:
                problems.append(f"{where}: step_no {raw['step_no']}")
            try:
                operation, phase = Operation(raw["operation"]), Phase(raw["phase"])
            except ValueError:
                problems.append(f"{where}: unknown phase or operation")
                continue
            expected_phase, expected_state = OPERATION_PHASE_AND_STATE[operation]
            if (phase, raw["state"]) != (expected_phase, expected_state):
                problems.append(f"{where}: {operation.value} needs {expected_phase.value}/{expected_state}")
            evidence_id = raw["evidence_id"]
            is_future_work = status == "INCOMPLETE" and phase is not Phase.FINDING
            if is_future_work and evidence_id:
                problems.append(f"{where}: future work must have empty evidence_id")
            if not is_future_work and status != "NOT_APPLICABLE":
                if not evidence_id:
                    problems.append(f"{where}: must cite evidence")
                elif evidence_id not in self.citable_ids:
                    problems.append(f"{where}: unknown evidence_id {evidence_id}")
                elif (
                    evidence_id in self.capture_time_by_file
                    and self.capture_time_by_file[evidence_id] > self.audit_at_by_case[case_id]
                ):
                    problems.append(f"{where}: cites {evidence_id}, captured after audit_at")
            if status == "COMPLETE" and phase is not Phase.VERIFICATION:
                problems.append(f"{where}: COMPLETE allows verification steps only")
            # Every real resource id in the dataset has this shape. A bucket
            # name or a user id here means a model answer leaked through as a
            # record (this caught user_20 and northstar-customer-data).
            if not RESOURCE_ID_PATTERN.fullmatch(raw["resource_id"]):
                problems.append(f"{where}: {raw['resource_id']} does not look like a resource id")
            steps.append(Step(raw["step_no"], phase, raw["system_id"], raw["resource_id"], operation, raw["state"], evidence_id))

        keys = [step_sort_key(step) for step in steps]
        if keys != sorted(keys):
            problems.append(f"{case_id}: steps are not in contract order")
        return problems
