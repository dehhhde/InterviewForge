# docker/

阶段 10 产出：`Dockerfile`（多阶段）、`Dockerfile.worker`、`docker-compose.yaml`
（api / worker / beat / redis / pgvector / langfuse-*）。

阶段 3 先用仓库根的 `docker-compose.dev.yaml` 起 PostgreSQL + pgvector。

实现清单见 `LEARNING_ROADMAP.md` §13。
