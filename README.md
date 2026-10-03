# task26100443 — 身份关联历史解析库

由上层归因分析调用的 Python 库（无接口层）。关联证据与版本历史存入 SQLite，
可同时回答**当前身份**与**任意历史版本的身份**，旧归因报告使用的身份关系
不会被后来的变更覆盖。

## 数据模型

- 单项目。`create()` 时给定全部访客 ID 与客户 ID（两类 ID 互不重复），
  版本 0 没有任何关联。
- 变更只有两种：`AddEvidence`（连接两个访客，或一个访客与一个客户）、
  `RevokeEvidence`（按证据 ID 撤销）。每条变更按追加顺序获得连续递增的
  版本号（1, 2, 3, ...），同批追加的中间版本同样可查询。
- 版本 v 的有效证据 = 新增版本 ≤ v 且未在 ≤ v 撤销的证据。同一对节点可有
  多条有效证据；有效证据表示无向、可传递的身份关联（连通分量即身份组）。
- 查询返回指定版本中该访客所在组的全部访客及该组客户；无客户时
  `customer_id` 为 `None`（`identified == False`），无关联的访客自成一组。

## 调用方式

```python
from identity_history import (
    IdentityHistoryStore, AddEvidence, RevokeEvidence, QueryItem,
)

# 初始化（已存在的库用 IdentityHistoryStore.open(path) 重新打开）
store = IdentityHistoryStore.create(
    "identity.db",
    visitor_ids=["v1", "v2", "v3"],
    customer_ids=["c1"],
)

# 批量追加变更，返回每条变更的版本号
versions = store.append_changes([
    AddEvidence("e1", visitor_a="v1", visitor_b="v2"),      # -> 版本 1
    AddEvidence("e2", visitor_a="v2", customer_id="c1"),    # -> 版本 2
    RevokeEvidence("e1"),                                   # -> 版本 3
])

# 批量历史查询：可跨版本、顺序任意，结果与输入一一对应
results = store.query([
    QueryItem("v1", 2),   # IdentityGroup(visitors=("v1","v2"), customer_id="c1")
    QueryItem("v1", 3),   # IdentityGroup(visitors=("v1",), customer_id=None)
    QueryItem("v1", 0),   # IdentityGroup(visitors=("v1",), customer_id=None)
])

store.close()  # 正常关闭后可用 open() 重新打开，全部版本仍可查询
```

约束：证据 ID 全局唯一（重新证明同一关系须用新证据 ID）；撤销的证据必须
此前已新增且未被撤销；查询版本必须在 `0..current_version` 内。违反时抛
`ValueError`。

## 实现说明

- `identity_history/types.py` — 变更与查询结果的数据类型。
- `identity_history/db.py` — SQLite 持久层（`nodes` 表 + `changes` 表，
  版本号为主键，批量写入用单事务）。
- `identity_history/store.py` — 主类。追加时把变更落盘并维护内存索引
  （证据 → 版本区间、节点 → 邻接证据）；查询时在指定版本的有效证据上做
  BFS，得到该版本的连通分量。重开库时按版本顺序回放全部变更恢复索引。

## 测试

```bash
python3 -m pytest tests/ -q          # 全部测试
python3 -m pytest tests/ -q -m acceptance   # 仅规模验收
```

- `tests/test_identity_history.py` — 正确性：同一关系多条证据、组内替代
  路径、最终连接撤销后的准确拆分、版本 0 / 撤销前后 / 同批中间版本、
  追加更多变更前后旧版本结果不变、重开库后全版本可查、输入校验。
- `tests/test_acceptance.py` — 规模验收：10 万访客、2 万客户、30 万条
  变更（含反复合并/拆分、同关系反复增删）、10 万条覆盖整个历史的查询。
  断言总耗时 ≤ 120 s、峰值内存 ≤ 512 MiB，并验证查询结果与查询顺序无关。
  本机实测：追加 0.67 s + 查询 0.55 s ≈ 1.2 s，峰值内存约 199 MiB。
