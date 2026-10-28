# 08 · 配置、LLM 客户端与 Prompt

## 1. 配置来源与优先级

[`app/config.py`](file:///d:/code/InterviewForge/app/config.py)

```
构造参数 > 进程环境变量 > .env（密钥/连接串）
        > configs/<APP_ENV>.yaml（行为参数） > 代码默认值
```

- 一律用 `get_settings()`（`lru_cache(maxsize=4)`）。显式传 `app_env` 会同步写
  `os.environ["APP_ENV"]`，保证 yaml 与字段值同源。
- `APP_ENV` 只能是 `development | testing | production`；yaml 里的 `app.env` 必须一致，
  否则启动即报错。
- yaml 只允许 `app / llm / agent / question / retrieval` 五段，未知段直接报错。
- `.env` 用 `KEY=` 空值表示「未选型」，`env_ignore_empty=True` 让它走默认值而不是解析失败。
- `upload_dir` 的相对路径按**项目根**解析（避免 uvicorn / 后台任务工作目录不同写错位置）。

## 2. Settings 字段

| 字段 | 环境变量 | 默认 | 说明 |
| --- | --- | --- | --- |
| `app_name` | `APP_NAME` | `InterviewForge` | |
| `app_env` | `APP_ENV` | `development` | 决定读哪份 yaml |
| `log_level` | `LOG_LEVEL` | `INFO` | |
| `cors_origins` | `CORS_ORIGINS`（JSON 列表） | `[]` | 非空时才挂 CORS |
| `deepseek_api_key` | `DEEPSEEK_API_KEY` | **必填** `SecretStr` | 永不回显 |
| `deepseek_base_url` | `DEEPSEEK_BASE_URL` | `https://api.deepseek.com` | OpenAI 兼容端点 |
| `llm_model_extract` | `LLM_MODEL_EXTRACT` | `deepseek-chat` | 抽取/NER |
| `llm_model_generate` | `LLM_MODEL_GENERATE` | `deepseek-chat` | 出题 |
| `llm_model_review` | `LLM_MODEL_REVIEW` | `deepseek-chat` | 语义评审 |
| `llm_model_vision` | `LLM_MODEL_VISION` | `""` | 空 = 未选型；图片/扫描件直接 422 |
| `embedding_provider/key/model/dim` | `EMBEDDING_*` | `None` | 设 provider 必须同时给 model + dim |
| `database_url` | `DATABASE_URL` | **必填** | `sqlite+aiosqlite:///...` |
| `upload_dir` | `UPLOAD_DIR` | `data/uploads` | 相对项目根 |
| `jwt_secret` | `JWT_SECRET` | **必填** `SecretStr` | 鉴权中间件尚未接线 |
| `jwt_algorithm` | `JWT_ALGORITHM` | `HS256` | |
| `access_token_expire_minutes` | `ACCESS_TOKEN_EXPIRE_MINUTES` | `60` | |
| `langfuse_*` | `LANGFUSE_*` | `None` | 预留观测，未接线 |

行为参数（yaml）：

| 段 | 字段 | development | testing | production |
| --- | --- | --- | --- | --- |
| `llm` | `request_timeout_seconds` | 60 | 5 | 30 |
| | `max_concurrency` | 4 | 2 | 16 |
| | `default_max_tokens` | 2048 | 256 | 2048 |
| | `transport_max_attempts` | 3 | 3 | 3 |
| `agent` | `recursion_limit` | 25 | 10 | 25 |
| | `validate_max_retries` | 2 | 1 | 2 |
| | `max_tokens_per_run` | 200000 | 100000 | 200000 |
| `question` | `total` | 15 | 3 | 15 |
| | `difficulty_ratio` | 0.3/0.5/0.2 | 0.34/0.33/0.33 | 0.3/0.5/0.2 |
| `retrieval` | `top_k_coarse / top_k_final` | 20 / 5 | 5 / 3 | 20 / 5 |
| | `dedup_similarity_threshold` | 0.85 | 0.85 | 0.85 |

校验规则：

- `difficulty_ratio` 三项之和必须为 1（容差 0.01）；
- `top_k_final ≤ top_k_coarse`；
- `max_tokens_per_run` 默认 200k：M5 实测一轮端到端约 157k / 33 次调用，
  这是「正常跑不碰线、跑偏烧穿前掐断」的位置。

## 3. LLM 客户端

[`app/utils/llm_client.py`](file:///d:/code/InterviewForge/app/utils/llm_client.py)

```python
client = LLMClient(settings=None, client=None)   # client 仅测试注入
client.model_for(purpose)                        # extract|generate|review|vision
await client.chat(purpose, messages, *, max_tokens=None, temperature=0.2, json_mode=False)
await client.chat_json(purpose, messages, *, schema=None, max_attempts=None, temperature=0.2)
```

### 3.1 两类重试（**别混用**）

| 层 | 触发 | 机制 | 参数 |
| --- | --- | --- | --- |
| 传输重试 | 连接错误 / 超时 / 限流（`APIConnectionError` / `APITimeoutError` / `RateLimitError`） | tenacity `AsyncRetrying`，指数退避 + jitter，同一条 messages 重发 | `llm.transport_max_attempts` |
| 校验重试 | 模型答非所问（JSON 解析失败 / schema 不合格） | `chat_json` 把错误反馈拼回对话再问 | `agent.validate_max_retries + 1` |

**上游明确回 4xx/5xx**（`APIStatusError`，实测 402 余额不足）**不进传输重试**：直接
`LLMError` 并把 `status_code` 放进 `details`。否则任务行只会记成 `internal_error`，
轮询方看不出「是账户没钱了」。

### 3.2 并发与用量

- `asyncio.Semaphore(llm.max_concurrency)` 限制同时在飞的调用。
- `UsageTotals` 按模型累计 `calls / failed_calls / prompt_tokens / completion_tokens`
  （`per_model` 细分）。
- **HTTP 侧每请求新建客户端**：`Semaphore` 与 `UsageTotals` 是「一次运行」的量纲，
  单例会跨请求累加用量，成本护栏就失效（`app/dependencies.get_llm()`，即 `LLMDep`）。

### 3.3 文本与 JSON

- `chat()` 返回纯文本；空 `choices` 抛 `LLMResponseError`。
- `chat_json()` 先剥 ```` ```json ```` 围栏，要求顶层是 **dict**；给了 `schema` 就
  `model_validate`。重试用尽抛 `LLMResponseError`（上游是通的，产出不可用）。

## 4. Prompt 模板

[`app/prompts/registry.py`](file:///d:/code/InterviewForge/app/prompts/registry.py)

```
app/prompts/templates/v1/
├── parse.system.j2 / parse.user.j2
├── validate.* / match.* / profile.* / plan.* / generate.* / review.*
├── ner.*          # 联系人抽取
├── ocr.*          # 图片/扫描件
└── job_fields.*   # 岗位图片字段
```

- `get_prompt(node, version="v1")` → `Prompt(node, version, system, user)`，
  `render(**ctx)` 返回 OpenAI messages。
- 环境固定 `autoescape=False`（产物喂模型不喂浏览器）与 `StrictUndefined`
  （漏传变量必须报错，不能静默渲染空串）。
- 新增一版 = 复制版本目录，节点代码不动，只换 `version`；trace 里的版本号用于归因。
- 枚举与字符串先归一成 `str` 再进 `lru_cache`（`Enum.__hash__` 与字符串哈希不同，
  直接当键会产出两份 Prompt）。

## 5. Prompt 设计禁令

- **不要让模型判日期**：实测 `deepseek-chat` 会把 `2026-06-30` 说成「晚于今天 2026-10-16」，
  把 `today` 写进 prompt 也照样错。日期/年限/区间一律留在确定性规则层。
- 模型只做「词典无解的软要求判定」「叙述润色」「出题」；分数、配额、排序、工序号由规则给。
- 所有引用必须能在原文逐字找到；prompt 要求模型贴原文，代码侧再核一遍。

## 6. 密钥与安全

- 密钥只进 `.env`（已 gitignore）：`DEEPSEEK_API_KEY`、`JWT_SECRET`、
  `EMBEDDING_API_KEY`、`LANGFUSE_*`。
- 行为配置只进 `configs/*.yaml`；`.env` 不入库、不回显。
- 错误响应不许带密钥、上游原始响应或绝对路径。
- `alembic.ini` 必须 ASCII（locale 是 GBK，中文会 `UnicodeDecodeError`）。

## 7. 配置变更的自检清单

1. 新增 yaml 字段 → 同步 `configs/{development,testing,production}.yaml` 与对应
   `*Params` 模型，否则某个环境启动即报「未识别配置段」。
2. 新增环境变量 → 同步 `Settings` 字段、`.env.example`（若有）与本文档。
3. 改 `question.total` / `difficulty_ratio` → 跑
   `tests/test_agents/test_plan_node.py` 与端到端评测，配额表会变。
4. 改 `max_tokens_per_run` → 注意判定点在 `generate` 扇出前，允许约「一轮扇出」的 overshoot。
