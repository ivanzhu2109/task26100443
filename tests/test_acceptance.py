"""规模验收：10 万访客、2 万客户、30 万变更、10 万条跨版本查询。

验证：
- 批量追加 + 批量查询总耗时 <= 120 秒；
- 进程峰值内存 <= 512 MiB；
- 查询结果不随查询顺序改变；
- 覆盖身份组反复合并/拆分、同一关系反复新增/撤销的历史。
"""

import os
import random
import resource
import sys
import time

import pytest

from identity_history import (
    AddEvidence,
    IdentityHistoryStore,
    QueryItem,
    RevokeEvidence,
)

NUM_VISITORS = 100_000
NUM_CUSTOMERS = 20_000
NUM_CHANGES = 300_000
NUM_QUERIES = 100_000
CLUSTER_SIZE = 8  # 每簇最多 8 访客 + 1 客户，保证每组 <=50 访客、<=1 客户
TIME_LIMIT_S = 120.0
MEM_LIMIT_MIB = 512.0


def _peak_rss_mib() -> float:
    rss = resource.getrusage(resource.RUSAGE_SELF).ru_maxrss
    # macOS 返回字节，Linux 返回 KiB
    if sys.platform == "darwin":
        return rss / (1024 * 1024)
    return rss / 1024


def _build_workload():
    """确定性生成 30 万条变更：簇内反复合并/拆分、同关系反复增删。"""
    rng = random.Random(20261004)
    visitors = [f"v{i}" for i in range(NUM_VISITORS)]
    customers = [f"c{i}" for i in range(NUM_CUSTOMERS)]
    clusters = []
    for start in range(0, NUM_VISITORS, CLUSTER_SIZE):
        members = visitors[start : start + CLUSTER_SIZE]
        customer = customers[len(clusters) % NUM_CUSTOMERS] if start // CLUSTER_SIZE < NUM_CUSTOMERS else None
        clusters.append((members, customer))

    changes = []
    active = []  # 当前有效证据的 evidence_id 列表（含所属簇内位置隐式）
    seq = 0
    while len(changes) < NUM_CHANGES:
        members, customer = rng.choice(clusters)
        do_revoke = active and rng.random() < 0.35
        if do_revoke:
            idx = rng.randrange(len(active))
            changes.append(RevokeEvidence(active.pop(idx)))
            continue
        seq += 1
        eid = f"e{seq}"
        if customer is not None and rng.random() < 0.2:
            changes.append(
                AddEvidence(eid, visitor_a=rng.choice(members), customer_id=customer)
            )
        else:
            a = rng.choice(members)
            b = rng.choice(members)
            if a == b:
                continue
            changes.append(AddEvidence(eid, visitor_a=a, visitor_b=b))
        active.append(eid)
    return visitors, customers, changes


@pytest.mark.acceptance
def test_scale_acceptance(tmp_path, capsys):
    visitors, customers, changes = _build_workload()
    path = str(tmp_path / "scale.db")

    t0 = time.perf_counter()
    store = IdentityHistoryStore.create(path, visitors, customers)
    # 分批追加，模拟真实调用
    batch = 10_000
    for i in range(0, len(changes), batch):
        store.append_changes(changes[i : i + batch])
    t_append = time.perf_counter() - t0
    assert store.current_version == NUM_CHANGES

    rng = random.Random(7)
    items = [
        QueryItem(rng.choice(visitors), rng.randrange(0, NUM_CHANGES + 1))
        for _ in range(NUM_QUERIES)
    ]
    t0 = time.perf_counter()
    results = store.query(items)
    t_query = time.perf_counter() - t0
    store.close()

    total = t_append + t_query
    peak = _peak_rss_mib()

    # 查询顺序无关性：打乱顺序重查，结果按原顺序还原后必须一致
    store = IdentityHistoryStore.open(path)
    order = list(range(NUM_QUERIES))
    rng.shuffle(order)
    shuffled_results = store.query([items[i] for i in order])
    restored = [None] * NUM_QUERIES
    for pos, idx in enumerate(order):
        restored[idx] = shuffled_results[pos]
    assert restored == results
    store.close()

    with capsys.disabled():
        print(
            f"\n[acceptance] 追加 {NUM_CHANGES} 条变更: {t_append:.2f}s; "
            f"查询 {NUM_QUERIES} 条: {t_query:.2f}s; 合计: {total:.2f}s "
            f"(上限 {TIME_LIMIT_S}s); 峰值内存: {peak:.1f} MiB "
            f"(上限 {MEM_LIMIT_MIB} MiB)"
        )
    assert total <= TIME_LIMIT_S
    assert peak <= MEM_LIMIT_MIB
