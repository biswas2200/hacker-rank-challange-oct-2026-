"""IdentityResolver: who is the person behind a request, under every name.

Old systems keep whatever identifier was current when they copied the data,
so a deletion audit has to search for past emails and phones too, not just
today's. The request email itself may already be a past one (AGENTS.md 6.2).
"""

from __future__ import annotations

import hashlib
from dataclasses import dataclass, field
from datetime import datetime

from repository import DatasetRepository, normalize_email


def email_hash(email: str) -> str:
    """The dataset's email_hash: SHA-256 of the lowercased address.

    Verified against user_identities.csv. Hashing past emails matters because
    some systems store only the hash (case_12's analytics_9914 is linked
    solely through the hash of an email that was replaced months earlier).
    """
    return hashlib.sha256(normalize_email(email).encode("utf-8")).hexdigest()


@dataclass
class SubjectProfile:
    user_id: str
    emails: set[str] = field(default_factory=set)
    phones: set[str] = field(default_factory=set)
    names: set[str] = field(default_factory=set)
    email_hashes: set[str] = field(default_factory=set)
    # user_audit_log rows that revealed a past identifier; they are the
    # participant-visible proof that an old email belonged to this person.
    identity_change_audit_ids: list[str] = field(default_factory=list)

    @property
    def searchable_identifiers(self) -> set[str]:
        # Names are left out: many people share a name, and the dataset
        # plants near-namesakes (Jonas Berg / Jonas Berglund) as decoys.
        return {self.user_id} | self.emails | self.phones | self.email_hashes


class IdentityResolver:
    def __init__(self, repository: DatasetRepository):
        self.repository = repository

    def resolve(self, request_email: str, cutoff: datetime) -> SubjectProfile | None:
        user_id = self._find_user_id(normalize_email(request_email))
        if user_id is None:
            return None
        current = self.repository.identity_by_user_id[user_id]
        profile = SubjectProfile(
            user_id=user_id,
            emails={current.email},
            phones={current.phone},
            names={current.name},
            email_hashes={current.email_hash},
        )
        for change in self.repository.identity_changes_by_user_id.get(user_id, []):
            # A change made after the cutoff did not exist yet at audit time,
            # so its values cannot have been copied anywhere we must check.
            if change.event_time > cutoff:
                continue
            profile.identity_change_audit_ids.append(change.audit_id)
            past_and_new_values = [v for v in (change.old_value, change.new_value) if v]
            if change.field_name == "email":
                profile.emails.update(normalize_email(v) for v in past_and_new_values)
            elif change.field_name == "phone":
                profile.phones.update(past_and_new_values)
            elif change.field_name == "email_hash":
                profile.email_hashes.update(v.lower() for v in past_and_new_values)
        profile.email_hashes.update(email_hash(email) for email in profile.emails)
        return profile

    def _find_user_id(self, email: str) -> str | None:
        current = self.repository.identity_by_current_email.get(email)
        if current:
            return current.user_id
        # The request may use an address the person has since replaced.
        for user_id, changes in self.repository.identity_changes_by_user_id.items():
            for change in changes:
                if change.field_name == "email" and normalize_email(change.old_value) == email:
                    return user_id
        return None
