# migrations/versions/

迁移序列从 `2056a1287299 (initial tables)` 开始（M3，四张表 + 索引 + 外键 + CHECK）。

## 加一张表 / 改一个字段的标准动作

```bash
# 1. 改 app/models/，并把新模型登记进 app/models/__init__.py
#    （autogenerate 只看已 import 的模型，漏登记会生成一份"没有表"的空迁移，
#      而它照样 upgrade 成功，直到运行期 no such table 才炸）
# 2. 对着一个临时库生成，别碰开发库
ALEMBIC_DATABASE_URL="sqlite:///$PWD/tmp.db" PYTHONPATH=. \
  .venv/Scripts/python.exe -X utf8 -m alembic revision --autogenerate -m "短描述"
# 3. **逐行读生成的文件**。autogenerate 认不出"改列语义"，
#    也可能把不相关的差异一并写进来；SQLite 的 batch 模式还会把改列变成建表-搬数据-改名
# 4. 复用到真库并检查漂移
PYTHONPATH=. .venv/Scripts/python.exe -X utf8 -m alembic upgrade head
PYTHONPATH=. .venv/Scripts/python.exe -X utf8 -m alembic check   # 必须 "No new upgrade operations"
rm tmp.db
```

`env.py` 与 `alembic.ini` 的规矩（URL 从配置取、ini 只能 ASCII）写在
`docs/adr/0007-sqlite-persistence-layer.md`。
