"""seed 灌库：幂等 + 数据自洽〔阶段 3 · M3〕

`app/scripts/seed.py` 的 docstring 承诺了三件事，这里一一对应地验：
反复跑结果一致、不用真实简历、`raw_text` 与 `parsed` 自洽。

最后一条最容易被跳过，也最贵：种子数据要是过不了自己的校验层，
M4 之后所有"规则判出了脏数据"的结论都得先怀疑是不是种子的锅。
"""

from __future__ import annotations

import re

from sqlalchemy import func, select

from app.extractors.base import evidence_belongs
from app.extractors.timeline_extractor import extract_timeline
from app.models.job import Job
from app.models.resume import Resume
from app.schemas.resume import ResumeSchema
from app.scripts.seed import _SEED_RESUMES, seed
from app.services.resume_service import validate_resume


async def test_seed_loads_two_jobs_and_three_resumes(session) -> None:
    written = await seed(session)
    assert written == {"jobs": 2, "resumes": 3}
    assert (await session.execute(select(func.count()).select_from(Job))).scalar_one() == 2
    assert (await session.execute(select(func.count()).select_from(Resume))).scalar_one() == 3


async def test_running_seed_twice_changes_nothing(session) -> None:
    """M3 验收里的"重启进程后数据仍在"要靠这条才成立：不幂等的 seed 会把库越灌越脏。"""
    await seed(session)
    first = {
        row.id: (row.name, row.raw_text, row.parsed)
        for row in (await session.execute(select(Resume))).scalars()
    }
    await seed(session)
    assert (await session.execute(select(func.count()).select_from(Resume))).scalar_one() == 3
    assert (await session.execute(select(func.count()).select_from(Job))).scalar_one() == 2
    second = {
        row.id: (row.name, row.raw_text, row.parsed)
        for row in (await session.execute(select(Resume))).scalars()
    }
    assert second == first


async def test_resume_ids_are_the_deterministic_seed_ones(session) -> None:
    await seed(session)
    ids = {row.id for row in (await session.execute(select(Resume))).scalars()}
    assert ids == {"seed-resume-1", "seed-resume-2", "seed-resume-3"}


def test_every_evidence_is_verbatim_in_the_source_text() -> None:
    """无证据即丢弃是抽取层的铁律，种子数据没有理由例外。"""
    for row in _SEED_RESUMES:
        parsed = ResumeSchema.model_validate(row["parsed"])
        items = (
            parsed.skills
            + parsed.educations
            + parsed.work_experiences
            + parsed.projects
        )
        assert items, row["id"]
        for item in items:
            assert evidence_belongs(row["raw_text"], item.evidence), (row["id"], item.evidence)


def test_seed_resumes_pass_the_validation_rules() -> None:
    for row in _SEED_RESUMES:
        outcome = validate_resume(row["raw_text"], row["parsed"])
        assert outcome.issues == (), (row["id"], outcome.issues)
        # `repairs` 非空说明种子数据本身不完整（比如年限是占位的 0）。
        assert outcome.repairs == (), (row["id"], outcome.repairs)


def test_declared_years_match_the_timeline() -> None:
    """`years_experience` 必须能被原文的时间线复算出来，容差 1 年。

    这条不是形式主义：栏目名不独占一行时 `extract_timeline` 会一个区间都找不到，
    于是年限"看起来正常"却没有任何证据。曾经就踩过，注释里写着判据。
    """
    for row in _SEED_RESUMES:
        timeline = extract_timeline(row["raw_text"])
        assert timeline.work_experiences, row["id"]
        assert timeline.years_experience is not None, row["id"]
        # `Evidence.value` 是字符串（抽取层与 LLM 打交道的统一形态），这里要比数值。
        computed = float(timeline.years_experience.value)
        assert abs(row["parsed"]["years_experience"] - computed) <= 1.0


def test_seed_uses_no_real_personal_data() -> None:
    """合成样本：不许出现仓库里的真简历人名，也不许出现看起来像手机号的长数字串。"""
    for row in _SEED_RESUMES:
        assert row["name"] in {"林可", "周砚", "苏澈"}
        assert not re.search(r"1[3-9]\d{9}", row["raw_text"])
        assert "@" not in row["raw_text"]


def test_job_requirements_are_lowercased_skill_tokens() -> None:
    """match 节点要拿它和 `parsed["skills"][*]["name"]` 求差集，大小写不统一就会漏匹配。"""
    from app.scripts.seed import _JOBS

    for job in _JOBS:
        assert job["required_skills"], job["title"]
        assert all(skill == skill.lower() for skill in job["required_skills"])
