"""CaseAuditor: runs every stage for one request and returns its CaseResult.

intent -> identity -> linked resources -> state at audit_at -> policy
decision -> ordered steps. It also keeps the intermediate results, so
`main.py explain <case_id>` can print why the answer came out as it did.
"""

from __future__ import annotations

from dataclasses import dataclass, field

from core.identity import IdentityResolver, SubjectProfile
from core.intent import IntentResult, KeywordIntentClassifier
from core.linking import ResourceLink, ResourceLinker
from core.planner import StepPlanner
from core.policy import Decision, PolicyEngine
from core.state import StateReconstructor
from models import CaseResult, DeletionRequest, DeletionStatus


@dataclass
class AuditTrail:
    """Everything the auditor worked out for one case, for `explain`."""

    intent: IntentResult #check if the person ask for deletion
    profile: SubjectProfile | None = None #check the person details(old mail, mail, phone number, name, email hash)
    links: list[ResourceLink] = field(default_factory=list)
    decisions: list[Decision] = field(default_factory=list)


class CaseAuditor:
    def __init__(
        self,
        intent_classifier: KeywordIntentClassifier,
        identity_resolver: IdentityResolver,
        linker: ResourceLinker,
        state_reconstructor: StateReconstructor,
        policy_engine: PolicyEngine,
        planner: StepPlanner,
    ):
        self.intent_classifier = intent_classifier
        self.identity_resolver = identity_resolver
        self.linker = linker
        self.state_reconstructor = state_reconstructor
        self.policy_engine = policy_engine
        self.planner = planner

    def audit(self, request: DeletionRequest) -> tuple[CaseResult, AuditTrail]:
        trail = AuditTrail(intent=self.intent_classifier.classify(request))
        if not trail.intent.authorizes_deletion:
            return CaseResult(request.case_id, DeletionStatus.NOT_APPLICABLE, []), trail

        trail.profile = self.identity_resolver.resolve(request.email, request.audit_at)
        if trail.profile is not None:
            trail.links = self.linker.link(trail.profile, request)
        trail.decisions = [
            self.policy_engine.evaluate(self.state_reconstructor.state_at(link, request)) for link in trail.links
        ]
        # A deletion request whose email matches nobody has nothing left to
        # find; no eval request hits this, but the planner then returns
        # COMPLETE with no steps instead of failing.
        status, steps = self.planner.plan(trail.decisions)
        return CaseResult(request.case_id, status, steps), trail
