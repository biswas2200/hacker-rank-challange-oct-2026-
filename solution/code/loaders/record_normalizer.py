"""RecordNormalizer: turns any RawEvidenceRecord into an Observation.

This is where field names from every evidence format are given one meaning.
The formats disagree on names (customer_reference / user_id /
subject_reference, rows=0 / http_status=404 / result=NOT_FOUND), so each
concept below lists every spelling seen in the dataset.
"""

from __future__ import annotations

import re

from models import Observation, Presence, RawEvidenceRecord
from repository import normalize_email

# The output contract names systems by resource type (support_* is always
# support_platform), even where an activity row or log names a reader such
# as search_service. The prefix is therefore the reliable source.
SYSTEM_BY_RESOURCE_PREFIX = {
    "account": "account_service",
    "search": "search_service",
    "analytics": "analytics_platform",
    "support": "support_platform",
    "export": "export_service",
    "data_flow": "integration_service",
    "backup": "backup_service",
    "audit_record": "security_service",
    "tombstone": "identity_service",
    "cache": "cache_service",
    "email_delivery": "email_delivery_service",
    "legal_hold": "legal_service",
    "marketing": "marketing_platform",
    "object": "object_storage",
}

USER_ID_PATTERN = re.compile(r"^user_\d+$")
EMAIL_PATTERN = re.compile(r"[\w.+-]+@[\w-]+(?:\.[\w-]+)+")
SHA256_HEX_PATTERN = re.compile(r"^[0-9a-f]{64}$")

_SUBJECT_REFERENCE_KEYS = {"customer_reference", "user_id", "user_ids", "subject_reference"}
# Keys naming what a lookup searched for rather than what the record holds.
_LOOKUP_KEYS = {
    "lookup_key", "match_key", "subject_key", "owner_key", "scan_key", "correlation_key", "subject_ref",
    "query", "subject_selector",
}
_NAME_KEYS = {"full_name", "display_name", "requester_name", "customer_name", "name"}
_PARENT_KEYS = {"source_resource_id", "parent_resource_id"}
_CATEGORY_KEYS = {"record_type", "data_category"}
_FOUND_WORDS = {"FOUND", "HIT", "IDENTIFIABLE", "ACTIVE", "ANONYMIZED", "REDACTED", "PRESENT"}
_NOT_FOUND_WORDS = {"NOT_FOUND", "MISS", "ABSENT"}
# Words that describe the record's condition beyond merely existing.
_CONDITION_KEYS = ("result", "account_status", "status")


def resource_prefix(resource_id: str) -> str:
    return re.sub(r"_\d+$", "", resource_id)


def _leaf_key(dotted_key: str) -> str:
    # "hits.documents[0].email" -> "email"; "phones[2]" -> "phones"
    return re.sub(r"\[\d+\]$", "", dotted_key.rsplit(".", 1)[-1])


def _is_null(value: object) -> bool:
    return value is None or (isinstance(value, str) and value.strip().lower() in {"", "null", "none"})


def _as_bool(value: object) -> bool | None:
    if _is_null(value):
        return None
    return str(value).strip().lower() in {"1", "true", "yes"}


class RecordNormalizer:
    def normalize(self, record: RawEvidenceRecord) -> Observation | None:
        resource_id = record.fields.get("resource_id")
        if _is_null(resource_id):
            return None
        resource_id = str(resource_id).strip()

        emails: set[str] = set()
        phones: set[str] = set()
        names: set[str] = set()
        email_hashes: set[str] = set()
        lookup_identifiers: set[str] = set()
        subject_user_id: str | None = None
        parent_resource_id: str | None = None
        data_category: str | None = None
        values_by_leaf: dict[str, object] = {}

        for dotted_key, value in record.fields.items():
            if _is_null(value):
                continue
            leaf = _leaf_key(dotted_key)
            # The shallowest occurrence wins: an outer status describes the
            # record, a nested one may describe something it contains.
            values_by_leaf.setdefault(leaf, value)
            text = str(value).strip()

            if leaf in _LOOKUP_KEYS:
                lookup_identifiers.add(normalize_email(text) if EMAIL_PATTERN.fullmatch(text) else text)
                continue

            # Emails are collected from every field, including free text,
            # because support messages quote addresses inside sentences.
            emails.update(normalize_email(found) for found in EMAIL_PATTERN.findall(text))

            if leaf in _SUBJECT_REFERENCE_KEYS and USER_ID_PATTERN.match(text):
                subject_user_id = subject_user_id or text
            elif leaf == "email_hash" or SHA256_HEX_PATTERN.match(text):
                email_hashes.add(text.lower())
            elif "phone" in leaf:
                phones.add(text)
            elif leaf in _NAME_KEYS:
                names.add(text)
            elif leaf in _PARENT_KEYS:
                parent_resource_id = text
            elif leaf in _CATEGORY_KEYS:
                data_category = text.upper()

        prefix = resource_prefix(resource_id)
        return Observation(
            file_id=record.file_id,
            observed_at=record.observed_at,
            location=record.location,
            resource_id=resource_id,
            system_id=SYSTEM_BY_RESOURCE_PREFIX.get(prefix) or record.system_hint or "unknown",
            data_category=data_category,
            presence=_presence(values_by_leaf, record.is_listing_row),
            stated_condition=_stated_condition(values_by_leaf),
            subject_user_id=subject_user_id,
            emails=frozenset(emails),
            phones=frozenset(phones),
            names=frozenset(names),
            email_hashes=frozenset(email_hashes),
            parent_resource_id=parent_resource_id,
            is_flow_enabled=_as_bool(values_by_leaf.get("enabled")),
            is_restore_allowed=_restore_allowed(values_by_leaf),
            lookup_identifiers=frozenset(lookup_identifiers),
        )


def _presence(values_by_leaf: dict[str, object], is_listing_row: bool) -> Presence:
    result = str(values_by_leaf.get("result", "")).upper()
    if result in _NOT_FOUND_WORDS:
        return Presence.ABSENT
    if result in _FOUND_WORDS:
        return Presence.PRESENT

    for status_key in ("http_status", "status_code"):
        status = str(values_by_leaf.get(status_key, ""))
        if status == "404":
            return Presence.ABSENT
        if status == "200":
            return Presence.PRESENT

    rows = values_by_leaf.get("rows")
    if rows is not None and str(rows).isdigit():
        return Presence.PRESENT if int(str(rows)) > 0 else Presence.ABSENT

    for existence_key in ("available", "archive_present"):
        exists = _as_bool(values_by_leaf.get(existence_key))
        if exists is not None:
            return Presence.PRESENT if exists else Presence.ABSENT

    # A PII scanner can only match a value inside a record that exists.
    if "matched_value" in values_by_leaf or "direct_identifiers" in values_by_leaf:
        return Presence.PRESENT

    # A log event that reports on the record (read_completed, heartbeat,
    # restore_candidate_evaluated) means the service read it. Only a
    # "..._started" line announces a lookup without saying what it found.
    event = str(values_by_leaf.get("event", ""))
    if event and not event.endswith("_started"):
        return Presence.PRESENT

    # A 503 or a bare "lookup started" line proves nothing either way.
    return Presence.PRESENT if is_listing_row else Presence.UNSTATED


def _restore_allowed(values_by_leaf: dict[str, object]) -> bool | None:
    """Whether a backup can still be restored, under each spelling in the logs."""
    explicit = _as_bool(values_by_leaf.get("restore_allowed"))
    if explicit is not None:
        return explicit
    if _as_bool(values_by_leaf.get("deletion_block")) is True:
        return False
    decision = str(values_by_leaf.get("decision", "")).lower()
    if decision in {"deny", "denied", "blocked"}:
        return False
    if decision in {"allow", "allowed", "approved"}:
        return True
    if _as_bool(values_by_leaf.get("eligible")) is True and _as_bool(values_by_leaf.get("deletion_block")) is False:
        return True
    return None


def _stated_condition(values_by_leaf: dict[str, object]) -> str | None:
    # "direct_identifiers=1" is a scanner's verdict that the record still
    # names someone, even when the log masks the value itself.
    if str(values_by_leaf.get("direct_identifiers", "")) == "1":
        return "IDENTIFIABLE"
    for key in _CONDITION_KEYS:
        value = values_by_leaf.get(key)
        if not _is_null(value):
            return str(value).upper()
    return None
