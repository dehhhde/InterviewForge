"""简历模型，含抽取结果 JSON（原设计写 JSONB，SQLite 没有，见计划 §6 风险 2）〔阶段 3〕

`parsed` 存的是 `ResumeSchema.model_dump(mode="json")` 的结果，**不拆成子表**。
理由：抽取结果的字段集还在动（M1/M2 期间已经加过 `dropped`、别名表这些），拆表等于每改一次
schema 就写一次 migration；而这份 JSON 只有"整份读出来喂给下游节点"这一种用法，
从来没有人按 `work_experiences[1].company` 查过。真要按技能检索的时候是 RAG（sqlite-vec），
不是关系查询。

`raw_text` 必须一起存：validate 的数据源是原文而不是 `parsed`（计划 §4.1 最后一条），
回放、重跑、录制 fixture 全靠它。
"""

from __future__ import annotations

from typing import Any

from sqlalchemy import JSON, String, Text
from sqlalchemy.orm import Mapped, mapped_column

from app.models.base import ORMModel


class Resume(ORMModel):
    """一份已上传、已解析的简历。"""

    __tablename__ = "resume"

    #: 落盘路径，**相对 upload_dir**（`{resume_id}.pdf`）。不存绝对路径：那等于把部署位置
    #: 写进数据库，换机器/换容器后整批记录作废。定位文件仍走 `locate_resume_file(upload_dir, …)`。
    stored_path: Mapped[str] = mapped_column(String(500), nullable=False, default="")
    #: 用户上传时的原始文件名，只用于列表页显示。
    file_name: Mapped[str] = mapped_column(String(255), nullable=False, default="")
    #: 列表页显示用；不复制 `years_experience` 之类的抽取字段成独立列——那是第二份真相，
    #: `parsed` 一改就漏同步。要按年限筛的时候走 JSON 提取或重建索引，别提前铺。
    name: Mapped[str] = mapped_column(String(120), nullable=False, default="", index=True)
    #: 抽取结果 JSON。见模块 docstring：整取整放，别改单元素。
    parsed: Mapped[dict[str, Any]] = mapped_column(JSON, nullable=False, default=dict)
    #: PDF 抽出来的纯文本，validate 与回放的唯一真相源。
    raw_text: Mapped[str] = mapped_column(Text, nullable=False, default="")
