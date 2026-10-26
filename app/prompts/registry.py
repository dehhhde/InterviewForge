"""Prompt 模板注册表：每个节点一个模板 + 版本标记〔阶段 4 · §7.4，阶段 9 迁 Langfuse registry〕

目录约定：`templates/<version>/<node>.system.j2` 与 `<node>.user.j2`。
新增一版模板 = 复制一个版本目录，节点代码不动，只改 `version` 参数或 yaml 里的
`agent.prompt_version`——这样"哪版模板变差了"能用 trace 里的版本号归因。
"""

from dataclasses import dataclass
from functools import lru_cache
from pathlib import Path
from typing import Any

import jinja2

from app.agents.state import NodeName

TEMPLATE_DIR = Path(__file__).parent / "templates"
DEFAULT_VERSION = "v1"


class UnknownPromptError(ValueError):
    """请求了不存在的节点模板或版本目录。"""


def _environment() -> jinja2.Environment:
    if not TEMPLATE_DIR.is_dir():
        raise UnknownPromptError(f"缺少模板目录：{TEMPLATE_DIR}")
    return jinja2.Environment(
        loader=jinja2.FileSystemLoader(TEMPLATE_DIR),
        # 产物喂给 LLM 而非浏览器，转义只会污染 prompt
        autoescape=False,
        # 漏传变量必须报错，不能静默渲染成空串——空 prompt 会换来一句莫名其妙的题目
        undefined=jinja2.StrictUndefined,
        trim_blocks=True,
        lstrip_blocks=True,
    )


_ENV = _environment()


@dataclass(frozen=True)
class Prompt:
    """一个节点的一版模板；`render()` 返回可直接交给 `LLMClient.chat()` 的 messages。"""

    node: str
    version: str
    system: jinja2.Template
    user: jinja2.Template

    def render(self, **context: Any) -> list[dict[str, str]]:
        return [
            {"role": "system", "content": self.system.render(**context)},
            {"role": "user", "content": self.user.render(**context)},
        ]


def get_prompt(node: NodeName | str, version: str = DEFAULT_VERSION) -> Prompt:
    """取模板。枚举与等值字符串必须命中同一份缓存，所以先归一成 str 再查。

    `Enum.__hash__` 返回的是成员名（"PLAN"）的哈希，与 `hash("plan")` 不同却相等，
    直接把 NodeName 当 lru_cache 的键会静默产出两份 Prompt。
    """
    name = node.value if isinstance(node, NodeName) else node
    return _load(name, version)


@lru_cache(maxsize=64)
def _load(name: str, version: str) -> Prompt:
    root = TEMPLATE_DIR / version
    if not root.is_dir():
        raise UnknownPromptError(f"没有版本 {version!r}，可用版本：{available_versions()}")

    try:
        system = _ENV.get_template(f"{version}/{name}.system.j2")
        user = _ENV.get_template(f"{version}/{name}.user.j2")
    except jinja2.TemplateNotFound as exc:
        raise UnknownPromptError(f"节点 {name!r} 缺少模板 {exc.name}") from exc

    return Prompt(node=name, version=version, system=system, user=user)


def available_versions() -> list[str]:
    """列出模板版本目录，供启动自检与报错提示使用。"""
    if not TEMPLATE_DIR.is_dir():
        return []
    return sorted(p.name for p in TEMPLATE_DIR.iterdir() if p.is_dir())
