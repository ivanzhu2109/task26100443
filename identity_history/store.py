"""SQLite 持久层：项目节点、证据及其新增/撤销版本。

表结构：
- meta(key, value)：当前版本号等元信息。
- nodes(id, kind)：初始化时给定的全部访客(kind=0)与客户(kind=1)。
- evidence(evidence_id, node_a, node_b, add_version, revoke_version)：
  每条证据一行；revoke_version 为 NULL 表示未撤销。
  版本 v 下证据有效  <=>  add_version <= v 且 (revoke_version IS NULL 或 revoke_version > v)。
"""

from __future__ import annotations

import sqlite3
from dataclasses import dataclass
from typing import Iterable, Iterator, Optional

KIND_VISITOR = 0
KIND_CUSTOMER = 1

_SCHEMA = """
CREATE TABLE IF NOT EXISTS meta (
    key TEXT PRIMARY KEY,
    value INTEGER NOT NULL
);
CREATE TABLE IF NOT EXISTS nodes (
    id TEXT PRIMARY KEY,
    kind INTEGER NOT NULL
);
CREATE TABLE IF NOT EXISTS evidence (
    evidence_id TEXT PRIMARY KEY,
    node_a TEXT NOT NULL,
    node_b TEXT NOT NULL,
    add_version INTEGER NOT NULL,
    revoke_version INTEGER
);
CREATE INDEX IF NOT EXISTS idx_evidence_a ON evidence(node_a);
CREATE INDEX IF NOT EXISTS idx_evidence_b ON evidence(node_b);
"""


@dataclass(frozen=True)
class EvidenceRow:
    evidence_id: str
    node_a: str
    node_b: str
    add_version: int
    revoke_version: Optional[int]  # None 表示尚未撤销


class Store:
    """封装 SQLite 读写。版本号从 1 开始连续递增，版本 0 为初始空状态。"""

    def __init__(self, db_path: str) -> None:
        self._conn = sqlite3.connect(db_path)
        self._conn.execute("PRAGMA journal_mode=WAL")
        self._conn.execute("PRAGMA synchronous=NORMAL")
        self._conn.executescript(_SCHEMA)
        self._pending = False

    def close(self) -> None:
        if self._pending:
            self._conn.commit()
            self._pending = False
        self._conn.close()

    # ---- 初始化与元信息 ----

    def initialize(self, visitor_ids: Iterable[str], customer_ids: Iterable[str]) -> None:
        with self._conn:
            self._conn.executemany(
                "INSERT INTO nodes(id, kind) VALUES (?, ?)",
                [(v, KIND_VISITOR) for v in visitor_ids],
            )
            self._conn.executemany(
                "INSERT INTO nodes(id, kind) VALUES (?, ?)",
                [(c, KIND_CUSTOMER) for c in customer_ids],
            )
            self._conn.execute(
                "INSERT INTO meta(key, value) VALUES ('version', 0)"
            )

    def is_initialized(self) -> bool:
        row = self._conn.execute(
            "SELECT value FROM meta WHERE key='version'"
        ).fetchone()
        return row is not None

    def current_version(self) -> int:
        row = self._conn.execute(
            "SELECT value FROM meta WHERE key='version'"
        ).fetchone()
        return int(row[0])

    def _set_version(self, version: int) -> None:
        self._conn.execute(
            "UPDATE meta SET value=? WHERE key='version'", (version,)
        )

    # ---- 节点 ----

    def load_customers(self) -> set[str]:
        cur = self._conn.execute("SELECT id FROM nodes WHERE kind=?", (KIND_CUSTOMER,))
        return {r[0] for r in cur}

    def load_visitors(self) -> set[str]:
        cur = self._conn.execute("SELECT id FROM nodes WHERE kind=?", (KIND_VISITOR,))
        return {r[0] for r in cur}

    # ---- 证据 ----

    def add_evidence(self, evidence_id: str, node_a: str, node_b: str, add_version: int) -> None:
        self._conn.execute(
            "INSERT INTO evidence(evidence_id, node_a, node_b, add_version, revoke_version)"
            " VALUES (?, ?, ?, ?, NULL)",
            (evidence_id, node_a, node_b, add_version),
        )
        self._pending = True

    def revoke_evidence(self, evidence_id: str, revoke_version: int) -> None:
        cur = self._conn.execute(
            "UPDATE evidence SET revoke_version=? WHERE evidence_id=? AND revoke_version IS NULL",
            (revoke_version, evidence_id),
        )
        if cur.rowcount != 1:
            raise ValueError(f"证据不存在或已撤销: {evidence_id!r}")
        self._pending = True

    def bump_version(self, version: int) -> None:
        self._set_version(version)
        self._pending = True

    def iter_evidence(self) -> Iterator[EvidenceRow]:
        cur = self._conn.execute(
            "SELECT evidence_id, node_a, node_b, add_version, revoke_version FROM evidence"
        )
        for eid, a, b, add_v, rev_v in cur:
            yield EvidenceRow(eid, a, b, add_v, rev_v)

    def commit(self) -> None:
        if self._pending:
            self._conn.commit()
            self._pending = False
