"""规模验收：10万访客、2万客户、30万变更、一批10万条跨版本查询。

- 覆盖身份组反复合并/拆分、同一关系反复新增/撤销。
- 断言总耗时 < 120s、进程峰值内存 < 512 MiB。
- 抽样与独立参考实现（增量重放）逐条核对。
- 验证历史查询结果不随批内查询顺序改变。
"""

import gc
import random
import resource
import sys
import time
from pathlib import Path

import pytest

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))

from identity_history import Change, IdentityHistory, QueryItem

N_VISITORS = 100_000
N_CUSTOMERS = 20_000
N_CHANGES = 300_000
N_QUERIES = 100_000
MAX_GROUP = 50
TIME_BUDGET_S = 120.0
MEM_BUDGET_MIB = 512.0


class WorkloadGenerator:
    """生成合法变更流；用组件跟踪保证每组 <=50 访客、<=1 客户。

    组件只含访客；客户作为组件的附属（cust_edge）跟踪，拆分时跟随其访客一端。
    """

    def __init__(self, rng, visitors, customers):
        self.rng = rng
        self.visitors = visitors
        self.free_customers = list(customers)
        self.node_comp = {v: i for i, v in enumerate(visitors)}
        self.members = {i: {v} for i, v in enumerate(visitors)}
        self.vv_edges = {i: set() for i in range(len(visitors))}  # 组件内访客-访客证据
        self.cust_edge = {i: None for i in range(len(visitors))}  # cid -> (eid, visitor, customer)
        self.live = {}  # eid -> (a, b)
        self.next_comp = len(visitors)
        self.eid = 0
        self.recent_pairs = []  # 最近出现过的关系，用于同一关系反复新增/撤销

    def _merge(self, ca, cb):
        keep, drop = (ca, cb) if len(self.members[ca]) >= len(self.members[cb]) else (cb, ca)
        for node in self.members[drop]:
            self.node_comp[node] = keep
        self.members[keep] |= self.members[drop]
        self.vv_edges[keep] |= self.vv_edges[drop]
        if self.cust_edge[drop] is not None:
            self.cust_edge[keep] = self.cust_edge[drop]
        del self.members[drop], self.vv_edges[drop], self.cust_edge[drop]
        return keep

    def _split(self, comp):
        members = self.members[comp]
        old_edges = self.vv_edges[comp]  # 只含有效证据
        adj = {n: [] for n in members}
        for eid in old_edges:
            a, b = self.live[eid]
            adj[a].append(b)
            adj[b].append(a)
        seen, parts = set(), []
        for n in members:
            if n in seen:
                continue
            part, stack = {n}, [n]
            seen.add(n)
            while stack:
                for nb in adj[stack.pop()]:
                    if nb not in seen:
                        seen.add(nb)
                        part.add(nb)
                        stack.append(nb)
            parts.append(part)
        if len(parts) == 1:
            return
        cust = self.cust_edge[comp]
        del self.members[comp], self.vv_edges[comp], self.cust_edge[comp]
        for part in parts:
            cid = self.next_comp
            self.next_comp += 1
            self.members[cid] = part
            self.vv_edges[cid] = {e for e in old_edges if self.live[e][0] in part}
            self.cust_edge[cid] = cust if (cust is not None and cust[1] in part) else None
            for n in part:
                self.node_comp[n] = cid

    def add(self):
        self.eid += 1
        eid = f"e{self.eid}"
        r = self.rng.random()
        a = self.rng.choice(self.visitors)
        ca = self.node_comp[a]
        if r < 0.10 and self.cust_edge[ca] is None and self.free_customers:
            c = self.free_customers.pop()
            self.cust_edge[ca] = (eid, a, c)
            self.live[eid] = (a, c)
            return Change("add", eid, a, c)
        if r < 0.20 and self.recent_pairs:
            a, b = self.rng.choice(self.recent_pairs)
        else:
            b = self.rng.choice(self.visitors)
        ca, cb = self.node_comp[a], self.node_comp[b]
        if ca != cb:
            too_big = len(self.members[ca]) + len(self.members[cb]) > MAX_GROUP
            two_customers = self.cust_edge[ca] is not None and self.cust_edge[cb] is not None
            if too_big or two_customers:
                return self.add()  # 重新选择，保证合法
        self.recent_pairs.append((a, b))
        if len(self.recent_pairs) > 5000:
            self.recent_pairs.pop(0)
        self.live[eid] = (a, b)
        if ca != cb:
            ca = self._merge(ca, cb)
        self.vv_edges[ca].add(eid)
        return Change("add", eid, a, b)

    def revoke(self):
        eid = self.rng.choice(list(self.live))
        a, b = self.live.pop(eid)
        ca = self.node_comp.get(a)
        if ca is None:  # a 是客户（客户只作 node_b，不会成为 node_a，防御性分支）
            ca = self.node_comp[b]
        edge = self.cust_edge.get(ca)
        if edge is not None and edge[0] == eid:
            self.cust_edge[ca] = None
            self.free_customers.append(edge[2])
        else:
            self.vv_edges[ca].discard(eid)
            self._split(ca)
        return Change("revoke", eid)


def generate_workload():
    rng = random.Random(42)
    visitors = [f"v{i}" for i in range(N_VISITORS)]
    customers = [f"c{i}" for i in range(N_CUSTOMERS)]
    gen = WorkloadGenerator(rng, visitors, customers)
    changes = []
    while len(changes) < N_CHANGES:
        if not gen.live or rng.random() < 0.55:
            changes.append(gen.add())
        else:
            changes.append(gen.revoke())
    queries = [QueryItem(rng.choice(visitors), rng.randrange(0, N_CHANGES + 1))
               for _ in range(N_QUERIES)]
    return visitors, customers, changes, queries


def reference_sample(changes, customers_set, sample):
    """独立参考：按版本增量重放，对抽样查询求连通分量。"""
    answers = {}
    active = {}
    adj = {}
    idx = 0
    for it in sorted(sample, key=lambda x: x.version):
        while idx < it.version:
            ch = changes[idx]
            idx += 1
            if ch.kind == "add":
                active[ch.evidence_id] = (ch.node_a, ch.node_b)
                adj.setdefault(ch.node_a, []).append(ch.node_b)
                adj.setdefault(ch.node_b, []).append(ch.node_a)
            else:
                a, b = active.pop(ch.evidence_id)
                adj[a].remove(b)
                adj[b].remove(a)
        seen, stack = {it.visitor_id}, [it.visitor_id]
        cust = None
        while stack:
            node = stack.pop()
            for nb in adj.get(node, ()):
                if nb in customers_set:
                    cust = nb
                elif nb not in seen:
                    seen.add(nb)
                    stack.append(nb)
        answers[(it.visitor_id, it.version)] = (tuple(sorted(seen)), cust)
    return answers


def peak_rss_mib():
    rss = resource.getrusage(resource.RUSAGE_SELF).ru_maxrss
    return rss / (1024 * 1024) if sys.platform == "darwin" else rss / 1024


@pytest.mark.acceptance
def test_scale_acceptance(tmp_path):
    visitors, customers, changes, queries = generate_workload()
    customers_set = set(customers)
    db = str(tmp_path / "scale.db")

    t0 = time.perf_counter()
    ih = IdentityHistory.create(db, visitors, customers)
    batch = 50_000
    for i in range(0, len(changes), batch):
        ih.append(changes[i:i + batch])
    t_append = time.perf_counter() - t0
    assert ih.current_version() == N_CHANGES
    ih.close()

    t1 = time.perf_counter()
    ih = IdentityHistory.open(db)  # 重新打开后查询全部历史
    results = ih.query(queries)
    t_query = time.perf_counter() - t1
    total = time.perf_counter() - t0

    # 顺序无关：逆序再查一遍，逐条比对
    results_rev = ih.query(list(reversed(queries)))
    assert results == list(reversed(results_rev))

    # 抽样 500 条与独立参考实现核对
    rng = random.Random(7)
    sample = rng.sample(queries, 500)
    ref = reference_sample(changes, customers_set, sample)
    checked = 0
    for res in results:
        key = (res.visitor_id, res.version)
        if key in ref:
            want_visitors, want_customer = ref[key]
            assert res.visitors == want_visitors, key
            assert res.customer == want_customer, key
            checked += 1
    assert checked == len(sample)
    ih.close()
    del results, results_rev, changes, queries
    gc.collect()

    peak = peak_rss_mib()
    print(f"\n[acceptance] append {N_CHANGES} changes: {t_append:.2f}s")
    print(f"[acceptance] query {N_QUERIES} items:     {t_query:.2f}s")
    print(f"[acceptance] total:                      {total:.2f}s (budget {TIME_BUDGET_S}s)")
    print(f"[acceptance] peak RSS:                   {peak:.1f} MiB (budget {MEM_BUDGET_MIB} MiB)")
    assert total < TIME_BUDGET_S
    assert peak < MEM_BUDGET_MIB
