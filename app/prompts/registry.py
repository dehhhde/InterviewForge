"""Prompt 模板注册表：每个节点一个模板 + 版本标记〔阶段 4 · §7.4，阶段 9 迁 Langfuse registry〕

目录约定：`templates/<version>/<node>.system.j2` 与 `<node>.user.j2`。
新增一版模板 = 复制一个版本目录，节点代码不动，只改 `version` 参数或 yaml 里的
`agent.prompt_version`——这样"哪版模板变差了"能用 trace 里的版本号归因。
调用点一律**不写 `version`**：版本是部署参数而不是代码事实，写进代码就没法按环境切换。
"""

from dataclasses import dataclass
from functools import lru_cache
from pathlib import Path
from typing import Any

import jinja2

from app.agents.state import NodeName
from app.config import get_settings

TEMPLATE_DIR = Path(__file__).parent / "templates"


class UnknownPromptError(ValueError):
    """请求了不存在的节点模板或版本目录。"""


@lru_cache(maxsize=8)
def _environment(root: Path) -> jinja2.Environment:
    if not root.is_dir():
        raise UnknownPromptError(f"缺少模板目录：{root}")
    return jinja2.Environment(
        loader=jinja2.FileSystemLoader(root),
        # 产物喂给 LLM 而非浏览器，转义只会污染 prompt
        autoescape=False,
        # 漏传变量必须报错，不能静默渲染成空串——空 prompt 会换来一句莫名其妙的题目
        undefined=jinja2.StrictUndefined,
        trim_blocks=True,
        lstrip_blocks=True,
    )


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


def get_prompt(node: NodeName | str, version: str | None = None) -> Prompt:
    """取模板。不写 `version` 时按配置走（`agent.prompt_version`）。

    枚举与等值字符串必须命中同一份缓存，所以先归一成 str 再查。
    `Enum.__hash__` 返回的是成员名（"PLAN"）的哈希，与 `hash("plan")` 不同却相等，
    直接把 NodeName 当 lru_cache 的键会静默产出两份 Prompt。
    """
    name = node.value if isinstance(node, NodeName) else node
    resolved = version if version is not None else get_settings().agent.prompt_version
    return _load(TEMPLATE_DIR, name, resolved)


@lru_cache(maxsize=64)
def _load(root: Path, name: str, version: str) -> Prompt:
    """缓存键带上 `root`：测试会把模板根目录指到临时目录，键里没有它就会读到上一用例的缓存。"""
    directory = root / version
    if not directory.is_dir():
        raise UnknownPromptError(f"没有版本 {version!r}，可用版本：{available_versions()}")

    environment = _environment(root)
    try:
        system = environment.get_template(f"{version}/{name}.system.j2")
        user = environment.get_template(f"{version}/{name}.user.j2")
    except jinja2.TemplateNotFound as exc:
        raise UnknownPromptError(f"节点 {name!r} 缺少模板 {exc.name}") from exc

    return Prompt(node=name, version=version, system=system, user=user)


def available_versions() -> list[str]:
    """列出模板版本目录，供启动自检与报错提示使用。"""
    if not TEMPLATE_DIR.is_dir():
        return []
    return sorted(p.name for p in TEMPLATE_DIR.iterdir() if p.is_dir())
