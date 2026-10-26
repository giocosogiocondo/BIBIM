"""OwnerInterpretation: one cohesive extraction responsibility; state supplied by the facade."""

from __future__ import annotations
from datetime import datetime, timezone


class OwnerInterpretation:
    def owner_fields(self, iid):
        owner = self.db.nodes[iid]
        result = {}
        for attr, prefix in (("OwningUser", "Owner"), ("LastModifyingUser", "LastModifiedBy")):
            actor = self.first(iid, attr)
            if actor is None:
                continue
            person = self.first(actor, "ThePerson")
            organization = self.first(actor, "TheOrganization")
            if person:
                n = self.db.nodes[person]
                given = n.get("GivenName") or ""
                family = n.get("FamilyName") or ""
                result[prefix + "_Name"] = (
                    " ".join(str(x) for x in (given, family) if x)
                    or n.get("Identification")
                    or n.get("Id")
                )
                result.update(self.flattened(prefix + "_Person", self.resolved_resource(person)))
            if organization:
                result[prefix + "_Organization"] = self.db.nodes[organization].get("Name")
        for attr, prefix in (
            ("OwningApplication", "Owner_Application"),
            ("LastModifyingApplication", "LastModified_Application"),
        ):
            application = self.first(iid, attr)
            if application:
                result.update(self.flattened(prefix, self.resolved_resource(application)))
        for attr in ("State", "ChangeAction"):
            result["Owner_" + attr] = owner.get(attr)
        for attr in ("CreationDate", "LastModifiedDate"):
            value = owner.get(attr)
            if value is not None:
                try:
                    result["Owner_" + attr + "_UTC"] = datetime.fromtimestamp(
                        value, timezone.utc
                    ).isoformat()
                except (TypeError, ValueError, OverflowError, OSError):
                    self.db.note("invalid_owner_timestamp", iid, attr)
        return result
