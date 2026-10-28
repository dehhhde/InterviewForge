"""全仓共用的 LLM 测试脚手架（T-018 收敛：以前 7 个文件各有一份 `_FakeCompletions`）。

除替身本身，还有两个同样"各处抄一份"的小件：`completion()`（一次往返的最小形状）与
`slot_settings()`（深拷贝配置 + 可区分的模型槽位假名）。它们放这里而不是 `tests/conftest.py`，
是因为只有涉及模型的用例需要它们。

只实现 `chat.completions.create`，按调用顺序吐预置回复。回复内容一律是写死的字符串，
所以断言只能落在**结构与调用行为**上，不许落在模型文案上（计划书 §5 原则）。

收敛的代价是零，收益是"新增一种注入方式只改一处"。原先 6 份副本的差别只有三点，
都在这里补齐（`raises=` / 回复列表里放异常 / `new_count`），所以副本可以全部删掉。

三种注入方式按用例形状选：

- `raises=异常`：每次 `create` 都抛它——"这个模型此刻不可用"。
- 回复列表里**放一个异常实例**：只在那一次抛——传输重试与 `chat_json` 的修复回路
  必须能测"第一次坏了、第二次好了"。
- 不注入：按顺序吐字符串，越界后一直吐最后一条（`min(...)`），所以"重做 N 轮"的
  用例不必准备 N 条回复。
"""

import json
from collections.abc import Sequence
from types import SimpleNamespace
from typing import Any

from app.config import Settings, get_settings


def completion(text: str) -> Any:
    """一次模型往返的最小形状：`choices[0].message.content` 加一段固定 usage。

    `FakeCompletions` 自己用它，"按 prompt 内容决定回复"的替身
    （`test_m5_pipeline.py` 的 `_TopicAwareCompletions`）也用它——usage 的形状一旦分叉，
    成本护栏那几条用例就只在其中一处成立。
    """
    return SimpleNamespace(
        choices=[SimpleNamespace(message=SimpleNamespace(content=text))],
        usage=SimpleNamespace(prompt_tokens=10, completion_tokens=5),
    )


def slot_settings(**slots: str) -> Settings:
    """深拷贝配置，把模型槽位改成**可区分**的假名字（`extract="extract-slot"`…）。

    必须拷贝：`get_settings()` 是 `lru_cache` 单例，直接改它会污染同进程的其他用例
    （AGENTS.md §6 说"用 monkeypatch 换，别改 `.env`"，这里是同一个道理）。
    假名字彼此不同，`model_for(purpose)` 的路由才有断言的余地。
    """
    copy = get_settings().model_copy(deep=True)
    for name, model in slots.items():
        setattr(copy, f"llm_model_{name}", model)
    return copy


def stamp(node: str) -> str:
    """`<node>@<当前配置的模板版本>`〔T-203〕。

    版本号是**部署参数而不是代码事实**（AGENTS.md §7），用例里写死 `@v1` 的后果是：
    换一版模板 = 红一片断言，而红的是字符串、不是行为。所有"节点盖了模板戳"的断言
    都走这里，`agent.prompt_version` 一换行它们照常成立。
    """
    return f"{node}@{get_settings().agent.prompt_version}"


class FakeCompletions:
    """按顺序吐预置回复，记下每次 `create` 的 kwargs。"""

    def __init__(self, replies: Sequence[Any] = (), raises: BaseException | None = None) -> None:
        self.replies = list(replies)
        self.raises = raises
        self.calls: list[dict[str, Any]] = []
        self.chat = SimpleNamespace(completions=self)

    async def create(self, **kwargs: Any) -> Any:
        self.calls.append(kwargs)
        if self.raises is not None:
            raise self.raises
        reply = self.replies[min(len(self.calls) - 1, len(self.replies) - 1)]
        if isinstance(reply, BaseException):
            raise reply
        return completion(reply)

    def new_count(self, baseline: int) -> int:
        """`baseline` 之后新增的往返次数。

        回退轮的用例要断的是"这一轮多花了几次调用"，而绝对次数会把首轮的量算进去，
        看不出差别。
        """
        return len(self.calls) - baseline

    def messages(self, index: int = -1) -> list[Any]:
        """第 `index` 次调用真正发出去的 `messages`。

        重问必须带上模型上次的原始输出，这类断言只关心消息序列，不关心 kwargs 里
        别的键；直接用 `calls[i]["messages"]` 会让人以为两者是两套记录。
        """
        return list(self.calls[index]["messages"])

    @property
    def user_text(self) -> str:
        """最近一次调用的 user 消息内容，用来断言"该进 prompt 的东西进去了"。"""
        messages = self.calls[-1]["messages"]
        return str(messages[-1]["content"])


def fenced_block(rendered: str, tag: str) -> str:
    """抠出 `<tag>…</tag>` 围栏里的正文。

    定位靠围栏而不是小标题（T-005 之后每个模板都把数据装进围栏）：小标题是给人读的散文，
    改一个字就该只改模板，不该红一片用例。

    只认**独占一行的开栏**：模板开头的注入声明会把标签名写在反引号里当散文讲，
    按子串找会先撞见那句说明，拿回来的就是说明书而不是数据。
    """
    lines = [line.strip() for line in rendered.splitlines()]
    opens = [i for i, line in enumerate(lines) if line == f"<{tag}>"]
    if len(opens) != 1:
        raise AssertionError(f"渲染结果里独占一行的 <{tag}> 开栏有 {len(opens)} 处，应当恰好 1 处")
    closes = [i for i, line in enumerate(lines) if line == f"</{tag}>" and i > opens[0]]
    if not closes:
        raise AssertionError(f"<{tag}> 围栏没有闭合行")
    return "\n".join(lines[opens[0] + 1 : closes[0]]).strip()


def prompt_block(rendered: str, tag: str) -> Any:
    """从渲染好的 user 消息里抠出某个围栏里的 JSON 并解析。"""
    return json.loads(fenced_block(rendered, tag))
