"""DatasetRepository: loads the CSV files in dataset/ into typed rows.

This is the only module that knows the CSV column names. Everything after it
works with the dataclasses in models.py and the lookup maps built here.
"""

from __future__ import annotations

import csv
import re
from collections import defaultdict
from datetime import datetime, timezone
from pathlib import Path

from models import (
    DeletionRequest,
    DeletionRule,
    EvidenceFile,
    IdentityChange,
    SystemActivity,
    UserIdentity,
)


def parse_utc_timestamp(text: str) -> datetime:
    """Parse the dataset's ISO-8601 timestamps into aware UTC datetimes.

    Aware datetimes are used everywhere so that "captured at or before
    audit_at" comparisons can never silently mix naive and aware values.
    """
    parsed = datetime.fromisoformat(text.strip().replace("Z", "+00:00"))
    if parsed.tzinfo is None:
        parsed = parsed.replace(tzinfo=timezone.utc)
    return parsed.astimezone(timezone.utc)


def normalize_email(email: str) -> str:
    # Evidence writes the same address in mixed case (JULES-LAURENT@PROTON.ME),
    # and email matching must not miss those copies.
    return email.strip().lower()


def _read_csv_rows(path: Path) -> list[dict[str, str]]:
    with path.open(newline="", encoding="utf-8") as handle:
        return list(csv.DictReader(handle))


class DatasetRepository:
    def __init__(self, dataset_dir: Path):
        self.dataset_dir = dataset_dir
        self.evaluation_requests = self._load_requests("deletion_requests.csv")
        self.sample_requests = self._load_requests("sample_deletion_requests.csv")
        self.identities = [
            UserIdentity(
                user_id=row["user_id"],
                name=row["name"],
                email=normalize_email(row["email"]),
                phone=row["phone"],
                email_hash=row["email_hash"],
                updated_at=parse_utc_timestamp(row["updated_at"]),
            )
            for row in _read_csv_rows(dataset_dir / "user_identities.csv")
        ]
        self.identity_changes = [
            IdentityChange(
                audit_id=row["audit_id"],
                user_id=row["user_id"],
                event_time=parse_utc_timestamp(row["event_time"]),
                field_name=row["field_name"],
                old_value=row["old_value"],
                new_value=row["new_value"],
            )
            for row in _read_csv_rows(dataset_dir / "user_audit_log.csv")
        ]
        self.activities = [
            SystemActivity(
                activity_id=row["activity_id"],
                event_time=parse_utc_timestamp(row["event_time"]),
                event_type=row["event_type"],
                source_system=row["source_system"],
                target_system=row["target_system"],
                resource_id=row["resource_id"],
                reported_result=row["reported_result"],
                details=row["details"],
            )
            for row in _read_csv_rows(dataset_dir / "system_activity.csv")
        ]
        self.rules = [
            DeletionRule(**row) for row in _read_csv_rows(dataset_dir / "deletion_rules.csv")
        ]
        self.evidence_files = [
            EvidenceFile(
                file_id=row["file_id"],
                file_path=row["file_path"],
                origin=row["origin"],
                captured_at=parse_utc_timestamp(row["captured_at"]),
            )
            for row in _read_csv_rows(dataset_dir / "evidence_files.csv")
        ]
        self._build_lookup_maps()

    def _load_requests(self, file_name: str) -> list[DeletionRequest]:
        return [
            DeletionRequest(
                case_id=row["case_id"],
                email=normalize_email(row["email"]),
                requested_at=parse_utc_timestamp(row["requested_at"]),
                reported_status=row["reported_status"],
                reported_at=parse_utc_timestamp(row["reported_at"]),
                audit_at=parse_utc_timestamp(row["audit_at"]),
                request_text=row["request_text"],
            )
            for row in _read_csv_rows(self.dataset_dir / file_name)
        ]

    def _build_lookup_maps(self) -> None:
        self.identity_by_user_id = {identity.user_id: identity for identity in self.identities}
        self.identity_by_current_email = {identity.email: identity for identity in self.identities}

        self.identity_changes_by_user_id: dict[str, list[IdentityChange]] = defaultdict(list)
        for change in sorted(self.identity_changes, key=lambda c: (c.event_time, c.audit_id)):
            self.identity_changes_by_user_id[change.user_id].append(change)

        self.activities_by_resource_id: dict[str, list[SystemActivity]] = defaultdict(list)
        for activity in sorted(self.activities, key=lambda a: (a.event_time, a.activity_id)):
            self.activities_by_resource_id[activity.resource_id].append(activity)

        self.rule_by_category = {rule.data_category: rule for rule in self.rules}
        self.evidence_file_by_id = {file.file_id: file for file in self.evidence_files}

    def catalog_aliases(self) -> dict[str, str]:
        """Other names the data catalog gives a resource: alias -> resource_id.

        Documents and screens often show only that name: a statement number
        (NST-2026-0718-1842 is export_1842) or a file name
        (delivery_confirmation_19.pdf is object_19).
        """
        aliases = {}
        for activity in self.activities:
            if activity.event_type != "LINK_RESOURCE_IDENTITY":
                continue
            match = re.search(rf"{re.escape(activity.resource_id)} \((?P<alias>[^)]+)\)", activity.details)
            if match and match.group("alias") != activity.resource_id:
                aliases[match.group("alias")] = activity.resource_id
        return aliases

    def evidence_path(self, evidence_file: EvidenceFile) -> Path:
        return self.dataset_dir / evidence_file.file_path
