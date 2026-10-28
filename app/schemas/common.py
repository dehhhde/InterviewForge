"""列表端点的响应外壳〔S3 · T-303 / OPT-6.3〕

本文件原先只有一句 docstring 宣传过一个 `ApiResponse[T]`——从来没有实现（BACKLOG P3-2
把它记在"空壳模块"里）。T-303 是它第一个真正的用户：列表端点从"裸数组"换成
`Page[T]`，因为前端要分页就绕不开两个问题——**这批之后还有多少条**、
**关键词过滤后的总数是多少**。裸数组只能回答"这次给了几条"，于是前端拉 200 条本地过滤。

`total` 与 `items` 必须来自**同一个条件**（服务层把同一份 `filters` 交给 `list` 与
`count`），否则页脚会显示一个和列表无关的数字。
"""

from __future__ import annotations

from pydantic import BaseModel, Field


class Page[DataT: BaseModel](BaseModel):
    """一页数据 + 满足同一条件的总行数。

    写成 PEP 695 的类型参数（`class Page[DataT: BaseModel]`）不是风格：仓库的 ruff 开了 `UP`，
    旧写法 `class Page(BaseModel, Generic[DataT])` 会撞 UP046。`BaseModel` 这个约束是**契约**
    而不是技术前提——实测不写约束，`response_model=Page[X]` 的 OpenAPI 一样能展开出
    `items: [$ref X]`；写它是为了"一页装的必须是一行行记录"这件事留在类型里，
    而不是只留在注释里。
    """

    #: 当前这一页，按各端点自己的排序（默认 `created_at` 倒序）。
    items: list[DataT] = Field(default_factory=list)
    #: **过滤后**的总数，不是全表行数——分页控件要的是"这些条件下一共多少条"。
    total: int = 0
