"""TextLoader: reads pipe-delimited shift handover notes.

Each check is one bullet line:
    - <time> | <system> | <resource_id> | <record_type> | <result> | <identifier>
Prose lines around the bullets carry no record facts and are skipped.
"""

from __future__ import annotations

from pathlib import Path

from loaders.base import EvidenceLoader
from models import EvidenceFile, RawEvidenceRecord
from repository import parse_utc_timestamp

_HANDOVER_COLUMNS = ("recorded_at", "system", "resource_id", "record_type", "result", "subject_reference")


class TextLoader(EvidenceLoader):
    file_suffixes = (".txt",)

    def load(self, evidence_file: EvidenceFile, path: Path) -> list[RawEvidenceRecord]:
        records: list[RawEvidenceRecord] = []
        for line_number, line in enumerate(path.read_text(encoding="utf-8").splitlines(), start=1):
            if not line.startswith("- ") or "|" not in line:
                continue
            values = [value.strip() for value in line[2:].split("|")]
            if len(values) != len(_HANDOVER_COLUMNS):
                continue
            fields = dict(zip(_HANDOVER_COLUMNS, values))
            records.append(
                RawEvidenceRecord(
                    file_id=evidence_file.file_id,
                    observed_at=parse_utc_timestamp(fields["recorded_at"]),
                    location=f"line {line_number}",
                    fields=fields,
                    system_hint=fields["system"],
                )
            )
        return records
