"""EvidenceIndex: every Observation from every file, queryable by cutoff.

Later stages never open evidence files; they ask this index. Every query
takes the case's audit_at, because the contract forbids using anything
that happened after it (AGENTS.md 6.2 and 6.6). Enforcing the cutoff here,
in one place, means no stage can forget it.
"""

from __future__ import annotations

from collections import defaultdict
from dataclasses import dataclass, field
from datetime import datetime

from loaders import default_loaders, loader_for
from loaders.base import EvidenceLoader
from loaders.record_normalizer import RecordNormalizer
from models import EvidenceFile, Observation, SnapshotListing
from repository import DatasetRepository


@dataclass
class FileLoadReport:
    file_id: str
    file_path: str
    loader_name: str | None
    raw_record_count: int = 0
    observation_count: int = 0
    error: str | None = None

    @property
    def needs_attention(self) -> bool:
        return self.error is not None or self.observation_count == 0


@dataclass
class EvidenceIndex:
    observations: list[Observation] = field(default_factory=list)
    load_reports: list[FileLoadReport] = field(default_factory=list)
    listings: list[SnapshotListing] = field(default_factory=list)

    def __post_init__(self) -> None:
        self._by_resource: dict[str, list[Observation]] = defaultdict(list)
        self._by_identifier: dict[str, list[Observation]] = defaultdict(list)
        self._by_file: dict[str, list[Observation]] = defaultdict(list)
        self._by_parent: dict[str, list[Observation]] = defaultdict(list)
        self._capture_time_by_file: dict[str, datetime] = {}

    @classmethod
    def build(
        cls,
        repository: DatasetRepository,
        loaders: list[EvidenceLoader] | None = None,
    ) -> "EvidenceIndex":
        loaders = loaders if loaders is not None else default_loaders()
        normalizer = RecordNormalizer()
        index = cls()
        for evidence_file in sorted(repository.evidence_files, key=lambda f: f.file_id):
            index._capture_time_by_file[evidence_file.file_id] = evidence_file.captured_at
            index._load_file(repository, evidence_file, loaders, normalizer)
        return index

    def _load_file(
        self,
        repository: DatasetRepository,
        evidence_file: EvidenceFile,
        loaders: list[EvidenceLoader],
        normalizer: RecordNormalizer,
    ) -> None:
        path = repository.evidence_path(evidence_file)
        loader = loader_for(path, loaders)
        report = FileLoadReport(evidence_file.file_id, evidence_file.file_path, type(loader).__name__ if loader else None)
        self.load_reports.append(report)
        if loader is None:
            report.error = "no loader for this format"
            return
        try:
            raw_records = loader.load(evidence_file, path)
        except Exception as error:  # one unreadable file must not stop the audit
            report.error = f"{type(error).__name__}: {error}"
            return
        report.raw_record_count = len(raw_records)
        self.listings.extend(loader.listings(evidence_file, path))
        for raw_record in raw_records:
            observation = normalizer.normalize(raw_record)
            if observation is not None:
                self._add(observation)
                report.observation_count += 1

    def _add(self, observation: Observation) -> None:
        self.observations.append(observation)
        self._by_resource[observation.resource_id].append(observation)
        self._by_file[observation.file_id].append(observation)
        if observation.parent_resource_id:
            self._by_parent[observation.parent_resource_id].append(observation)
        identifiers = (
            set(observation.emails)
            | set(observation.phones)
            | set(observation.email_hashes)
            | set(observation.lookup_identifiers)
        )
        if observation.subject_user_id:
            identifiers.add(observation.subject_user_id)
        for identifier in identifiers:
            self._by_identifier[identifier].append(observation)

    def _is_visible_at(self, observation: Observation, cutoff: datetime) -> bool:
        # What counts is when the fact was true, not when the file holding it
        # was exported: a log exported on 09-18 still records what a lookup
        # saw at 08:57 on 09-16. The public samples confirm this (case_09
        # links account_3960 only through such a log line). Whether the file
        # may also be *cited* is a separate question, see is_citable_at.
        return observation.observed_at <= cutoff

    def is_citable_at(self, file_id: str, cutoff: datetime) -> bool:
        """A step may only name evidence that existed by the cutoff (AGENTS.md 6.6)."""
        return self._capture_time_by_file[file_id] <= cutoff

    def listings_for_system(self, system_id: str, cutoff: datetime) -> list[SnapshotListing]:
        """Complete store listings a step may cite at the cutoff."""
        return [
            listing
            for listing in self.listings
            if listing.system_id == system_id
            and listing.observed_at <= cutoff
            and self.is_citable_at(listing.file_id, cutoff)
        ]

    def by_resource(self, resource_id: str, cutoff: datetime) -> list[Observation]:
        return [o for o in self._by_resource.get(resource_id, []) if self._is_visible_at(o, cutoff)]

    def by_identifier(self, identifier: str, cutoff: datetime) -> list[Observation]:
        return [o for o in self._by_identifier.get(identifier, []) if self._is_visible_at(o, cutoff)]

    def by_parent(self, parent_resource_id: str, cutoff: datetime) -> list[Observation]:
        """Records derived from the given one (search documents of an account)."""
        return [o for o in self._by_parent.get(parent_resource_id, []) if self._is_visible_at(o, cutoff)]

    def by_file(self, file_id: str, cutoff: datetime) -> list[Observation]:
        return [o for o in self._by_file.get(file_id, []) if self._is_visible_at(o, cutoff)]
