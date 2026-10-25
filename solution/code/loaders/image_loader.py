"""ImageLoader: reads admin-console screenshots with Gemini.

Screenshots have no text layer, so this is the one loader that needs a
model. Gemini copies the visible records into a fixed JSON schema
(llm/prompts/media_extract.md). The loader then turns them into ordinary
RawEvidenceRecords, so the rest of the pipeline cannot tell an image from a
SQL row.

Each screenshot is read with LOW thinking first. needs_second_look checks
that read in plain code, and EscalationPolicy decides when a HIGH-thinking
read replaces it (llm/cascade.py).
"""

from __future__ import annotations

from pathlib import Path

from llm.cascade import HIGH, LOW, EscalationPolicy
from llm.gemini_client import GeminiClient
from loaders.base import EvidenceLoader
from loaders.pdf_loader import RESOURCE_ID_PATTERN
from models import EvidenceFile, RawEvidenceRecord

PROMPT_PATH = Path(__file__).resolve().parent.parent / "llm" / "prompts" / "media_extract.md"
PROMPT_VERSION = "media-v2"
_PERSONAL_DATA_FIELDS = ("name", "email", "phone", "email_hash")

_NULLABLE_STRING = {"type": "STRING", "nullable": True}
_NULLABLE_BOOLEAN = {"type": "BOOLEAN", "nullable": True}
MEDIA_SCHEMA = {
    "type": "OBJECT",
    "properties": {
        "screen_subject_user_id": _NULLABLE_STRING,
        "records": {
            "type": "ARRAY",
            "items": {
                "type": "OBJECT",
                "properties": {
                    "resource_id": {"type": "STRING"},
                    "status": _NULLABLE_STRING,
                    "data_category": _NULLABLE_STRING,
                    "user_id": _NULLABLE_STRING,
                    "name": _NULLABLE_STRING,
                    "email": _NULLABLE_STRING,
                    "phone": _NULLABLE_STRING,
                    "email_hash": _NULLABLE_STRING,
                    "enabled": _NULLABLE_BOOLEAN,
                    "restore_allowed": _NULLABLE_BOOLEAN,
                    "parent_resource_id": _NULLABLE_STRING,
                },
                "required": ["resource_id"],
            },
        },
    },
    "required": ["records"],
}

# Gemini's field names mapped to the names RecordNormalizer already knows.
_FIELD_NAMES = {
    "user_id": "customer_reference",
    "name": "full_name",
    "email": "email",
    "phone": "phone",
    "email_hash": "email_hash",
    "status": "status",
    "data_category": "record_type",
    "enabled": "enabled",
    "restore_allowed": "restore_allowed",
    "parent_resource_id": "source_resource_id",
}


class ImageLoader(EvidenceLoader):
    file_suffixes = (".png", ".jpg", ".jpeg")

    def __init__(
        self,
        gemini: GeminiClient,
        aliases: dict[str, str] | None = None,
        escalation: EscalationPolicy | None = None,
    ):
        self.gemini = gemini
        # Catalog names (file names, statement numbers, user_17) -> resource ids.
        self.aliases = aliases or {}
        self.escalation = escalation or EscalationPolicy()

    def load(self, evidence_file: EvidenceFile, path: Path) -> list[RawEvidenceRecord]:
        answer = self._read_with_second_look(evidence_file.file_id, path)
        if not answer:
            return []  # offline and not cached: reported as unread by the index
        screen_subject = answer.get("screen_subject_user_id")
        records = []
        for position, item in enumerate(answer.get("records", []), start=1):
            resource_id = self._as_resource_id(item["resource_id"])
            if resource_id is None:
                continue
            fields = {_FIELD_NAMES[k]: v for k, v in item.items() if k in _FIELD_NAMES and v is not None}
            fields["resource_id"] = resource_id
            # The parent gets the same check. Gemini sometimes names a bucket
            # (northstar-customer-data), a ticket (CS-5034) or a person
            # (user_20) as the parent, and the linker would then audit that
            # name as if it were a stored record.
            parent_id = self._as_resource_id(item.get("parent_resource_id"))
            if parent_id:
                fields["source_resource_id"] = parent_id
            else:
                fields.pop("source_resource_id", None)
            # A screen saying "Not found" reports a lookup miss; the
            # normalizer reads that from a `result` field.
            status_word = str(item.get("status") or "").strip().upper().replace(" ", "_")
            if status_word in {"NOT_FOUND", "ABSENT", "MISSING"}:
                fields["result"] = "NOT_FOUND"
            # The screen's subject (a filter or customer header) says whom the
            # record belongs to, like a lookup key; it is not record content.
            if screen_subject:
                fields["subject_selector"] = screen_subject
            records.append(
                RawEvidenceRecord(
                    file_id=evidence_file.file_id,
                    observed_at=evidence_file.captured_at,
                    location=f"screenshot record {position}",
                    fields=fields,
                    is_listing_row=True,  # shown on screen, so it exists
                )
            )
        return records

    def _read(self, path: Path, thinking_level: str, attempt: int = 1) -> dict | None:
        return self.gemini.generate_json(
            purpose="media",
            prompt_version=PROMPT_VERSION,
            prompt=PROMPT_PATH.read_text(encoding="utf-8"),
            schema=MEDIA_SCHEMA,
            binary_part=(path.read_bytes(), "image/png"),
            thinking_level=thinking_level,
            attempt=attempt,
        )

    def _careful_read(self, file_id: str, path: Path) -> dict | None:
        """A HIGH read; read once more if it makes a mistake the checks know.

        A careful read can slip the same way a quick one does (it put
        case_50's name and email on "user_20" in 1 of 4 runs). The first
        answer that passes the checks is used; if both fail, the one with
        fewer doubts, and the run's notes say so.
        """
        first = self._read(path, HIGH)
        if first is None or not self.needs_second_look(first):
            return first
        second = self._read(path, HIGH, attempt=2)
        if second is None:
            return first
        remaining = self.needs_second_look(second)
        self.escalation.careful_retries.append((file_id, remaining))
        return second if len(remaining) <= len(self.needs_second_look(first)) else first

    def _read_with_second_look(self, file_id: str, path: Path) -> dict | None:
        policy = self.escalation
        if policy.cheap_pass_stopped():
            policy.high_only.append(file_id)
            return self._careful_read(file_id, path)
        quick = self._read(path, LOW)
        if quick is None:
            return None  # offline and not cached
        policy.cheap_reads += 1
        reasons = self.needs_second_look(quick)
        if reasons:
            policy.flagged.append((file_id, reasons))
            return self._careful_read(file_id, path) or quick
        if policy.take_spot_check():
            careful = self._read(path, HIGH)
            if careful is not None:
                policy.spot_checked.append(file_id)
                if self._audit_facts(careful) != self._audit_facts(quick):
                    policy.spot_check_differences.append(file_id)
                return careful
        return quick

    def needs_second_look(self, answer: dict) -> list[str]:
        """Plain-code checks for mistakes a quick read makes; empty means no doubt.

        Each check stands for a mistake seen in testing. The model itself
        never flagged these reads as unsure, so code has to.
        """
        reasons = []
        records = answer.get("records", [])
        listed = {self._as_resource_id(record.get("resource_id")) for record in records}
        on_records = {
            str(record.get(field)).strip().lower()
            for record in records if self._as_resource_id(record.get("resource_id"))
            for field in _PERSONAL_DATA_FIELDS if record.get(field)
        }
        for record in records:
            # case_50: the name and email went onto "user_20" instead of
            # analytics_7438. Non-records are dropped below, so personal data
            # that no real record carries would be lost without a second look.
            if self._as_resource_id(record.get("resource_id")) is None and any(
                str(record.get(field)).strip().lower() not in on_records
                for field in _PERSONAL_DATA_FIELDS if record.get(field)
            ):
                reasons.append(f"personal data only on {record.get('resource_id')}, which is not a record")
        # case_46: the screen is about user_17, the catalog name of
        # tombstone_17, but the quick read did not list it.
        subject = str(answer.get("screen_subject_user_id") or "").strip()
        if subject in self.aliases and self.aliases[subject] not in listed:
            reasons.append(f"screen is about {subject} ({self.aliases[subject]}) but does not list it")
        if not records:
            reasons.append("no records read")
        return reasons

    def _audit_facts(self, answer: dict) -> dict[str, tuple]:
        """What the audit takes from a read, used to compare a spot check with the quick read."""
        facts = {}
        for record in answer.get("records", []):
            resource_id = self._as_resource_id(record.get("resource_id"))
            if resource_id is None:
                continue
            facts[resource_id] = (
                tuple(field for field in _PERSONAL_DATA_FIELDS if record.get(field)),
                str(record.get("status") or "").strip().lower(),
                record.get("enabled"),
                record.get("restore_allowed"),
                self._as_resource_id(record.get("parent_resource_id")),
            )
        return facts

    def _as_resource_id(self, shown_id: str | None) -> str | None:
        """The resource id behind a name shown on screen, or None if it is no record.

        Gemini also copies orders (ORD-...), tickets (CS-...), user ids and
        bucket names. Only catalog resource ids, or catalog aliases of one
        (user_17 is tombstone_17), are records.
        """
        if not shown_id:
            return None
        shown_id = str(shown_id).strip()
        resource_id = self.aliases.get(shown_id, shown_id)
        return resource_id if RESOURCE_ID_PATTERN.fullmatch(resource_id) else None
