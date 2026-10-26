"""题目 DTO：题干 / 考察点 / 参考答案 / 评分标准 / 追问 / evidence〔阶段 7 · §10.4.6〕

`generate` 的产出契约，也是 `review` 打分的对象。两条硬规则：
1. `source="resume"` 必须带 `evidence`：声称基于简历出题，就得能指回原文一句。
2. 考察点与评分标准不许为空：缺 rubric 的题目只能靠语感判等，`review` 无法客观化。
"""

from typing import Literal

from pydantic import BaseModel, Field, model_validator

Difficulty = Literal["easy", "medium", "hard"]
QuestionSource = Literal["resume", "jd", "generic"]


class QuestionSchema(BaseModel):
    """一道面试题。"""

    stem: str = Field(min_length=6)
    difficulty: Difficulty
    source: QuestionSource = "generic"
    evidence: str = ""
    checkpoints: list[str] = Field(min_length=1)
    reference_answer: str = Field(min_length=1)
    rubric: list[str] = Field(default_factory=list)
    follow_ups: list[str] = Field(default_factory=list)

    @model_validator(mode="after")
    def _check_source_evidence(self) -> "QuestionSchema":
        if self.source == "resume" and len(self.evidence.strip()) < 4:
            raise ValueError(f"source=resume 但 evidence 不足：{self.evidence!r}")
        return self

    @model_validator(mode="after")
    def _check_list_items_not_blank(self) -> "QuestionSchema":
        for label, items in (
            ("考察点", self.checkpoints),
            ("评分标准", self.rubric),
            ("追问", self.follow_ups),
        ):
            for item in items:
                if not item.strip():
                    raise ValueError(f"{label}里存在空白条目")
        return self
