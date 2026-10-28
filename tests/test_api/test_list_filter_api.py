r"""列表端的 `total` 与关键词筛选〔S3 · T-303 / OPT-6.3〕

这一张卡换掉的是前端"拉 200 条再本地过滤"那个凑出来的读法，所以要钉的是三件事：

1. **`total` 数的是过滤后的行数**，不是这一页给了几条，也不是全表——分页控件拿它算页数，
   三者任何一个错了页脚就会和列表各说各话；
2. **关键词里的 `%`、`_`、`\` 是字面量**。它们是 LIKE 的通配符，不转义就等于搜索框里打
   一个 `%` 就能"命中全部"，那已经不是搜索而是让用户手写 SQL 的模式串；
3. **搜索只打在标识列上**（姓名 / 文件名 / 岗位名 / 公司名 / 报告头的四列快照），
   `raw_text`、`parsed`、`jd_text` 是正文，不在其中——这条由服务层传给 `keyword_filter`
   的列名决定，用例把它钉住，以后"顺手加一列"就得先想清楚加的是不是正文。

断言只落在结构与数量上：命中几条、id 是哪个，不碰任何渲染文案。
"""

from __future__ import annotations

import pytest
from httpx import AsyncClient

from app.dependencies import get_sessionmaker
from app.repositories.interview_repository import InterviewRepository
from app.repositories.job_repository import JobRepository
from app.repositories.resume_repository import ResumeRepository
from app.utils.db import session_scope

#: 三个列表端点 + 各自的搜索列与建行方式。放在一张表里是为了让"每条契约三个端点都成立"
#: 这句话有证据，而不是只在简历那一条上测过就推广。
_RESUMES = "/api/v1/resumes/list"
_JOBS = "/api/v1/jobs"
_INTERVIEWS = "/api/v1/interviews/list"


async def _seed_resumes(*names: str) -> None:
    async with session_scope(get_sessionmaker()) as session:
        for name in names:
            await ResumeRepository(session).create(
                name=name, raw_text=f"{name} 的简历原文", file_name=f"{name}.pdf"
            )


async def _seed_jobs(*titles: str) -> None:
    async with session_scope(get_sessionmaker()) as session:
        for title in titles:
            await JobRepository(session).create(
                title=title, company="示例公司", jd_text="精通 Python", required_skills=[]
            )


async def _seed_reports(*pairs: tuple[str, str]) -> str:
    """按报告头的四列快照建行，返回那一份共同的简历 id。

    两份报告挂**同一份名为"占位"的简历**：快照列与 `resume.name` 故意不同名，
    于是"回 join 认出身"的实现会立刻搜不到东西，而读自己列的实现照常命中。
    `job_id` 留空是合法的（`SET NULL`，岗位可以已被下线）——所以这里**不建 `job` 行**
    也能按岗位名搜到报告，这正是 ADR-0013 要的形状。
    """
    async with session_scope(get_sessionmaker()) as session:
        owner = await ResumeRepository(session).create(
            name="占位", raw_text="占位简历原文", file_name="占位.pdf"
        )
        for resume_name, job_title in pairs:
            await InterviewRepository(session).create(
                resume_id=owner.id,
                job_id=None,
                resume_name=resume_name,
                resume_file_name=f"{resume_name}.pdf",
                job_title=job_title,
                job_company="示例公司",
                status="completed",
            )
        return owner.id


# --- total 与 items 同源 ---


async def test_total_counts_the_filtered_rows_not_the_page(client: AsyncClient) -> None:
    """`limit=1` 只砍 `items`，`total` 仍是过滤后的总数——分页控件靠这两个数算页数。"""
    await _seed_resumes("甲", "乙", "丙")

    body = (await client.get(_RESUMES, params={"limit": 1, "q": "甲"})).json()

    assert len(body["items"]) == 1
    assert body["total"] == 1

    unfiltered = (await client.get(_RESUMES, params={"limit": 1})).json()
    assert len(unfiltered["items"]) == 1
    assert unfiltered["total"] == 3


@pytest.mark.parametrize(
    ("path", "keyword"),
    [(_RESUMES, "简历甲"), (_JOBS, "岗位甲"), (_INTERVIEWS, "报甲")],
    ids=["resumes", "jobs", "interviews"],
)
async def test_each_list_endpoint_filters_by_its_keyword(
    client: AsyncClient, path: str, keyword: str
) -> None:
    """三个端点吃同一个参数、同一份条件：只改一个等于把另外两个留在旧行为上。

    三张表各铺两行、其中一行含关键词，且**前缀互不相同**（简历/岗位/报），所以一个关键词
    不会串到另一张表上；而"另外两个端点没接上筛选"会表现为 `total == 2`，
    不会因为库里没数据而假绿。
    """
    await _seed_resumes("简历甲", "简历乙")
    await _seed_jobs("岗位甲", "岗位乙")
    await _seed_reports(("报甲", "题甲"), ("报乙", "题乙"))

    body = (await client.get(path, params={"q": keyword})).json()

    assert len(body["items"]) == 1
    assert body["total"] == 1


# --- LIKE 通配符必须是字面量 ---


@pytest.mark.parametrize("wildcard", ["%", "_", r"\%"], ids=["percent", "underscore", "backslash"])
async def test_a_wildcard_keyword_matches_only_rows_that_literally_contain_it(
    client: AsyncClient, wildcard: str
) -> None:
    """搜 `%` 不许"命中全部"，搜 `_` 不许替掉一个字符——转义漏了就是这两条红。

    只打在简历端点上测：转义在 `repositories.base.contains()` 一处做完，三个端点共用同一个
    条件构造器，换个端点测的是同一段代码。
    """
    await _seed_resumes("林可", f"怪名字{wildcard}号")

    body = (await client.get(_RESUMES, params={"q": wildcard})).json()

    assert body["total"] == 1
    assert len(body["items"]) == 1


async def test_an_underscore_keyword_does_not_stand_for_one_character(client: AsyncClient) -> None:
    """`甲_乙` 若被当模式串会命中"甲X乙"，而它本该只命中字面写法。"""
    await _seed_resumes("甲X乙", "甲_乙")

    body = (await client.get(_RESUMES, params={"q": "甲_乙"})).json()

    assert body["total"] == 1
    assert body["items"][0]["name"] == "甲_乙"


# --- 搜索列的边界 ---


async def test_keyword_matches_the_file_name_too(client: AsyncClient) -> None:
    """`file_name` 也是标识：只搜 `name` 的话，改了展示名就搜不到自己上传的那个文件。"""
    await _seed_resumes("林可")

    body = (await client.get(_RESUMES, params={"q": "林可.pdf"})).json()

    assert body["total"] == 1


async def test_report_search_reads_its_own_snapshot_not_the_source_row(client: AsyncClient) -> None:
    """搜报告打的是 `interview` 行的四列快照，不是回 join 认出来的出身（ADR-0013）。

    两个方向一起断：`resume` 行现在叫"占位"，快照里叫"林可"——按"林可"搜得到、按"占位"搜不到，
    才说明读的是快照。只断前一半的话，"回 join"的实现也可能碰巧命中。
    """
    await _seed_reports(("林可", "后端工程师"))

    by_snapshot = (await client.get(_INTERVIEWS, params={"q": "林可"})).json()
    by_current_name = (await client.get(_INTERVIEWS, params={"q": "占位"})).json()
    by_job_title = (await client.get(_INTERVIEWS, params={"q": "工程师"})).json()

    assert by_snapshot["total"] == 1
    assert by_current_name["total"] == 0
    assert by_job_title["total"] == 1


async def test_report_search_also_matches_its_own_id(client: AsyncClient) -> None:
    """贴一段 report id 也能定位那一行。

    旧的本地过滤就搜 `report.id`（`ReportsView` 的占位文案至今写着"报告 id"），换成服务端
    筛选不能把这项能力 quietly 摘掉。id 是主键、不是正文，所以它进白名单没有额外代价；
    片段匹配靠 `LIKE '%…%'`，uuid hex 的前缀中段都成立。
    """
    await _seed_reports(("林可", "后端工程师"))
    listed = (await client.get(_INTERVIEWS)).json()
    interview_id = listed["items"][0]["id"]

    whole = (await client.get(_INTERVIEWS, params={"q": interview_id})).json()
    fragment = (await client.get(_INTERVIEWS, params={"q": interview_id[8:16]})).json()

    assert whole["total"] == 1
    assert fragment["total"] == 1
    assert fragment["items"][0]["id"] == interview_id


async def test_search_never_reads_the_body_columns(client: AsyncClient) -> None:
    """正文不是搜索目标：`raw_text` 里写着"Kafka"不等于这份简历能被搜成 Kafka。

    这一条挡的是"顺手把 `raw_text` / `parsed` / `jd_text` 加进搜索列"——SQLite 上没有全文索引，
    那种 LIKE 会整表扫，而且会把"搜一个人的名字"变成"搜所有提过这词的正文"。
    """
    async with session_scope(get_sessionmaker()) as session:
        await ResumeRepository(session).create(
            name="林可", raw_text="精通 Kafka 与 Flink", file_name="林可.pdf"
        )
        await JobRepository(session).create(
            title="后端工程师", company="示例公司", jd_text="负责 Kafka 集群", required_skills=[]
        )

    assert (await client.get(_RESUMES, params={"q": "Kafka"})).json()["total"] == 0
    assert (await client.get(_JOBS, params={"q": "Kafka"})).json()["total"] == 0


# --- 空关键词 = 不加条件 ---


@pytest.mark.parametrize("blank", ["", "   "], ids=["empty", "whitespace"])
async def test_a_blank_keyword_lists_everything(client: AsyncClient, blank: str) -> None:
    """搜索框清空就是回到"全量分页"，不许把空串当成一次"搜空串"命中 0 条。"""
    await _seed_resumes("甲", "乙")

    body = (await client.get(_RESUMES, params={"q": blank})).json()

    assert body["total"] == 2
    assert len(body["items"]) == 2


# --- 参数上限 ---


async def test_an_overlong_keyword_is_rejected_before_the_query(client: AsyncClient) -> None:
    """64 字是契约的一部分：越线在进服务层之前就 422，而不是拿去扫一遍 LIKE。"""
    response = await client.get(_RESUMES, params={"q": "林" * 65})

    assert response.status_code == 422
    assert response.json()["error"]["code"] == "validation_failed"
