"""种子数据灌库〔阶段 3 · §6.7〕

跑法（`PYTHONPATH=.` 不能省，`app.scripts` 不是已安装包）：

    PYTHONIOENCODING=utf-8 PYTHONPATH=. .venv/Scripts/python.exe -m app.scripts.seed

三条设计约束：

1. **幂等**。岗位按自然键 `(title, company)` upsert，简历用**固定 id**（`seed-resume-*`）
   先查后写。M3 验收里"重启进程后数据仍在"要靠反复跑这个脚本来验，
   跑一次插一份、跑两次插两份的数据比没数据更糟。
2. **不用真实简历**。这三份是手写的合成样本，姓名/公司/学校全是编的。
   真实样本（`tests/fixtures/resumes/`）带 PII，不进开发库，只进测试的临时库。
3. **`raw_text` 与 `parsed` 必须自洽**：每个 `evidence` 都是 `raw_text` 里的逐字片段。
   M4 之后的规则校验会拿原文核对证据，种子数据要是编不出处，
   就会被自己的校验层判成脏数据，届时分不清是规则的锅还是种子的锅。
"""

from __future__ import annotations

import asyncio
from datetime import date
from typing import Any

from app.config import get_settings
from app.dependencies import dispose_engine, get_sessionmaker
from app.repositories.job_repository import JobRepository
from app.repositories.resume_repository import ResumeRepository
from app.schemas.resume import ResumeSchema
from app.utils.db import session_scope

#: 两份岗位：一份压 Java 后端（与种子简历 1、2 有交集），一份压前端（只有简历 3 对得上）。
_JOBS: tuple[dict[str, Any], ...] = (
    {
        "title": "后端开发工程师",
        "company": "蓝鲸云计算",
        "min_years": 3.0,
        "required_skills": ["java", "spring boot", "mysql", "redis"],
        "jd_text": (
            "岗位职责：负责核心业务的后端服务设计与实现。\n"
            "任职要求：精通 Java，熟悉 Spring Boot；掌握 MySQL 的索引与事务；"
            "了解 Redis 缓存设计；有分布式经验优先。"
        ),
    },
    {
        "title": "前端开发工程师",
        "company": "灯塔智能",
        "min_years": 1.0,
        "required_skills": ["typescript", "react", "css"],
        "jd_text": (
            "岗位职责：负责产品前端界面与交互实现。\n"
            "任职要求：熟练使用 TypeScript 与 React；理解 CSS 布局；"
            "关注性能与可访问性。"
        ),
    },
)


def _resume(
    resume_id: str,
    *,
    name: str,
    raw_text: str,
    years: float,
    skills: list[tuple[str, str]],
    school: tuple[str, str, int, int],
    work: tuple[str, str, date, date],
    project: tuple[str, str],
) -> dict[str, Any]:
    """把一份合成简历摊成 `{id, name, stored_path, file_name, raw_text, parsed}`。

    `parsed` 走 `ResumeSchema` 而不是裸 dict：种子数据一旦能绕过 schema，
    下游就会习惯"库里什么东西都可能有"，那 layer 校验就白写了。
    """
    school_name, degree, start_year, graduate_year = school
    company, role, start, end = work
    project_name, project_summary = project
    schema = ResumeSchema.model_validate(
        {
            "name": name,
            "years_experience": years,
            "skills": [
                {"name": skill_name, "level": level, "evidence": skill_name}
                for skill_name, level in skills
            ],
            "educations": [
                {
                    "school": school_name,
                    "degree": degree,
                    "major": "计算机科学与技术",
                    "start_year": start_year,
                    "graduate_year": graduate_year,
                    "evidence": school_name,
                }
            ],
            "work_experiences": [
                {
                    "company": company,
                    "role": role,
                    "start": start,
                    "end": end,
                    "evidence": company,
                }
            ],
            "projects": [
                {"name": project_name, "summary": project_summary, "evidence": project_name}
            ],
        }
    )
    return {
        "id": resume_id,
        "name": name,
        "stored_path": f"seed/{resume_id}.md",
        "file_name": f"{name}.md",
        "raw_text": raw_text,
        "parsed": schema.model_dump(mode="json"),
    }


_SEED_RESUMES: tuple[dict[str, Any], ...] = (
    _resume(
        "seed-resume-1",
        name="林可",
        # 栏目名必须独占一行、不含数字且短于 12 字（`sections.header_kind` 的三条守卫），
        # 否则 `extract_timeline` 一个区间都找不到——见本文件 docstring 第 3 条。
        raw_text=(
            "林可 · 后端开发工程师\n"
            "\n"
            "教育背景\n"
            "西湖大学 计算机科学与技术 本科 2016-2020\n"
            "\n"
            "工作经历\n"
            "蓝鲸云计算  Java 后端工程师  2020-07 至 2023-06\n"
            "\n"
            "项目经历\n"
            "订单中心重构  负责拆库拆表与缓存一致性\n"
            "\n"
            "专业技能\n"
            "Java、Spring Boot、MySQL、Redis\n"
        ),
        years=3.0,
        skills=[("Java", "proficient"), ("Spring Boot", "proficient"), ("MySQL", "familiar")],
        school=("西湖大学", "bachelor", 2016, 2020),
        work=("蓝鲸云计算", "Java 后端工程师", date(2020, 7, 1), date(2023, 6, 30)),
        project=("订单中心重构", "负责拆库拆表与缓存一致性"),
    ),
    _resume(
        "seed-resume-2",
        name="周砚",
        raw_text=(
            "周砚 · 资深后端\n"
            "\n"
            "教育背景\n"
            "南山大学 软件工程 硕士 2014-2017\n"
            "\n"
            "工作经历\n"
            "星辰支付  后端工程师  2019-03 至 2024-02\n"
            "\n"
            "项目经历\n"
            "清算对账平台  日均千万级流水\n"
            "\n"
            "专业技能\n"
            "Java、MySQL、Redis、Kafka\n"
        ),
        years=5.0,
        skills=[("Java", "proficient"), ("MySQL", "proficient"), ("Redis", "familiar")],
        school=("南山大学", "master", 2014, 2017),
        work=("星辰支付", "后端工程师", date(2019, 3, 1), date(2024, 2, 29)),
        project=("清算对账平台", "日均千万级流水"),
    ),
    _resume(
        "seed-resume-3",
        name="苏澈",
        raw_text=(
            "苏澈 · 前端\n"
            "\n"
            "教育背景\n"
            "北辰学院 数字媒体 本科 2021-2025\n"
            "\n"
            "工作经历\n"
            "灯塔智能  前端实习生  2024-07 至 2025-06\n"
            "\n"
            "项目经历\n"
            "组件库建设  沉淀 40 个业务组件\n"
            "\n"
            "专业技能\n"
            "TypeScript、React、CSS\n"
        ),
        years=1.0,
        skills=[("TypeScript", "familiar"), ("React", "proficient"), ("CSS", "proficient")],
        school=("北辰学院", "bachelor", 2021, 2025),
        work=("灯塔智能", "前端实习生", date(2024, 7, 1), date(2025, 6, 30)),
        project=("组件库建设", "沉淀 40 个业务组件"),
    ),
)


async def seed(session: Any) -> dict[str, int]:
    """灌 2 个岗位 + 3 份简历，返回每种写了几行。反复调用结果一致。"""
    jobs = JobRepository(session)
    resumes = ResumeRepository(session)
    written = {"jobs": 0, "resumes": 0}

    for payload in _JOBS:
        existing = await jobs.get_by_title_and_company(payload["title"], payload["company"])
        if existing is None:
            await jobs.create(**payload)
        else:
            await jobs.update(existing.id, **payload)
        written["jobs"] += 1

    for payload in _SEED_RESUMES:
        row = dict(payload)
        resume_id = row.pop("id")
        if await resumes.get_by_id(resume_id) is None:
            # id 是显式给的（seed-resume-*），不是 uuid 默认值——这就是幂等的全部机制。
            await resumes.create(id=resume_id, **row)
        else:
            await resumes.update(resume_id, **row)
        written["resumes"] += 1

    return written


async def _main() -> None:
    settings = get_settings()
    try:
        async with session_scope(get_sessionmaker()) as session:
            written = await seed(session)
        print(f"种子数据完成：岗位 {written['jobs']} 份，简历 {written['resumes']} 份")
        print(f"库：{settings.database_url}")
        # 不打印行数以外的内容：库里是简历，控制台是 GBK，个人信息不该往日志里刷。
    finally:
        await dispose_engine()


if __name__ == "__main__":
    asyncio.run(_main())
