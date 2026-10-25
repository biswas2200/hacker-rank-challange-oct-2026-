"""RequestEpisodes: where one person's audit ends and their next one begins.

Some people file more than one request (priya.raman: case_61 audited
2026-02-14, then case_07 audited 2026-09-12). Evidence gathered for the
earlier audit belongs to that audit. In the public samples, the records found
only by those earlier lookups (object_19, tombstone_17, account_6523) are not
part of the later answer. So every request looks only at evidence and links
that appear after the same person's previous audit_at.
"""

from __future__ import annotations

from collections import defaultdict
from datetime import datetime

from core.identity import IdentityResolver
from models import DeletionRequest


class RequestEpisodes:
    def __init__(self, identity_resolver: IdentityResolver, all_requests: list[DeletionRequest]):
        # Every request counts as an episode boundary, deletion or not: an
        # access request's lookups were also gathered for that earlier audit.
        audits_by_user: dict[str, list[datetime]] = defaultdict(list)
        self._user_by_case: dict[str, str] = {}
        for request in all_requests:
            profile = identity_resolver.resolve(request.email, request.audit_at)
            if profile is None:
                continue
            self._user_by_case[request.case_id] = profile.user_id
            audits_by_user[profile.user_id].append(request.audit_at)
        self._audits_by_user = {user: sorted(times) for user, times in audits_by_user.items()}

    def starts_after(self, request: DeletionRequest) -> datetime | None:
        """The previous audit's cutoff for this person, or None for a first request.

        Only audits that finished before this request was submitted count.
        An audit that overlaps this request is part of the same story.
        """
        user_id = self._user_by_case.get(request.case_id)
        earlier = [t for t in self._audits_by_user.get(user_id, []) if t < request.requested_at]
        return earlier[-1] if earlier else None
