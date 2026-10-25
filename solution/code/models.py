"""Shared vocabulary of the audit pipeline: input rows, evidence and output steps.

Every stage passes these types to the next, so a reader can follow the whole
pipeline by reading this file first. The enums mirror the output contract in
AGENTS.md section 6.4 exactly; adding a member here would let the planner
emit a value the grader rejects, so they are closed on purpose.
"""

from __future__ import annotations

from dataclasses import dataclass, field
from datetime import datetime
from enum import Enum


# --------------------------------------------------------------------------
# Output contract
# --------------------------------------------------------------------------

class DeletionStatus(str, Enum):
    COMPLETE = "COMPLETE"
    INCOMPLETE = "INCOMPLETE"
    NOT_APPLICABLE = "NOT_APPLICABLE"


class Phase(str, Enum):
    # Declaration order is the required output order.
    FINDING = "FINDING"
    REMEDIATION = "REMEDIATION"
    VERIFICATION = "VERIFICATION"


class Operation(str, Enum):
    """Allowed step operations, in the order the contract tables list them.

    StepPlanner sorts steps by this declaration order, which also delivers the
    "disable flows and block restores before touching records" rule for free.
    """

    REPORT_REMAINING_RECORD = "REPORT_REMAINING_RECORD"
    REPORT_EXPOSED_PII = "REPORT_EXPOSED_PII"
    REPORT_ACTIVE_DATA_FLOW = "REPORT_ACTIVE_DATA_FLOW"
    REPORT_RESTORE_RISK = "REPORT_RESTORE_RISK"
    DISABLE_DATA_FLOW = "DISABLE_DATA_FLOW"
    BLOCK_RESTORE = "BLOCK_RESTORE"
    DELETE_RECORD = "DELETE_RECORD"
    REDACT_RECORD = "REDACT_RECORD"
    ANONYMIZE_RECORD = "ANONYMIZE_RECORD"
    VERIFY_RECORD_ABSENT = "VERIFY_RECORD_ABSENT"
    VERIFY_RECORD_PRESENT = "VERIFY_RECORD_PRESENT"
    VERIFY_RECORD_REDACTED = "VERIFY_RECORD_REDACTED"
    VERIFY_RECORD_ANONYMIZED = "VERIFY_RECORD_ANONYMIZED"
    VERIFY_DATA_FLOW_DISABLED = "VERIFY_DATA_FLOW_DISABLED"
    VERIFY_RESTORE_BLOCKED = "VERIFY_RESTORE_BLOCKED"


# The contract fixes one phase and one state per operation, so they are
# derived here instead of being chosen (and possibly mismatched) by callers.
OPERATION_PHASE_AND_STATE: dict[Operation, tuple[Phase, str]] = {
    Operation.REPORT_REMAINING_RECORD: (Phase.FINDING, "PRESENT"),
    Operation.REPORT_EXPOSED_PII: (Phase.FINDING, "PRESENT"),
    Operation.REPORT_ACTIVE_DATA_FLOW: (Phase.FINDING, "ACTIVE"),
    Operation.REPORT_RESTORE_RISK: (Phase.FINDING, "ACTIVE"),
    Operation.DISABLE_DATA_FLOW: (Phase.REMEDIATION, "DISABLED"),
    Operation.BLOCK_RESTORE: (Phase.REMEDIATION, "BLOCKED"),
    Operation.DELETE_RECORD: (Phase.REMEDIATION, "ABSENT"),
    Operation.REDACT_RECORD: (Phase.REMEDIATION, "REDACTED"),
    Operation.ANONYMIZE_RECORD: (Phase.REMEDIATION, "ANONYMIZED"),
    Operation.VERIFY_RECORD_ABSENT: (Phase.VERIFICATION, "ABSENT"),
    Operation.VERIFY_RECORD_PRESENT: (Phase.VERIFICATION, "PRESENT"),
    Operation.VERIFY_RECORD_REDACTED: (Phase.VERIFICATION, "REDACTED"),
    Operation.VERIFY_RECORD_ANONYMIZED: (Phase.VERIFICATION, "ANONYMIZED"),
    Operation.VERIFY_DATA_FLOW_DISABLED: (Phase.VERIFICATION, "DISABLED"),
    Operation.VERIFY_RESTORE_BLOCKED: (Phase.VERIFICATION, "BLOCKED"),
}


@dataclass(frozen=True)
class Step:
    step_no: int
    phase: Phase
    system_id: str
    resource_id: str
    operation: Operation
    state: str
    evidence_id: str

    def to_json_dict(self) -> dict:
        return {
            "step_no": self.step_no,
            "phase": self.phase.value,
            "system_id": self.system_id,
            "resource_id": self.resource_id,
            "operation": self.operation.value,
            "state": self.state,
            "evidence_id": self.evidence_id,
        }


@dataclass
class CaseResult:
    case_id: str
    status: DeletionStatus
    steps: list[Step] = field(default_factory=list)


# --------------------------------------------------------------------------
# Dataset rows (one dataclass per CSV in dataset/)
# --------------------------------------------------------------------------

@dataclass(frozen=True)
class DeletionRequest:
    case_id: str
    email: str
    requested_at: datetime
    reported_status: str  # a workflow claim, never proof (AGENTS.md 6.5)
    reported_at: datetime
    audit_at: datetime
    request_text: str


@dataclass(frozen=True)
class UserIdentity:
    user_id: str
    name: str
    email: str
    phone: str
    email_hash: str
    updated_at: datetime


@dataclass(frozen=True)
class IdentityChange:
    """One row of user_audit_log.csv: a past value of an identity field."""

    audit_id: str
    user_id: str
    event_time: datetime
    field_name: str
    old_value: str
    new_value: str


@dataclass(frozen=True)
class SystemActivity:
    activity_id: str
    event_time: datetime
    event_type: str
    source_system: str
    target_system: str
    resource_id: str
    reported_result: str  # a claim; evidence decides the real state
    details: str


@dataclass(frozen=True)
class DeletionRule:
    rule_id: str
    data_category: str
    condition: str
    required_action: str


@dataclass(frozen=True)
class EvidenceFile:
    file_id: str
    file_path: str
    origin: str
    captured_at: datetime


# --------------------------------------------------------------------------
# Normalized evidence
# --------------------------------------------------------------------------

class Presence(str, Enum):
    """What one piece of evidence says about whether a record exists."""

    PRESENT = "PRESENT"
    ABSENT = "ABSENT"
    # The file mentions the resource but says nothing about existence,
    # e.g. a log line that only starts a lookup.
    UNSTATED = "UNSTATED"


@dataclass(frozen=True)
class RawEvidenceRecord:
    """A single record as a loader found it, before any interpretation.

    Loaders only flatten their format into field/value pairs; one shared
    RecordNormalizer then interprets the fields. Keeping interpretation in
    one place stops six loaders from drifting into six different meanings
    of "deleted".
    """

    file_id: str
    observed_at: datetime
    # Where inside the file the record sits (SQL table, JSON path, log line).
    # Replica-drift detection needs the SQL table name.
    location: str
    fields: dict[str, object]
    # Hint for the owning system when the format states it outside the
    # record (JSON collection's data_store, log line's service).
    system_hint: str | None = None
    # A row in a table or collection dump exists simply by being listed.
    # A log line has to state the outcome (rows=1, result=hit) instead.
    is_listing_row: bool = False


@dataclass(frozen=True)
class SnapshotListing:
    """A complete listing of one store's records at one moment.

    A snapshot table or collection proves a record is gone by not listing
    it, which no single Observation can express. case_01 and case_31 are
    verified this way: evidence_54's primary table and evidence_51's empty
    customer_profiles collection simply do not contain the deleted account.
    """

    file_id: str
    observed_at: datetime
    system_id: str
    location: str
    resource_ids: frozenset[str]


@dataclass(frozen=True)
class Observation:
    """One fact about one resource, taken from one evidence file."""

    file_id: str
    observed_at: datetime
    location: str
    resource_id: str
    system_id: str
    # Only the category the evidence states itself; the policy engine falls
    # back to the resource prefix when this is None.
    data_category: str | None
    presence: Presence
    # The record's own status word when it has one (IDENTIFIABLE, ANONYMIZED,
    # DELETED, DELETION_PENDING). Needed to tell a compliant anonymized
    # analytics row from one that still points back to a person.
    stated_condition: str | None
    # The stable user reference (user_42) when the record carries one.
    subject_user_id: str | None
    emails: frozenset[str]
    phones: frozenset[str]
    names: frozenset[str]
    email_hashes: frozenset[str]
    # Resource this one was derived from, e.g. search_001 -> account_006.
    parent_resource_id: str | None
    # Data flows and backups: True means it can still copy or restore data.
    is_flow_enabled: bool | None = None
    is_restore_allowed: bool | None = None
    # What a lookup searched for (lookup_key, query, subject_selector). It
    # links the record to a person, but it is not data the record holds: an
    # anonymized analytics row found by looking up an email is still
    # anonymized. Kept apart from the identifier sets above for that reason.
    lookup_identifiers: frozenset[str] = frozenset()

    @property
    def exposes_direct_identifiers(self) -> bool:
        return bool(self.emails or self.phones or self.names or self.email_hashes)

    @property
    def is_replica_copy(self) -> bool:
        return "replica" in self.location
