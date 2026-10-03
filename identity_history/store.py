"""身份关联历史库主类：追加变更 + 历史查询。"""

from __future__ import annotations

from typing import Dict, Iterable, List, Optional, Sequence, Set, Tuple

from . import db
from .types import (
    AddEvidence,
    Change,
    IdentityGroup,
    QueryItem,
    RevokeEvidence,
)


class IdentityHistoryStore:
    """单项目身份关联历史库。

    - create(): 以全部访客/客户 ID 初始化新库（版本 0 无任何关联）。
    - open(): 重新打开已有库，全部历史版本仍可查询。
    - append_changes(): 批量追加变更，返回每条变更的版本号。
    - query(): 批量历史查询，可跨版本、顺序任意。
    """

    def __init__(self) -> None:
        self._conn = None
        self._visitors: Set[str] = set()
        self._customers: Set[str] = set()
        self._version: int = 0
        # evidence_id -> (node_a, node_b)
        self._evidence_nodes: Dict[str, Tuple[str, str]] = {}
        # evidence_id -> 新增版本
        self._add_version: Dict[str, int] = {}
        # evidence_id -> 撤销版本（未撤销则无此项）
        self._revoke_version: Dict[str, int] = {}
        # node_id -> 关联的证据 ID 列表
        self._adj: Dict[str, List[str]] = {}

    # ------------------------------------------------------------------ 建库

    @classmethod
    def create(
        cls,
        path: str,
        visitor_ids: Iterable[str],
        customer_ids: Iterable[str],
    ) -> "IdentityHistoryStore":
        """创建新库。visitor_ids 与 customer_ids 必须互不重复。"""
        visitors = set(visitor_ids)
        customers = set(customer_ids)
        overlap = visitors & customers
        if overlap:
            raise ValueError(f"访客 ID 与客户 ID 重复: {sorted(overlap)[:5]}")
        store = cls()
        store._conn = db.connect(path)
        if db.is_initialized(store._conn):
            store._conn.close()
            raise ValueError(f"数据库已初始化，请使用 open(): {path}")
        db.init_schema(store._conn)
        db.insert_nodes(store._conn, visitors, customers)
        store._visitors = visitors
        store._customers = customers
        for node in visitors | customers:
            store._adj[node] = []
        return store

    @classmethod
    def open(cls, path: str) -> "IdentityHistoryStore":
        """打开已有库并恢复全部历史。"""
        store = cls()
        store._conn = db.connect(path)
        if not db.is_initialized(store._conn):
            store._conn.close()
            raise ValueError(f"数据库未初始化，请使用 create(): {path}")
        store._visitors, store._customers = db.load_nodes(store._conn)
        for node in store._visitors | store._customers:
            store._adj[node] = []
        for version, kind, evidence_id, node_a, node_b in db.load_change_rows(
            store._conn
        ):
            store._version = version
            if kind == "add":
                store._apply_add(evidence_id, node_a, node_b, version)
            else:
                store._apply_revoke(evidence_id, version)
        return store

    @property
    def current_version(self) -> int:
        return self._version

    # -------------------------------------------------------------- 内部状态

    def _apply_add(
        self, evidence_id: str, node_a: str, node_b: str, version: int
    ) -> None:
        self._evidence_nodes[evidence_id] = (node_a, node_b)
        self._add_version[evidence_id] = version
        self._adj[node_a].append(evidence_id)
        self._adj[node_b].append(evidence_id)

    def _apply_revoke(self, evidence_id: str, version: int) -> None:
        self._revoke_version[evidence_id] = version

    def _edge_valid_at(self, evidence_id: str, version: int) -> bool:
        if self._add_version[evidence_id] > version:
            return False
        revoked = self._revoke_version.get(evidence_id)
        return revoked is None or revoked > version

    # -------------------------------------------------------------- 追加变更

    def append_changes(self, changes: Sequence[Change]) -> List[int]:
        """批量追加变更，按顺序分配连续版本号并返回。"""
        rows = []
        versions: List[int] = []
        for change in changes:
            self._version += 1
            version = self._version
            versions.append(version)
            if isinstance(change, AddEvidence):
                node_a, node_b = self._validate_add(change)
                self._apply_add(change.evidence_id, node_a, node_b, version)
                rows.append((version, "add", change.evidence_id, node_a, node_b))
            elif isinstance(change, RevokeEvidence):
                self._validate_revoke(change)
                self._apply_revoke(change.evidence_id, version)
                rows.append((version, "revoke", change.evidence_id, None, None))
            else:
                raise TypeError(f"未知变更类型: {type(change)!r}")
        db.append_change_rows(self._conn, rows)
        return versions

    def _validate_add(self, change: AddEvidence) -> Tuple[str, str]:
        if change.evidence_id in self._evidence_nodes:
            raise ValueError(f"证据 ID 已存在: {change.evidence_id}")
        if change.visitor_a not in self._visitors:
            raise ValueError(f"未知访客: {change.visitor_a}")
        has_b = change.visitor_b is not None
        has_c = change.customer_id is not None
        if has_b == has_c:
            raise ValueError("visitor_b 与 customer_id 必须且只能有一个非空")
        if has_b:
            if change.visitor_b not in self._visitors:
                raise ValueError(f"未知访客: {change.visitor_b}")
            if change.visitor_b == change.visitor_a:
                raise ValueError("证据不能连接访客自身")
            return change.visitor_a, change.visitor_b  # type: ignore[return-value]
        if change.customer_id not in self._customers:
            raise ValueError(f"未知客户: {change.customer_id}")
        return change.visitor_a, change.customer_id  # type: ignore[return-value]

    def _validate_revoke(self, change: RevokeEvidence) -> None:
        if change.evidence_id not in self._evidence_nodes:
            raise ValueError(f"证据不存在: {change.evidence_id}")
        if change.evidence_id in self._revoke_version:
            raise ValueError(f"证据已撤销: {change.evidence_id}")

    # -------------------------------------------------------------- 历史查询

    def query(self, items: Sequence[QueryItem]) -> List[IdentityGroup]:
        """批量历史查询，结果与输入一一对应。"""
        return [self._query_one(item) for item in items]

    def _query_one(self, item: QueryItem) -> IdentityGroup:
        if item.visitor_id not in self._visitors:
            raise ValueError(f"未知访客: {item.visitor_id}")
        if not 0 <= item.version <= self._version:
            raise ValueError(
                f"版本 {item.version} 不存在（当前范围 0..{self._version}）"
            )
        version = item.version
        visited: Set[str] = {item.visitor_id}
        stack = [item.visitor_id]
        visitors: List[str] = []
        customer_id: Optional[str] = None
        while stack:
            node = stack.pop()
            if node in self._visitors:
                visitors.append(node)
            else:
                customer_id = node
            for evidence_id in self._adj[node]:
                if not self._edge_valid_at(evidence_id, version):
                    continue
                node_a, node_b = self._evidence_nodes[evidence_id]
                other = node_b if node_a == node else node_a
                if other not in visited:
                    visited.add(other)
                    stack.append(other)
        return IdentityGroup(tuple(sorted(visitors)), customer_id)

    # ------------------------------------------------------------------ 关闭

    def close(self) -> None:
        """关闭数据库连接。"""
        if self._conn is not None:
            self._conn.close()
            self._conn = None

    def __enter__(self) -> "IdentityHistoryStore":
        return self

    def __exit__(self, *exc: object) -> None:
        self.close()
