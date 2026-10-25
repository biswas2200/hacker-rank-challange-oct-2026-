"""JsonLoader: reads record bundles, service exports and ledgers.

The JSON files come in several shapes (snapshot bundles, per-service
exports, flat ledgers). Rather than one parser per shape, the loader walks
the tree and treats every object that has a resource_id as one record.
"""

from __future__ import annotations

import json
from datetime import datetime
from pathlib import Path

from loaders.base import EvidenceLoader, flatten_nested_fields
from models import EvidenceFile, RawEvidenceRecord, SnapshotListing
from repository import parse_utc_timestamp

# Keys that state when something was seen, nearest-first wins. A bundle
# exported on 09-17 can hold a snapshot captured on 09-09, and the audit
# cutoff must be compared with the 09-09 time.
_OBSERVATION_TIME_KEYS = ("observed_at", "recorded_at", "captured_at", "exported_at")
_SYSTEM_HINT_KEYS = ("data_store", "system")


class JsonLoader(EvidenceLoader):
    file_suffixes = (".json",)

    def load(self, evidence_file: EvidenceFile, path: Path) -> list[RawEvidenceRecord]:
        document = json.loads(path.read_text(encoding="utf-8"))
        records: list[RawEvidenceRecord] = []
        self._collect_records(
            node=document,
            json_path="$",
            observed_at=evidence_file.captured_at,
            system_hint=None,
            evidence_file=evidence_file,
            records=records,
        )
        return records

    def listings(self, evidence_file: EvidenceFile, path: Path) -> list[SnapshotListing]:
        # Snapshot bundles hold {"data_store", "collection", "records": [...]}
        # entries: a dump of that collection at captured_at, empty or not.
        document = json.loads(path.read_text(encoding="utf-8"))
        found: list[SnapshotListing] = []

        def walk(node: object, json_path: str) -> None:
            if isinstance(node, list):
                for position, child in enumerate(node):
                    walk(child, f"{json_path}[{position}]")
            elif isinstance(node, dict):
                if isinstance(node.get("records"), list) and isinstance(node.get("data_store"), str):
                    found.append(
                        SnapshotListing(
                            file_id=evidence_file.file_id,
                            observed_at=_own_observation_time(node) or evidence_file.captured_at,
                            system_id=node["data_store"],
                            location=json_path,
                            resource_ids=frozenset(
                                str(r["resource_id"]) for r in node["records"] if isinstance(r, dict) and "resource_id" in r
                            ),
                        )
                    )
                    return
                for key, child in node.items():
                    walk(child, f"{json_path}.{key}")

        walk(document, "$")
        return found

    def _collect_records(
        self,
        node: object,
        json_path: str,
        observed_at: datetime,
        system_hint: str | None,
        evidence_file: EvidenceFile,
        records: list[RawEvidenceRecord],
    ) -> None:
        if isinstance(node, list):
            for position, child in enumerate(node):
                self._collect_records(
                    child, f"{json_path}[{position}]", observed_at, system_hint, evidence_file, records
                )
            return
        if not isinstance(node, dict):
            return

        observed_at = _own_observation_time(node) or observed_at
        system_hint = next((node[key] for key in _SYSTEM_HINT_KEYS if isinstance(node.get(key), str)), system_hint)

        if "resource_id" in node:
            records.append(
                RawEvidenceRecord(
                    file_id=evidence_file.file_id,
                    observed_at=observed_at,
                    location=json_path,
                    fields=flatten_nested_fields(node),
                    system_hint=system_hint,
                    is_listing_row=True,
                )
            )
            # A record can embed others (a search query and its hits); they
            # are kept inside this record's fields rather than emitted twice.
            return

        for key, child in node.items():
            # Service exports group records under their system id, which is
            # the only place the owning system is stated.
            child_hint = key if key.endswith(("_service", "_platform")) else system_hint
            self._collect_records(child, f"{json_path}.{key}", observed_at, child_hint, evidence_file, records)


def _own_observation_time(node: dict) -> datetime | None:
    for key in _OBSERVATION_TIME_KEYS:
        value = node.get(key)
        if isinstance(value, str):
            return parse_utc_timestamp(value)
    return None
