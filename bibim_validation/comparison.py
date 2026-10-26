"""ViewComparison: independent validation responsibility, sharing the Validator context."""

from __future__ import annotations
import json
import re
from collections import Counter
from .values import quote, empty, same


class ViewComparison:
    def detail(self, entity, iid, category, field, status, expected=None, actual=None):
        self.details.append(
            dict(
                entity=entity,
                ifc_id=iid,
                category=category,
                field=field,
                status=status,
                expected=expected,
                actual=actual,
            )
        )

    def rows(self, view, entity):
        if view not in self.views:
            return {}, Counter(), set()
        result, counts, columns = {}, Counter(), set()
        for row in self.con.execute("SELECT * FROM " + quote(view)):
            item = dict(row)
            iid = item.get("ifc_id")
            counts[iid] += 1
            extras = json.loads(item.pop("etc", None) or "{}")
            if not isinstance(extras, dict):
                raise ValueError(f"{view}: etc must be a JSON object")
            for k, v in extras.items():
                if k in item and not empty(item[k]) and not same(v, item[k], self.tolerance):
                    self.detail(entity, iid, "layout", k, "conflicting_etc", item[k], v)
                else:
                    item[k] = v
            columns.update(item)
            result[iid] = item
        return result, counts, columns

    def check(self, entity, iid, category, names, expected, rows, stats, unit=None):
        if empty(expected):
            return
        candidates = []
        for view, row in rows:
            folded = (
                self.report.get("views", {})
                .get(view, {})
                .get("column_policy", {})
                .get("uniform_units", {})
            )
            for name in names:
                target = folded.get(name, {}).get("column", name)
                if target not in row and unit:
                    target = name + "_" + unit
                if target in row:
                    candidates.append((view, target, row[target], folded.get(name)))
                else:
                    # SQLite case-insensitive name collisions get a 10-hex suffix.
                    # Match the exact original spelling; never pick by value.
                    pattern = re.compile(
                        re.escape(name)
                        + r"_[0-9a-f]{10}"
                        + ("(?:_" + re.escape(unit) + ")?" if unit else "")
                        + r"$"
                    )
                    for key in row:
                        if pattern.fullmatch(key):
                            original = (
                                key.removesuffix("_" + unit)
                                if unit and key.endswith("_" + unit)
                                else key
                            )
                            candidates.append((view, key, row[key], folded.get(original)))
        candidates = list({(v, k): (v, k, a, f) for v, k, a, f in candidates}.values())
        if len(candidates) > 1 and category in ("Property", "Quantity"):
            stats["unverified"] += 1
            self.detail(
                entity,
                iid,
                category,
                names[0],
                "ambiguous_column_mapping",
                expected,
                [v + "." + k for v, k, _, _ in candidates],
            )
            return
        stats["expected"] += 1
        if not candidates:
            stats["missing"] += 1
            self.detail(entity, iid, category, names[0], "missing", expected)
            return
        good = True
        for view, key, actual, folded in candidates:
            self.checked_columns[view].add(key)
            good &= same(expected, actual, self.tolerance)
            if not same(expected, actual, self.tolerance):
                self.detail(entity, iid, category, view + "." + key, "mismatch", expected, actual)
            if unit:
                # Folded label or separate unit field is independently checked.
                encoded = (
                    folded.get("unit") if folded else unit if key.endswith("_" + unit) else None
                )
                actual_unit = encoded or dict(rows)[view].get(key + "_Unit")
                if actual_unit != unit:
                    good = False
                    self.detail(entity, iid, "Unit", key, "unit_mismatch", unit, actual_unit)
                elif not encoded:
                    self.checked_columns[view].add(key + "_Unit")
        stats["matched" if good else "mismatched"] += 1
