"""ResourceLinker: every resource that holds, or held, the subject's data.

Links come from four sources, strongest first. Each link records why it was
made, so `explain` can show the reasoning and a weak link can be told apart
from a proven one:

1. EVIDENCE_IDENTIFIER  an evidence record carries one of the subject's
                        identifiers (email, past email, phone, hash, user_id).
2. CATALOG_LINK         a LINK_RESOURCE_IDENTITY activity names the subject.
3. PARENT_RESOURCE      a linked record was derived from another one
                        (search_001 indexes account_006), so both belong.
4. WORKFLOW_WINDOW      the deletion workflow touched a record that carries
                        no identifier at all; see core/workflow_matching.py.

The fourth source exists because a correctly deleted or anonymized record
has, by definition, nothing left that names the person. Those records still
have to be verified, and the only participant-visible trace of who they
belonged to is when the person's deletion workflow touched them.
"""

from __future__ import annotations

import re
from dataclasses import dataclass, field
from datetime import datetime
from enum import Enum

from core.episodes import RequestEpisodes
from core.identity import SubjectProfile
from core.index import EvidenceIndex
from core.workflow_matching import WorkflowWindowMatcher
from loaders.record_normalizer import SYSTEM_BY_RESOURCE_PREFIX, resource_prefix
from models import DeletionRequest
from repository import DatasetRepository, normalize_email

# "Data catalog linked export_1842 (NST-2026-0718-1842) to ingrid.dahl@... and user_18."
_CATALOG_LINK_DETAILS = re.compile(
    r"linked (?P<resource>\S+) \((?P<alias>[^)]+)\) to (?P<email>\S+) and (?P<user>user_\d+)"
)
# "Attachment catalog confirmed object_19 (...) belongs only to priya.raman@... and user_07"
_CATALOG_OWNERSHIP_DETAILS = re.compile(
    r"confirmed (?P<resource>\S+) \((?P<alias>[^)]+)\) belongs only to (?P<email>\S+) and (?P<user>user_\d+)"
)


class LinkReason(str, Enum):
    EVIDENCE_IDENTIFIER = "EVIDENCE_IDENTIFIER"
    CATALOG_LINK = "CATALOG_LINK"
    PARENT_RESOURCE = "PARENT_RESOURCE"
    WORKFLOW_WINDOW = "WORKFLOW_WINDOW"


@dataclass
class ResourceLink:
    resource_id: str
    system_id: str
    reasons: set[LinkReason] = field(default_factory=set)
    # file_id / activity_id values that established the link, for explain.
    proof_ids: set[str] = field(default_factory=set)
    # Another name the catalog uses for the resource (a statement number,
    # a file name); evidence may mention only that name.
    aliases: set[str] = field(default_factory=set)


class ResourceLinker:
    def __init__(
        self,
        repository: DatasetRepository,
        index: EvidenceIndex,
        workflow_matcher: WorkflowWindowMatcher,
        episodes: RequestEpisodes,
    ):
        self.repository = repository
        self.index = index
        self.workflow_matcher = workflow_matcher
        self.episodes = episodes

    def link(self, profile: SubjectProfile, request: DeletionRequest) -> list[ResourceLink]:
        cutoff = request.audit_at
        episode_start = self.episodes.starts_after(request)

        def in_this_episode(moment: datetime) -> bool:
            return episode_start is None or moment > episode_start

        links: dict[str, ResourceLink] = {}

        def add(resource_id: str, reason: LinkReason, proof_id: str | None = None) -> ResourceLink:
            link = links.setdefault(
                resource_id,
                ResourceLink(resource_id, SYSTEM_BY_RESOURCE_PREFIX.get(resource_prefix(resource_id), "unknown")),
            )
            link.reasons.add(reason)
            if proof_id:
                link.proof_ids.add(proof_id)
            return link

        for identifier in sorted(profile.searchable_identifiers):
            for observation in self.index.by_identifier(identifier, cutoff):
                if not in_this_episode(observation.observed_at):
                    continue
                add(observation.resource_id, LinkReason.EVIDENCE_IDENTIFIER, observation.file_id)

        for activity, alias in self._catalog_links_for(profile, cutoff):
            if not in_this_episode(activity.event_time):
                continue
            add(activity.resource_id, LinkReason.CATALOG_LINK, activity.activity_id).aliases.add(alias)

        self._add_parent_resources(links, add, cutoff, in_this_episode)

        for activity in self.workflow_matcher.matched_activities(request.case_id):
            if activity.resource_id not in links:
                add(activity.resource_id, LinkReason.WORKFLOW_WINDOW, activity.activity_id)

        return [links[resource_id] for resource_id in sorted(links)]

    def _catalog_links_for(self, profile: SubjectProfile, cutoff: datetime):
        for activity in self.repository.activities:
            if activity.event_type != "LINK_RESOURCE_IDENTITY" or activity.event_time > cutoff:
                continue
            match = _CATALOG_LINK_DETAILS.search(activity.details) or _CATALOG_OWNERSHIP_DETAILS.search(
                activity.details
            )
            if not match:
                continue
            names_subject = (
                match.group("user") == profile.user_id
                or normalize_email(match.group("email").rstrip(".;")) in profile.emails
            )
            if names_subject:
                yield activity, match.group("alias")

    def _add_parent_resources(self, links: dict[str, ResourceLink], add, cutoff: datetime, in_this_episode) -> None:
        # Walk both directions until nothing new appears: a search document
        # names its source account, and an account's derived documents name it.
        frontier = list(links)
        while frontier:
            resource_id = frontier.pop()
            for observation in self.index.by_resource(resource_id, cutoff):
                if not in_this_episode(observation.observed_at):
                    continue
                parent = observation.parent_resource_id
                if parent and parent not in links:
                    add(parent, LinkReason.PARENT_RESOURCE, observation.file_id)
                    frontier.append(parent)
            for observation in self.index.by_parent(resource_id, cutoff):
                if in_this_episode(observation.observed_at) and observation.resource_id not in links:
                    add(observation.resource_id, LinkReason.PARENT_RESOURCE, observation.file_id)
                    frontier.append(observation.resource_id)
