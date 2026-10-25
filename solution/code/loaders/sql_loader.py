"""SqlLoader: reads SQLite-compatible snapshot exports.

Each file is executed into a private in-memory database instead of being
parsed with regexes, because the exports use real SQL (quoted names, NULLs,
CHECK constraints) and SQLite already reads it correctly.
"""

from __future__ import annotations

import json
import sqlite3
from collections import defaultdict
from pathlib import Path

from loaders.base import EvidenceLoader, flatten_nested_fields
from loaders.record_normalizer import SYSTEM_BY_RESOURCE_PREFIX, resource_prefix
from models import EvidenceFile, RawEvidenceRecord, SnapshotListing

# snapshot_scope only records when each origin was captured; it describes the
# file, not any customer's data.
_METADATA_TABLES = {"snapshot_scope"}


class SqlLoader(EvidenceLoader):
    file_suffixes = (".sql",)

    def listings(self, evidence_file: EvidenceFile, path: Path) -> list[SnapshotListing]:
        # Only primary-store tables count as complete listings. Replica and
        # inventory tables may lag or hold a subset, so a record missing from
        # them proves nothing.
        resource_ids_by_table_and_system: dict[tuple[str, str], set[str]] = defaultdict(set)
        for record in self.load(evidence_file, path):
            if not record.location.endswith("primary"):
                continue
            resource_id = str(record.fields.get("resource_id"))
            system_id = SYSTEM_BY_RESOURCE_PREFIX.get(resource_prefix(resource_id), record.system_hint or "unknown")
            resource_ids_by_table_and_system[(record.location, system_id)].add(resource_id)
        return [
            SnapshotListing(evidence_file.file_id, evidence_file.captured_at, system_id, table, frozenset(ids))
            for (table, system_id), ids in sorted(resource_ids_by_table_and_system.items())
        ]

    def load(self, evidence_file: EvidenceFile, path: Path) -> list[RawEvidenceRecord]:
        connection = sqlite3.connect(":memory:")
        connection.row_factory = sqlite3.Row
        try:
            connection.executescript(path.read_text(encoding="utf-8"))
            table_names = [
                row["name"]
                for row in connection.execute(
                    "SELECT name FROM sqlite_master WHERE type='table' ORDER BY name"
                )
            ]
            records: list[RawEvidenceRecord] = []
            for table_name in table_names:
                if table_name in _METADATA_TABLES:
                    continue
                for row in connection.execute(f'SELECT * FROM "{table_name}" ORDER BY rowid'):
                    records.append(
                        RawEvidenceRecord(
                            file_id=evidence_file.file_id,
                            # The whole snapshot is one point in time, so the
                            # manifest capture time is when every row was seen.
                            observed_at=evidence_file.captured_at,
                            location=table_name,
                            fields=_row_fields(row),
                            system_hint=row["origin_system"] if "origin_system" in row.keys() else None,
                            is_listing_row=True,
                        )
                    )
            return records
        finally:
            connection.close()


def _row_fields(row: sqlite3.Row) -> dict[str, object]:
    fields = {column: row[column] for column in row.keys()}
    # Replica tables store the customer record as a JSON blob; the
    # identifiers we need are inside it.
    record_json = fields.pop("record_json", None)
    if record_json:
        fields.update(flatten_nested_fields(json.loads(record_json)))
    return fields
