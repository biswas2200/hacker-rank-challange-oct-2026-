"""EvidenceLoader: the common interface every evidence format implements.

A loader's only job is to turn one file into RawEvidenceRecords. It does not
decide what a field means; RecordNormalizer does that for all formats at once.
"""

from __future__ import annotations

from abc import ABC, abstractmethod
from pathlib import Path

from models import EvidenceFile, RawEvidenceRecord, SnapshotListing


class EvidenceLoader(ABC):
    file_suffixes: tuple[str, ...] = ()

    def supports(self, path: Path) -> bool:
        return path.suffix.lower() in self.file_suffixes

    @abstractmethod
    def load(self, evidence_file: EvidenceFile, path: Path) -> list[RawEvidenceRecord]:
        ...

    def listings(self, evidence_file: EvidenceFile, path: Path) -> list[SnapshotListing]:
        """Complete store listings in the file. Most formats have none."""
        return []


def flatten_nested_fields(value: object, path: str = "") -> dict[str, object]:
    """Flatten nested dicts and lists into dotted keys.

    Evidence nests identifiers at different depths (a search hit's email
    lives under hits.documents[0].email). Flattening lets the normalizer find
    them without knowing each file's shape.
    """
    flat: dict[str, object] = {}
    if isinstance(value, dict):
        for key, child in value.items():
            flat.update(flatten_nested_fields(child, f"{path}.{key}" if path else str(key)))
    elif isinstance(value, list):
        for position, child in enumerate(value):
            flat.update(flatten_nested_fields(child, f"{path}[{position}]"))
    else:
        flat[path] = value
    return flat
