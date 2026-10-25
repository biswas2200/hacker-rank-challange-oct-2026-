"""PolicyEngine: what deletion_rules.csv requires for a resource, and whether it holds.

The required treatment is chosen from the strongest available source:

  1. a data category the evidence states for the record (evidence_03 rows
     say SEARCH_INDEX_ENTRY, SUPPORT_MESSAGE, ...);
  2. the treatment the deletion workflow attempted. The workflow acts on the
     organization's own classification, which the evidence often leaves
     unstated: it sent DELETE for support_002 (an attachment, not a message
     to redact) and for analytics_9914, and the public answers agree;
  3. the category implied by the resource type (search_* is a search entry).

Its outcome is a Decision naming the finding to report (if the rule is not
met), the remediation that fixes it, and the check that proves the final
state. The step planner turns Decisions into ordered steps.
"""

from __future__ import annotations

from dataclasses import dataclass
from enum import Enum

from core.state import BACKUP_PREFIX, FLOW_PREFIX, Condition, ResourceState
from loaders.record_normalizer import resource_prefix
from models import Operation
from repository import DatasetRepository

CATEGORY_BY_PREFIX = {
    "account": "USER_PROFILE",
    "search": "SEARCH_INDEX_ENTRY",
    "cache": "CACHE_ENTRY",
    "support": "SUPPORT_MESSAGE",
    "object": "SUPPORT_ATTACHMENT",
    "analytics": "ANALYTICS_PROFILE",
    "telemetry": "PRODUCT_TELEMETRY",
    "export": "USER_EXPORT",
    "marketing": "MARKETING_PROFILE",
    "email_delivery": "EMAIL_DELIVERY_LOG",
    "audit_record": "SECURITY_AUDIT_LOG",
    "legal_hold": "LEGAL_HOLD_RECORD",
    "backup": "BACKUP_ARCHIVE",
    "tombstone": "ACCOUNT_TOMBSTONE",
}


class Treatment(str, Enum):
    DELETE = "DELETE"
    REDACT = "REDACT"
    ANONYMIZE = "ANONYMIZE"
    RETAIN = "RETAIN"
    # Backups are retained (rule_14) but their restore path must be closed.
    BLOCK_RESTORE = "BLOCK_RESTORE"
    DISABLE_FLOW = "DISABLE_FLOW"


@dataclass(frozen=True)
class Decision:
    state: ResourceState
    treatment: Treatment
    finding: Operation | None
    remediation: Operation | None
    verification: Operation

    @property
    def is_compliant(self) -> bool:
        return self.finding is None


_PRESENT = {Condition.PRESENT_IDENTIFIABLE, Condition.PRESENT_DEIDENTIFIED}


class PolicyEngine:
    def __init__(self, repository: DatasetRepository):
        self.action_by_category = {rule.data_category: rule.required_action for rule in repository.rules}

    def treatment_for(self, state: ResourceState) -> Treatment:
        prefix = resource_prefix(state.resource_id)
        if prefix == FLOW_PREFIX:
            return Treatment.DISABLE_FLOW
        if prefix == BACKUP_PREFIX:
            return Treatment.BLOCK_RESTORE
        if state.stated_category in self.action_by_category:
            return Treatment(self.action_by_category[state.stated_category])
        if state.intended_action:
            return Treatment(state.intended_action)
        category = CATEGORY_BY_PREFIX.get(prefix)
        # An unknown record type is treated as personal data to delete:
        # keeping it is the riskier mistake.
        return Treatment(self.action_by_category.get(category, "DELETE"))

    def evaluate(self, state: ResourceState) -> Decision:
        treatment = self.treatment_for(state)
        condition = state.condition
        if treatment is Treatment.DISABLE_FLOW:
            if condition is Condition.FLOW_ACTIVE:
                return Decision(state, treatment, Operation.REPORT_ACTIVE_DATA_FLOW,
                                Operation.DISABLE_DATA_FLOW, Operation.VERIFY_DATA_FLOW_DISABLED)
            return Decision(state, treatment, None, None, Operation.VERIFY_DATA_FLOW_DISABLED)
        if treatment is Treatment.BLOCK_RESTORE:
            if condition is Condition.RESTORE_OPEN:
                return Decision(state, treatment, Operation.REPORT_RESTORE_RISK,
                                Operation.BLOCK_RESTORE, Operation.VERIFY_RESTORE_BLOCKED)
            return Decision(state, treatment, None, None, Operation.VERIFY_RESTORE_BLOCKED)
        if treatment is Treatment.DELETE:
            if condition in _PRESENT:
                return Decision(state, treatment, Operation.REPORT_REMAINING_RECORD,
                                Operation.DELETE_RECORD, Operation.VERIFY_RECORD_ABSENT)
            # UNKNOWN is not reported: no evidence shows the record remains,
            # and a workflow claim of failure is not proof (case_23's
            # account_410 has only a FAILED claim and no finding).
            return Decision(state, treatment, None, None, Operation.VERIFY_RECORD_ABSENT)
        if treatment in (Treatment.REDACT, Treatment.ANONYMIZE):
            remediation, verification = (
                (Operation.REDACT_RECORD, Operation.VERIFY_RECORD_REDACTED)
                if treatment is Treatment.REDACT
                else (Operation.ANONYMIZE_RECORD, Operation.VERIFY_RECORD_ANONYMIZED)
            )
            if condition is Condition.PRESENT_IDENTIFIABLE:
                return Decision(state, treatment, Operation.REPORT_EXPOSED_PII, remediation, verification)
            if condition is Condition.ABSENT:
                # Removed entirely; there is no operation to bring it back,
                # so the honest check is that it is gone.
                return Decision(state, treatment, None, None, Operation.VERIFY_RECORD_ABSENT)
            return Decision(state, treatment, None, None, verification)
        # RETAIN: nothing to remove; prove the record is still kept.
        return Decision(state, treatment, None, None, Operation.VERIFY_RECORD_PRESENT)
