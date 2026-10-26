"""BaseParser 抽象基类 + ParserRegistry 工厂〔阶段 5 · §8.1〕

解析器是**同步**的：PDF 解码是 CPU 密集而非 IO 等待，节点侧用
`await asyncio.to_thread(registry.parse, path)` 调它，别在事件循环里直接跑。
保持同步还让单测不需要事件循环。

`ParserRegistry` 不提供内置默认实现：注册什么就支持什么，避免"看起来支持其实没装依赖"
这类只有在运行时才暴露的错误。
"""

import dataclasses
from abc import ABC, abstractmethod
from collections.abc import Iterable
from dataclasses import dataclass
from pathlib import Path

from app.exceptions.base import ParseError


@dataclass(frozen=True)
class ParsedDocument:
    """一个文件解析出来的产物。`raw_text` 原样落进 `AgentState.raw_text`。"""

    raw_text: str
    page_count: int
    #: 整份扫描件会被 `PdfParser` 直接抛错，所以这里的 True 只会来自 image 路径
    #: （多模态 LLM 认出的扫描件），用于告诉下游这份文本是"读图得来的"。
    is_scanned: bool = False
    parser: str = ""
    #: 非致命问题（如个别页没有文字层），节点应把它并进只追加的 `errors` 通道
    warnings: tuple[str, ...] = ()
    #: 用到的模板标记，形如 `ocr@v1`（只有走 LLM 的解析器才有值），供节点写进 trace
    prompt_version: str = ""


class BaseParser(ABC):
    """一种文件格式一个实现。"""

    #: 小写、带点的扩展名，`ParserRegistry` 按它路由
    extensions: tuple[str, ...] = ()

    @property
    def name(self) -> str:
        return type(self).__name__

    @abstractmethod
    def parse(self, path: Path) -> ParsedDocument:
        """读出文本。读不出来就抛 `ParseError`，绝不返回空 `raw_text`——
        空文本会让下游抽取出一份"看起来干净"的空简历。"""


class ParserRegistry:
    """按扩展名选实现；未知类型与缺失文件都在解析前就报 `ParseError`。"""

    def __init__(self, parsers: Iterable[BaseParser] = ()) -> None:
        self._by_extension: dict[str, BaseParser] = {}
        for parser in parsers:
            self.register(parser)

    def register(self, parser: BaseParser) -> None:
        if not parser.extensions:
            raise ValueError(f"{parser.name} 未声明 extensions，无法路由")
        for ext in parser.extensions:
            key = ext.lower()
            if key in self._by_extension:
                previous = self._by_extension[key].name
                raise ValueError(f"扩展名 {key} 重复注册：{previous} 与 {parser.name}")
            self._by_extension[key] = parser

    @property
    def supported(self) -> tuple[str, ...]:
        return tuple(sorted(self._by_extension))

    def for_path(self, path: Path) -> BaseParser:
        ext = path.suffix.lower()
        parser = self._by_extension.get(ext)
        if parser is None:
            raise ParseError(
                f"不支持的简历文件类型：{ext or '(无扩展名)'}",
                code="parse_unsupported_type",
                details={"file": path.name, "supported": list(self.supported)},
            )
        return parser

    def parse(self, path: Path) -> ParsedDocument:
        # 先判类型再判存在性：`.txt` 的简历该报"不支持"，而不是含糊的"文件不存在"
        parser = self.for_path(path)
        if not path.is_file():
            raise ParseError(
                "简历文件不存在或已被清理",
                code="parse_file_missing",
                details={"path": str(path)},
            )
        return dataclasses.replace(parser.parse(path), parser=parser.name)
