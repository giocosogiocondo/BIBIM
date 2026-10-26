"""Sequential, isolated workers for every SQLite output (including previews)."""

from __future__ import annotations
import csv
import json
import subprocess
import sys
import time
from pathlib import Path
from bibim_config import PROJECT_ROOT
from bibim_io import write_json


def source_ifc(db, seen=None):
    seen = set() if seen is None else seen
    db = db.resolve()
    if db in seen:
        raise ValueError("Cycle in source manifest chain")
    seen.add(db)
    report = db.with_suffix(".build.json")
    if not report.exists():
        raise FileNotFoundError(f"Missing source manifest: {report}")
    data = json.loads(report.read_text(encoding="utf-8"))
    candidate = Path(data.get("source_ifc") or data.get("input") or "")
    if not candidate.is_absolute():
        candidate = report.parent / candidate
    if candidate.suffix.lower() == ".ifc":
        if not candidate.is_file():
            raise FileNotFoundError(f"Source IFC missing: {candidate}")
        return candidate.resolve(), data
    if candidate.suffix.lower() in (".sqlite", ".db"):
        original, _ = source_ifc(candidate, seen)
        return original, data
    raise ValueError(f"Manifest does not identify an IFC input: {report}")


def discover(root):
    return sorted(
        p
        for p in root.rglob("*")
        if p.is_file()
        and p.suffix.lower() in (".sqlite", ".db")
        and ".building." not in p.name
        and not any(
            part.startswith(("entity_validation", ".")) for part in p.relative_to(root).parts
        )
    )


def save(output, records, elapsed):
    report = dict(
        databases=len(records),
        elapsed_seconds=elapsed,
        statuses={
            s: sum(r["status"] == s for r in records)
            for s in sorted({r["status"] for r in records})
        },
        note="Agreement is for implemented independent checks only. Unverified source fields and output columns remain in each detailed report.",
        results=records,
    )
    write_json(output / "batch_summary.json", report)
    names = [
        "database",
        "source_ifc",
        "status",
        "elapsed_seconds",
        "source_objects",
        "matched_objects",
        "missing_objects",
        "extra_objects",
        "duplicate_objects",
        "expected_values",
        "matched_values",
        "missing_values",
        "mismatched_values",
        "unverified_fields",
        "unchecked_output_columns",
        "report_folder",
        "error",
    ]
    with (output / "batch_summary.csv").open("w", encoding="utf-8-sig", newline="") as f:
        writer = csv.DictWriter(f, fieldnames=names, extrasaction="ignore")
        writer.writeheader()
        writer.writerows(records)


def run(root, output, list_only=False):
    root = root.resolve()
    databases = discover(root)
    if not databases:
        raise ValueError(f"No database files found: {root}")
    if list_only:
        for db in databases:
            try:
                source, report = source_ifc(db)
                print(
                    f'{db.relative_to(root)} | {report.get("layout", "unknown")} | {source}',
                    flush=True,
                )
            except Exception as exc:
                print(f"{db.relative_to(root)} | ERROR: {exc}", flush=True)
        print(f"Total databases: {len(databases)}")
        return 0
    output.mkdir(parents=True, exist_ok=False)
    print(f"Databases: {len(databases)}\nResults: {output}\n", flush=True)
    records, started = [], time.perf_counter()
    try:
        for index, db in enumerate(databases, 1):
            relative = db.relative_to(root)
            folder = output / relative.with_suffix("")
            folder.mkdir(parents=True)
            record = dict(
                database=str(relative), source_ifc="", status="error", report_folder=str(folder)
            )
            begin = time.perf_counter()
            print(f"[{index}/{len(databases)}] {relative}", flush=True)
            try:
                validate_database(db, folder, record)
            except Exception as exc:
                record["error"] = str(exc)
                print("  ERROR: " + str(exc), flush=True)
            record["elapsed_seconds"] = round(time.perf_counter() - begin, 3)
            records.append(record)
            save(output, records, time.perf_counter() - started)
    except KeyboardInterrupt:
        save(output, records, time.perf_counter() - started)
        print("\nInterrupted. Completed results saved.", flush=True)
        return 130
    save(output, records, time.perf_counter() - started)
    print("\nFinished. Summary: " + str(output / "batch_summary.csv"), flush=True)
    return (
        1
        if any(r["status"] in ("error", "differences", "unsupported_layout") for r in records)
        else 0
    )


def validate_database(db, folder, record):
    script = PROJECT_ROOT / "validate_entity_views.py"
    metadata = json.loads(db.with_suffix(".build.json").read_text(encoding="utf-8"))
    if metadata.get("layout") != "entity-pairs":
        record.update(
            status="unsupported_layout",
            error=f'Layout {metadata.get("layout")!r} is not entity-pairs; not counted as passed',
        )
        print("  SKIP: " + record["error"], flush=True)
    else:
        source, metadata = source_ifc(db)
        record["source_ifc"] = str(source)
        code = execute_worker(script, source, db, folder)
        collect_result(folder, code, record)


def execute_worker(script, source, db, folder):
    with (folder / "validation.log").open("w", encoding="utf-8") as log:
        process = subprocess.Popen(
            [
                sys.executable,
                "-X",
                "utf8",
                str(script),
                str(source),
                str(db),
                "--out",
                str(folder),
            ],
            stdout=subprocess.PIPE,
            stderr=subprocess.STDOUT,
            text=True,
            encoding="utf-8",
            errors="replace",
        )
        try:
            for line in process.stdout:
                log.write(line)
                log.flush()
                print("  " + line.rstrip(), flush=True)
            code = process.wait()
        except BaseException:
            process.terminate()
            process.wait()
            raise
        finally:
            process.stdout.close()
    return code


def collect_result(folder, code, record):
    result_path = folder / "entity_validation.json"
    if not result_path.exists() or code not in (0, 1):
        record["error"] = f"Worker failed (exit {code}); see validation.log"
    else:
        result = json.loads(result_path.read_text(encoding="utf-8"))
        entities = result["entities"]
        for key in ("source_objects", "matched_objects"):
            record[key] = sum(s[key] for s in entities)
        for out_key, entity_key in [
            ("missing_objects", "missing_ids"),
            ("extra_objects", "extra_ids"),
            ("duplicate_objects", "duplicate_ids"),
        ]:
            record[out_key] = sum(len(s[entity_key]) for s in entities)
        for key in ("expected", "matched", "missing", "mismatched"):
            record[key + "_values"] = sum(s["values"].get(key, 0) for s in entities)
        record["unverified_fields"] = sum(s["values"].get("unverified", 0) for s in entities)
        record["unchecked_output_columns"] = sum(
            len(columns) for s in entities for columns in s["unchecked_output_columns"].values()
        )
        record["status"] = (
            "differences"
            if code == 1
            else (
                "passed_with_unverified"
                if record["unverified_fields"] or record["unchecked_output_columns"]
                else "passed"
            )
        )
        print("  Result: " + record["status"], flush=True)
