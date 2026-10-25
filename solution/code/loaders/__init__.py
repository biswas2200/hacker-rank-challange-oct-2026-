"""Evidence loaders, one per file format, plus the registry that picks one."""

from __future__ import annotations

from pathlib import Path

from loaders.base import EvidenceLoader
from loaders.json_loader import JsonLoader
from loaders.log_loader import LogLoader
from loaders.pdf_loader import PdfLoader
from loaders.sql_loader import SqlLoader
from loaders.text_loader import TextLoader


def default_loaders() -> list[EvidenceLoader]:
    # Formats readable without a model. Screenshots need ImageLoader, which
    # build_components adds with a GeminiClient; without it they are reported
    # as unread rather than silently skipped.
    return [SqlLoader(), JsonLoader(), LogLoader(), TextLoader(), PdfLoader()]


def loader_for(path: Path, loaders: list[EvidenceLoader]) -> EvidenceLoader | None:
    return next((loader for loader in loaders if loader.supports(path)), None)
