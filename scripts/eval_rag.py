"""检索质量评测：Recall@k / Precision@k 与消融实验〔阶段 6 · §9.6，M6 留壳〕

**这是壳，不是实现**：`scripts/eval_e2e.py` 测端到端产出，本文件要测的是它的前置环节——
`match` 节点从 JD 要求项召回简历片段这一步的质量。留壳的理由与 `eval_e2e.py` 不同：
不是"没写完"，而是**没有可测的东西**——检索本身还没接上。

要测什么（形状先定下来，实现时不必再重新设计）：

1. **Recall@k / Precision@k**：对每条 JD 要求项，人工标注它在简历里的对应片段，
   与检索器实际返回的前 k 条比对。分母是标注集，不是"返回条数"，否则恒返回空列表的
   实现能刷出 100% 精确率。
2. **消融**：同一批简历跑两遍 `match`，一遍 `NullRetriever`、一遍真检索器，比较
   `matched_gaps` 里 `source="retrieval"` 的占比、`evidence_ratio`、以及为了补差距多花的
   LLM 调用数。只有对比数字才能回答"接检索到底值不值"，单跑一遍只能说"它跑了"。
3. **归因可分**：词典 / 模型 / 检索三种判据的召回必须分开统计（`docs/TODO.md` §2.2
   已挂账）——分不开就等于没接，因为看不出是哪一路在起作用。

卡住的原因（恢复前先读这段）：

- `app/vectorstore/retriever.py` 只有 `SkillRetriever` 协议 + `NullRetriever`（恒召回 0 条），
  `app/vectorstore/client.py` / `job_indexer.py` 还是阶段 6 的壳；
- embedding 未选型（`docs/TODO.md` §3：`fastembed` 本地跑 vs 暂时不做），
  `settings.embedding_provider` 为空时 `Settings` 的校验器不允许配 `embedding_dim`；
- 没有标注集：`tests/fixtures/` 里只有 1 份真简历，Recall 需要人工对应片段。

恢复后要验的第一件事：`make_match_node(retriever=...)` 的装配点能换成真实现，且
`tests/test_agents/test_match_node.py` 里那条"记录型假向量库"用例可以拿真库重放。
"""

import sys
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT))

from app.config import get_settings  # noqa: E402


def main() -> int:
    settings = get_settings()
    print(__doc__.strip().splitlines()[0])
    print("状态：留壳，未实现。")
    if settings.embedding_provider:
        ready = f"已配置 embedding_provider={settings.embedding_provider}，可以实现本脚本"
        print(ready + "；要测的三件事见文件顶部")
    else:
        print("embedding 未选型（settings.embedding_provider 为空），检索器没有真实现可测")
    print("欠账与恢复条件：docs/TODO.md §2.2 第一条、§3 第三条")
    return 2


if __name__ == "__main__":
    raise SystemExit(main())
