# docker/ —— 空目录，而且按现决议**不会**被填上

这个目录原本承诺"阶段 10 产出：`Dockerfile`（多阶段）、`Dockerfile.worker`、
`docker-compose.yaml`（api / worker / beat / redis / pgvector / langfuse-*）"，
又说"阶段 3 先用仓库根的 `docker-compose.dev.yaml` 起 PostgreSQL + pgvector"。

实测（2026-10-27）：`docker/` 里只有本文件，仓库根**没有任何** `docker-compose*.yaml`。
它广告的那套编排一个都不存在，而项目的部署形态早就另定了（ADR-0007 / ADR-0011、AGENTS.md §8）：

- 关系库是 **SQLite**（`data/interview_forge.db`，WAL 模式），向量用 **sqlite-vec**——
  不是 PostgreSQL，也不是 pgvector；
- 异步任务是 FastAPI **`BackgroundTasks`**（同进程协程）——不是 Celery，没有 Redis，
  所以"worker / beat 两个容器"这套形状在这里没有对应物；
- LangGraph 的 checkpoint 落在独立的 `data/checkpoints.db`（`SqliteSaver`）；
- 可观测靠 `task` 行 + `trace` 通道，没有 Langfuse。

**起服务只有一条命令**：`bash start.sh [port]`（cmd.exe 用 `start.bat`）。它把 `.venv`
解释器、`PYTHONPATH=.`、`-X utf8` 三样都钉好；`RELOAD=1 bash start.sh` 加 `--reload`。
单机自用形态下没有第二个进程要编排。顺带一提：`Makefile` 在本机也不可执行（没有 make），
它现在只是一份命令速查表。

目录先留着不删：删除动作要维护者点头（`docs/TASK_PLAN.md` 的 D-8）。
真要容器化，得先回答两个前置问题——单文件 SQLite 怎么挂卷、`BackgroundTasks` 要不要挪出进程
（后者已挂在 `docs/TODO.md` §2.4 的僵尸任务那条上）。
