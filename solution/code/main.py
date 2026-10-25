"""Command-line entry point for the Ghost Data deletion-audit agent.

    python code/main.py index-report   # how many observations each evidence file yields
    python code/main.py link-check     # linked resources vs the public sample answers
    python code/main.py decision-check # per-resource policy decisions vs the sample answers
    python code/main.py run            # audit all 518 requests, write output.csv
    python code/main.py eval           # score samples + custom cases, write evaluation_report.md
    python code/main.py validate       # check output.csv against the contract
    python code/main.py explain case_11

Add --no-llm to any command to run fully offline from saved model answers.
Add --fresh to ignore saved answers and ask Gemini again, as a real run would.
"""

from __future__ import annotations

import argparse
import csv
import json
import sys
from types import SimpleNamespace

from config import Settings


def build_argument_parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(description="Audit deletion requests against evidence.")
    parser.add_argument(
        "--no-llm",
        action="store_true",
        help="never call Gemini; use cached model output and keyword rules only",
    )
    parser.add_argument(
        "--fresh",
        action="store_true",
        help="ignore saved model answers and call Gemini again (new answers are still saved)",
    )
    commands = parser.add_subparsers(dest="command", required=True)
    commands.add_parser("index-report", help="load all evidence and report observations per file")
    commands.add_parser("link-check", help="compare linked resources with the public sample answers")
    commands.add_parser("decision-check", help="compare per-resource policy decisions with the sample answers")
    commands.add_parser("run", help="audit every request and write output.csv")
    commands.add_parser("eval", help="score the public samples and custom cases; write evaluation_report.md")
    commands.add_parser("validate", help="check output.csv against the output contract")
    explain = commands.add_parser("explain", help="print the decision trail for one case")
    explain.add_argument("case_id")
    return parser


def print_index_report(settings: Settings) -> int:
    index = build_components(settings).index
    for report in index.load_reports:
        marker = "!!" if report.needs_attention else "  "
        detail = f" ({report.error})" if report.error else ""
        print(
            f"{marker} {report.file_id:<12} {report.loader_name or '-':<11} "
            f"raw={report.raw_record_count:<4} observations={report.observation_count:<4} "
            f"{report.file_path}{detail}"
        )
    flagged = sum(report.needs_attention for report in index.load_reports)
    print(f"\n{len(index.observations)} observations from {len(index.load_reports)} files; {flagged} flagged")
    return 0


def print_link_check(settings: Settings) -> int:
    """Compare linked resources with the resources named in sample_output.csv.

    The sample answers are used here only to measure the linker, never as
    input to it.
    """
    components = build_components(settings)
    repository, index = components.repository, components.index
    identity_resolver, linker = components.identity_resolver, components.linker
    workflow_matcher = components.workflow_matcher
    expected_by_case = {
        case_id: {step["resource_id"] for step in steps}
        for case_id, (_, steps) in load_sample_answers(settings).items()
    }

    # Some expected resources appear in no participant-facing file at all
    # (account_004, audit_record_6645): no linker can find those, so they are
    # counted apart from misses the linker could still fix.
    traceable = {o.resource_id for o in index.observations} | {a.resource_id for a in repository.activities}

    def why_missed(resource_id: str, case_id: str) -> str:
        if resource_id not in traceable:
            return "no trace in any file"
        if case_id in workflow_matcher.unresolved_candidates.get(resource_id, set()):
            return "tied with " + ", ".join(sorted(workflow_matcher.unresolved_candidates[resource_id] - {case_id}))
        return "not linked"

    found_total = missed_total = untraceable_total = extra_total = paired_total = 0
    for request in repository.sample_requests:
        profile = identity_resolver.resolve(request.email, request.audit_at)
        links = linker.link(profile, request) if profile else []
        linked = {link.resource_id for link in links}
        expected = expected_by_case[request.case_id]
        missed, extra = expected - linked, linked - expected
        found_total += len(expected & linked)
        missed_total += len(missed)
        untraceable_total += sum(resource_id not in traceable for resource_id in missed)
        extra_total += len(extra)
        paired_total += len(linked & workflow_matcher.paired_resources)
        if missed or extra:
            reasons = {link.resource_id: sorted(r.value for r in link.reasons) for link in links}
            missed_detail = {resource_id: why_missed(resource_id, request.case_id) for resource_id in sorted(missed)}
            extra_detail = {resource_id: reasons[resource_id] for resource_id in sorted(extra)}
            print(f"{request.case_id}: missed {missed_detail} extra {extra_detail}")
    print(
        f"\nexpected resources found={found_total} missed={missed_total} "
        f"(of which {untraceable_total} have no trace in any file) extra={extra_total}"
    )
    print(
        f"links made by tie-pairing in the samples={paired_total}; across all requests "
        f"paired={len(workflow_matcher.paired_resources)} still ambiguous={len(workflow_matcher.unresolved_candidates)}"
    )
    return 0


def build_components(settings: Settings, sample_overrides: dict | None = None) -> SimpleNamespace:
    """Wire the pipeline stages together once, in dependency order.

    sample_overrides maps a public sample case_id to a request that takes its
    place (the custom evaluation cases).
    """
    # Imported here so `--help` works before any dependency is installed.
    from core.episodes import RequestEpisodes
    from core.identity import IdentityResolver
    from core.index import EvidenceIndex
    from core.auditor import CaseAuditor
    from core.intent import EnsembleIntentClassifier, GeminiIntentClassifier, KeywordIntentClassifier
    from core.linking import ResourceLinker
    from core.planner import StepPlanner
    from core.policy import PolicyEngine
    from core.state import StateReconstructor
    from core.workflow_matching import WorkflowWindowMatcher
    from llm.cascade import EscalationPolicy
    from llm.gemini_client import GeminiClient, UsageTracker
    from loaders import default_loaders
    from loaders.image_loader import ImageLoader
    from loaders.pdf_loader import PdfLoader
    from repository import DatasetRepository

    repository = DatasetRepository(settings.dataset_dir)
    if sample_overrides:
        repository.sample_requests = [sample_overrides.get(r.case_id, r) for r in repository.sample_requests]
    usage = UsageTracker()
    gemini = GeminiClient(settings, usage)
    aliases = repository.catalog_aliases()
    screenshot_escalation = EscalationPolicy()
    loaders = [loader for loader in default_loaders() if not isinstance(loader, PdfLoader)]
    loaders += [PdfLoader(aliases), ImageLoader(gemini, aliases, screenshot_escalation)]
    index = EvidenceIndex.build(repository, loaders)
    identity_resolver = IdentityResolver(repository)
    all_requests = repository.sample_requests + repository.evaluation_requests
    intent_classifier = EnsembleIntentClassifier(KeywordIntentClassifier(), GeminiIntentClassifier(gemini))
    intent_classifier.prepare(all_requests)
    deletion_requests = [r for r in all_requests if intent_classifier.classify(r).authorizes_deletion]
    episodes = RequestEpisodes(identity_resolver, all_requests)
    workflow_matcher = WorkflowWindowMatcher(repository, index, identity_resolver, deletion_requests, episodes)
    linker = ResourceLinker(repository, index, workflow_matcher, episodes)
    state_reconstructor = StateReconstructor(repository, index, episodes)
    policy_engine = PolicyEngine(repository)
    usage.cascade_notes = screenshot_escalation.notes("Screenshots") + [
        f"Intent second look {line}" for line in intent_classifier.second_looks
    ]
    return SimpleNamespace(
        repository=repository,
        usage=usage,
        screenshot_escalation=screenshot_escalation,
        index=index,
        identity_resolver=identity_resolver,
        intent_classifier=intent_classifier,
        episodes=episodes,
        workflow_matcher=workflow_matcher,
        linker=linker,
        state_reconstructor=state_reconstructor,
        policy_engine=policy_engine,
        auditor=CaseAuditor(
            intent_classifier, identity_resolver, linker, state_reconstructor, policy_engine, StepPlanner()
        ),
    )


def load_sample_answers(settings: Settings) -> dict[str, tuple[str, list[dict]]]:
    """case_id -> (status, steps) from sample_output.csv, for measuring only."""
    with (settings.dataset_dir / "sample_output.csv").open(encoding="utf-8") as handle:
        return {
            row["case_id"]: (row["deletion_status"], json.loads(row["steps_json"]))
            for row in csv.DictReader(handle)
        }


def print_decision_check(settings: Settings) -> int:
    """Compare per-resource policy decisions with the public sample answers.

    For every expected resource the check compares the finding (or its
    absence), the verification operation, and the evidence a COMPLETE case
    or a finding must cite. Status is INCOMPLETE as soon as one decision has
    a finding. The sample answers are used only to measure.
    """
    components = build_components(settings)
    answers = load_sample_answers(settings)
    status_hits = finding_hits = verification_hits = evidence_hits = evidence_total = compared = 0
    for request in components.repository.sample_requests:
        expected_status, expected_steps = answers[request.case_id]
        profile = components.identity_resolver.resolve(request.email, request.audit_at)
        links = components.linker.link(profile, request) if profile else []
        decisions = {
            link.resource_id: components.policy_engine.evaluate(
                components.state_reconstructor.state_at(link, request)
            )
            for link in links
        }
        predicted_status = "INCOMPLETE" if any(not d.is_compliant for d in decisions.values()) else "COMPLETE"
        status_hits += predicted_status == expected_status
        problems = []
        if predicted_status != expected_status:
            problems.append(f"status {predicted_status} (expected {expected_status})")
        for resource_id in sorted({step["resource_id"] for step in expected_steps}):
            steps = [step for step in expected_steps if step["resource_id"] == resource_id]
            expected_finding = next((s["operation"] for s in steps if s["phase"] == "FINDING"), None)
            expected_check = next((s for s in steps if s["phase"] == "VERIFICATION"), None)
            decision = decisions.get(resource_id)
            if decision is None:
                continue  # a linking miss; link-check reports those
            compared += 1
            got_finding = decision.finding.value if decision.finding else None
            finding_hits += got_finding == expected_finding
            got_check = decision.verification.value
            verification_hits += expected_check is not None and got_check == expected_check["operation"]
            cited_step = next((s for s in steps if s["phase"] == "FINDING"), None) or (
                expected_check if expected_status == "COMPLETE" else None
            )
            if cited_step:
                evidence_total += 1
                evidence_hits += decision.state.evidence_id == cited_step["evidence_id"]
            if (
                got_finding != expected_finding
                or (expected_check and got_check != expected_check["operation"])
                or (cited_step and decision.state.evidence_id != cited_step["evidence_id"])
            ):
                state = decision.state
                problems.append(
                    f"{resource_id}: got {got_finding}/{got_check}/{state.evidence_id} "
                    f"[{state.condition.value}, {decision.treatment.value}] expected "
                    f"{expected_finding}/{expected_check['operation'] if expected_check else None}/"
                    f"{cited_step['evidence_id'] if cited_step else '-'}"
                )
        if problems:
            print(f"{request.case_id}: " + "; ".join(problems))
    total = len(components.repository.sample_requests)
    print(
        f"\nstatus {status_hits}/{total}; on {compared} linked expected resources: "
        f"finding {finding_hits}/{compared}, verification {verification_hits}/{compared}, "
        f"cited evidence {evidence_hits}/{evidence_total}"
    )
    return 0


def run_audit(settings: Settings) -> int:
    """Audit every evaluation request and write output.csv at the solution root."""
    from output.writer import OutputWriter

    components = build_components(settings)
    results = [components.auditor.audit(request)[0] for request in components.repository.evaluation_requests]
    OutputWriter().write(results, settings.output_csv_path)
    counts = {status: sum(r.status.value == status for r in results) for status in ("COMPLETE", "INCOMPLETE", "NOT_APPLICABLE")}
    print(f"wrote {len(results)} rows to {settings.output_csv_path}: {counts}")
    for note in components.usage.cascade_notes:
        print(f"  {note}")
    usage_path = settings.evaluation_dir / "usage_report.md"
    usage_path.write_text(
        components.usage.to_markdown(settings, len(components.repository.evaluation_requests)), encoding="utf-8"
    )
    print(f"wrote {usage_path}")
    return validate_output(settings, components)


def validator_issues(settings: Settings, components: SimpleNamespace) -> list[str]:
    from output.validator import OutputValidator

    repository = components.repository
    citable_ids = (
        {f.file_id for f in repository.evidence_files}
        | {a.activity_id for a in repository.activities}
        | {c.audit_id for c in repository.identity_changes}
    )
    validator = OutputValidator(
        citable_ids,
        {f.file_id: f.captured_at for f in repository.evidence_files},
        {r.case_id: r.audit_at for r in repository.evaluation_requests},
    )
    return validator.validate(settings.output_csv_path, repository.evaluation_requests)


def validate_output(settings: Settings, components: SimpleNamespace | None = None) -> int:
    issues = validator_issues(settings, components or build_components(settings))
    for issue in issues[:40]:
        print("  !!", issue)
    print(f"validator: {len(issues)} issue(s)")
    return 1 if issues else 0


def evaluate_samples(settings: Settings) -> int:
    """Score the 32 public samples and the custom cases, then write evaluation_report.md."""
    from evaluation.evaluate import Evaluator
    from output.writer import OutputWriter, serialize_steps

    answers = load_sample_answers(settings)
    components = build_components(settings)
    results = [components.auditor.audit(request)[0] for request in components.repository.sample_requests]
    OutputWriter().write(results, settings.evaluation_dir / "sample_predictions.csv")
    predicted = {r.case_id: (r.status.value, json.loads(serialize_steps(r.steps))) for r in results}
    evaluator = Evaluator()
    metrics = evaluator.compare(predicted, answers)
    sample_table = evaluator.to_markdown(metrics, "Public samples (32 cases)")
    print(sample_table)
    print("cases not fully matching:", ", ".join(metrics.failing_cases) or "none")
    misses = describe_sample_misses(components, answers, predicted, metrics.failing_cases)

    custom_rows = run_custom_cases(settings, answers)
    for row in custom_rows:
        print(f"{row['case_id']}: expected {row['expected_status']}, got {row['got_status']}, "
              f"steps match {row['steps_match']}, intent {row['intent']}")

    report = render_evaluation_report(settings, components, sample_table, misses, custom_rows)
    report_path = settings.evaluation_dir / "evaluation_report.md"
    report_path.write_text(report, encoding="utf-8")
    print(f"wrote {report_path}")
    return 0


def describe_sample_misses(components, answers, predicted, failing_cases: list[str]) -> list[str]:
    """One line per imperfect sample: which expected records were missed or added, and why."""
    traceable = {o.resource_id for o in components.index.observations} | {
        a.resource_id for a in components.repository.activities
    }
    lines = []
    for case_id in failing_cases:
        expected = {step["resource_id"] for step in answers[case_id][1]}
        got = {step["resource_id"] for step in predicted[case_id][1]}
        missed = [
            f"{resource_id} ({'appears in no dataset file' if resource_id not in traceable else 'not linked'})"
            for resource_id in sorted(expected - got)
        ]
        extra = sorted(got - expected)
        detail = "; ".join(filter(None, [
            f"missed {', '.join(missed)}" if missed else "",
            f"extra {', '.join(extra)}" if extra else "",
            "same records, different steps or order" if not missed and not extra else "",
        ]))
        lines.append(f"- {case_id}: {detail}")
    return lines


def run_custom_cases(settings: Settings, answers: dict) -> list[dict]:
    """Audit each custom case in place of the sample it is based on, and compare."""
    from dataclasses import replace

    from evaluation.custom_cases import CUSTOM_CASES
    from output.writer import serialize_steps
    from repository import DatasetRepository

    samples = {r.case_id: r for r in DatasetRepository(settings.dataset_dir).sample_requests}
    overrides = {
        case.based_on: replace(
            samples[case.based_on],
            case_id=case.case_id,
            request_text=case.request_text or samples[case.based_on].request_text,
            email=case.email or samples[case.based_on].email,
        )
        for case in CUSTOM_CASES
    }
    components = build_components(settings, sample_overrides=overrides)
    rows = []
    for case in CUSTOM_CASES:
        request = overrides[case.based_on]
        result, trail = components.auditor.audit(request)
        got_steps = json.loads(serialize_steps(result.steps))
        expected_status, expected_steps = (
            (case.expected_status, []) if case.expected_status else answers[case.based_on]
        )
        steps_match = got_steps == expected_steps
        rows.append({
            "case_id": case.case_id,
            "based_on": case.based_on,
            "tests": case.tests,
            "email": request.email,
            "request_text": request.request_text,
            "expected_status": expected_status,
            "expected_step_count": len(expected_steps),
            "got_status": result.status.value,
            "got_step_count": len(got_steps),
            "steps_match": steps_match,
            "intent": f"{trail.intent.intent.value} ({trail.intent.matched_cue})",
            "passed": result.status.value == expected_status and steps_match,
        })
    return rows


def render_evaluation_report(settings: Settings, components, sample_table: str, misses: list[str], custom_rows: list[dict]) -> str:
    counts: dict[str, int] = {}
    if settings.output_csv_path.exists():
        with settings.output_csv_path.open(encoding="utf-8") as handle:
            for row in csv.DictReader(handle):
                counts[row["deletion_status"]] = counts.get(row["deletion_status"], 0) + 1
    issues = validator_issues(settings, components) if counts else ["output.csv not written yet"]
    passed = sum(row["passed"] for row in custom_rows)
    lines = [
        "# Evaluation report",
        "",
        "Written by `python code/main.py eval`. Sections 1-3 are measured by running the agent;",
        "section 4 is our own account of what went wrong and what we changed.",
        "",
        "## 1. Public samples: status, step, field and sequence accuracy",
        "",
        sample_table.replace("### Public samples (32 cases)", "The 32 cases in `dataset/sample_output.csv`.").strip(),
        "",
        "Samples that are not an exact match, and why:",
        "",
        *(misses or ["- none"]),
        "",
        f"## 2. Custom evaluation cases ({passed} of {len(custom_rows)} passed)",
        "",
        "Each case changes one thing about a public sample the agent already answers exactly, so the",
        "expected answer is known (code/evaluation/custom_cases.py).",
        "",
        "| Case | Based on | Expected | Got | Steps identical | Intent decided by | Result |",
        "| --- | --- | --- | --- | --- | --- | --- |",
    ]
    for row in custom_rows:
        lines.append(
            f"| {row['case_id']} | {row['based_on']} | {row['expected_status']} ({row['expected_step_count']} steps) | "
            f"{row['got_status']} ({row['got_step_count']} steps) | {'yes' if row['steps_match'] else 'no'} | "
            f"{row['intent']} | {'pass' if row['passed'] else 'FAIL'} |"
        )
    lines.append("")
    for row in custom_rows:
        lines += [
            f"- **{row['case_id']}**: {row['tests']}",
            f"  - email: `{row['email']}`",
            f"  - request text: \"{row['request_text']}\"",
        ]
    status_summary = ", ".join(f"{status} {count}" for status, count in sorted(counts.items()))
    lines += [
        "",
        "## 3. Full dataset run",
        "",
        f"- output.csv: {sum(counts.values())} rows ({status_summary or 'none'})",
        f"- Contract validator (`python code/main.py validate`): {len(issues)} issue(s)",
        "- The hidden answers are not available, so the full run is checked against the contract, not scored.",
        "",
        "## 4. Failures found and improvements made",
        "",
        (settings.evaluation_dir / "failures_and_improvements.md").read_text(encoding="utf-8").strip(),
        "",
    ]
    return "\n".join(lines)


def explain_case(settings: Settings, case_id: str) -> int:
    """Print every intermediate result for one case."""
    components = build_components(settings)
    repository = components.repository
    request = next((r for r in repository.sample_requests + repository.evaluation_requests if r.case_id == case_id), None)
    if request is None:
        print(f"unknown case {case_id}")
        return 1
    result, trail = components.auditor.audit(request)
    print(f"{case_id}  email={request.email}  reported={request.reported_status}  audit_at={request.audit_at}")
    print(f"intent: {trail.intent.intent.value} (cue: {trail.intent.matched_cue!r})")
    if trail.profile:
        print(f"subject: {trail.profile.user_id}; emails={sorted(trail.profile.emails)}")
    episode_start = components.episodes.starts_after(request)
    if episode_start:
        print(f"earlier audit for this person ended {episode_start}; older evidence ignored")
    links_by_id = {link.resource_id: link for link in trail.links}
    for decision in trail.decisions:
        state, link = decision.state, links_by_id[decision.state.resource_id]
        paired = " (paired guess)" if link.resource_id in components.workflow_matcher.paired_resources else ""
        print(
            f"  {state.resource_id:<20} linked by {sorted(r.value for r in link.reasons)}{paired}\n"
            f"  {'':<20} state {state.condition.value} via {state.evidence_id}; "
            f"treatment {decision.treatment.value}; finding {decision.finding.value if decision.finding else '-'}"
        )
    print(f"status: {result.status.value}")
    for step in result.steps:
        print(f"  {step.step_no:>2} {step.phase.value:<12} {step.operation.value:<26} {step.system_id}/{step.resource_id} {step.evidence_id}")
    return 0


def main(argv: list[str] | None = None) -> int:
    arguments = build_argument_parser().parse_args(argv)
    settings = Settings.from_environment(use_llm=not arguments.no_llm, read_cache=not arguments.fresh)
    if arguments.command == "index-report":
        return print_index_report(settings)
    if arguments.command == "link-check":
        return print_link_check(settings)
    if arguments.command == "decision-check":
        return print_decision_check(settings)
    if arguments.command == "run":
        return run_audit(settings)
    if arguments.command == "validate":
        return validate_output(settings)
    if arguments.command == "eval":
        return evaluate_samples(settings)
    if arguments.command == "explain":
        return explain_case(settings, arguments.case_id)
    print(f"'{arguments.command}' is not built yet (see the roadmap in docs/architecture_plan.html).")
    return 1


if __name__ == "__main__":
    sys.exit(main())
