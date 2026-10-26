"""IFC input validation and publication of a completely built database."""

from __future__ import annotations
import hashlib
import os
import re
import sqlite3
import tempfile
from collections import Counter
from contextlib import closing
from pathlib import Path
import ifcopenshell
from bibim_config import BUILD_VERSION
from bibim_io import write_json
from .pipeline import build
from .integrity import check_raw_entity_counts, check_sqlite_integrity
from .raw_import import raw_database
from .schemas import schema_name


def read_ifc(source, log):
    original = source.read_bytes()
    if original.startswith(b"version https://git-lfs.github.com/spec/"):
        raise ValueError("Git LFS 포인터입니다. 실제 IFC 파일을 다운로드하세요.")
    if b"\x00" in original:
        raise ValueError(
            "IFC에 NUL 손상이 있습니다. 원본을 다시 받아야 합니다. 손상된 노드를 자동 삭제하지 않습니다."
        )
    expected_ids = {int(x) for x in re.findall(rb"(?m)^\s*#(\d+)\s*=", original)}
    log(f"IFC 읽기: {source.name}")
    model = ifcopenshell.open(str(source))
    schema_name(model.schema_identifier)
    actual_ids = {entity.id() for entity in model}
    if not actual_ids or (expected_ids and actual_ids != expected_ids):
        raise ValueError(
            f"STEP 엔티티 보존 검사 실패: 파일 {len(expected_ids)}, 파싱 {len(actual_ids)}"
        )
    return model, original, len(actual_ids)


def finish_report(report, model, source, destination, original, count):
    source_hash = hashlib.sha256(original).hexdigest()
    if hashlib.sha256(source.read_bytes()).hexdigest() != source_hash:
        raise RuntimeError("변환 중 입력 파일이 변경되었습니다.")
    report.update(
        {
            "input": str(source),
            "output": str(destination),
            "source_sha256": source_hash,
            "raw_nodes": count,
            "raw_tables_included": True,
            "geometry_mode": "original IFC representation entities; no tessellated mesh",
            "integrity_check": "ok",
            "version": BUILD_VERSION,
            "source_schema_identifier": model.schema_identifier,
            "diagnostic_counts": dict(Counter(item["code"] for item in report["diagnostics"])),
        }
    )


def convert(source, destination, *, overwrite=False, log=print):
    source, destination = Path(source).resolve(), Path(destination).resolve()
    if source == destination:
        raise ValueError("입력과 출력 경로가 같습니다.")
    if destination.exists() and not overwrite:
        raise FileExistsError(f"출력이 이미 있습니다: {destination}")
    model, original, total = read_ifc(source, log)
    destination.parent.mkdir(parents=True, exist_ok=True)
    with tempfile.TemporaryDirectory(prefix=".bibim-build-", dir=destination.parent) as temp:
        raw, output = Path(temp) / "raw.sqlite", Path(temp) / "views.sqlite"
        log(f"원시 DB 생성: {total:,}개 엔티티")
        count = raw_database(model, raw)
        log("DOCS 기반 View 생성 중...")
        report = build(raw, output, log=log)
        log("원시 테이블 병합 및 무결성 검사...")
        with closing(sqlite3.connect(output)) as con:
            check_raw_entity_counts(con, model, count)
            check_sqlite_integrity(con, "SQLite integrity_check 실패")
            con.commit()
        finish_report(report, model, source, destination, original, count)
        write_json(output.with_suffix(".build.json"), report)
        os.replace(output, destination)
        os.replace(output.with_suffix(".build.json"), destination.with_suffix(".build.json"))
    log(f'완료: {len(report["views"])}개 View / {count:,}개 엔티티 / {destination}')
    return report
