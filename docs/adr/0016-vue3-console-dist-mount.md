# ADR-0016：控制台改用 Vue 3 构建产物，同源挂载 `frontend/dist` 并做 SPA 回退

日期：2026-10-25
状态：已决定（**取代 ADR-0012** 的零构建与静态目录形态；同源挂载的结论保留）
相关：`frontend/`、`app/main.py`、`tests/test_api/test_m6_api.py`、ADR-0011（后台化）、
ADR-0013（写入侧语义）、ADR-0014（图片草稿）、ADR-0015（评审纯批注）

## 背景

ADR-0012 用三个文件（`index.html` / `app.css` / `app.js`）解决了"能点"的问题：原生 ES +
`fetch`，零构建、零依赖。它足够覆盖当时的四个视图，但维护者随后要求界面与交互"对标企业级
项目"——需要统一的组件层、表单校验、暗色主题、响应式布局、表格筛选与分页、图表，以及
更细的加载 / 空 / 错误状态。手写这些会迅速把 `app.js` 变成第二套框架，且无法被类型检查。

这台机器上 Node 只在工具链里，但**生产运行仍然只需要 Python**：构建一次，uvicorn 只读产物。
因此本 ADR 把"是否构建"从运行期约束改成开发期步骤，不改变部署形态（同源、单进程、SQLite）。

## 决策

1. **技术栈**：Vue 3.5 + TypeScript 5.9 + Vite 7 + Element Plus 2.14 + Pinia 3 +
   vue-router 4（history 模式）+ ECharts 6 + Axios。依赖全部打进 `dist/`，运行期不引 CDN。
2. **只服务构建产物**。`FRONTEND_DIST = <repo>/frontend/dist`；源码目录（`src/`、`index.html`）
   不对外暴露。没有 `dist/` 时不挂载，`create_app()` 不因此抛错。
3. **挂载顺序不变**：`include_router(api_router)` 之后才 `app.mount("/", …)`。`/` 上的静态
   挂载排在前会吃掉 `/api/v1/...`（ADR-0012 的硬约束继续有效）。
4. **SPA 回退只给导航请求**：`_SpaStaticFiles.get_response` 捕获静态 404，仅在
   ①路径不以 `api/` 开头 ②最后一段不含 `.` 时返回 `index.html`。于是：
   - `/reports/<id>` 刷新可用（history 模式深链）；
   - `/api/v1/typo` 仍是 404 JSON，不会变成一份 200 的 HTML；
   - `/assets/missing.js` 仍是 404，不把缺资源伪装成页面。
   Windows 上 `StaticFiles.get_path` 会把分隔符换成 `\`，判形前先统一为正斜杠（实测 Starlette 1.8）。
5. **开发走 Vite 代理**：`npm run dev` 固定 `127.0.0.1:3000`，代理 `/api`、`/docs`、
   `/redoc`、`/openapi.json` 到 `127.0.0.1:8000`。页面与接口在开发期也视为同源，
   不需要改后端 CORS；漏代理 `/docs` 会让页头文档链接被 SPA 回退成 `index.html`。
6. **页面只做展示与轮询**。配额、评分、证据核对、409/force 的删除语义都在服务端；
   前端保留的是既有客户端契约：任务 2 秒轮询并可取消、`completed`/`failed` 停、
   空报告按 `status` 分支、写请求网络失败提示"先刷新确认、别直接重试"。
   复杂事实一律走 Vue 文本插值（默认转义），不引入 `v-html`。
7. **测试不依赖真产物**。`tests/test_api/test_m6_api.py` 用 `tmp_path` 造一个最小 `dist`
   （`index.html` + 一个带扩展名的资源），钉住挂载顺序、深链回退、`api/` 404 与缺目录不挂载。
   CI 不必为了 Python 测试跑 npm。
8. **仓库只收源码**：`node_modules/` 与 `frontend/dist/` 进 `.gitignore`；
   `legacy-console/` 暂存 ADR-0012 的三件旧文件，不挂载、不参与构建。

## 理由

- Element Plus 是中文企业后台的事实标准，表单、表格、抽屉、消息框可直接覆盖岗位录入、
  删除二次确认、报告筛选等流程；自研这些控件会把"界面升级"变成"再造一个组件库"。
- Vite 的 dev server 与 proxy 让前端可在 3000 热更新，生产仍由 FastAPI 单端口供页，
  避免部署成两个服务或引入 Nginx。
- 只读 `dist/` 让运行期依赖收敛：Python 端不必知道 npm，测试也不必装 Node。
- SPA 回退若"所有 404 都回 index"，客户端会把接口 404 误读成页面路由，排障成本远高于
  在静态层多写十行判形。

## 权衡与代价

- **多了一个构建步骤**。改前端后必须 `npm run build` 才会反映到 8000 端口；只跑
  `uvicorn` 看到的是旧产物。启动脚本已补提示。
- **产物不进仓库**，因此"克隆即可跑"的边界变成"Python 可跑、页面需构建一次"。
  这是有意的：避免每次前端提交都带一份压缩产物 diff。
- **包体偏大**：当前为可靠性全量引入 Element Plus 与 ECharts，主包约 1.25 MB JS
  （gzip ~406 KB），工作台图表块约 1.1 MB（gzip ~382 KB）。后续可按需引入或
  `manualChunks` 拆包，以首屏为代价换取简单性。
- **浏览器基线提高**：Vite 7 产物面向现代 ES 模块浏览器，不再照顾旧 Edge/IE。
- **无鉴权不变**：页面让写端点更容易被误触，但这不是本 ADR 引入的问题；
  认证欠账仍在 `docs/TODO.md`（所有表没有 `user_id`）。
- **旧页面暂时并存**：`legacy-console/` 只作为历史参照，若确认不再需要应删除，
  避免后来者误以为它仍是线上入口。

## 后续影响

- `AGENTS.md` §3 的"控制台"条目改由本 ADR 定义；提交前三绿之外，前端另加
  `npm run typecheck`（`npm run build` 已包含它）。
- `start.sh` / `start.bat` 在缺 `frontend/dist` 时给出构建命令，不再假设源目录可直服。
- `docs/adr/README.md` 的 0012 标注"已被 0016 取代"；ADR-0012 正文保留历史决策。
- 若再做前端验收，仍需浏览器实测：页面路由、主题切换、上传/轮询、409 二次确认、
  空报告三种 `status` 分支与移动端断点。
