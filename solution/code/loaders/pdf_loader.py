"""PdfLoader: reads business documents (statements, registers, attachments).

The PDFs have a text layer, so pypdf reads them without any model call.
They are laid out for people, not machines, so instead of parsing a layout
the loader picks out the identifiers it can recognize by pattern. Each
resource id in a document becomes one record that carries every identifier
in that document, because a document like a support attachment is about
one person.
"""

from __future__ import annotations

import re
from pathlib import Path

from pypdf import PdfReader

from loaders.base import EvidenceLoader
from models import EvidenceFile, RawEvidenceRecord

RESOURCE_ID_PATTERN = re.compile(
    r"\b(?:account|search|analytics|support|export|data_flow|backup|audit_record|tombstone|"
    r"cache|email_delivery|legal_hold|marketing|telemetry|object)_\d+\b"
)
USER_ID_PATTERN = re.compile(r"\buser_\d+\b")
EMAIL_PATTERN = re.compile(r"[\w.+-]+@[\w-]+(?:\.[\w-]+)+")
PHONE_PATTERN = re.compile(r"\+\d[\d ()-]{6,}\d")
_ATTACHMENT_PATTERN = re.compile(r"attachment properties|upload history", re.IGNORECASE)


class PdfLoader(EvidenceLoader):
    file_suffixes = (".pdf",)

    def __init__(self, aliases: dict[str, str] | None = None):
        # Catalog names (statement numbers, file names) -> resource ids.
        self.aliases = aliases or {}

    def load(self, evidence_file: EvidenceFile, path: Path) -> list[RawEvidenceRecord]:
        text = "\n".join(page.extract_text() or "" for page in PdfReader(path).pages)
        document_identifiers = {
            "user_ids": sorted(set(USER_ID_PATTERN.findall(text))),
            "emails": sorted(set(EMAIL_PATTERN.findall(text))),
            "phones": sorted(set(PHONE_PATTERN.findall(text))),
        }
        # A document that describes its own upload ("Attachment properties
        # and upload history", file name, checksum) is a file attached to a
        # support case. Rule_06 deletes such attachments, while a support
        # message (rule_05) is only redacted, so the category matters.
        is_support_attachment = bool(_ATTACHMENT_PATTERN.search(text))
        return [
            RawEvidenceRecord(
                file_id=evidence_file.file_id,
                observed_at=evidence_file.captured_at,
                location=f"pdf mention {position}",
                fields={
                    "resource_id": resource_id,
                    **_as_single_fields(document_identifiers),
                    **(
                        {"record_type": "SUPPORT_ATTACHMENT"}
                        if is_support_attachment and resource_id.startswith("support_")
                        else {}
                    ),
                },
                is_listing_row=True,
            )
            for position, resource_id in enumerate(self._resource_ids_in(text), start=1)
        ]

    def _resource_ids_in(self, text: str) -> list[str]:
        found = set(RESOURCE_ID_PATTERN.findall(text))
        # A user id alias (tombstone_17 is "user_17") would match every
        # document that merely mentions the person, so only distinctive
        # names such as statement numbers and file names are searched.
        found.update(
            resource_id
            for alias, resource_id in self.aliases.items()
            if not USER_ID_PATTERN.fullmatch(alias) and alias in text
        )
        return sorted(found)


def _as_single_fields(identifiers: dict[str, list[str]]) -> dict[str, str]:
    # The normalizer expects scalar fields; numbered keys keep every value.
    return {
        f"{kind}[{position}]": value
        for kind, values in identifiers.items()
        for position, value in enumerate(values)
    }
