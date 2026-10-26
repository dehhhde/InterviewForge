"""节点测试共用的 LLM 替身。

`_FakeCompletions` 的形状与 `tests/test_agents/test_validate_node.py` 里那份一致：
只实现 `chat.completions.create`，按调用顺序吐预置回复。回复内容一律是写死的字符串，
所以断言只能落在**结构与调用行为**上，不许落在模型文案上（计划书 §5 原则）。
"""

import json
from collections.abc import Sequence
from types import SimpleNamespace
from typing import Any


class FakeCompletions:
    """按顺序吐预置回复；`raises` 非空时每次都抛那个异常。"""

    def __init__(self, replies: Sequence[str] = (), raises: Exception | None = None) -> None:
        self.replies = list(replies)
        self.raises = raises
        self.calls: list[dict[str, Any]] = []
        self.chat = SimpleNamespace(completions=self)

    async def create(self, **kwargs: Any) -> Any:
        self.calls.append(kwargs)
        if self.raises is not None:
            raise self.raises
        text = self.replies[min(len(self.calls) - 1, len(self.replies) - 1)]
        return SimpleNamespace(
            choices=[SimpleNamespace(message=SimpleNamespace(content=text))],
            usage=SimpleNamespace(prompt_tokens=10, completion_tokens=5),
        )

    @property
    def user_text(self) -> str:
        """最近一次调用的 user 消息内容，用来断言"该进 prompt 的东西进去了"。"""
        messages = self.calls[-1]["messages"]
        return str(messages[-1]["content"])


def prompt_block(rendered: str, label: str) -> Any:
    """从渲染好的 user 消息里抠出某一段 JSON。

    定位靠模板里的小标题：变量名不出现在渲染结果里，写死它等于埋一颗"改模板就红"的雷。
    """
    tail = rendered.split(label, 1)[1].split("\n\n", 1)[1]
    return json.loads(tail.split("\n\n", 1)[0])
