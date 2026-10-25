"""StateReconstructor: what was true of one resource at the case's audit_at.

Only evidence can establish a state. Workflow statuses and activity results
are claims (AGENTS.md 6.6): activity_3814 says search_052 failed to delete,
but evidence_03 is what proves the record is still there. Activity is used
for two narrower things:

  * the action the deletion workflow attempted on a resource shows what
    treatment the policy wanted for it (see intended_action), and
  * an explicit connector or restore-access check is a last resort for
    flows and backups that no evidence file describes.

How readings combine:

  1. Every observation inside the request's episode, observed by the cutoff,
     becomes a reading. The latest reading wins.
  2. Replica drift: a replica copy that still holds the record outranks an
     earlier or equal "gone" reading. Deleting from the primary does not
     delete the failover copy (case_05, evidence_54).
  3. Absence by omission: a complete listing of the record's store (a
     primary SQL table, a snapshot collection), taken after the workflow
     last touched the record, proves it is gone by not listing it. case_01
     cites evidence_54 and case_31 cites evidence_51 this way. It is used
     only when nothing else describes the resource.
"""

from __future__ import annotations

from dataclasses import dataclass
from datetime import datetime
from enum import Enum

from core.episodes import RequestEpisodes
from core.index import EvidenceIndex
from core.linking import ResourceLink
from loaders.record_normalizer import resource_prefix
from models import DeletionRequest, Observation, Presence, SystemActivity
from repository import DatasetRepository

FLOW_PREFIX = "data_flow"
BACKUP_PREFIX = "backup"
DELETION_WORKFLOW_SOURCE = "deletion_worker"
# Activity types that show which treatment the workflow attempted.
INTENDED_ACTION_BY_EVENT = {
    "DELETE_RECORD": "DELETE",
    "REDACT_RECORD": "REDACT",
    "ANONYMIZE_RECORD": "ANONYMIZE",
    "RETAIN_RECORD": "RETAIN",
}


class Condition(str, Enum):
    ABSENT = "ABSENT"
    # The record exists and still holds a name, email, phone or email hash.
    PRESENT_IDENTIFIABLE = "PRESENT_IDENTIFIABLE"
    # The record exists but no longer holds direct identifiers (redacted,
    # anonymized, or a tombstone keeping only its user_id).
    PRESENT_DEIDENTIFIED = "PRESENT_DEIDENTIFIED"
    FLOW_ACTIVE = "FLOW_ACTIVE"
    FLOW_DISABLED = "FLOW_DISABLED"
    RESTORE_OPEN = "RESTORE_OPEN"
    RESTORE_BLOCKED = "RESTORE_BLOCKED"
    # Nothing participant-visible describes the resource at the cutoff.
    UNKNOWN = "UNKNOWN"


@dataclass(frozen=True)
class ResourceState:
    resource_id: str
    system_id: str
    condition: Condition
    # file_id or activity_id that proves the condition; None when UNKNOWN.
    evidence_id: str | None
    as_of: datetime | None
    # The category the evidence states for the record, if any.
    stated_category: str | None
    # DELETE / REDACT / ANONYMIZE / RETAIN as attempted by the deletion workflow.
    intended_action: str | None
    # Latest activity_id on the resource by the cutoff. Cited only when a
    # COMPLETE case must cite something and no evidence file describes the
    # resource; activity ids are allowed evidence ids (AGENTS.md 6.6).
    latest_activity_id: str | None = None


@dataclass(frozen=True)
class _Reading:
    condition: Condition
    observed_at: datetime
    evidence_id: str
    is_replica: bool


class StateReconstructor:
    def __init__(self, repository: DatasetRepository, index: EvidenceIndex, episodes: RequestEpisodes):
        self.repository = repository
        self.index = index
        self.episodes = episodes

    def state_at(self, link: ResourceLink, request: DeletionRequest) -> ResourceState:
        cutoff = request.audit_at
        episode_start = self.episodes.starts_after(request)
        observations = [
            observation
            for observation in self.index.by_resource(link.resource_id, cutoff)
            if episode_start is None or observation.observed_at > episode_start
        ]
        activities = [
            activity
            for activity in self.repository.activities_by_resource_id.get(link.resource_id, [])
            if activity.event_time <= cutoff and (episode_start is None or activity.event_time > episode_start)
        ]
        readings = [
            reading
            for observation in observations
            # A step may cite only files captured by the cutoff (AGENTS.md 6.6).
            if self.index.is_citable_at(observation.file_id, cutoff)
            and (reading := self._reading_from(observation)) is not None
        ]
        chosen = self._combine(readings)
        if chosen is None:
            chosen = self._absence_by_omission(link, activities, cutoff, episode_start)
        if chosen is None:
            chosen = self._reading_from_checks(link.resource_id, activities)

        stated_categories = [o.data_category for o in observations if o.data_category]
        return ResourceState(
            resource_id=link.resource_id,
            system_id=link.system_id,
            condition=chosen.condition if chosen else Condition.UNKNOWN,
            evidence_id=chosen.evidence_id if chosen else None,
            as_of=chosen.observed_at if chosen else None,
            stated_category=stated_categories[-1] if stated_categories else None,
            intended_action=self._intended_action(activities),
            latest_activity_id=activities[-1].activity_id if activities else None,
        )

    @staticmethod
    def _reading_from(observation: Observation) -> _Reading | None:
        prefix = resource_prefix(observation.resource_id)
        condition_word = observation.stated_condition or ""
        condition: Condition | None
        if prefix == FLOW_PREFIX:
            if observation.is_flow_enabled is True or condition_word == "ACTIVE":
                condition = Condition.FLOW_ACTIVE
            elif observation.is_flow_enabled is False or condition_word == "DISABLED":
                condition = Condition.FLOW_DISABLED
            else:
                condition = None
        elif prefix == BACKUP_PREFIX:
            if observation.is_restore_allowed is True or condition_word == "ACTIVE":
                condition = Condition.RESTORE_OPEN
            elif observation.is_restore_allowed is False or condition_word == "BLOCKED":
                condition = Condition.RESTORE_BLOCKED
            else:
                condition = None
        elif observation.presence is Presence.ABSENT:
            condition = Condition.ABSENT
        elif observation.presence is Presence.PRESENT:
            identifiable = observation.exposes_direct_identifiers or condition_word == "IDENTIFIABLE"
            condition = Condition.PRESENT_IDENTIFIABLE if identifiable else Condition.PRESENT_DEIDENTIFIED
        else:
            condition = None
        if condition is None:
            return None
        return _Reading(condition, observation.observed_at, observation.file_id, observation.is_replica_copy)

    @staticmethod
    def _combine(readings: list[_Reading]) -> _Reading | None:
        if not readings:
            return None
        # At equal times a "still there" reading wins over "gone": one
        # snapshot can show the primary without the row and a replica with it.
        is_present = {Condition.PRESENT_IDENTIFIABLE, Condition.PRESENT_DEIDENTIFIED}
        latest = max(readings, key=lambda r: (r.observed_at, r.condition in is_present, r.evidence_id))
        if latest.condition is Condition.ABSENT:
            surviving_replica_copies = [
                r for r in readings if r.is_replica and r.condition in is_present and r.observed_at >= latest.observed_at
            ]
            if surviving_replica_copies:
                return max(surviving_replica_copies, key=lambda r: (r.observed_at, r.evidence_id))
        return latest

    def _absence_by_omission(
        self,
        link: ResourceLink,
        activities: list[SystemActivity],
        cutoff: datetime,
        episode_start: datetime | None,
    ) -> _Reading | None:
        # Only a listing taken after the workflow last touched the record can
        # show the result of that action.
        last_touched = max((a.event_time for a in activities), default=None)
        candidates = [
            listing
            for listing in self.index.listings_for_system(link.system_id, cutoff)
            if link.resource_id not in listing.resource_ids
            and (episode_start is None or listing.observed_at > episode_start)
            and (last_touched is None or listing.observed_at > last_touched)
        ]
        if not candidates:
            return None
        latest = max(candidates, key=lambda listing: (listing.observed_at, listing.file_id))
        return _Reading(Condition.ABSENT, latest.observed_at, latest.file_id, is_replica=False)

    @staticmethod
    def _reading_from_checks(resource_id: str, activities: list[SystemActivity]) -> _Reading | None:
        """Explicit status checks, for flows and backups no file describes."""
        prefix = resource_prefix(resource_id)
        for activity in reversed(activities):
            if prefix == FLOW_PREFIX and activity.event_type == "CHECK_DATA_FLOW":
                enabled = "remains enabled" in activity.details
                condition = Condition.FLOW_ACTIVE if enabled else Condition.FLOW_DISABLED
                return _Reading(condition, activity.event_time, activity.activity_id, is_replica=False)
            if prefix == BACKUP_PREFIX and activity.event_type == "CHECK_RESTORE_ACCESS":
                restorable = "can still be restored" in activity.details
                condition = Condition.RESTORE_OPEN if restorable else Condition.RESTORE_BLOCKED
                return _Reading(condition, activity.event_time, activity.activity_id, is_replica=False)
        return None

    @staticmethod
    def _intended_action(activities: list[SystemActivity]) -> str | None:
        attempted = [
            INTENDED_ACTION_BY_EVENT[a.event_type]
            for a in activities
            if a.source_system == DELETION_WORKFLOW_SOURCE and a.event_type in INTENDED_ACTION_BY_EVENT
        ]
        return attempted[-1] if attempted else None
