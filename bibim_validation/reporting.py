"""JSON/CSV presentation of independently computed validation results."""

import json
import csv
from .values import write_json


def write_entity_reports(output, result):
    write_json(output / "entity_validation.json", result)
    with (output / "entity_summary.csv").open("w", encoding="utf-8-sig", newline="") as f:
        names = [
            "entity",
            "source_objects",
            "view_rows",
            "matched_objects",
            "missing_objects",
            "extra_objects",
            "duplicate_objects",
            "expected_values",
            "matched_values",
            "missing_values",
            "mismatched_values",
            "unverified_fields",
            "value_agreement_percent",
        ]
        writer = csv.DictWriter(f, fieldnames=names)
        writer.writeheader()
        for s in result["entities"]:
            writer.writerow(
                dict(
                    entity=s["entity"],
                    source_objects=s["source_objects"],
                    view_rows=s["view_rows"],
                    matched_objects=s["matched_objects"],
                    missing_objects=len(s["missing_ids"]),
                    extra_objects=len(s["extra_ids"]),
                    duplicate_objects=len(s["duplicate_ids"]),
                    **{
                        k + "_values": s["values"].get(k, 0)
                        for k in ("expected", "matched", "missing", "mismatched")
                    },
                    unverified_fields=s["values"].get("unverified", 0),
                    value_agreement_percent=s["value_agreement_percent"],
                )
            )
    with (output / "entity_differences.csv").open("w", encoding="utf-8-sig", newline="") as f:
        writer = csv.DictWriter(
            f, fieldnames=["entity", "ifc_id", "category", "field", "status", "expected", "actual"]
        )
        writer.writeheader()
        for row in result["details"]:
            writer.writerow(
                {
                    k: json.dumps(v, ensure_ascii=False) if isinstance(v, (dict, list)) else v
                    for k, v in row.items()
                }
            )
