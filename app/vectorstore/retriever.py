"""JD 要求项的向量检索接口〔阶段 7 · §10.4.3 的降级分支，实现属阶段 6〕

M4 只留**接口 + 空实现**：embedding 尚未选型（`docs/TODO.md` §3），而 `match` 节点
不能因为缺检索就跑不起来，所以这里定义 seam，`NullRetriever` 恒返回空列表，节点据此
退回"词典 + 文本证据"的无检索判定。

阶段 6 接 sqlite-vec 时新增一个实现类即可，`match_node` 不用改——它只认这个 Protocol，
embedding 依赖被隔离在装配点（`make_match_node(retriever=...)`）之外。
"""

from collections.abc import Sequence
from dataclasses import dataclass
from typing import Protocol


@dataclass(frozen=True)
class RetrievedPassage:
    """一段与查询语义相近的简历片段。`score` 是相似度，越大越像。"""

    text: str
    score: float


class SkillRetriever(Protocol):
    """按查询召回简历片段。实现方负责 embedding 与索引，本接口不规定用哪种。"""

    async def retrieve(self, query: str, top_k: int) -> Sequence[RetrievedPassage]: ...


class NullRetriever:
    """无检索降级实现：永远召回 0 条。

    它是"没接线"的唯一合法状态。**不要**在这里塞正则匹配当"简易检索"——那会让
    阶段 6 换实现时没人说得清召回率的变化究竟来自 embedding 还是来自那段正则。
    """

    async def retrieve(self, query: str, top_k: int) -> Sequence[RetrievedPassage]:
        return ()
