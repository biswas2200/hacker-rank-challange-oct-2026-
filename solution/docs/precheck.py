"""Pre-submission checklist. Builds a candidate code.zip in a temp folder and checks everything.

    python docs/precheck.py            # prints PASS/FAIL per item and a GO / NO-GO line

Nothing in the repository is changed. The candidate zip is replayed offline in a temp folder
(fresh unzip + dataset), so the reproduction test cannot overwrite the real reports.
"""

from __future__ import annotations

import csv
import json
import re
import shutil
import subprocess
import sys
import tempfile
import zipfile
from pathlib import Path

ROOT = Path(__file__).resolve().parent.parent
ZIP_INCLUDE = ["README.md", "requirements.txt", ".env.example", "code"]
ZIP_EXCLUDE_PARTS = {"__pycache__", ".venv", "venv", "node_modules", ".git", ".DS_Store"}
SECRET_PATTERNS = [r"AIza[0-9A-Za-z_\-]{20,}", r"sk-[A-Za-z0-9]{20,}", r"BEGIN [A-Z ]*PRIVATE KEY",
                   r"\"client_secret\"", r"\"refresh_token\"", r"ya29\.[0-9A-Za-z_\-]+"]
results: list[tuple[bool, str, str]] = []


def check(ok: bool, item: str, detail: str = "") -> None:
    results.append((bool(ok), item, detail))


def build_zip(target: Path) -> None:
    with zipfile.ZipFile(target, "w", zipfile.ZIP_DEFLATED) as archive:
        for name in ZIP_INCLUDE:
            path = ROOT / name
            files = [path] if path.is_file() else sorted(p for p in path.rglob("*") if p.is_file())
            for file in files:
                relative = file.relative_to(ROOT)
                if ZIP_EXCLUDE_PARTS & set(relative.parts) or file.suffix in {".pyc", ".zip"}:
                    continue
                archive.write(file, relative.as_posix())


def main() -> int:
    python = sys.executable
    output = ROOT / "output.csv"
    requests = list(csv.DictReader((ROOT / "dataset" / "deletion_requests.csv").open(encoding="utf-8")))

    # A. output.csv
    with output.open(encoding="utf-8", newline="") as handle:
        reader = csv.DictReader(handle)
        header, rows = reader.fieldnames, list(reader)
    check(header == ["case_id", "deletion_status", "steps_json"], "A1 output.csv header is exact", str(header))
    check(len(rows) == 518 and [r["case_id"] for r in rows] == [r["case_id"] for r in requests],
          "A2 518 rows, one per request, in deletion_requests.csv order", f"{len(rows)} rows")
    counts = {s: sum(r["deletion_status"] == s for r in rows) for s in ("COMPLETE", "INCOMPLETE", "NOT_APPLICABLE")}
    check(sum(counts.values()) == len(rows), "A3 every status is COMPLETE / INCOMPLETE / NOT_APPLICABLE", str(counts))
    validate = subprocess.run([python, "-W", "ignore", "code/main.py", "--no-llm", "validate"], cwd=ROOT,
                              capture_output=True, text=True)
    check("validator: 0 issue(s)" in validate.stdout, "A4 contract validator: 0 issues (step order, evidence, ids)",
          validate.stdout.strip().splitlines()[-1] if validate.stdout.strip() else validate.stderr[-200:])

    # B. reports
    evaluation = (ROOT / "code/evaluation/evaluation_report.md").read_text(encoding="utf-8")
    for needle, item in [("Status accuracy", "B1 case-level status accuracy"),
                         ("Step exact match by position", "B2 step-level accuracy"),
                         ("Agreement by position", "B3 field-level accuracy"),
                         ("Sequence accuracy", "B4 sequence accuracy"),
                         ("## 4. Failures found and improvements made", "B6 failures found and improvements made")]:
        check(needle in evaluation, f"{item} in evaluation_report.md")
    custom = re.findall(r"^\| custom_\d+ \|.*\| (pass|FAIL) \|$", evaluation, flags=re.MULTILINE)
    check(len(custom) >= 3, "B5 at least 3 custom evaluation cases in evaluation_report.md", f"results: {custom}")
    usage = (ROOT / "code/evaluation/usage_report.md").read_text(encoding="utf-8")
    for needle, item in [("Google Vertex AI", "B7 provider"), ("gemini-", "B8 model name"), ("Model calls", "B9 call counts"),
                         ("Input tokens", "B10 input/output tokens"), ("average tokens per request", "B11 total + average tokens"),
                         ("cost per request", "B12 total + per-request cost"), ("**overall**", "B13 overall totals")]:
        check(needle in usage, f"{item} in usage_report.md")
    overall = re.search(r"\| \*\*overall\*\* \| all \| (\d+) \| (\d+) \|", usage)
    check(overall and overall.group(1) == overall.group(2), "B14 usage report is from a live run (every call live)",
          f"calls {overall.group(1)}, live {overall.group(2)}" if overall else "no overall row")

    # C. code.zip candidate
    temp = Path(tempfile.mkdtemp(prefix="precheck_"))
    candidate = temp / "code.zip"
    build_zip(candidate)
    names = zipfile.ZipFile(candidate).namelist()
    required = ["README.md", "requirements.txt", "code/main.py", "code/evaluation/evaluation_report.md",
                "code/evaluation/usage_report.md", "code/llm/prompts/intent.md", "code/llm/prompts/media_extract.md"]
    check(all(n in names for n in required), "C1 zip has README, requirements, main.py, both reports, prompts")
    banned = [n for n in names if n.startswith("dataset/") or ZIP_EXCLUDE_PARTS & set(n.split("/"))
              or n.endswith((".zip", ".tar", ".gz", ".tgz", ".7z", ".rar", ".pyc")) or Path(n).name == ".env"]
    check(not banned, "C2 no dataset/, venv, __pycache__, nested archives or .env in the zip", str(banned[:5]))
    leaks = []
    for name in names:
        text = zipfile.ZipFile(candidate).read(name).decode("utf-8", errors="ignore")
        leaks += [f"{name}: {p}" for p in SECRET_PATTERNS if re.search(p, text)]
    check(not leaks, "C3 no API keys, tokens or private keys in the zip", str(leaks[:3]))
    size_mb = candidate.stat().st_size / 1e6
    check(size_mb < 20, "C4 zip size is reasonable", f"{size_mb:.2f} MB, {len(names)} files")

    # D. fresh unzip replays the final run offline, byte for byte
    replay = temp / "replay"
    zipfile.ZipFile(candidate).extractall(replay)
    (replay / "dataset").symlink_to(ROOT / "dataset", target_is_directory=True)
    run = subprocess.run([python, "-W", "ignore", "code/main.py", "--no-llm", "run"], cwd=replay,
                         capture_output=True, text=True)
    replayed = replay / "output.csv"
    same = replayed.exists() and replayed.read_bytes() == output.read_bytes()
    check(same, "D1 fresh unzip + dataset, offline run: identical output.csv",
          run.stdout.strip().splitlines()[0] if run.stdout.strip() else run.stderr[-300:])
    check("validator: 0 issue(s)" in run.stdout, "D2 replay passes the validator")

    # E. rules and logs
    core_sources = [p for p in (ROOT / "code").rglob("*.py") if "evaluation" not in p.parts and p.name != "main.py"]
    golden_reads = [str(p.relative_to(ROOT)) for p in core_sources if "sample_output" in p.read_text(encoding="utf-8")]
    check(not golden_reads, "E1 pipeline code never reads sample_output.csv (only eval tools do)", str(golden_reads))
    log = (ROOT / "log.txt").read_text(encoding="utf-8")
    entries = re.split(r"^(?=## \[)", log, flags=re.MULTILINE)[1:]
    invalid = [
        entry.splitlines()[0] for entry in entries
        if len(re.findall(r"^tool=\S.*$", entry, flags=re.MULTILINE)) != 1
        or len(re.findall(r"^model=claude-[a-z0-9-]+$", entry, flags=re.MULTILINE)) != 1
    ]
    check(entries and not invalid, "E2 every log.txt entry has exactly one tool= and one model= line",
          f"{len(entries)} entries; invalid: {invalid[:3]}")
    check(not any(re.search(p, log) for p in SECRET_PATTERNS), "E3 no secrets in log.txt")
    pinned = all("==" in line for line in (ROOT / "requirements.txt").read_text().split() if line.strip())
    check(pinned, "E4 requirements.txt pins every version")

    shutil.rmtree(temp, ignore_errors=True)
    width = max(len(item) for _, item, _ in results)
    for ok, item, detail in results:
        print(f"{'PASS' if ok else 'FAIL'}  {item:<{width}}  {detail}")
    failed = [item for ok, item, _ in results if not ok]
    print(f"\n{len(results) - len(failed)}/{len(results)} passed -> {'GO' if not failed else 'NO-GO: ' + '; '.join(failed)}")
    return 0 if not failed else 1


if __name__ == "__main__":
    sys.exit(main())
