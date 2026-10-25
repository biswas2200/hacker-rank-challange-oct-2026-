"""WorkflowWindowMatcher: links identifier-free records to the request whose
deletion workflow touched them.

Why this exists: once a record is correctly deleted or anonymized it no
longer names anyone, yet the contract still asks us to verify it. The
activity stream never names the person either, but each request's workflow
runs in a short window around its reported_at (in the public samples, every
workflow event falls between 1.5 hours before and 2 hours after it).

Windows of different requests overlap, so a time match alone is ambiguous.
The matcher resolves it by elimination, the way one solves a logic grid:

  * a record that evidence ties to some person belongs to that person only;
  * a request that already has a proven record of a given type (say an
    account) does not need a second one of that type from a time match;
  * a record whose window now contains exactly one request in need goes to
    that request, which can in turn settle other records. Repeat until
    nothing changes.
  * when several requests still compete for one primary-account deletion,
    the request whose reported_status matches that deletion's reported
    result wins. The workflow's claim is derived from its own primary
    action (COMPLETE <- SUCCESS, PARTIALLY_COMPLETE <- PARTIAL,
    NOT_STARTED <- FAILED); this held for every shared window in the public
    samples. The claim is only used to tell requests apart, never as proof
    of the record's state.

  * a request whose known actions use one wording family (see
    workflow_family) cannot own an action worded in another family.
  * when k records of one type still tie between the same k requests, they
    are paired in a fixed order (see _pair_tied_groups) and marked as paired.

Records that stay ambiguous in any other way are left unlinked: missing a
verification step costs less than attributing a stranger's record to the
subject.

Only requests that authorize deletion may compete. A non-deletion request
(case_539 asks what data is held) starts no deletion workflow, yet its
reported_at often falls in a deletion batch's window. Left in, it keeps
real candidates ambiguous.
"""

from __future__ import annotations

from collections import defaultdict
from datetime import timedelta

from core.episodes import RequestEpisodes
from core.identity import IdentityResolver, email_hash
from core.index import EvidenceIndex
from loaders.record_normalizer import resource_prefix
from models import DeletionRequest, SystemActivity
from repository import DatasetRepository, normalize_email

WORKFLOW_STARTS_BEFORE_REPORT = timedelta(hours=1.5)
WORKFLOW_ENDS_AFTER_REPORT = timedelta(hours=2)
# Only the deletion workflow acts on behalf of one request. Background jobs
# (cache refreshes, index updates, notification sends) run in the same
# windows for everyone, and in the samples every false match came from them.
DELETION_WORKFLOW_SOURCE = "deletion_worker"
PRIMARY_ACCOUNT_PREFIX = "account"
# The deletion worker words its activity details in one style per workflow,
# and every action of one request's workflow uses the same style. In the
# samples, COMPLETE-family requests show "Policy action completed for X",
# while requests with leftovers show "received the final result" and the
# connector or restore checks. The primary-profile delete is shared by all.
_WORKFLOW_FAMILY_BY_DETAIL_PREFIX = [
    ("Policy action completed", "POLICY_ACTION"),
    ("Deletion worker received the final result", "RESULT_CHECK"),
    ("Connector status check", "RESULT_CHECK"),
    ("Restore-access check", "RESULT_CHECK"),
    ("Deletion worker reported the requested operation", "DIRECT_REPORT"),
    ("Deletion worker stopped after", "DIRECT_REPORT"),
    ("Deletion worker completed some internal work", "DIRECT_REPORT"),
]


def workflow_family(activity: SystemActivity) -> str | None:
    """The workflow wording family of a deletion-worker activity, or None if shared."""
    for prefix, family in _WORKFLOW_FAMILY_BY_DETAIL_PREFIX:
        if activity.details.startswith(prefix):
            return family
    return None


REPORTED_STATUS_BY_PRIMARY_RESULT = {
    "SUCCESS": "COMPLETE",
    "PARTIAL": "PARTIALLY_COMPLETE",
    "FAILED": "NOT_STARTED",
}


def build_identifier_owner_map(repository: DatasetRepository) -> dict[str, str]:
    """Every identifier any user has ever had, mapped to that user."""
    owner_by_identifier: dict[str, str] = {}
    for identity in repository.identities:
        for identifier in (identity.user_id, identity.email, identity.phone, identity.email_hash):
            owner_by_identifier[identifier] = identity.user_id
    for change in repository.identity_changes:
        for value in (change.old_value, change.new_value):
            if not value:
                continue
            if change.field_name == "email":
                owner_by_identifier.setdefault(normalize_email(value), change.user_id)
                owner_by_identifier.setdefault(email_hash(value), change.user_id)
            else:
                owner_by_identifier.setdefault(value, change.user_id)
    return owner_by_identifier


class WorkflowWindowMatcher:
    def __init__(
        self,
        repository: DatasetRepository,
        index: EvidenceIndex,
        identity_resolver: IdentityResolver,
        deletion_requests: list[DeletionRequest],
        episodes: RequestEpisodes,
    ):
        self.repository = repository
        self.episodes = episodes
        self._matched_by_case: dict[str, list[SystemActivity]] = defaultdict(list)
        # Records attributed by tie-pairing rather than by elimination.
        self.paired_resources: set[str] = set()
        self._match_all(index, identity_resolver, deletion_requests)

    def matched_activities(self, case_id: str) -> list[SystemActivity]:
        return self._matched_by_case.get(case_id, [])

    def _match_all(
        self,
        index: EvidenceIndex,
        identity_resolver: IdentityResolver,
        deletion_requests: list[DeletionRequest],
    ) -> None:
        owners_by_resource = self._owners_by_resource(index)

        proven_types_by_case: dict[str, set[str]] = {}
        window_activities_by_case: dict[str, list[SystemActivity]] = {}
        families_by_case: dict[str, set[str]] = defaultdict(set)
        for request in deletion_requests:
            profile = identity_resolver.resolve(request.email, request.audit_at)
            subject_user_id = profile.user_id if profile else None
            # Same episode rule as ResourceLinker: a record proven only by an
            # earlier audit's evidence must not stop this request's match.
            episode_start = self.episodes.starts_after(request)
            proven_resources = {
                observation.resource_id
                for identifier in (profile.searchable_identifiers if profile else ())
                for observation in index.by_identifier(identifier, request.audit_at)
                if episode_start is None or observation.observed_at > episode_start
            }
            proven_types_by_case[request.case_id] = {resource_prefix(r) for r in proven_resources}
            window_activities_by_case[request.case_id] = [
                activity
                for activity in self._activities_in_window(request)
                # A record that evidence ever tied to somebody else is not
                # this subject's, whatever the timing says.
                if subject_user_id in owners_by_resource.get(activity.resource_id, {subject_user_id})
            ]
            for activity in window_activities_by_case[request.case_id]:
                if activity.resource_id in proven_resources and workflow_family(activity):
                    families_by_case[request.case_id].add(workflow_family(activity))

        reported_status_by_case = {request.case_id: request.reported_status for request in deletion_requests}
        claimed_resources: set[str] = set()

        def claim(case_id: str, resource_id: str, by_pairing: bool) -> None:
            matched = [a for a in window_activities_by_case[case_id] if a.resource_id == resource_id]
            self._matched_by_case[case_id].extend(matched)
            if by_pairing:
                self.paired_resources.add(resource_id)
            claimed_resources.add(resource_id)
            proven_types_by_case[case_id].add(resource_prefix(resource_id))
            families_by_case[case_id].update(workflow_family(a) for a in matched if workflow_family(a))

        def current_candidates() -> dict[str, set[str]]:
            candidates: dict[str, set[str]] = defaultdict(set)
            for case_id, activities in window_activities_by_case.items():
                for activity in activities:
                    if activity.resource_id in claimed_resources:
                        continue
                    if resource_prefix(activity.resource_id) in proven_types_by_case[case_id]:
                        continue
                    family = workflow_family(activity)
                    if family and families_by_case[case_id] and family not in families_by_case[case_id]:
                        continue
                    candidates[activity.resource_id].add(case_id)
            for resource_id, cases in candidates.items():
                if len(cases) > 1:
                    candidates[resource_id] = self._cases_whose_claim_matches(
                        resource_id, cases, window_activities_by_case, reported_status_by_case
                    )
            return candidates

        while True:
            candidates = current_candidates()
            progress = False
            for resource_id in sorted(candidates):
                cases = candidates[resource_id]
                # An earlier claim in this pass may already have settled the case.
                if len(cases) == 1 and resource_prefix(resource_id) not in proven_types_by_case[next(iter(cases))]:
                    claim(next(iter(cases)), resource_id, by_pairing=False)
                    progress = True
            if progress:
                continue
            if not self._pair_tied_groups(candidates, window_activities_by_case, claim):
                break

        # Kept for `explain` and link-check: which records stayed ambiguous,
        # and between which requests.
        self.unresolved_candidates = {r: c for r, c in current_candidates().items() if len(c) > 1}

    @staticmethod
    def _pair_tied_groups(candidates, window_activities_by_case, claim) -> bool:
        """Settle groups where k records of one type tie between the same k requests.

        Each request takes at most one record per type, so every request in
        such a group gets exactly one. The evidence cannot say which one goes
        where. Records are paired in workflow time order with requests in
        case_id order: deterministic, and it matched both tied groups in the
        public samples (case_25/26 and case_27/28). Pairing is a guess, and
        claims made this way are marked in `paired_resources` so later stages
        and `explain` can tell them apart from proven links.
        """
        groups: dict[tuple[str, frozenset[str]], list[str]] = defaultdict(list)
        for resource_id, cases in candidates.items():
            if len(cases) > 1:
                groups[(resource_prefix(resource_id), frozenset(cases))].append(resource_id)
        for (_, cases), resource_ids in sorted(groups.items(), key=lambda item: sorted(item[1])):
            if len(resource_ids) != len(cases):
                continue

            def first_event_time(resource_id: str):
                return min(
                    a.event_time
                    for case_id in cases
                    for a in window_activities_by_case[case_id]
                    if a.resource_id == resource_id
                )

            ordered_resources = sorted(resource_ids, key=lambda r: (first_event_time(r), r))
            for case_id, resource_id in zip(sorted(cases), ordered_resources):
                claim(case_id, resource_id, by_pairing=True)
            return True
        return False

    @staticmethod
    def _cases_whose_claim_matches(
        resource_id: str,
        candidate_cases: set[str],
        window_activities_by_case: dict[str, list[SystemActivity]],
        reported_status_by_case: dict[str, str],
    ) -> set[str]:
        if resource_prefix(resource_id) != PRIMARY_ACCOUNT_PREFIX:
            return candidate_cases
        matching = set()
        for case_id in candidate_cases:
            for activity in window_activities_by_case[case_id]:
                if activity.resource_id == resource_id and activity.event_type == "DELETE_RECORD":
                    implied_status = REPORTED_STATUS_BY_PRIMARY_RESULT.get(activity.reported_result)
                    if implied_status == reported_status_by_case[case_id]:
                        matching.add(case_id)
        return matching

    def _activities_in_window(self, request: DeletionRequest) -> list[SystemActivity]:
        window_start = request.reported_at - WORKFLOW_STARTS_BEFORE_REPORT
        window_end = min(request.reported_at + WORKFLOW_ENDS_AFTER_REPORT, request.audit_at)
        return [
            activity
            for activity in self.repository.activities
            if window_start <= activity.event_time <= window_end
            and activity.source_system == DELETION_WORKFLOW_SOURCE
        ]

    def _owners_by_resource(self, index: EvidenceIndex) -> dict[str, set[str]]:
        """Who each record ever belonged to, according to any evidence.

        Deliberately ignores the audit cutoff: ownership is used here only to
        rule strangers out, and a record that later shows a stranger's email
        was never this subject's to begin with.
        """
        owner_by_identifier = build_identifier_owner_map(self.repository)
        owners: dict[str, set[str]] = defaultdict(set)
        for observation in index.observations:
            identifiers = (
                set(observation.emails)
                | set(observation.phones)
                | set(observation.email_hashes)
                | set(observation.lookup_identifiers)
            )
            if observation.subject_user_id:
                identifiers.add(observation.subject_user_id)
            for identifier in identifiers:
                if identifier in owner_by_identifier:
                    owners[observation.resource_id].add(owner_by_identifier[identifier])
        return owners
