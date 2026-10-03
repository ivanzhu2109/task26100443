"""SQLite 持久层：负责节点与变更记录的存取。"""

from __future__ import annotations

import sqlite3
from typing import Iterable, List, Optional, Set, Tuple

SCHEMA_VERSION = "1"

_SCHEMA = """
CREATE TABLE meta (
    key TEXT PRIMARY KEY,
    value TEXT NOT NULL
);
CREATE TABLE nodes (
    node_id TEXT PRIMARY KEY,
    kind TEXT NOT NULL CHECK (kind IN ('visitor', 'customer'))
);
CREATE TABLE changes (
    version INTEGER PRIMARY KEY,
    kind TEXT NOT NULL CHECK (kind IN ('add', 'revoke')),
    evidence_id TEXT NOT NULL,
    node_a TEXT,
    node_b TEXT
);
"""


def connect(path: str) -> sqlite3.Connection:
    """打开（必要时创建）数据库连接。"""
    conn = sqlite3.connect(path)
    conn.execute("PRAGMA journal_mode=WAL")
    conn.execute("PRAGMA synchronous=NORMAL")
    return conn


def is_initialized(conn: sqlite3.Connection) -> bool:
    """判断数据库是否已初始化过。"""
    row = conn.execute(
        "SELECT name FROM sqlite_master WHERE type='table' AND name='meta'"
    ).fetchone()
    return row is not None


def init_schema(conn: sqlite3.Connection) -> None:
    """建表并写入 schema 版本。"""
    with conn:
        conn.executescript(_SCHEMA)
        conn.execute(
            "INSERT INTO meta(key, value) VALUES ('schema_version', ?)",
            (SCHEMA_VERSION,),
        )


def insert_nodes(
    conn: sqlite3.Connection,
    visitor_ids: Iterable[str],
    customer_ids: Iterable[str],
) -> None:
    """写入全部访客与客户节点。"""
    with conn:
        conn.executemany(
            "INSERT INTO nodes(node_id, kind) VALUES (?, 'visitor')",
            ((v,) for v in visitor_ids),
        )
        conn.executemany(
            "INSERT INTO nodes(node_id, kind) VALUES (?, 'customer')",
            ((c,) for c in customer_ids),
        )


def load_nodes(conn: sqlite3.Connection) -> Tuple[Set[str], Set[str]]:
    """读出 (访客集合, 客户集合)。"""
    visitors: Set[str] = set()
    customers: Set[str] = set()
    for node_id, kind in conn.execute("SELECT node_id, kind FROM nodes"):
        if kind == "visitor":
            visitors.add(node_id)
        else:
            customers.add(node_id)
    return visitors, customers


def append_change_rows(
    conn: sqlite3.Connection,
    rows: Iterable[Tuple[int, str, str, Optional[str], Optional[str]]],
) -> None:
    """批量写入变更行：(version, kind, evidence_id, node_a, node_b)。"""
    with conn:
        conn.executemany(
            "INSERT INTO changes(version, kind, evidence_id, node_a, node_b)"
            " VALUES (?, ?, ?, ?, ?)",
            rows,
        )


def load_change_rows(
    conn: sqlite3.Connection,
) -> List[Tuple[int, str, str, Optional[str], Optional[str]]]:
    """按版本顺序读出全部变更行。"""
    return list(
        conn.execute(
            "SELECT version, kind, evidence_id, node_a, node_b"
            " FROM changes ORDER BY version"
        )
    )
