"""上传用例共用的假字节（T-018：以前三个文件各抄一份同样的 bytes）。

它们的唯一职责是**过魔数那一关**（ADR-0017）并落盘，不打算被任何解析器读懂；
所以字面量必须纯 ASCII——本机控制台是 GBK，非 ASCII 的 bytes 字面量写进源码就是给自己挖坑。
"""

from __future__ import annotations

#: 只走落盘、不真解析的最小伪 PDF
PDF_BYTES = b"%PDF-1.4\n% stub body for upload staging only\n%%EOF\n"
