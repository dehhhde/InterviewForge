# ADR-0006：PDF 解析引擎选 PyMuPDF，放弃 pypdf

日期：2026-10-15
状态：已决定（M1 开工前置项）
相关：`app/parsers/pdf_parser.py`、`requirements.txt`、计划书 §6 风险 1

## 背景

简历 PDF 有两类硬骨头，`M1` 的抽取质量直接取决于此：

1. **双栏/多版式**。抽出纯文本时若把左右两栏按行粘在一起，`parse` 就会把右栏的技能
   当成左栏经历的一部分，`validate` 只能看到"字段错位"这种下游难修的脏数据。
2. **扫描件**。整页只有图片没有文字层，必须识别出来改走多模态 LLM，否则会静默产出
   空 `raw_text` 和一份"看起来很干净"的空简历。

原设计（stub docstring）写 PyMuPDF，但 `requirements.txt` 装的是 pypdf，二者能力不等价。

## 实测（Python 3.14.7 / Windows）

样本是手工按 PDF 语法直接写出的两份中性文件（不依赖任何被测试库）：`twocol.pdf` 的内容流
按 y 坐标交替写入左右两栏各 6 行，`scanned.pdf` 只有一张铺满页面的 JPEG、没有文字层。

| | pypdf 6.20.1 | pdfplumber 0.11.10 | PyMuPDF 1.28.2 |
|---|---|---|---|
| 双栏样例首行 | `LEFT0:… RIGHT0:…`（两栏粘连） | `LEFT0:… RIGHT0:…`（两栏粘连） | `LEFT0:…`（分栏保留） |
| 坐标 | 无词/块级 API | 12 个词 + `x0/top` | 6 个块 + `bbox` |
| 扫描件判定 | `text_len=0, images=1` | 同左 | 同左 |
| 单页耗时 | 1.7ms | 5.3ms | 27ms（含首次初始化） |
| 许可证 | MIT | MIT（含 pdfminer.six） | **AGPL-3.0 或商业授权（双许可）** |
| 安装体积 | 3.9MB | 33MB（拉 pillow/pypdfium2/cryptography 等 5 个依赖） | 54MB（自带 MuPDF，单包） |

## 决策

**`pdf_parser.py` 用 PyMuPDF（`import pymupdf`），从 `requirements.txt` 删除 pypdf。**

- 只有它默认就按块给出分栏结果，`pypdf`/`pdfplumber` 的默认行聚类都会把两栏粘成一行；
  用 pdfplumber 得自己按 `x0` 做列聚类——那正是 MuPDF 已经做完的工作。
- 扫描件三家都能判，不构成区分项；但 PyMuPDF 一次 `open()` 就能同时拿文字块、图片列表
  和页面尺寸，接口最少。
- `fitz` 这个别名在 1.28 已标记 deprecated，统一写 `import pymupdf`。

## 权衡与代价

- **AGPL-3.0**：网络服务对外提供也要遵循 AGPL（或买商业授权）。本项目定位是学习+自托管，
  可接受；一旦要闭源分发，替换成本被限制在 `pdf_parser.py` 一个文件内——这是 M1 里
  `BaseParser` 存在的唯一理由。
- 54MB 体积、首次打开 PDF 的初始化耗时（27ms）都比 pypdf 贵，但相对一次 LLM 调用可忽略。
- 放弃 pypdf 意味着没有 MIT 兜底；如果将来 AGPL 成为阻塞项，退路是 pdfplumber（MIT）
  加自写列聚类，而不是回到 pypdf。
- 本实测用的是合成 PDF，只覆盖版式与扫描件判定，**未覆盖 CJK 字体解码**；M1 需要用真简历
  复验（见下方待办）。

## 后续影响

- `requirements.txt`：`pypdf>=6.20` → `PyMuPDF>=1.28`。
- `pdf_parser.py` 的接口约定：返回 `(raw_text, is_scanned, page_count)`，扫描件直接抛
  `ParseError` 交由 image 路径处理，不在 parser 里静默降级。
## 真简历复验（2026-10-15，`tests/fixtures/resumes/附件简历-梁清生.pdf`）

中文双栏排版的直聘导出件，2 页，字体为子集化 `AlibabaPuHuiTi`：

- CJK 解码正常，无乱码；姓名、手机、邮箱、学校全部原样取出（`tests/test_parsers.py::test_real_resume_decodes_cjk_in_reading_order` 已固化为断言）。
- **新发现**：`get_text()` 默认按内容流顺序输出，这份简历里"广州应用科技学院…"块排在姓名块之前 →
  必须用 `sort=True`，否则抽取器拿到的段落顺序与视觉顺序不一致。
- 该文件含 2 张图片（头像/图标）但文字层充足 → 佐证扫描件判定不能只看图片数，
  实现里用的是"每页平均字数 < 40 且全表如此"。
- 仍缺一份真扫描件：扫描件路径目前用合成件（整页 JPEG、无文字层）验证，M1 内需用真图复跑。
