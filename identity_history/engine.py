"""对外接口：批量追加变更、批量历史版本查询。

查询模型：版本 v 下，证据 e 有效 <=> add_version <= v 且未在 v 及之前撤销。
有效证据构成无向图，访客的身份组即其连通分量（仅沿有效边扩展）。
查询时在内存中按版本过滤做 BFS；邻接表从 SQLite 惰性加载，追加变更后失效重建。
"""

from __future__ import annotations

from dataclasses import dataclass
from typing import Optional, Sequence

from identity_history.store import Store

ADD = "add"
REVOKE = "revoke"


@dataclass(frozen=True)
class Change:
    """一条变更：kind 为 "add" 或 "revoke"。

    add:    evidence_id 全局唯一，连接 (node_a, node_b)，两端为访客-访客或访客-客户。
    revoke: 按 evidence_id 撤销此前已新增且未撤销的证据；node_a/node_b 忽略。
    """

    kind: str
    evidence_id: str
    node_a: Optional[str] = None
    node_b: Optional[str] = None


@dataclass(frozen=True)
class QueryItem:
    visitor_id: str
    version: int


@dataclass(frozen=True)
class QueryResult:
    visitor_id: str
    version: int
    visitors: tuple[str, ...]      # 该版本下所在身份组的全部访客（排序）
    customer: Optional[str]        # 组对应客户；None 表示未识别

    @property
    def identified(self) -> bool:
        return self.customer is not None


# 邻接表条目：(邻居节点, 新增版本, 撤销版本或 None)
_Edge = tuple[str, int, Optional[int]]


class IdentityHistory:
    """单项目身份关联历史库。用 create 新建、open 重新打开。"""

    def __init__(self, store: Store) -> None:
        self._store = store
        self._customers = store.load_customers()
        self._visitors = store.load_visitors()
        self._adj: Optional[dict[str, list[_Edge]]] = None

    # ---- 生命周期 ----

    @classmethod
    def create(
        cls,
        db_path: str,
        visitor_ids: Sequence[str],
        customer_ids: Sequence[str],
    ) -> "IdentityHistory":
        visitors = list(visitor_ids)
        customers = list(customer_ids)
        if len(set(visitors)) != len(visitors) or len(set(customers)) != len(customers):
            raise ValueError("访客ID或客户ID存在重复")
        if set(visitors) & set(customers):
            raise ValueError("访客ID与客户ID必须互不重复")
        store = Store(db_path)
        if store.is_initialized():
            store.close()
            raise ValueError(f"库已初始化，请用 open 打开: {db_path}")
        store.initialize(visitors, customers)
        return cls(store)

    @classmethod
    def open(cls, db_path: str) -> "IdentityHistory":
        store = Store(db_path)
        if not store.is_initialized():
            store.close()
            raise ValueError(f"库未初始化，请用 create 创建: {db_path}")
        return cls(store)

    def close(self) -> None:
        self._store.close()

    def __enter__(self) -> "IdentityHistory":
        return self

    def __exit__(self, *exc: object) -> None:
        self.close()

    def current_version(self) -> int:
        return self._store.current_version()

    # ---- 批量追加 ----

    def append(self, changes: Sequence[Change]) -> list[int]:
        """批量追加变更，返回每条变更分配到的连续版本号。"""
        if not changes:
            return []
        base = self._store.current_version()
        versions: list[int] = []
        for offset, ch in enumerate(changes, start=1):
            version = base + offset
            if ch.kind == ADD:
                self._store.add_evidence(ch.evidence_id, ch.node_a, ch.node_b, version)
            elif ch.kind == REVOKE:
                self._store.revoke_evidence(ch.evidence_id, version)
            else:
                raise ValueError(f"未知变更类型: {ch.kind!r}")
            versions.append(version)
        self._store.bump_version(base + len(changes))
        self._store.commit()
        self._adj = None  # 邻接缓存失效
        return versions

    # ---- 批量历史查询 ----

    def query(self, items: Sequence[QueryItem]) -> list[QueryResult]:
        """批量历史查询；同批可跨版本、顺序任意，结果与输入顺序一一对应。"""
        if not items:
            return []
        adj = self._adjacency()
        current = self._store.current_version()
        return [self._query_one(adj, it, current) for it in items]

    def _query_one(
        self, adj: dict[str, list[_Edge]], item: QueryItem, current: int
    ) -> QueryResult:
        if not 0 <= item.version <= current:
            raise ValueError(f"版本不存在: {item.version}")
        if item.visitor_id not in self._visitors:
            raise ValueError(f"未知访客: {item.visitor_id!r}")
        v = item.version
        seen = {item.visitor_id}
        stack = [item.visitor_id]
        customers: set[str] = set()
        while stack:
            node = stack.pop()
            for neighbor, add_v, rev_v in adj.get(node, ()):
                if add_v <= v and (rev_v is None or rev_v > v):
                    if neighbor in self._customers:
                        customers.add(neighbor)
                    elif neighbor not in seen:
                        seen.add(neighbor)
                        stack.append(neighbor)
        customer = next(iter(customers)) if customers else None
        return QueryResult(
            visitor_id=item.visitor_id,
            version=v,
            visitors=tuple(sorted(seen)),
            customer=customer,
        )

    def _adjacency(self) -> dict[str, list[_Edge]]:
        if self._adj is None:
            adj: dict[str, list[_Edge]] = {}
            for row in self._store.iter_evidence():
                adj.setdefault(row.node_a, []).append((row.node_b, row.add_version, row.revoke_version))
                adj.setdefault(row.node_b, []).append((row.node_a, row.add_version, row.revoke_version))
            self._adj = adj
        return self._adj
