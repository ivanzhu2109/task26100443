"""正确性测试：手工核定的小规模历史 + 随机历史对照独立参考实现。"""

import random
import sys
from pathlib import Path

import pytest

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))

from identity_history import Change, IdentityHistory, QueryItem


@pytest.fixture()
def db_path(tmp_path):
    return str(tmp_path / "history.db")


def make(db_path, visitors, customers):
    return IdentityHistory.create(db_path, visitors, customers)


def q(ih, pairs):
    return ih.query([QueryItem(v, ver) for v, ver in pairs])


# ---------- 手工核定的小规模历史 ----------

def test_handcrafted_full_history(db_path):
    # 访客 v1..v4，客户 c1。
    # v1: e1(v1-v2)  v2: e2(v2-v3)  v3: e3(v3-c1)  v4: 撤销e1  v5: e4(v1-v4)
    ih = make(db_path, ["v1", "v2", "v3", "v4"], ["c1"])
    versions = ih.append([
        Change("add", "e1", "v1", "v2"),
        Change("add", "e2", "v2", "v3"),
        Change("add", "e3", "v3", "c1"),
        Change("revoke", "e1"),
        Change("add", "e4", "v1", "v4"),
    ])
    assert versions == [1, 2, 3, 4, 5]

    # 独立核定的预期：每个版本、每个访客 -> (组内访客, 客户)
    expected = {
        0: {"v1": (("v1",), None), "v2": (("v2",), None),
            "v3": (("v3",), None), "v4": (("v4",), None)},
        1: {"v1": (("v1", "v2"), None), "v2": (("v1", "v2"), None),
            "v3": (("v3",), None), "v4": (("v4",), None)},
        2: {"v1": (("v1", "v2", "v3"), None), "v2": (("v1", "v2", "v3"), None),
            "v3": (("v1", "v2", "v3"), None), "v4": (("v4",), None)},
        3: {"v1": (("v1", "v2", "v3"), "c1"), "v2": (("v1", "v2", "v3"), "c1"),
            "v3": (("v1", "v2", "v3"), "c1"), "v4": (("v4",), None)},
        # 撤销 e1 后：v1 独立，v2-v3-c1 仍同组
        4: {"v1": (("v1",), None), "v2": (("v2", "v3"), "c1"),
            "v3": (("v2", "v3"), "c1"), "v4": (("v4",), None)},
        5: {"v1": (("v1", "v4"), None), "v2": (("v2", "v3"), "c1"),
            "v3": (("v2", "v3"), "c1"), "v4": (("v1", "v4"), None)},
    }
    items = [QueryItem(v, ver) for ver in expected for v in ("v1", "v2", "v3", "v4")]
    for res in ih.query(items):
        want_visitors, want_customer = expected[res.version][res.visitor_id]
        assert res.visitors == want_visitors, (res.visitor_id, res.version)
        assert res.customer == want_customer, (res.visitor_id, res.version)
        assert res.identified == (want_customer is not None)
    ih.close()


def test_same_relation_multiple_evidence(db_path):
    # 同一对节点两条证据：撤销一条不影响另一条
    ih = make(db_path, ["v1", "v2"], ["c1"])
    ih.append([
        Change("add", "e1", "v1", "v2"),
        Change("add", "e2", "v1", "v2"),
        Change("revoke", "e1"),
    ])
    r = q(ih, [("v1", 3), ("v2", 3)])
    assert r[0].visitors == ("v1", "v2") and r[1].visitors == ("v1", "v2")
    ih.append([Change("revoke", "e2")])
    r = q(ih, [("v1", 4), ("v2", 4)])
    assert r[0].visitors == ("v1",) and r[1].visitors == ("v2",)
    ih.close()


def test_alternative_path_keeps_group(db_path):
    # 环：v1-v2, v2-v3, v1-v3。撤销环上任意一条，组不拆
    ih = make(db_path, ["v1", "v2", "v3"], ["c1"])
    ih.append([
        Change("add", "e1", "v1", "v2"),
        Change("add", "e2", "v2", "v3"),
        Change("add", "e3", "v1", "v3"),
        Change("add", "e4", "v3", "c1"),
        Change("revoke", "e2"),  # 撤销冗余路径上的证据
    ])
    r = q(ih, [("v1", 5), ("v2", 5), ("v3", 5)])
    for res in r:
        assert res.visitors == ("v1", "v2", "v3")
        assert res.customer == "c1"
    ih.close()


def test_final_link_revoked_splits_group(db_path):
    # 链 v1-v2-v3-c1，撤销最后的连接后必须按剩余关系准确拆分
    ih = make(db_path, ["v1", "v2", "v3"], ["c1"])
    ih.append([
        Change("add", "e1", "v1", "v2"),
        Change("add", "e2", "v2", "v3"),
        Change("add", "e3", "v3", "c1"),
        Change("revoke", "e2"),  # 拆掉链中间：{v1,v2} 与 {v3,c1}
    ])
    r = q(ih, [("v1", 4), ("v2", 4), ("v3", 4)])
    assert r[0].visitors == ("v1", "v2") and r[0].customer is None
    assert r[1].visitors == ("v1", "v2") and r[1].customer is None
    assert r[2].visitors == ("v3",) and r[2].customer == "c1"
    ih.append([Change("revoke", "e1"), Change("revoke", "e3")])
    r = q(ih, [("v1", 6), ("v2", 6), ("v3", 6)])
    assert r[0].visitors == ("v1",) and r[1].visitors == ("v2",)
    assert r[2].visitors == ("v3",) and r[2].customer is None
    ih.close()


def test_reprove_same_relation_uses_new_evidence_id(db_path):
    ih = make(db_path, ["v1", "v2"], [])
    ih.append([
        Change("add", "e1", "v1", "v2"),
        Change("revoke", "e1"),
        Change("add", "e2", "v1", "v2"),  # 重新证明同一关系，新证据ID
    ])
    r = q(ih, [("v1", 2), ("v1", 3)])
    assert r[0].visitors == ("v1",)
    assert r[1].visitors == ("v1", "v2")
    ih.close()


def test_mid_batch_versions_queryable(db_path):
    # 同批追加的中间版本必须可查询
    ih = make(db_path, ["v1", "v2", "v3"], [])
    ih.append([
        Change("add", "e1", "v1", "v2"),
        Change("add", "e2", "v2", "v3"),
        Change("revoke", "e1"),
    ])
    r = q(ih, [("v1", 1), ("v3", 2), ("v2", 3)])
    assert r[0].visitors == ("v1", "v2")
    assert r[1].visitors == ("v1", "v2", "v3")
    assert r[2].visitors == ("v2", "v3")
    ih.close()


def test_history_immutable_and_reopen(db_path):
    ih = make(db_path, ["v1", "v2"], ["c1"])
    ih.append([Change("add", "e1", "v1", "c1"), Change("add", "e2", "v1", "v2")])
    before = q(ih, [("v1", 1), ("v2", 1), ("v1", 2)])
    ih.append([Change("revoke", "e1"), Change("revoke", "e2")])
    after = q(ih, [("v1", 1), ("v2", 1), ("v1", 2)])
    assert before == after  # 更早版本的查询结果不被后续变更改变
    ih.close()
    ih2 = IdentityHistory.open(db_path)  # 重新打开后所有版本仍可查询
    reopened = q(ih2, [("v1", 1), ("v2", 1), ("v1", 2), ("v1", 4)])
    assert reopened[:3] == before
    assert reopened[3].visitors == ("v1",) and reopened[3].customer is None
    ih2.close()


def test_query_order_independent(db_path):
    ih = make(db_path, ["v1", "v2", "v3"], ["c1"])
    ih.append([Change("add", "e1", "v1", "v2"), Change("add", "e2", "v2", "c1")])
    items = [QueryItem("v1", 0), QueryItem("v2", 2), QueryItem("v1", 1), QueryItem("v3", 2)]
    fwd = ih.query(items)
    rev = ih.query(list(reversed(items)))
    assert fwd == list(reversed(rev))  # 同一查询项答案与批内顺序无关
    ih.close()


def test_empty_changes_and_queries(db_path):
    ih = make(db_path, ["v1"], [])
    assert ih.append([]) == []
    assert ih.query([]) == []
    assert ih.current_version() == 0
    ih.close()


def test_invalid_version_and_unknown_visitor(db_path):
    ih = make(db_path, ["v1"], [])
    with pytest.raises(ValueError):
        ih.query([QueryItem("v1", 1)])
    with pytest.raises(ValueError):
        ih.query([QueryItem("ghost", 0)])
    ih.close()


def test_overlapping_ids_rejected(db_path):
    with pytest.raises(ValueError):
        make(db_path, ["v1", "x"], ["x"])


# ---------- 随机历史对照独立参考实现 ----------

def reference_answer(changes, visitors, customers, visitor, version):
    """独立参考实现：从头重建 version 时刻的图并求连通分量。"""
    active = {}
    for i, ch in enumerate(changes, start=1):
        if i > version:
            break
        if ch.kind == "add":
            active[ch.evidence_id] = (ch.node_a, ch.node_b)
        else:
            active.pop(ch.evidence_id, None)
    adj = {}
    for a, b in active.values():
        adj.setdefault(a, set()).add(b)
        adj.setdefault(b, set()).add(a)
    seen, stack = {visitor}, [visitor]
    found_customers = set()
    while stack:
        node = stack.pop()
        for nb in adj.get(node, ()):
            if nb in customers:
                found_customers.add(nb)
            elif nb not in seen:
                seen.add(nb)
                stack.append(nb)
    return tuple(sorted(seen)), (next(iter(found_customers)) if found_customers else None)


def _comp_customers(active, node, customers):
    """测试辅助：当前有效证据下 node 所在分量的客户集合。"""
    adj = {}
    for a, b in active.values():
        adj.setdefault(a, set()).add(b)
        adj.setdefault(b, set()).add(a)
    seen, stack = {node}, [node]
    found = set()
    while stack:
        for nb in adj.get(stack.pop(), ()):
            if nb in customers:
                found.add(nb)
            elif nb not in seen:
                seen.add(nb)
                stack.append(nb)
    return found


def test_randomized_against_reference(db_path):
    rng = random.Random(20261004)
    visitors = [f"v{i}" for i in range(12)]
    customers = [f"c{i}" for i in range(3)]
    customers_set = set(customers)
    ih = make(db_path, visitors, customers)
    changes = []
    live = {}
    eid = 0
    while len(changes) < 200:
        if live and rng.random() < 0.35:
            ch = Change("revoke", rng.choice(list(live)))
            live.pop(ch.evidence_id)
        else:
            if rng.random() < 0.3:
                a, b = rng.choice(visitors), rng.choice(customers)
            else:
                a, b = rng.sample(visitors, 2)
            # 遵守输入约束：合并后每组最多 1 个客户
            merged = _comp_customers(live, a, customers_set) | _comp_customers(live, b, customers_set)
            if b in customers_set:
                merged.add(b)
            if len(merged) > 1:
                continue
            eid += 1
            ch = Change("add", f"e{eid}", a, b)
            live[ch.evidence_id] = (a, b)
        changes.append(ch)
        ih.append([ch])  # 逐条追加，覆盖大量中间版本
    items = [QueryItem(rng.choice(visitors), rng.randrange(0, len(changes) + 1))
             for _ in range(500)]
    for res in ih.query(items):
        want = reference_answer(changes, visitors, customers_set,
                                res.visitor_id, res.version)
        assert (res.visitors, res.customer) == want, res
    ih.close()
