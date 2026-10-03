# identity-history：身份关联历史解析库

用 Python + SQLite 实现的单项目身份关联历史库，供上层归因分析直接调用（无接口层）。
同时回答"当前身份"与"历史身份"：错误证据被撤销导致身份组拆分后，旧归因报告
当时使用的身份关系仍可原样查询，不会被今天的关系覆盖。

## 数据模型

- 初始化时给定全部访客 ID 与客户 ID（两类互不重复），版本 0 无任何关联。
- 变更只有两种：新增证据（`add`）、撤销证据（`revoke`）。每条变更按追加顺序
  获得连续递增的版本号（从 1 开始）；同批追加的中间版本同样可查询。
- 证据连接两个访客，或一个访客与一个客户；证据 ID 全局唯一，重新证明同一
  关系须使用新证据 ID。
- 版本 v 下证据有效 `<=>` 新增版本 ≤ v 且未在 v 及之前撤销。有效证据构成
  无向、可传递的关联：身份组即连通分量。同一对节点可有多条有效证据，
  撤销冗余路径上的证据不会拆组。
- 证据与版本历史全部存入 SQLite，正常关闭重开后所有版本仍可查询。

## 调用方式

```python
from identity_history import Change, IdentityHistory, QueryItem

# 新建（已存在的库用 IdentityHistory.open(path) 重新打开）
ih = IdentityHistory.create("history.db",
                            visitor_ids=["v1", "v2", "v3"],
                            customer_ids=["c1"])

# 批量追加变更，返回每条变更分配到的版本号
versions = ih.append([
    Change("add", "e1", "v1", "v2"),     # 版本 1：访客-访客证据
    Change("add", "e2", "v2", "c1"),     # 版本 2：访客-客户证据
    Change("revoke", "e1"),              # 版本 3：撤销 e1
])

# 批量历史查询：同批可跨版本、顺序任意，结果与输入顺序一一对应
results = ih.query([
    QueryItem("v1", 2),   # -> visitors=("v1","v2"), customer="c1"
    QueryItem("v1", 3),   # -> visitors=("v1",),     customer=None（未识别）
    QueryItem("v1", 0),   # -> visitors=("v1",),     customer=None
])
for r in results:
    print(r.version, r.visitors, r.customer, r.identified)

ih.close()  # 或用作上下文管理器：with IdentityHistory.create(...) as ih:
```

约束（输入保证，库内仅做轻校验）：每个版本每组最多 50 个访客、最多 1 个
客户；撤销的证据此前已新增且未撤销；查询的版本号已存在。

## 实现要点

- `identity_history/store.py`：SQLite 持久层。`evidence` 表记录每条证据的
  新增/撤销版本，`meta` 表记录当前版本号；单事务批量写入。
- `identity_history/engine.py`：对外接口。查询时将全部证据惰性加载为内存
  邻接表（追加变更后失效重建），按查询版本过滤有效边做 BFS 求连通分量。
  由于每组最多 50 个访客，单次查询代价有界。

## 测试

```bash
python3 -m pytest tests/            # 全部测试（含规模验收）
python3 -m pytest tests/ -m "not acceptance"  # 仅快速正确性测试
```

- `tests/test_identity_history.py`：手工核定的小规模历史（版本 0、同批中间
  版本、撤销前后、重开库后）逐版本精确比对；同一关系多条证据、组内替代
  路径、最终连接撤销后的准确拆分；随机历史对照独立参考实现。
- `tests/test_acceptance_scale.py`：10 万访客、2 万客户、30 万条变更
  （覆盖组反复合并/拆分、同一关系反复新增/撤销）、一批 10 万条覆盖整个
  历史的查询。断言总耗时 < 120s、峰值内存 < 512 MiB，抽样 500 条与独立
  参考实现核对，并验证查询结果不随批内顺序改变。

本机最近一次验收实测：追加 30 万变更 1.7s，10 万条查询 0.6s，总计约 2.3s，
峰值内存约 281 MiB。
