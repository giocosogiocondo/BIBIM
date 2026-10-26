"""LegacyRowProjection: one cohesive extraction responsibility; state supplied by the facade."""

from __future__ import annotations
from ..reader import unpack


class LegacyRowProjection:
    def query_row(self, iid):
        """An object core plus property candidates; all details have separate views."""
        row = self.direct(iid)
        row.update(self.flat_properties(iid, record_details=True))
        row.update(self.relation_summary(iid))
        storeys = self.storeys(iid)
        if len(storeys) == 1:
            values = self.direct(next(iter(storeys)))
            for name in ("ifc_id", "Name", "Elevation", "Elevation_Unit"):
                if name in values:
                    row["Storey_" + name] = values[name]
        return row

    def row(self, iid):
        return self._row(iid)

    def row_with_provenance(self, iid):
        """Return values and proof tokens without exporting provenance columns.

        Missing tokens mean that an expansion's source/meaning was not proven;
        consumers must retain those columns rather than deduplicate by value.
        """
        provenance = {}
        return self._row(iid, provenance), provenance

    def _row(self, iid, provenance=None):
        out = self.direct(iid)
        if provenance is not None:
            provenance.update(self._direct_provenance(iid, out, ("self",)))

        def update(values, sources=None):
            out.update(values)
            if provenance is not None:
                # Later expansion can overwrite an earlier column. Its prior
                # source must never survive an unproven replacement.
                for column in values:
                    provenance.pop(column, None)
                provenance.update(sources or {})

        property_sources = {} if provenance is not None else None
        update(
            self.flat_properties(iid, record_details=True, provenance=property_sources),
            property_sources,
        )
        relation_sources = {} if provenance is not None else None
        update(self.relations(iid, provenance=relation_sources), relation_sources)
        storeys = self.storeys(iid)
        if len(storeys) == 1:
            storey = next(iter(storeys))
            values = self.direct(storey)
            sources = (
                self._direct_provenance(storey, values, ("storey",))
                if provenance is not None
                else {}
            )
            update(
                {"Storey_" + k: v for k, v in values.items()},
                {"Storey_" + k: token for k, token in sources.items()},
            )
        # Direct referenced resources are also available at named boundaries.
        for attr, ids in self.db.edges.get(iid, {}).items():
            if attr == "HasPropertySets" or not ids:
                continue
            objects = [self.resource(target) for target in ids]
            raw = unpack(self.db.nodes[iid].get(attr))
            if isinstance(raw, list):
                update({attr: objects})
            elif len(objects) == 1:
                # Resource traversals have cycle/depth boundaries and owned
                # inverses; leave their provenance unproven for conservative
                # deduplication rather than reconstruct a guessed path.
                update({attr + "_" + key: value for key, value in objects[0].items()})
        return out
