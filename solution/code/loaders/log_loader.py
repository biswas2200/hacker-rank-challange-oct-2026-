"""LogLoader: reads service logs written in six different line styles.

The logs mix bracketed, pipe-separated, logfmt, syslog, JSON-per-line and
"message=<escaped logfmt>" styles. Every style still reduces to the same
three things: a timestamp, the emitting service, and key=value pairs, so
the loader parses those generically instead of keeping one parser per file.

One lookup is spread over several lines that share a trace id: the
"started" line names the email being looked up, and the "completed" line
names the resource and whether it was found. Lines are therefore grouped by
trace, and each resource mentioned in a trace becomes one record that also
carries the trace's lookup keys.
"""

from __future__ import annotations

import json
import re
from collections import defaultdict
from dataclasses import dataclass
from datetime import datetime
from pathlib import Path

from loaders.base import EvidenceLoader
from models import EvidenceFile, RawEvidenceRecord
from repository import parse_utc_timestamp

_LEADING_TIMESTAMP = re.compile(r"^(\d{4}-\d{2}-\d{2}T\d{2}:\d{2}:\d{2}Z)\s+(.*)$")
_KEY_VALUE_PAIR = re.compile(r'(\w+)=("(?:[^"\\]|\\.)*"|\S+)')
# Positional styles name the service and trace before any key=value pairs.
_BRACKET_STYLE = re.compile(r"^\[\w+\] \[(?P<service>[\w-]+)\] \[(?P<trace>[\w-]+)\] (?P<event>\w+):")
_PIPE_STYLE = re.compile(r"^\w+ \| (?P<service>[\w-]+) \| (?P<trace>[\w-]+) \| (?P<event>\w+) \|")
_SYSLOG_STYLE = re.compile(r"^<\d+>\d+ \S+ (?P<service>[\w-]+) (?P<trace>[\w-]+) - - ")

# Keys whose value is the identifier a lookup searched for. They describe
# the whole trace, so they are copied onto the trace's resource records.
_TRACE_LOOKUP_KEYS = (
    "lookup_key", "match_key", "subject_key", "owner_key", "scan_key", "correlation_key", "subject_ref",
)


@dataclass
class _ParsedLine:
    line_number: int
    timestamp: datetime
    service: str | None
    trace_id: str | None
    fields: dict[str, str]


class LogLoader(EvidenceLoader):
    file_suffixes = (".log",)

    def load(self, evidence_file: EvidenceFile, path: Path) -> list[RawEvidenceRecord]:
        parsed_lines = [
            parsed
            for line_number, line in enumerate(path.read_text(encoding="utf-8").splitlines(), start=1)
            if (parsed := _parse_line(line_number, line)) is not None
        ]

        lines_by_trace: dict[str, list[_ParsedLine]] = defaultdict(list)
        for parsed in parsed_lines:
            # A line without a trace id (record_lookup ledgers) stands alone.
            trace_key = parsed.trace_id or f"line-{parsed.line_number}"
            lines_by_trace[trace_key].append(parsed)

        records: list[RawEvidenceRecord] = []
        for trace_lines in lines_by_trace.values():
            records.extend(_records_from_trace(evidence_file, trace_lines))
        return sorted(records, key=lambda record: record.location)


def _records_from_trace(evidence_file: EvidenceFile, trace_lines: list[_ParsedLine]) -> list[RawEvidenceRecord]:
    lookup_fields = {
        key: line.fields[key] for line in trace_lines for key in _TRACE_LOOKUP_KEYS if key in line.fields
    }
    # Which connection pool served the trace tells a replica read from a
    # primary read, the log-side equivalent of a replica SQL table.
    pool = next((line.fields["pool"] for line in trace_lines if "pool" in line.fields), "")

    records = []
    for line in trace_lines:
        if "resource_id" not in line.fields:
            continue
        # Only the resource line's own outcome counts. Other lines in the
        # trace report unrelated outcomes (cache_lookup result=miss) that
        # would otherwise be mistaken for "record not found".
        fields = {**lookup_fields, **line.fields}
        records.append(
            RawEvidenceRecord(
                file_id=evidence_file.file_id,
                observed_at=line.timestamp,
                location=f"line {line.line_number}" + (f" pool={pool}" if pool else ""),
                fields=fields,
                system_hint=line.fields.get("system") or line.service,
            )
        )
    return records


def _parse_line(line_number: int, line: str) -> _ParsedLine | None:
    match = _LEADING_TIMESTAMP.match(line.strip())
    if not match:
        return None
    timestamp, body = parse_utc_timestamp(match.group(1)), match.group(2)

    if body.startswith("{"):
        fields = {key: str(value) for key, value in json.loads(body).items()}
        return _ParsedLine(line_number, timestamp, fields.get("service"), fields.get("trace_id"), fields)

    fields = _key_value_pairs(body)
    # "message=" wraps a second, escaped logfmt payload; unwrap it so its
    # resource_id and lookup keys are visible like any other field.
    if "message" in fields:
        fields.update(_key_value_pairs(fields.pop("message").replace('\\"', '"')))

    service = fields.get("service") or fields.get("function")
    trace_id = fields.get("trace_id") or fields.get("requestId")
    for positional_style in (_BRACKET_STYLE, _PIPE_STYLE, _SYSLOG_STYLE):
        positional = positional_style.match(body)
        if positional:
            service, trace_id = positional.group("service"), positional.group("trace")
            if "event" in positional.groupdict():
                fields.setdefault("event", positional.group("event"))
            break
    return _ParsedLine(line_number, timestamp, service, trace_id, fields)


def _key_value_pairs(text: str) -> dict[str, str]:
    return {key: value.strip('"') for key, value in _KEY_VALUE_PAIR.findall(text)}
