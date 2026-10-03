"""身份关联历史解析库：证据追加/撤销 + 任意历史版本的身份组查询。"""

from identity_history.engine import (
    Change,
    IdentityHistory,
    QueryItem,
    QueryResult,
)

__all__ = [
    "Change",
    "IdentityHistory",
    "QueryItem",
    "QueryResult",
]
