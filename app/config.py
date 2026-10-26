"""全局配置管理 Pydantic Settings〔阶段 2 · §5.4〕

两个来源，靠优先级合成一个 `Settings`：
init 参数 > 进程环境变量 > `.env`（密钥/连接串）
> `configs/<APP_ENV>.yaml`（行为参数） > 代码默认值。

构造一律走 `get_settings()`：它负责挑选 yaml 文件并校验 `APP_ENV` 与
yaml 里的 `app.env` 是否一致。直接 `Settings(...)` 可能让行为参数来自
另一套环境的 yaml，因此 `get_settings()` 会抛错拦下这种组合。
"""

import os
from collections.abc import Mapping
from functools import lru_cache
from pathlib import Path
from typing import Any, Literal, cast, get_args

import yaml
from dotenv import dotenv_values
from pydantic import BaseModel, Field, SecretStr, field_validator, model_validator
from pydantic.fields import FieldInfo
from pydantic_settings import BaseSettings, PydanticBaseSettingsSource, SettingsConfigDict

PROJECT_ROOT = Path(__file__).resolve().parents[1]
CONFIG_DIR = PROJECT_ROOT / "configs"
ENV_FILE = PROJECT_ROOT / ".env"

EnvName = Literal["development", "testing", "production"]
LogLevel = Literal["DEBUG", "INFO", "WARNING", "ERROR", "CRITICAL"]
_VALID_ENVS: tuple[str, ...] = get_args(EnvName)
_PARAM_SECTIONS = ("llm", "agent", "question", "retrieval")


class LlmParams(BaseModel):
    """LLM 调用护栏，来自 yaml 的 `llm:` 段。"""

    request_timeout_seconds: int = Field(default=30, gt=0)
    max_concurrency: int = Field(default=4, gt=0)
    default_max_tokens: int = Field(default=2048, gt=0)
    transport_max_attempts: int = Field(default=3, ge=1, le=10)


class AgentParams(BaseModel):
    """LangGraph 工作流上限与重试，来自 yaml 的 `agent:` 段。"""

    recursion_limit: int = Field(default=25, gt=1)
    validate_max_retries: int = Field(default=2, ge=0)
    #: 单次运行的 token 护栏（prompt+completion 合计）。M5 实测一轮端到端约 157k
    #: （33 次调用），默认 200k 是"正常跑不碰线、跑偏烧穿前掐断"的位置。
    #: 判定点在 `generate` 扇出之前——那是唯一"还没花出题的大钱"的时刻。
    max_tokens_per_run: int = Field(default=200_000, gt=0)


class DifficultyRatio(BaseModel):
    """题目难度配比，三项之和必须为 1。"""

    easy: float = Field(default=0.3, ge=0, le=1)
    medium: float = Field(default=0.5, ge=0, le=1)
    hard: float = Field(default=0.2, ge=0, le=1)

    @model_validator(mode="after")
    def _check_sum(self) -> "DifficultyRatio":
        total = self.easy + self.medium + self.hard
        if abs(total - 1) > 0.01:
            raise ValueError(f"difficulty_ratio 三项之和须为 1，当前 {total:.2f}")
        return self


class QuestionParams(BaseModel):
    """出题数量与难度，来自 yaml 的 `question:` 段。"""

    total: int = Field(default=15, gt=0)
    difficulty_ratio: DifficultyRatio = Field(default_factory=DifficultyRatio)


class RetrievalParams(BaseModel):
    """向量检索的召回与去重阈值，来自 yaml 的 `retrieval:` 段。"""

    top_k_coarse: int = Field(default=20, gt=0)
    top_k_final: int = Field(default=5, gt=0)
    dedup_similarity_threshold: float = Field(default=0.85, ge=0, le=1)

    @model_validator(mode="after")
    def _check_top_k(self) -> "RetrievalParams":
        if self.top_k_final > self.top_k_coarse:
            raise ValueError("top_k_final 不能大于 top_k_coarse")
        return self


class _YamlSettingsSource(PydanticBaseSettingsSource):
    """把 yaml 的顶层段映射成 Settings 字段，作为最低优先级的显式来源。"""

    def __init__(self, settings_cls: type[BaseSettings], data: Mapping[str, Any]) -> None:
        super().__init__(settings_cls)
        self._data = dict(data)

    def get_field_value(self, field: FieldInfo, field_name: str) -> tuple[Any, str, bool]:
        return self._data.get(field_name), field_name, self.field_is_complex(field)

    def __call__(self) -> dict[str, Any]:
        return dict(self._data)


def _env_name_from_runtime() -> EnvName:
    """按 pydantic-settings 自身的优先级（环境变量压过 .env）决定读哪个 yaml。"""
    raw = os.environ.get("APP_ENV") or dotenv_values(ENV_FILE).get("APP_ENV") or "development"
    if raw not in _VALID_ENVS:
        raise ValueError(f"APP_ENV 只能是 {list(_VALID_ENVS)} 之一，当前 {raw!r}")
    return cast("EnvName", raw)


def _load_yaml_data(env_name: EnvName) -> dict[str, Any]:
    path = CONFIG_DIR / f"{env_name}.yaml"
    if not path.is_file():
        raise FileNotFoundError(f"缺少配置文件：{path}")
    loaded: Any = yaml.safe_load(path.read_text(encoding="utf-8")) or {}
    if not isinstance(loaded, dict):
        raise ValueError(f"{path} 顶层必须是 YAML 映射")

    app: dict[str, Any] = dict(loaded.get("app") or {})
    declared_env = app.get("env")
    if declared_env is not None and declared_env != env_name:
        raise ValueError(f"{path.name} 的 app.env={declared_env!r} 与 APP_ENV={env_name!r} 不一致")

    data: dict[str, Any] = {
        "app_name": app.get("name"),
        "log_level": app.get("log_level"),
        "cors_origins": app.get("cors_origins"),
    }
    data.update({k: loaded[k] for k in _PARAM_SECTIONS if k in loaded})
    unknown = set(loaded) - {"app", *_PARAM_SECTIONS}
    if unknown:
        raise ValueError(f"{path.name} 含未识别的配置段：{sorted(unknown)}")
    return {k: v for k, v in data.items() if v is not None}


class Settings(BaseSettings):
    """进程内唯一的配置视图。用 `get_settings()` 取，不要直接实例化。"""

    model_config = SettingsConfigDict(
        env_file=ENV_FILE,
        env_file_encoding="utf-8",
        case_sensitive=False,
        extra="ignore",
        # .env 用 `KEY=` 空值表示"该项未选型"，须忽略而非按类型解析
        env_ignore_empty=True,
    )

    app_name: str = "InterviewForge"
    app_env: EnvName = "development"
    log_level: LogLevel = "INFO"
    cors_origins: list[str] = Field(default_factory=list)

    deepseek_api_key: SecretStr
    deepseek_base_url: str = "https://api.deepseek.com"
    llm_model_extract: str = "deepseek-chat"
    llm_model_generate: str = "deepseek-chat"
    llm_model_review: str = "deepseek-chat"
    #: 空串 = 尚未选型。`ImageParser` 遇到空值直接报错，绝不退回纯文本模型——
    #: 用文本模型"读"图片不会报错，只会编出一份看起来很完整的简历，比失败更糟。
    llm_model_vision: str = ""

    embedding_provider: str | None = None
    embedding_api_key: SecretStr | None = None
    embedding_model: str | None = None
    embedding_dim: int | None = Field(default=None, gt=0)

    database_url: str
    upload_dir: Path = Path("data/uploads")

    jwt_secret: SecretStr
    jwt_algorithm: str = "HS256"
    access_token_expire_minutes: int = Field(default=60, gt=0)

    langfuse_public_key: str | None = None
    langfuse_secret_key: SecretStr | None = None
    langfuse_host: str | None = None

    llm: LlmParams = Field(default_factory=LlmParams)
    agent: AgentParams = Field(default_factory=AgentParams)
    question: QuestionParams = Field(default_factory=QuestionParams)
    retrieval: RetrievalParams = Field(default_factory=RetrievalParams)

    @model_validator(mode="after")
    def _check_embedding_selection(self) -> "Settings":
        if self.embedding_provider and not (self.embedding_model and self.embedding_dim):
            raise ValueError("设了 EMBEDDING_PROVIDER 就要同时给 EMBEDDING_MODEL 与 EMBEDDING_DIM")
        return self

    @field_validator("upload_dir")
    @classmethod
    def _anchor_upload_dir(cls, value: Path) -> Path:
        # 相对路径按项目根解析，避免 uvicorn / 后台任务的工作目录差异写错位置
        return value if value.is_absolute() else PROJECT_ROOT / value

    @classmethod
    def settings_customise_sources(
        cls,
        settings_cls: type[BaseSettings],
        init_settings: PydanticBaseSettingsSource,
        env_settings: PydanticBaseSettingsSource,
        dotenv_settings: PydanticBaseSettingsSource,
        file_secret_settings: PydanticBaseSettingsSource,
    ) -> tuple[PydanticBaseSettingsSource, ...]:
        data = _load_yaml_data(_env_name_from_runtime())
        return (
            init_settings,
            env_settings,
            dotenv_settings,
            _YamlSettingsSource(settings_cls, data),
            file_secret_settings,
        )

    @property
    def is_production(self) -> bool:
        return self.app_env == "production"


@lru_cache(maxsize=4)
def get_settings(app_env: EnvName | None = None) -> Settings:
    """取配置。`app_env` 显式指定时同步设置进程环境，保证 yaml 与字段值同源。"""
    if app_env is not None:
        if app_env not in _VALID_ENVS:
            raise ValueError(f"app_env 只能是 {list(_VALID_ENVS)} 之一，当前 {app_env!r}")
        os.environ["APP_ENV"] = app_env
    settings = Settings()
    if app_env is not None and settings.app_env != app_env:
        raise ValueError(f"APP_ENV 被改成 {settings.app_env!r}，与请求的 {app_env!r} 不一致")
    return settings
