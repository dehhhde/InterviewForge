/* InterviewForge 控制台：只做展示与轮询，判断全在服务端。
   页面与 API 同源（FastAPI 挂载本目录），所以不需要任何构建工具或 CDN。 */
"use strict";

const DIFF = { easy: "简单", medium: "中等", hard: "较难" };
const DIFF_CLASS = { easy: "badge-ok", medium: "badge-live", hard: "badge-warn" };
const SOURCE = { resume: "简历", jd: "JD", generic: "通用" };
const LEVEL = { basic: "了解", familiar: "熟悉", proficient: "精通" };
const DEGREE = { college: "大专", bachelor: "本科", master: "硕士", phd: "博士", other: "其他" };
const STATUS = {
  completed: ["已完成", "badge-ok"],
  running: ["进行中", "badge-live"],
  pending: ["排队中", "badge-muted"],
  aborted: ["已中止", "badge-warn"],
  failed: ["失败", "badge-bad"],
};

const $ = (id) => document.getElementById(id);
const api = () => $("api-base").value.replace(/\/$/, "");

/* 页面被双击本地文件（file://）打开时，每个请求都是跨域请求：服务照常处理、照常落库，
   但浏览器不允许 JS 读回执，fetch 只留一句 "Failed to fetch"。这时长得像"服务挂了"，
   实际上换回同源地址就是好的——所以这句话必须翻成人能照做的动作。 */
const openedAsFile = () => location.protocol === "file:";

function esc(value) {
  return String(value ?? "").replace(/[&<>"']/g, (ch) =>
    ({ "&": "&amp;", "<": "&lt;", ">": "&gt;", '"': "&quot;", "'": "&#39;" })[ch]
  );
}

function badge(text, cls) {
  return `<span class="badge ${cls || "badge-muted"}">${esc(text)}</span>`;
}

function statusBadge(status) {
  const [text, cls] = STATUS[status] || [status, "badge-muted"];
  return badge(text, cls);
}

function setFootError(message) {
  $("foot-error").textContent = message || "";
}

/* fetch 抛错 ≠ 请求没送达：它只说明"浏览器没让 JS 读到响应"。
   标上 network 让调用方能分清"服务回了个错误码"和"根本没听到回音"。 */
function networkFailure(cause) {
  const err = new Error(
    openedAsFile()
      ? "页面是 file:// 打开的：浏览器不允许本地文件页读跨域响应，" +
        `所以服务即使正常处理了请求，这里也只能显示失败。请用 ${consoleUrl()} 打开控制台。`
      : `没拿到 ${api()} 的响应：确认这台服务在跑（跨域被拦也会是这个样子）。`
  );
  err.network = true;
  err.code = "network_error";
  err.cause = cause;
  return err;
}

/* 从当前选中的 API 根地址推出页面的根地址：file:// 下唯一能救回同源的入口。 */
function consoleUrl() {
  const root = api().match(/^(https?:\/\/[^/]+)/);
  return root ? root[1] + "/" : "http://127.0.0.1:8000/";
}

/* 写端点撞上 network 失败时不能写"失败"：那两个字会诱导人立刻重试，
   而请求多半已经执行完并落了库，重试就是第二份数据（实测重复上传真会多出简历行）。 */
function failureHint(err, failedText, retryCost) {
  if (!err.network) return failedText;
  return (
    "结果未知：浏览器没读到回执，但服务可能已经执行了（" +
    retryCost + "）。先刷新列表确认，别直接重试。"
  );
}

async function request(path, options) {
  let response;
  try {
    response = await fetch(api() + path, options);
  } catch (err) {
    throw networkFailure(err);
  }
  const text = await response.text();
  let payload = null;
  if (text) {
    try {
      payload = JSON.parse(text);
    } catch (err) {
      payload = null;
    }
  }
  if (!response.ok) {
    const body = payload && payload.error;
    const problems = body && body.details && body.details.problems;
    const failure = new Error(
      `${response.status} ${(body && body.code) || "request_failed"}：` +
        `${(body && body.message) || text.slice(0, 160) || "无响应体"}` +
        (problems ? ` ${JSON.stringify(problems)}` : "")
    );
    // 状态码与 code 挂在错误对象上：删除简历要按 409 决定要不要二次确认后带 force 重试，
    // 光比文案（中文消息）会把判据写成给机器看流水账。
    failure.status = response.status;
    failure.code = (body && body.code) || "request_failed";
    failure.details = (body && body.details) || null;
    throw failure;
  }
  return payload;
}

const sleep = (ms) => new Promise((resolve) => setTimeout(resolve, ms));

/* 轮询票号：切换任务或重新发起时递增，旧的循环下一轮发现自己过期就退出。
   不这么做的话两次轮询会抢着写同一个进度面板。 */
let pollToken = 0;

async function pollTask(taskId, render) {
  const token = ++pollToken;
  for (let round = 0; round < 900; round += 1) {
    if (token !== pollToken) return null;
    let task;
    try {
      task = await request(`/tasks/${taskId}`);
    } catch (err) {
      setFootError("读取任务进度失败：" + err.message);
      // file:// 下这一条永远读不到，再循环 900 轮只会把按钮锁死在"上传中"。
      if (err.network && openedAsFile()) return null;
      await sleep(3000);
      continue;
    }
    if (token !== pollToken) return null;
    render(task);
    setFootError("");
    if (task.status === "completed" || task.status === "failed") return task;
    await sleep(2000);
  }
  return null;
}

function renderSteps(container, steps) {
  if (!steps || !steps.length) {
    container.innerHTML = '<li>等待第一个 super-step</li>';
    return;
  }
  const seen = new Map();
  container.innerHTML = steps
    .map((step) => {
      const node = step.node || "?";
      const times = (seen.get(node) || 0) + 1;
      seen.set(node, times);
      const cls = node === "generate_item" ? " fan" : times > 1 ? " redo" : "";
      const ms = typeof step.ms === "number" ? ` ${Math.round(step.ms)}ms` : "";
      return `<li class="${cls.trim()}">${esc(node + (times > 1 && node !== "generate_item" ? "×" + times : ""))}${esc(ms)}</li>`;
    })
    .join("");
}

/* ---------------- 视图切换 ---------------- */

$("tabs").addEventListener("click", (event) => {
  const button = event.target.closest(".tab");
  if (!button) return;
  document.querySelectorAll(".tab").forEach((tab) => tab.classList.toggle("is-active", tab === button));
  const view = button.dataset.view;
  document.querySelectorAll(".view").forEach((panel) =>
    panel.classList.toggle("is-active", panel.id === "view-" + view)
  );
});

/* ---------------- 简历 ---------------- */

function resumeLabel(resume) {
  return resume.name || resume.file_name || resume.id.slice(0, 8);
}

async function loadResumes() {
  const rows = await request("/resumes/list?limit=100");
  $("resume-count").textContent = `共 ${rows.length} 份`;
  $("resume-list").innerHTML = rows.length
    ? rows
        .map(
          (row) => `<li class="row-wrap">
            <button class="row" data-resume="${esc(row.id)}">
              <span class="row-main">
                <div class="row-title">${esc(resumeLabel(row))}</div>
                <div class="row-sub">${esc(row.file_name)} · ${esc(row.created_at.slice(0, 19).replace("T", " "))}</div>
              </span>
              ${badge(row.id.slice(0, 8))}
            </button>
            <span class="row-actions">
              <button class="btn" data-rename="${esc(row.id)}">改名</button>
              <button class="btn btn-danger" data-delete-resume="${esc(row.id)}">删除</button>
            </span>
          </li>`
        )
        .join("")
    : '<li class="empty">还没有简历</li>';

  const select = $("gen-resume");
  const previous = select.value;
  select.innerHTML = rows
    .map((row) => `<option value="${esc(row.id)}">${esc(resumeLabel(row))}</option>`)
    .join("");
  if (previous && rows.some((row) => row.id === previous)) select.value = previous;
}

function renderParsed(parsed) {
  const skills = (parsed.skills || [])
    .map(
      (item) =>
        `<span class="chip" title="${esc(item.evidence)}">${esc(item.name)}` +
        `<span class="chip-level">${esc(LEVEL[item.level] || "未标程度")}</span></span>`
    )
    .join("");
  const educations = (parsed.educations || [])
    .map(
      (item) =>
        `<li>${esc(item.school)} · ${esc(item.major || "专业未明")} · ` +
        `${esc(DEGREE[item.degree] || item.degree)}` +
        `${item.start_year ? ` （${esc(item.start_year)}–${esc(item.graduate_year || "?")}）` : ""}</li>`
    )
    .join("");
  const works = (parsed.work_experiences || [])
    .map(
      (item) =>
        `<li>${esc(item.company)}${item.role ? ` · ${esc(item.role)}` : ""}` +
        `${item.start ? ` · ${esc(item.start)} 至 ${esc(item.end || "至今")}` : ""}` +
        (item.summary ? `<div class="row-sub">${esc(item.summary)}</div>` : "") +
        "</li>"
    )
    .join("");
  const projects = (parsed.projects || [])
    .map(
      (item) =>
        `<li>${esc(item.name)}${item.role ? ` · ${esc(item.role)}` : ""}` +
        (item.highlights && item.highlights.length
          ? `<ul class="checkpoints">${item.highlights.map((h) => `<li>${esc(h)}</li>`).join("")}</ul>`
          : "") +
        "</li>"
    )
    .join("");

  return `
    <div class="report-meta">
      ${badge(parsed.name || "未识别姓名", "badge-live")}
      ${badge(`经验 ${parsed.years_experience ?? "?"} 年`)}
      ${parsed.phone ? badge("电话 " + parsed.phone) : ""}
      ${parsed.email ? badge(parsed.email) : ""}
    </div>
    <h3>技能 ${skills ? `<span class="count">${(parsed.skills || []).length} 项</span>` : ""}</h3>
    ${skills ? `<div class="chips">${skills}</div>` : '<p class="empty">未抽到技能</p>'}
    <h3>教育</h3>${educations ? `<ul class="ask">${educations}</ul>` : '<p class="empty">无</p>'}
    <h3>工作</h3>${works ? `<ul class="ask">${works}</ul>` : '<p class="empty">无</p>'}
    <h3>项目</h3>${projects ? `<ul class="ask">${projects}</ul>` : '<p class="empty">无</p>'}
    <p class="hint">技能条目的 hover 提示是抽取时核对过的简历原文；无证据的值不会出现在这里。</p>`;
}

async function openResume(id) {
  document.querySelectorAll("#resume-list .row").forEach((row) =>
    row.classList.toggle("is-selected", row.dataset.resume === id)
  );
  const resume = await request(`/resumes/${id}`);
  const panel = $("resume-detail");
  panel.hidden = false;
  $("resume-detail-body").innerHTML = renderParsed(resume.parsed || {});
}

$("resume-list").addEventListener("click", (event) => {
  const rename = event.target.closest("[data-rename]");
  if (rename) {
    renameResume(rename.dataset.rename).catch((err) => setFootError(err.message));
    return;
  }
  const remove = event.target.closest("[data-delete-resume]");
  if (remove) {
    deleteResume(remove.dataset.deleteResume).catch((err) => setFootError(err.message));
    return;
  }
  const row = event.target.closest("[data-resume]");
  if (row) openResume(row.dataset.resume).catch((err) => setFootError(err.message));
});

async function renameResume(id) {
  const resume = await request(`/resumes/${id}`);
  const name = window.prompt(
    "简历展示名（只改这一个字段，抽取结果与原文不动）",
    resumeLabel(resume)
  );
  if (name === null || !name.trim()) return;
  await request(`/resumes/${id}`, {
    method: "PUT",
    headers: { "Content-Type": "application/json" },
    body: JSON.stringify({ name }),
  });
  setFootError("");
  await loadResumes();
}

async function deleteResume(id) {
  if (!window.confirm("删除这份简历？它名下的报告也会一起消失（库里没有回收站）。")) return;
  try {
    await request(`/resumes/${id}`, { method: "DELETE" });
  } catch (err) {
    // 409 是服务端在"名下还有报告"时的刹车。这里不再静默 force 重试：
    // 二次确认必须由人再说一次，页面不替人决定要不要删报告。
    if (err.code !== "already_exists") throw err;
    const count = (err.details && err.details.interview_count) || 0;
    if (!window.confirm(`这份简历名下有 ${count} 份报告，连报告一起删？`)) return;
    await request(`/resumes/${id}?force=true`, { method: "DELETE" });
  }
  setFootError("");
  $("resume-detail").hidden = true;
  await Promise.all([loadResumes(), loadInterviews()]);
}

$("btn-upload").addEventListener("click", async () => {
  const input = $("upload-file");
  if (!input.files.length) {
    $("upload-hint").textContent = "先选一个文件";
    return;
  }
  const body = new FormData();
  body.append("file", input.files[0]);
  $("btn-upload").disabled = true;
  $("upload-hint").textContent = "上传中…";
  $("upload-task").hidden = true;
  try {
    const receipt = await request("/resumes/upload", { method: "POST", body });
    $("upload-hint").textContent = `已受理：resume ${receipt.resume_id.slice(0, 8)} / task ${receipt.task_id.slice(0, 8)}`;
    $("upload-task").hidden = false;
    const task = await pollTask(receipt.task_id, (current) =>
      renderSteps($("upload-steps"), current.progress.steps)
    );
    if (task && task.status === "completed") {
      $("upload-hint").textContent = "解析完成";
      await loadResumes();
      openResume(receipt.resume_id).catch(() => {});
    } else if (task) {
      $("upload-hint").textContent = "解析失败，见页脚";
      setFootError(JSON.stringify(task.error));
    } else {
      // 轮询被抢票 / 跑到上限 / 跨域读不到：简历行多半已经在库里，只是进度看不见了。
      $("upload-hint").textContent = "已受理，但读不到解析进度——刷新简历列表确认，别急着重传";
    }
    input.value = "";
  } catch (err) {
    $("upload-hint").textContent = failureHint(err, "上传失败", "再传一次就是多一份简历");
    setFootError(err.message);
  } finally {
    $("btn-upload").disabled = false;
  }
});

/* ---------------- 岗位 ---------------- */

// 当前处在编辑态的岗位 id；空串表示表单是"新建"。
let editingJobId = "";

function jobFormValues() {
  return {
    title: $("job-title").value.trim(),
    company: $("job-company").value.trim(),
    jd_text: $("job-text").value,
    min_years: Number($("job-min-years").value || 0),
  };
}

function resetJobForm(mode) {
  editingJobId = "";
  $("job-form-title").textContent = "新建岗位";
  $("btn-job-cancel").hidden = true;
  $("job-title").value = "";
  $("job-company").value = "";
  $("job-min-years").value = "0";
  $("job-text").value = "";
  if (mode !== "keep-hint") $("job-hint").textContent = "";
}

async function loadJobs() {
  const rows = await request("/jobs");
  $("job-count").textContent = `共 ${rows.length} 个`;
  $("job-list").innerHTML = rows.length
    ? rows
        .map(
          (row) => `<li>
            <div class="job-row">
              <div class="job-row-main">
                <div class="row-title">${esc(row.title)}${row.company ? ` · ${esc(row.company)}` : ""}</div>
                <div class="row-sub"><code>${esc(row.id)}</code> · 期望 ${esc(row.min_years)} 年</div>
                <div class="chips">${(row.required_skills || [])
                  .map((name) => `<span class="chip">${esc(name)}</span>`)
                  .join("")}</div>
                <div class="job-jd">${esc(row.jd_text)}</div>
              </div>
              <span class="row-actions">
                <button class="btn" data-edit-job="${esc(row.id)}">编辑</button>
                <button class="btn btn-danger" data-delete-job="${esc(row.id)}">删除</button>
              </span>
            </div>
          </li>`
        )
        .join("")
    : '<li class="empty">库里还没有岗位，先跑 `python -m app.scripts.seed` 或直接在这里建一个</li>';

  const select = $("gen-job");
  const previous = select.value;
  select.innerHTML = rows.map((row) => `<option value="${esc(row.id)}">${esc(row.title)} · ${esc(row.company)}</option>`).join("");
  if (previous && rows.some((row) => row.id === previous)) select.value = previous;
}

$("job-list").addEventListener("click", (event) => {
  const edit = event.target.closest("[data-edit-job]");
  if (edit) {
    startEditJob(edit.dataset.editJob).catch((err) => setFootError(err.message));
    return;
  }
  const remove = event.target.closest("[data-delete-job]");
  if (remove) {
    deleteJob(remove.dataset.deleteJob).catch((err) => setFootError(err.message));
    return;
  }
});

async function startEditJob(id) {
  const job = await request(`/jobs/${id}`);
  editingJobId = id;
  $("job-form-title").textContent = `编辑岗位 ${id.slice(0, 8)}`;
  $("btn-job-cancel").hidden = false;
  $("job-title").value = job.title;
  $("job-company").value = job.company;
  $("job-min-years").value = String(job.min_years);
  $("job-text").value = job.jd_text;
  $("job-hint").textContent = "改了 JD 正文会重新按词典派生技能清单";
  document.querySelector('[data-view="jobs"]').click();
  $("job-title").focus();
}

async function deleteJob(id) {
  if (!window.confirm("删除这个岗位？历史报告不会跟着走（报告头里有岗位名快照）。")) return;
  await request(`/jobs/${id}`, { method: "DELETE" });
  setFootError("");
  if (editingJobId === id) resetJobForm();
  await Promise.all([loadJobs(), loadInterviews()]);
}

$("btn-job-save").addEventListener("click", async () => {
  const values = jobFormValues();
  if (!values.title) {
    $("job-hint").textContent = "岗位名称必填";
    return;
  }
  if (values.jd_text.trim().length < 5) {
    $("job-hint").textContent = "JD 正文太短（至少 5 个字），出题没有依据";
    return;
  }
  $("btn-job-save").disabled = true;
  $("job-hint").textContent = "保存中…";
  try {
    const path = editingJobId ? `/jobs/${editingJobId}` : "/jobs";
    const row = await request(path, {
      method: editingJobId ? "PUT" : "POST",
      headers: { "Content-Type": "application/json" },
      body: JSON.stringify(values),
    });
    $("job-hint").textContent = `已保存：${row.title}（技能 ${(row.required_skills || []).length} 项）`;
    resetJobForm("keep-hint");
    await loadJobs();
  } catch (err) {
    // 409 = 自然键（岗位名 + 公司）撞了已存在的行，错误体里带着那一行的 id
    $("job-hint").textContent =
      err.code === "already_exists"
        ? "这个岗位名与公司已经存在"
        : failureHint(err, "保存失败", "岗位名 + 公司是自然键，再存一次会撞 409");
    setFootError(err.message);
  } finally {
    $("btn-job-save").disabled = false;
  }
});

$("btn-job-cancel").addEventListener("click", () => resetJobForm());

$("btn-job-upload").addEventListener("click", async () => {
  const input = $("job-file");
  if (!input.files.length) {
    $("job-upload-hint").textContent = "先选一个 JD 文件";
    return;
  }
  const file = input.files[0];
  const body = new FormData();
  body.append("file", file);
  // 标题优先取表单里填的，没填就退化成文件名（去掉后缀）：建岗的自然键不能空着
  body.append("title", $("job-title").value.trim() || file.name.replace(/\.[^.]+$/, ""));
  body.append("company", $("job-company").value.trim());
  body.append("min_years", $("job-min-years").value || "0");
  $("btn-job-upload").disabled = true;
  $("job-upload-hint").textContent = "上传中…";
  try {
    const row = await request("/jobs/upload", { method: "POST", body });
    $("job-upload-hint").textContent =
      `已入库：${row.title}（认出 ${row.required_skills.length} 项技能，JD ${row.jd_text.length} 字）`;
    input.value = "";
    await loadJobs();
  } catch (err) {
    $("job-upload-hint").textContent = failureHint(err, "上传失败", "同名同公司会撞 409，那说明其实已经入库了");
    setFootError(err.message);
  } finally {
    $("btn-job-upload").disabled = false;
  }
});

/* 图片识别只填表单，不写库：认出来的字段灌进去，认不出的**不动**已经填好的值。
   岗位名是自然键的一半，机器猜歪了得由人在这儿拦下来。 */
function applyJobDraft(draft) {
  const filled = [];
  const blanks = [];
  if (draft.title) {
    $("job-title").value = draft.title;
    filled.push("岗位名称");
  } else {
    blanks.push("岗位名称（必填）");
  }
  if (draft.company) {
    $("job-company").value = draft.company;
    filled.push("公司");
  } else {
    blanks.push("公司");
  }
  if (Number(draft.min_years) > 0) {
    $("job-min-years").value = String(draft.min_years);
    filled.push("期望年限");
  } else {
    blanks.push("期望年限（按不限）");
  }
  if (draft.jd_text) {
    $("job-text").value = draft.jd_text;
    filled.push(`JD ${draft.jd_text.length} 字`);
  }
  const parts = [`认出 ${filled.length} 项：${filled.join("、") || "无"}`];
  if (blanks.length) parts.push(`没认出：${blanks.join("、")}`);
  if ((draft.required_skills || []).length) parts.push(`技能 ${draft.required_skills.length} 项`);
  if ((draft.notes || []).length) parts.push(draft.notes.join("；"));
  $("job-image-hint").textContent = parts.join(" · ");

  if ((draft.missing_required || []).includes("title")) {
    $("job-hint").textContent = "岗位名称没认出来，请手动填写（必填）";
    $("job-title").focus();
  }
}

$("btn-job-inspect").addEventListener("click", async () => {
  const input = $("job-image-file");
  if (!input.files.length) {
    $("job-image-hint").textContent = "先选一张图片";
    return;
  }
  const body = new FormData();
  body.append("file", input.files[0]);
  $("btn-job-inspect").disabled = true;
  $("job-image-hint").textContent = "识别中…图片要读一次模型，可能要等几十秒";
  try {
    applyJobDraft(await request("/jobs/inspect-image", { method: "POST", body }));
    input.value = "";
  } catch (err) {
    $("job-image-hint").textContent = "识别失败";
    setFootError(err.message);
  } finally {
    $("btn-job-inspect").disabled = false;
  }
});

/* ---------------- 报告 ---------------- */

/* 批注是"哪几题该改"的清单，不是拦门的闸门（ADR-0015）。列表按条数打标，
   详情页把批注按题号对回题目——两处都只读 `interview.review_issues`，不另立真相。 */
function issueCount(issues) {
  return issues ? issues.length : 0;
}

function issuesByIndex(issues) {
  const grouped = new Map();
  (issues || []).forEach((issue) => {
    if (issue.index === null || issue.index === undefined) return;
    if (!grouped.has(issue.index)) grouped.set(issue.index, []);
    grouped.get(issue.index).push(issue);
  });
  return grouped;
}

function issuesBadge(issues) {
  return issueCount(issues) ? badge(`${issueCount(issues)} 条批注`, "badge-warn") : "";
}

async function loadInterviews() {
  const rows = await request("/interviews/list?limit=100");
  $("report-count").textContent = `共 ${rows.length} 份`;
  $("report-list").innerHTML = rows.length
    ? rows
        .map((row) => `<li><button class="row" data-interview="${esc(row.id)}">
          <span class="row-main">
            <div class="row-title">${esc(reportPairLabel(row))}</div>
            <div class="row-sub"><code>${esc(row.id.slice(0, 12))}</code> · ${esc(row.created_at.slice(0, 19).replace("T", " "))}</div>
          </span>
          <span>${statusBadge(row.status)} ${badge(`${row.total_questions} 题`)} ${issuesBadge(row.review_issues)}</span>
        </button></li>`).join("")
    : '<li class="empty">还没有报告</li>';
}

/* "哪份简历 × 哪个岗位"直接读报告行上的快照列，不回 `resumes` / `jobs` 里查：
   简历能改名、岗位能下线（`job_id` 置空），而报告说的是生成那一刻的事实。 */
function reportPairLabel(report) {
  const resume = report.resume_name || report.resume_file_name || "未命名简历";
  const job = report.job_title || "未记录岗位";
  return report.job_company ? `${resume} × ${job}（${report.job_company}）` : `${resume} × ${job}`;
}

function reportFileName(report) {
  const resume = report.resume_name || report.resume_file_name || report.id.slice(0, 8);
  const job = report.job_title || report.id.slice(0, 8);
  return `面试题-${resume}-${job}.md`;
}

function renderQuestion(item, index, flagged) {
  const evidence = item.source === "generic" && !item.evidence
    ? ""
    : `<h4>依据出处（${esc(SOURCE[item.source] || item.source)}）</h4>
       <p class="evidence">${esc(item.evidence || "无逐字依据")}</p>`;
  // 被点名的题：徽章+就地批注。题目照样交付，只是面试官能看到哪几题模型自己都不满意。
  const notes = flagged && flagged.length
    ? `<div class="qflag"><h4>评审批注 ${flagged.length} 条</h4><ul class="checkpoints">${flagged
        .map((issue) => `<li>${esc(issue.problem || "")}${issue.fix ? ` · 建议：${esc(issue.fix)}` : ""}</li>`)
        .join("")}</ul></div>`
    : "";
  return `<article class="question${flagged && flagged.length ? " flagged" : ""}">
    <div class="question-head">
      <span class="qno">第 ${index + 1} 题</span>
      ${badge(DIFF[item.difficulty] || item.difficulty, DIFF_CLASS[item.difficulty])}
      ${badge("来源 " + (SOURCE[item.source] || item.source))}
      ${flagged && flagged.length ? badge(`${flagged.length} 条批注`, "badge-warn") : ""}
    </div>
    <p class="stem">${esc(item.stem)}</p>
    <h4>参考答案</h4><p class="checkpoints">${esc(item.reference_answer)}</p>
    <h4>考察点</h4><ul class="checkpoints">${item.checkpoints.map((c) => `<li>${esc(c)}</li>`).join("")}</ul>
    ${item.rubric && item.rubric.length ? `<h4>打分标准</h4><ul class="checkpoints">${item.rubric.map((r) => `<li>${esc(r)}</li>`).join("")}</ul>` : ""}
    ${item.follow_ups && item.follow_ups.length ? `<h4>追问</h4><ol class="ask">${item.follow_ups.map((f) => `<li>${esc(f)}</li>`).join("")}</ol>` : ""}
    ${evidence}
    ${notes}
  </article>`;
}

function renderReviewIssues(issues) {
  if (!issues || !issues.length) return "";
  const origin = { model: "模型", rule: "规则" };
  const rows = issues
    .map(
      (issue) => `<tr>
        <td>${issue.index === null || issue.index === undefined ? "整批" : `第 ${issue.index + 1} 题`}</td>
        <td>${esc(origin[issue.origin] || issue.origin || "")}</td>
        <td>${esc(issue.problem || "")}</td>
        <td>${esc(issue.fix || "")}</td>
      </tr>`
    )
    .join("");
  return `<div class="issues"><h3>评审批注 ${issues.length} 条（只标出该改的题，不影响交付）</h3>
    <table><thead><tr><th>题号</th><th>来源</th><th>问题</th><th>建议</th></tr></thead>
    <tbody>${rows}</tbody></table></div>`;
}

function renderReport(report, target) {
  const meta = target.querySelector(".report-meta");
  const title = target.querySelector("h2");
  title.textContent = reportPairLabel(report);
  meta.innerHTML = [
    statusBadge(report.status),
    badge(`${report.total_questions} 题`),
    badge("批次最新题数 " + report.questions.length),
    issuesBadge(report.review_issues),
    badge("report " + report.id.slice(0, 12)),
    badge("更新 " + report.updated_at.slice(0, 19).replace("T", " ")),
    // 岗位行已经不在了（`job_id` 被 SET NULL），标题来自快照列
    report.job_id ? "" : badge("岗位已下线", "badge-warn"),
  ].join(" ");

  const body = target.querySelector(".report-body");
  if (!report.questions.length) {
    // ADR-0015 之后评审不拦题目，空报告只剩"还在跑"与"没跑到收尾"两种成因。
    const note =
      report.status === "running"
        ? "报告还在生成中，题目由评审收尾一次性落库。"
        : report.status === "completed"
          ? "收尾拿到了空批次：出题环节这一轮没产出任何题（看下面的批注与错误）。"
          : "后台任务没能跑到收尾（上游报错或成本护栏触发），库里因此没有题目行。";
    body.innerHTML = `<p class="empty">${esc(note)}</p>` + renderReviewIssues(report.review_issues);
    return;
  }
  const flagged = issuesByIndex(report.review_issues);
  body.innerHTML =
    report.questions
      .map((item, index) => renderQuestion(item, index, flagged.get(index)))
      .join("") + renderReviewIssues(report.review_issues);
}

$("report-list").addEventListener("click", (event) => {
  const row = event.target.closest("[data-interview]");
  if (!row) return;
  request(`/interviews/${row.dataset.interview}`)
    .then((report) => {
      $("report-panel-list").hidden = false;
      renderReport(report, $("report-panel-list"));
      state.current = report;
    })
    .catch((err) => setFootError(err.message));
});

/* ---------------- 发起出题 ---------------- */

$("btn-generate").addEventListener("click", async () => {
  const resumeId = $("gen-resume").value;
  const jobId = $("gen-job").value;
  if (!resumeId || !jobId) {
    $("gen-hint").textContent = "简历和岗位都要选";
    return;
  }
  $("btn-generate").disabled = true;
  $("gen-hint").textContent = "已提交，后台跑图中…";
  $("report-panel").hidden = true;
  $("progress-panel").hidden = false;
  $("prog-task").textContent = "";
  $("prog-result").hidden = true;
  $("prog-error").hidden = true;
  renderSteps($("prog-steps"), []);
  try {
    const receipt = await request("/interviews/generate", {
      method: "POST",
      headers: { "Content-Type": "application/json" },
      body: JSON.stringify({ resume_id: resumeId, job_id: jobId }),
    });
    $("prog-task").textContent = receipt.task_id;
    $("gen-hint").textContent = `interview ${receipt.interview_id.slice(0, 8)} 进行中`;
    const task = await pollTask(receipt.task_id, (current) => {
      $("prog-status").innerHTML = statusBadge(current.status);
      renderSteps($("prog-steps"), current.progress.steps);
    });
    if (!task) {
      // 抢票、上限、或跨域读不到：图多半还在后台跑，只是这里没人报进度了。
      $("gen-hint").textContent = "读不到任务进度——稍后刷新报告列表确认，别直接再点一次生成";
      return;
    }
    if (task.status === "failed") {
      $("prog-error").hidden = false;
      $("prog-error-json").textContent = JSON.stringify(task.error, null, 2);
      $("gen-hint").textContent = "后台任务失败";
    } else {
      $("prog-result").hidden = false;
      $("prog-result-json").textContent = JSON.stringify(task.result, null, 2);
      $("gen-hint").textContent = "图跑完了";
    }
    const report = await request(`/interviews/${receipt.interview_id}`);
    state.current = report;
    $("report-panel").hidden = false;
    renderReport(report, $("report-panel"));
    await loadInterviews();
  } catch (err) {
    $("gen-hint").textContent = failureHint(err, "发起失败", "再生成一次就是两份报告、一轮模型开销");
    setFootError(err.message);
  } finally {
    $("btn-generate").disabled = false;
  }
});

/* ---------------- 导出与打印 ---------------- */

function toMarkdown(report) {
  const flagged = issuesByIndex(report.review_issues);
  const lines = [
    `# 面试题 · ${reportPairLabel(report)}`,
    "",
    `> 报告 id \`${report.id}\`｜状态 ${report.status}｜题目 ${report.questions.length} 道｜` +
      `批注 ${issueCount(report.review_issues)} 条｜` +
      `生成时间 ${report.created_at.slice(0, 19).replace("T", " ")}`,
    "",
  ];
  if (!report.questions.length) {
    lines.push("这份报告没有题目行（后台任务没跑到收尾，或仍在生成中）。", "");
  }
  report.questions.forEach((item, index) => {
    const notes = flagged.get(index) || [];
    lines.push(
      `## 第 ${index + 1} 题 · ${DIFF[item.difficulty] || item.difficulty} · ${SOURCE[item.source] || item.source}` +
        (notes.length ? ` · 批注 ${notes.length} 条` : ""),
      "",
      `**题干**：${item.stem}`,
      "",
      `**参考答案**：${item.reference_answer}`,
      "",
      "**考察点**",
      ...item.checkpoints.map((c) => `- ${c}`),
      ""
    );
    if (item.rubric && item.rubric.length) {
      lines.push("**打分标准**", ...item.rubric.map((r) => `- ${r}`), "");
    }
    if (item.follow_ups && item.follow_ups.length) {
      lines.push("**追问**", ...item.follow_ups.map((f) => `- ${f}`), "");
    }
    if (item.evidence) {
      lines.push(`**依据出处**：${item.evidence}`, "");
    }
    if (notes.length) {
      lines.push(
        "**评审批注**",
        ...notes.map((issue) => `- ${issue.problem || ""}${issue.fix ? ` 建议：${issue.fix}` : ""}`),
        ""
      );
    }
  });
  if (report.review_issues && report.review_issues.length) {
    lines.push(
      "## 评审批注",
      ...report.review_issues.map(
        (issue) =>
          `- 第 ${(issue.index ?? -1) + 1} 题（${issue.origin || "?"}）：${issue.problem || ""}` +
          `${issue.fix ? ` 建议：${issue.fix}` : ""}`
      ),
      ""
    );
  }
  return lines.join("\n");
}

function download(filename, text) {
  const blob = new Blob([text], { type: "text/markdown;charset=utf-8" });
  const url = URL.createObjectURL(blob);
  const link = document.createElement("a");
  link.href = url;
  link.download = filename;
  link.click();
  URL.revokeObjectURL(url);
}

$("btn-export").addEventListener("click", () => {
  const report = state.current;
  if (!report) {
    setFootError("先生成或打开一份报告");
    return;
  }
  download(reportFileName(report), toMarkdown(report));
});

$("btn-print").addEventListener("click", () => window.print());

/* ---------------- 启动 ---------------- */

const state = { current: null };

async function checkHealth() {
  const el = $("health");
  try {
    const health = await request("/health");
    el.textContent = `服务在线 · ${health.env}`;
    el.className = "badge badge-ok";
  } catch (err) {
    // "服务不可达"只在真的联系不上时才成立：file:// 下服务是活的，被拦的是响应的读。
    el.textContent = err.network && openedAsFile() ? "跨域被拦 · 请用服务端地址打开" : "服务不可达";
    el.className = "badge " + (err.network && openedAsFile() ? "badge-warn" : "badge-bad");
    setFootError(err.message);
  }
}

/* 顶部横幅：本地双击打开页面时唯一要说清的一件事——换地址。
   链接指向当前 API 根地址的主机，所以选了别的端口也不会指错地方。 */
function showOriginBanner() {
  const banner = $("origin-warning");
  banner.hidden = !openedAsFile();
  if (openedAsFile()) $("origin-link").href = $("origin-link").textContent = consoleUrl();
}

async function boot() {
  showOriginBanner();
  await checkHealth();
  try {
    // 简历与岗位先就位：报告列表要用它们把 id 翻成中文名。
    await Promise.all([loadResumes(), loadJobs()]);
    await loadInterviews();
  } catch (err) {
    setFootError(err.message);
  }
}

$("api-base").addEventListener("change", () => {
  boot();
});

boot();
