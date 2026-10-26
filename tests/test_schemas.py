"""契约层测试：ResumeSchema / QuestionSchema 各三条——正常、缺必填、幻觉脏数据。

只断言结构与规则，不依赖任何 LLM 输出。
"""

import json
from pathlib import Path
from typing import Any

import pytest
from pydantic import ValidationError

from app.schemas.question import QuestionSchema
from app.schemas.resume import ResumeSchema, SkillItem

FIXTURES = Path(__file__).parent / "fixtures"


def _resume_payload(**overrides: Any) -> dict[str, Any]:
    payload: dict[str, Any] = {
        "name": "测试候选人",
        "years_experience": 3,
        "skills": [
            {"name": "Python", "level": "熟悉", "years": 2, "evidence": "三年 Python 后端"}
        ],
    }
    payload.update(overrides)
    return payload


def test_resume_accepts_clean_extraction() -> None:
    payload = json.loads((FIXTURES / "resume_ok.json").read_text(encoding="utf-8"))
    resume = ResumeSchema.model_validate(payload)

    assert resume.phone == "8613800138000"
    assert [s.level for s in resume.skills] == ["proficient", "familiar", None]
    assert resume.educations[0].degree == "bachelor"
    assert resume.work_experiences[1].end is None
    assert resume.projects[0].highlights


def test_resume_rejects_missing_required_fields() -> None:
    with pytest.raises(ValidationError) as exc:
        ResumeSchema.model_validate({"email": "a@b.com"})
    missing = {e["loc"][0] for e in exc.value.errors() if e["type"] == "missing"}
    assert {"name", "years_experience"} <= missing

    with pytest.raises(ValidationError, match="evidence"):
        ResumeSchema.model_validate(_resume_payload(skills=[{"name": "Go"}]))


def test_resume_rejects_hallucinated_or_normalizes_dirty_data() -> None:
    with pytest.raises(ValidationError, match="倒挂"):
        ResumeSchema.model_validate(
            _resume_payload(
                work_experiences=[
                    {
                        "company": "某公司",
                        "start": "2024-06-01",
                        "end": "2023-01-01",
                        "evidence": "2023.01-2024.06 某公司",
                    }
                ]
            )
        )

    # 电话里塞了非数字、学历用了词典外的词：前者是抽取错误，后者按 other 兜底
    with pytest.raises(ValidationError, match="电话号码"):
        ResumeSchema.model_validate(_resume_payload(phone="待补充"))
    resume = ResumeSchema.model_validate(
        _resume_payload(
            educations=[{"school": "某大学", "degree": "中专", "evidence": "某大学 中专"}]
        )
    )
    assert resume.educations[0].degree == "other"


def test_resume_merges_duplicate_skills_keeping_richer() -> None:
    resume = ResumeSchema.model_validate(
        _resume_payload(
            skills=[
                SkillItem(name="Redis", level="basic", years=1, evidence="了解 Redis"),
                SkillItem(name="redis", level="proficient", years=5, evidence="精通 Redis 内核"),
            ]
        )
    )
    assert len(resume.skills) == 1
    assert (resume.skills[0].level, resume.skills[0].years) == ("proficient", 5)


def test_question_accepts_evidenced_resume_question() -> None:
    question = QuestionSchema.model_validate(
        {
            "stem": "你提到库存扣减 P99 从 180ms 降到 45ms，具体是怎么做到的？",
            "difficulty": "medium",
            "source": "resume",
            "evidence": "扣减接口 P99 从 180ms 降到 45ms",
            "checkpoints": ["定位瓶颈的方法", "缓存与原子操作的取舍"],
            "reference_answer": "先压测定位……",
            "rubric": ["说清压测与指标得 1 分", "谈到一致性风险得 1 分"],
            "follow_ups": ["上线后如何防止回退？"],
        }
    )
    assert question.difficulty == "medium"
    assert len(question.checkpoints) == 2


def test_question_rejects_missing_required_fields() -> None:
    with pytest.raises(ValidationError) as exc:
        QuestionSchema.model_validate({"difficulty": "easy"})
    missing = {e["loc"][0] for e in exc.value.errors() if e["type"] == "missing"}
    assert {"stem", "checkpoints", "reference_answer"} <= missing

    with pytest.raises(ValidationError, match="checkpoints"):
        QuestionSchema.model_validate(
            {
                "stem": "讲讲你的项目",
                "difficulty": "easy",
                "checkpoints": [],
                "reference_answer": "……",
            }
        )


def test_question_rejects_unsourced_resume_claim() -> None:
    base: dict[str, Any] = {
        "stem": "你的 Kafka 消费积压是怎么处理的？",
        "difficulty": "hard",
        "checkpoints": ["积压定位"],
        "reference_answer": "……",
        "rubric": ["能说出重平衡策略得 1 分"],
    }
    with pytest.raises(ValidationError, match="evidence"):
        QuestionSchema.model_validate({**base, "source": "resume", "evidence": "  "})

    with pytest.raises(ValidationError, match="空白条目"):
        QuestionSchema.model_validate({**base, "rubric": [" "]})
