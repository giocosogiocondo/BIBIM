"""EntityValidation: independent validation responsibility, sharing the Validator context."""

from __future__ import annotations
import re
from collections import Counter, defaultdict
import ifcopenshell
import ifcopenshell.util.placement
import ifcopenshell.util.unit
import numpy as np
from .values import collapse, canonical


class EntityValidation:
    def run(self, requested):
        groups = defaultdict(list)
        for obj in self.model.by_type("IfcObjectDefinition"):
            if not obj.is_a("IfcTypeObject"):
                groups[canonical(obj, self.schema)].append(obj)
        entities = requested or sorted(groups)
        summaries = []
        for entity in entities:
            if not entity.startswith("Ifc"):
                entity = "Ifc" + entity
            declaration = self.schema.declaration_by_name(entity)
            if declaration is None or not hasattr(declaration, "supertype"):
                raise ValueError(f"Unknown entity: {entity}")
            objects = groups.get(entity, [])
            view = "View_" + entity.removeprefix("Ifc")
            basic, counts, columns = self.rows(view, entity)
            custom_view = view + "CustomPset"
            custom, custom_counts, custom_columns = self.rows(custom_view, entity)
            expected_ids = {o.id() for o in objects}
            missing, extra = sorted(expected_ids - basic.keys()), sorted(
                basic.keys() - expected_ids
            )
            duplicates = {str(i): n for i, n in counts.items() if n > 1}
            stats = defaultdict(Counter)
            for iid in missing:
                self.detail(entity, iid, "Object", view, "missing_object")
            for iid in extra:
                self.detail(entity, iid, "Object", view, "extra_object")
            for iid, n in counts.items():
                if n > 1:
                    self.detail(entity, iid, "Object", view, "duplicate_object", 1, n)
            for iid, n in custom_counts.items():
                if iid not in expected_ids or n > 1:
                    self.detail(
                        entity,
                        iid,
                        "Object",
                        custom_view,
                        "extra_or_duplicate_custom_object",
                        1,
                        n,
                    )
            for obj in objects:
                self._compare_object(entity, obj, view, basic, custom_view, custom, stats)
            totals = Counter()
            for s in stats.values():
                totals.update(s)
            summaries.append(
                dict(
                    entity=entity,
                    source_objects=len(objects),
                    view_rows=sum(counts.values()),
                    matched_objects=len(expected_ids & basic.keys()),
                    missing_ids=missing,
                    extra_ids=extra,
                    duplicate_ids=duplicates,
                    custom_view_rows=sum(custom_counts.values()),
                    categories=dict(stats),
                    values=dict(totals),
                    value_agreement_percent=(
                        100 * totals["matched"] / totals["expected"]
                        if totals["expected"]
                        else None
                    ),
                    unchecked_output_columns={
                        view: sorted(columns - self.checked_columns[view] - {"ifc_id"}),
                        custom_view: sorted(
                            custom_columns - self.checked_columns[custom_view] - {"ifc_id"}
                        ),
                    },
                )
            )
            print(
                f'{entity}: objects {len(expected_ids & basic.keys())}/{len(objects)}, values {totals["matched"]}/{totals["expected"]}, unverified {totals["unverified"]}',
                flush=True,
            )
        return summaries

    def _compare_object(self, entity, obj, view, basic, custom_view, custom, stats):
        rows = [(view, basic.get(obj.id(), {}))]
        if obj.id() in custom:
            rows.append((custom_view, custom[obj.id()]))
        props = self._check_attributes(entity, obj, rows, stats)
        self._check_properties(entity, obj, props, rows, stats)
        self._check_placement(entity, obj, rows, stats)

    def _check_attributes(self, entity, obj, rows, stats):
        expected = [("Attribute", "ifc_class", obj.is_a(), None, None)]
        expected.extend(("Attribute", k, v, u, error) for k, v, u, error in self.scalars(obj))
        props, types = self.properties(obj)
        for t in types:
            prefix = t.is_a().removeprefix("Ifc")
            if prefix in ("DoorStyle", "WindowStyle"):
                prefix = prefix[:-5] + "style"
            expected.extend(
                ("Type", k, v, u, error) for k, v, u, error in self.scalars(t, prefix + "_")
            )
        buckets = defaultdict(list)
        for category, key, value, unit, error in expected:
            if error:
                stats[category]["unverified"] += 1
                self.detail(entity, obj.id(), category, key, "unverified", actual=error)
            else:
                buckets[(category, key, unit)].append(value)
        for (category, key, unit), values in buckets.items():
            self.check(
                entity,
                obj.id(),
                category,
                [key],
                collapse(values),
                rows,
                stats[category],
                unit,
            )
        return props

    def _check_properties(self, entity, obj, props, rows, stats):
        for (set_name, path), records in props.items():
            category = records[0][0]
            if any(r[3] for r in records):
                stats[category]["unverified"] += 1
                self.detail(
                    entity,
                    obj.id(),
                    category,
                    set_name + "_" + "_".join(path),
                    "unverified",
                    actual=[r[3] for r in records if r[3]],
                )
                continue
            pairs = []
            for _, value, unit, _ in records:
                if (value, unit) not in pairs:
                    pairs.append((value, unit))
            value = pairs[0][0] if len(pairs) == 1 else [v for v, _ in pairs]
            units = {u for _, u in pairs}
            unit = next(iter(units)) if len(units) == 1 else None
            if len(units) > 1:
                stats["Unit"]["unverified"] += 1
                self.detail(
                    entity,
                    obj.id(),
                    "Unit",
                    set_name + "_" + "_".join(path),
                    "unverified",
                    actual="mixed measurement units; value comparison only",
                )
            suffix = "_" + "_".join(path)
            names = [
                set_name + suffix,
                re.sub(r"^(?:Pset|Qto)_", "", set_name, flags=re.I) + suffix,
            ]
            self.check(entity, obj.id(), category, names, value, rows, stats[category], unit)

    def _check_placement(self, entity, obj, rows, stats):
        placement = getattr(obj, "ObjectPlacement", None)
        if placement is not None and placement.is_a("IfcLocalPlacement"):
            try:
                # Own orientation does not affect own origin; parent's full frame is required.
                parent = placement.PlacementRelTo
                matrix = (
                    ifcopenshell.util.placement.get_local_placement(parent)
                    if parent
                    else np.eye(4)
                )
                xyz = matrix @ np.array(
                    list(placement.RelativePlacement.Location.Coordinates)
                    + ([0.0] if len(placement.RelativePlacement.Location.Coordinates) == 2 else [])
                    + [1.0]
                )
                if not np.isfinite(matrix).all() or not np.isfinite(xyz).all():
                    raise ValueError("invalid parent frame or location")
                for i, axis in enumerate("XYZ"):
                    self.check(
                        entity,
                        obj.id(),
                        "Placement",
                        ["Placement_" + axis],
                        float(xyz[i]),
                        rows,
                        stats["Placement"],
                    )
            except (
                ValueError,
                TypeError,
                AttributeError,
                RuntimeError,
                RecursionError,
            ) as exc:
                stats["Placement"]["unverified"] += 1
                self.detail(entity, obj.id(), "Placement", "XYZ", "unverified", actual=str(exc))
