"""Storage invariants, independent of property extraction and row generation."""

from collections import Counter
from .reader import quote


def check_sqlite_integrity(connection, message="Output SQLite integrity check failed"):
    if connection.execute("PRAGMA integrity_check").fetchone()[0] != "ok":
        raise RuntimeError(message)


def check_unique_view_rows(connection, view_names):
    for name in view_names:
        count, unique_count = connection.execute(
            f"SELECT COUNT(*),COUNT(DISTINCT ifc_id) FROM {quote(name)}"
        ).fetchone()
        if count != unique_count:
            raise RuntimeError(f"Duplicate object rows: {name}")


def check_raw_entity_counts(connection, model, expected_total):
    if connection.execute("SELECT COUNT(*) FROM id_map").fetchone()[0] != expected_total:
        raise RuntimeError("전체 엔티티 행 수 불일치")
    for cls, expected in Counter(entity.is_a() for entity in model).items():
        if connection.execute(f"SELECT COUNT(*) FROM {quote(cls)}").fetchone()[0] != expected:
            raise RuntimeError(f"원시 테이블 행 수 불일치: {cls}")
