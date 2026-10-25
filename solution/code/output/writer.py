"""OutputWriter: writes CaseResults as output.csv in the contract format.

Columns are case_id,deletion_status,steps_json. Rows follow case number
order (case_33, case_34, ... case_550), the same order as
deletion_requests.csv. JSON is compact with a fixed key order, so two runs on
the same input produce byte-identical files.
"""

from __future__ import annotations

import csv
import json
import re
from pathlib import Path

from models import CaseResult, Step

OUTPUT_COLUMNS = ["case_id", "deletion_status", "steps_json"]


def case_number_order(case_id: str) -> tuple[int, str]:
    # Plain string order would put case_100 before case_33.
    match = re.search(r"(\d+)$", case_id)
    return (int(match.group(1)) if match else -1, case_id)


def serialize_steps(steps: list[Step]) -> str:
    return json.dumps([step.to_json_dict() for step in steps], ensure_ascii=False, separators=(",", ":"))


class OutputWriter:
    def write(self, results: list[CaseResult], path: Path) -> None:
        path.parent.mkdir(parents=True, exist_ok=True)
        with path.open("w", newline="", encoding="utf-8") as handle:
            writer = csv.writer(handle, lineterminator="\n")
            writer.writerow(OUTPUT_COLUMNS)
            for result in sorted(results, key=lambda r: case_number_order(r.case_id)):
                writer.writerow([result.case_id, result.status.value, serialize_steps(result.steps)])
