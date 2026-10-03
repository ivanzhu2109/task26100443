"""身份关联历史库的数据类型定义。"""

from __future__ import annotations

from dataclasses import dataclass
from typing import Optional, Tuple, Union


@dataclass(frozen=True)
class AddEvidence:
    """新增证据：连接两个访客，或连接一个访客与一个客户。

    visitor_b 与 customer_id 必须且只能有一个非空。
    """

    evidence_id: str
    visitor_a: str
    visitor_b: Optional[str] = None
    customer_id: Optional[str] = None


@dataclass(frozen=True)
class RevokeEvidence:
    """撤销证据：按证据 ID 撤销此前新增且未撤销的证据。"""

    evidence_id: str


Change = Union[AddEvidence, RevokeEvidence]


@dataclass(frozen=True)
class QueryItem:
    """单条历史查询：访客 ID + 已存在的版本号。"""

    visitor_id: str
    version: int


@dataclass(frozen=True)
class IdentityGroup:
    """查询结果：指定版本中该访客所在身份组。

    customer_id 为 None 表示该组未识别（无客户）。
    """

    visitors: Tuple[str, ...]
    customer_id: Optional[str]

    @property
    def identified(self) -> bool:
        return self.customer_id is not None
