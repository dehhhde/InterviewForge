"""技能抽取：词典命中 + 就近程度词，不调 LLM〔阶段 5 · §8.6.2〕

技能是四个抽取器里唯一"词典就够用"的一类：技术名有稳定写法（Spring Boot、MySQL、
Redis…），程度词（熟悉/掌握/精通）就写在名字前面。扫一遍既准又免费，没必要花 token。

代价是**词典外的名字一律不认**，所以命不中的拉丁词会进 `unmapped` 提示补词典——和
`sections.py` 里"像标题但没认出"是同一个思路：宁可提示漏抽，也不瞎猜技能名。

`level` 取"名字左边最近的那个程度词"：`熟悉 MySQL、PostgreSQL 索引原理` 里两个技能都
该算"熟悉"；`熟悉 Spring Boot 自动配置，掌握 Spring Cloud` 中间换了词，就按新的算。
名字右边出现的程度词不认——那多半是在描述别的对象（"Redis 缓存，熟悉其持久化机制"）。
"""

import re
from collections.abc import Iterable, Sequence
from dataclasses import dataclass, field

from app.extractors.base import collapse
from app.extractors.sections import split_sections
from app.schemas.resume import SkillItem, SkillLevel

#: 别名（小写比对）-> 展示用正名。紧凑写法与带空格写法都收，命中哪个都归到同一个名字。
SKILL_ALIASES: dict[str, str] = {
    # 语言与运行时
    "java": "Java",
    "jvm": "JVM",
    "kotlin": "Kotlin",
    "python": "Python",
    "go": "Go",
    "golang": "Go",
    "rust": "Rust",
    "c++": "C++",
    "c#": "C#",
    "javascript": "JavaScript",
    "typescript": "TypeScript",
    "shell": "Shell",
    "sql": "SQL",
    # 后端框架与生态
    "spring": "Spring",
    "spring boot": "Spring Boot",
    "springboot": "Spring Boot",
    "spring mvc": "Spring MVC",
    "springmvc": "Spring MVC",
    "spring cloud": "Spring Cloud",
    "springcloud": "Spring Cloud",
    "spring ai": "Spring AI",
    "springai": "Spring AI",
    "spring security": "Spring Security",
    "springsecurity": "Spring Security",
    "mybatis": "MyBatis",
    "mybatis-plus": "MyBatis-Plus",
    "mybatisplus": "MyBatis-Plus",
    "netty": "Netty",
    "dubbo": "Dubbo",
    "junit": "JUnit",
    "flowable": "Flowable",
    "nacos": "Nacos",
    "openfeign": "OpenFeign",
    "gateway": "Gateway",
    "oauth": "OAuth",
    "jwt": "JWT",
    "grpc": "gRPC",
    "restful": "RESTful",
    "rest": "RESTful",
    # 数据与缓存
    "mysql": "MySQL",
    "postgresql": "PostgreSQL",
    "postgres": "PostgreSQL",
    "oracle": "Oracle",
    "sqlserver": "SQL Server",
    "sql server": "SQL Server",
    "mongodb": "MongoDB",
    "redis": "Redis",
    "caffeine": "Caffeine",
    "elasticsearch": "Elasticsearch",
    "clickhouse": "ClickHouse",
    "minio": "MinIO",
    "navicat": "Navicat",
    # 消息队列
    "rabbitmq": "RabbitMQ",
    "rocketmq": "RocketMQ",
    "kafka": "Kafka",
    # 架构与方法论
    "ddd": "DDD",
    "微服务": "微服务",
    "分布式": "分布式",
    "高并发": "高并发",
    "设计模式": "设计模式",
    "领域建模": "领域建模",
    "领域驱动": "DDD",
    "mvcc": "MVCC",
    "rbac": "RBAC",
    # 运维、工具与前端
    "docker": "Docker",
    "k8s": "Kubernetes",
    "kubernetes": "Kubernetes",
    "jenkins": "Jenkins",
    "git": "Git",
    "github": "GitHub",
    "gitlab": "GitLab",
    "linux": "Linux",
    "nginx": "Nginx",
    "tomcat": "Tomcat",
    "maven": "Maven",
    "gradle": "Gradle",
    "idea": "IDEA",
    "pycharm": "PyCharm",
    "eclipse": "Eclipse",
    "vue": "Vue",
    "react": "React",
    "vite": "Vite",
    "html": "HTML",
    "css": "CSS",
    "android": "Android",
    "ios": "iOS",
    # AI 相关
    "mcp": "MCP",
    "agent": "Agent",
    "langchain": "LangChain",
    "langgraph": "LangGraph",
    "llm": "LLM",
    "openai": "OpenAI",
}

#: 程度词 -> schema 的档位。只认表里的词，表外一律当"没说程度"留 None——
#: 猜高一级会直接污染面试题的难度设定。
LEVEL_WORDS: dict[str, SkillLevel] = {
    "精通": "proficient",
    "专家": "proficient",
    "熟练运用": "familiar",
    "熟练": "familiar",
    "熟悉": "familiar",
    "掌握": "familiar",
    "擅长": "familiar",
    "运用": "basic",
    "了解": "basic",
    "会用": "basic",
}
#: 档位高低，供" JD 要精通、简历只写熟悉"这类比较复用（`match_service` 用它判 partial）
LEVEL_RANK: dict[str, int] = {"basic": 0, "familiar": 1, "proficient": 2}

#: 短到会成为英文碎片的别名，必须原样大小写命中（否则正文里小写的 "go" 也算一门语言）
_CASE_SENSITIVE = {"go", "r", "c"}
#: `unmapped` 里不配当技能名的杂词（网址碎片与通用缩写）
_IGNORED_TOKENS = frozenset(
    {
        "http", "https", "www", "com", "cn", "app", "web", "sdk", "api",
        "ide", "and", "the", "or", "qq",
    }
)
_TOKEN_MIN_CHARS = 3
#: `unmapped` 只是提示，太长会淹没 `errors` 通道
_MAX_UNMAPPED = 8

#: 词边界：拉丁别名两侧不能再接拉丁字母或数字，免得 "Go" 命中 "logo"、"Rest" 命中 "Restore"
_WORD_CHARS = "A-Za-z0-9"
_LATIN_TOKEN_RE = re.compile(r"[A-Za-z][A-Za-z0-9+#.\-]{2,}")


@dataclass(frozen=True)
class SkillResult:
    skills: list[SkillItem] = field(default_factory=list)
    notes: tuple[str, ...] = ()
    #: 长得像技术名、词典却没命中的词，提示该补 `SKILL_ALIASES`
    unmapped: tuple[str, ...] = ()

    def as_resume_fields(self) -> dict[str, object]:
        return {"skills": [s.model_dump(mode="json") for s in self.skills]}


def extract_skills(raw_text: str) -> SkillResult:
    """优先只扫"专业技能"栏目；没有该栏目时退回全文，并把退回这件事写进 notes。"""
    sections = split_sections(raw_text)
    lines = [collapse(line) for line in sections.lines_of("skills")]
    notes: list[str] = []
    if not lines:
        lines = [collapse(line) for line in raw_text.splitlines()]
        notes.append("没找到技能栏目，技能是从全文按词典硬扫出来的，可能把求职意向当成技能")

    found: dict[str, SkillItem] = {}
    leftovers: list[str] = []
    for line in lines:
        if not line:
            continue
        used: list[tuple[int, int]] = []
        for start, end, name in _hits(line):
            used.append((start, end))
            level = _level_before(line, start)
            previous = found.get(name.lower())
            candidate = SkillItem(name=name, level=level, evidence=line)
            found[name.lower()] = _richer(previous, candidate)
        leftovers.extend(_unknown_tokens(line, used))

    if not found:
        notes.append("词典一个技能都没命中，要么简历没写技能，要么该补 SKILL_ALIASES 了")
    return SkillResult(
        skills=list(found.values()),
        notes=tuple(notes),
        unmapped=tuple(_dedupe(leftovers)[:_MAX_UNMAPPED]),
    )


@dataclass(frozen=True)
class SkillMention:
    """一行里的一次技能提及：正名 + 左边最近的程度词（没有程度词时是 None）。"""

    name: str
    level: SkillLevel | None


def skill_mentions_in(line: str) -> list[SkillMention]:
    """一行提到了哪些技能。与 `extract_skills` 同一套判据，但**不产出证据、不写进简历**。

    JD 侧复用它是刻意的：岗位要求与简历技能用同一本别名词典归一，"Springboot" 和
    "Spring Boot" 才会算成同一项要求；否则缺口清单里全是假差集。
    """
    mentions: list[SkillMention] = []
    for start, _end, name in _hits(line):
        mentions.append(SkillMention(name=name, level=_level_before(line, start)))
    return mentions


def _alias_patterns() -> list[tuple[str, re.Pattern[str]]]:
    """(正名, 编译好的模式)，按别名长度从长到短排——`Spring Boot` 得抢在 `Spring` 前面。"""
    ordered = sorted(SKILL_ALIASES.items(), key=lambda item: len(item[0]), reverse=True)
    out: list[tuple[str, re.Pattern[str]]] = []
    for alias, name in ordered:
        escaped = re.escape(alias)
        if alias in _CASE_SENSITIVE:
            pattern = rf"(?<![{_WORD_CHARS}]){re.escape(name)}(?![{_WORD_CHARS}])"
            out.append((name, re.compile(pattern)))
            continue
        # 别名里的空格在 PDF 里可能被拉开，容忍空白与连字符的混合
        flexible = escaped.replace(r"\ ", r"[\s\-]?")
        pattern = rf"(?<![{_WORD_CHARS}]){flexible}(?![{_WORD_CHARS}])"
        out.append((name, re.compile(pattern, re.IGNORECASE)))
    return out


_ALIAS_PATTERNS = _alias_patterns()


def _hits(line: str) -> Iterable[tuple[int, int, str]]:
    """一行里命中的技能（位置 + 正名）。已经属于更长别名的片段不再算第二次。"""
    taken: list[tuple[int, int]] = []
    for name, pattern in _ALIAS_PATTERNS:
        for match in pattern.finditer(line):
            span = match.span()
            if _overlaps(span, taken):
                continue
            taken.append(span)
            yield span[0], span[1], name


def _overlaps(span: tuple[int, int], taken: Sequence[tuple[int, int]]) -> bool:
    return any(span[0] < end and start < span[1] for start, end in taken)


def _level_before(line: str, index: int) -> SkillLevel | None:
    """技能名左边最近的程度词。"""
    best: tuple[int, SkillLevel] | None = None
    head = line[:index]
    for word in LEVEL_WORDS:
        position = head.rfind(word)
        if position >= 0 and (best is None or position > best[0]):
            best = (position, LEVEL_WORDS[word])
    return best[1] if best else None


def _unknown_tokens(line: str, used: Sequence[tuple[int, int]]) -> list[str]:
    """像产品名却没进词典的拉丁词——留着当"该补词典"的线索，绝不当技能输出。"""
    out: list[str] = []
    for match in _LATIN_TOKEN_RE.finditer(line):
        span = match.span()
        if _overlaps(span, used):
            continue
        token = match.group()
        if token.lower() in _IGNORED_TOKENS or len(token) < _TOKEN_MIN_CHARS:
            continue
        if token.islower():
            continue  # 全小写多半是英文碎词（"the web app"），不是产品名
        out.append(token)
    return out


def _dedupe(tokens: Iterable[str]) -> list[str]:
    seen: set[str] = set()
    out: list[str] = []
    for token in tokens:
        key = token.lower()
        if key not in seen:
            seen.add(key)
            out.append(token)
    return out


def _richer(previous: SkillItem | None, candidate: SkillItem) -> SkillItem:
    """同一技能在多处出现时留程度更高的那条（证据也就更硬）。"""
    if previous is None:
        return candidate
    left = LEVEL_RANK.get(previous.level or "", -1)
    right = LEVEL_RANK.get(candidate.level or "", -1)
    return candidate if right > left else previous
