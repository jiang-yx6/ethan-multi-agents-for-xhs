/* 可独立打开的交互原型。演示状态与真实服务、账号和模型完全分离。 */
"use strict";

const STORAGE_KEY = "xhs-studio-prototype-v1";
const icons = {
  workspace: '<rect x="3" y="3" width="7" height="7" rx="1"/><rect x="14" y="3" width="7" height="7" rx="1"/><rect x="3" y="14" width="7" height="7" rx="1"/><path d="M14 17h7m-3.5-3.5v7"/>',
  archive: '<path d="M4 8h16v12H4zM3 4h18v4H3zm6 8h6"/>',
  profile: '<circle cx="12" cy="8" r="4"/><path d="M4 21v-2a8 8 0 0 1 16 0v2"/>',
  accounts: '<rect x="3" y="5" width="18" height="14" rx="3"/><circle cx="9" cy="11" r="2"/><path d="M6 16a3 3 0 0 1 6 0m3-6h3m-3 4h3"/>',
  settings: '<path d="M4 7h16M4 17h16"/><circle cx="8" cy="7" r="3"/><circle cx="16" cy="17" r="3"/>',
  reset: '<path d="M3 10a9 9 0 1 1 2 8M3 4v6h6"/>',
  plus: '<path d="M12 5v14M5 12h14"/>',
  arrow: '<path d="M5 12h14m-5-5 5 5-5 5"/>',
  close: '<path d="m6 6 12 12M18 6 6 18"/>',
};
const icon = (name) => `<svg viewBox="0 0 24 24" fill="none" stroke="currentColor" stroke-width="1.5" stroke-linecap="round" stroke-linejoin="round" aria-hidden="true">${icons[name] || icons.workspace}</svg>`;
const esc = (value = "") => String(value).replace(/[&<>"']/g, (c) => ({"&":"&amp;","<":"&lt;",">":"&gt;",'"':"&quot;","'":"&#39;"}[c]));
const clone = (value) => JSON.parse(JSON.stringify(value));
const statuses = {
  ideating: ["待选题", "warning", 0], researching: ["资料整理中", "neutral", 1],
  pending_review: ["待你审核", "warning", 3], approved: ["已批准 · 待发布", "", 4],
  submitting: ["正在提交", "neutral", 4], submitted: ["已提交 · 待核实", "warning", 4],
  unknown: ["结果待核实", "warning", 4], published: ["已确认发布", "", 5],
  failed: ["执行失败", "danger", 0], cancelled: ["已取消", "neutral", 0],
};
const stages = ["选题", "资料", "创作", "审核", "发布"];
const initialProfile = {name:"小林的周末", positioning:"用城市漫步，找回生活里的松弛感", audience:"想在周末出门走走的城市上班族", style:"自然、具体，有一点生活感", experiences:"周末常沿江散步，喜欢记录小店和街巷", preferences:"实用路线、低成本体验、真实照片", boundaries:"不编造亲身体验，不使用夸张的效果承诺"};
const initialBody = "周末不想赶行程，就沿着江边慢慢走。\n\n这次留给自己半天，不设打卡数量，也没有一定要走到的终点。\n\n① 先找一段容易抵达的江边步道\n出门前看一下天气和交通，带水、穿舒服的鞋。\n\n② 路过喜欢的地方，就停一会儿\n树荫下坐一坐，看看来往的人，顺手记录街边的小细节。\n\n③ 把返程留得轻松一点\n天色变暗前找好回家的车站，把这一天慢慢收好。\n\n具体路段、交通和照片，还需要补上这次实际出行的信息。\n你有一条愿意反复走的散步路线吗？";
function makeTask(id, title, status, index) {
  return {id, title, status, index, version:2, goal:"整理一篇有个人体验、方便照着走的周末路线分享", type:"攻略", body:initialBody,
    tags:["周末散步", "城市漫步", "生活里的小美好"], imageName:"散步封面设计稿", profile:clone(initialProfile),
    materials:"周末走过江边步道；希望补充交通和休息点。", constraints:"不写未经核实的价格，不编造体验。", useTrends:false,
    history:[{version:1, title:"周末江边散步攻略", note:"初版草稿", body:initialBody}, {version:2, title, note:"调整为更自然的表达，等待人工审核", body:initialBody}],
    events:["已根据需求生成 3 个选题方向", "已整理用户提供的出行素材", "草稿 v2 已完成，等待人工审核"]};
}
function initialState() {
  return {profile:clone(initialProfile), selected:"demo-walk", tasks:[
    makeTask("demo-walk", "不赶路的周末，沿江走走", "pending_review", "01"),
    makeTask("demo-coffee", "一杯咖啡里的周末松弛感", "ideating", "02"),
    makeTask("demo-home", "把小小的阳台，留给自己", "unknown", "03"),
    makeTask("demo-archive", "下班后，把生活慢慢捡起来", "published", "04")],
    model:{provider:"deepseek", model:"", api_key_env:"DEEPSEEK_API_KEY"}};
}
let state;
try { state = JSON.parse(localStorage.getItem(STORAGE_KEY)); } catch { state = null; }
if (!state || !Array.isArray(state.tasks) || !state.tasks.length) state = initialState();
let page = "workspace", filter = "all", query = "", tab = "edit", role = "ideation", selectedIdea = 0, dirty = false;
let toastTimer;
const images = new Map();
const main = document.querySelector("#main");
const modal = document.querySelector("#modal");
const task = () => state.tasks.find((item) => item.id === state.selected) || state.tasks[0];
const statusBadge = (value) => `<span class="status ${statuses[value][1]}">${statuses[value][0]}</span>`;
function persist() { try { localStorage.setItem(STORAGE_KEY, JSON.stringify(state)); } catch { toast("浏览器存储不可用，本次更改仅在当前页面保留。"); } }
function toast(message) {
  const element = document.querySelector("#toast"); element.textContent = message; element.classList.add("visible");
  clearTimeout(toastTimer); toastTimer = setTimeout(() => element.classList.remove("visible"), 3200);
}
function navigate(next) {
  if (dirty && !window.confirm("当前编辑尚未保存，是否放弃这次编辑？")) return;
  dirty = false; page = next; filter = next === "archive" ? "published" : "all"; tab = "edit"; query = "";
  document.querySelectorAll("[data-page]").forEach((el) => el.classList.toggle("active", el.dataset.page === page));
  document.querySelector("#page-label").textContent = {workspace:"创作工作台", archive:"内容归档", profile:"创作画像", accounts:"发布账号", settings:"技能与模型"}[page];
  render();
}
function pageHeading(eyebrow, title, note, action = "") {
  return `<div class="page-heading"><div><p class="eyebrow">${eyebrow}</p><h1>${title}</h1><p class="heading-note">${note}</p></div>${action}</div>`;
}
function cover(title) {
  return `<div class="cover"><div class="cover-top">A SLOW WEEKEND / 生活记录</div><div class="cover-title">${esc(title)}</div><svg viewBox="0 0 300 160" fill="none" aria-hidden="true"><path d="M170 170C70 110 285 50 150-10" stroke="#c2ceba" stroke-width="59"/><path d="M225 170C135 100 305 70 215 0" stroke="#a8b9a0" stroke-width="2" stroke-dasharray="4 6"/><path d="M20 145h90M38 131V95h28v36m14 0V72h20v59M253 79V45h16v34" stroke="#99a98b" stroke-width="1.2"/><circle cx="198" cy="87" r="4" fill="#b56043"/><circle cx="121" cy="24" r="4" fill="#b56043"/></svg><span class="cover-tag">WALK & FEEL</span></div>`;
}
function taskList() {
  const items = state.tasks.filter((item) => (!query || item.title.includes(query)) &&
    (filter === "all" || (filter === "attention" && ["ideating","pending_review","approved","unknown","submitted","failed"].includes(item.status)) || (filter === "published" && item.status === "published")));
  return `<div class="section-label">${page === "archive" ? "已发布的内容" : "你的创作"}<span class="count">${String(items.length).padStart(2,"0")}</span></div>
    <input class="task-search" id="task-search" aria-label="搜索创作任务" placeholder="搜索一篇创作…" value="${esc(query)}">
    <div class="filter-tabs"><button data-filter="all" class="${filter === "all" ? "active" : ""}">全部</button><button data-filter="attention" class="${filter === "attention" ? "active" : ""}">待我处理</button><button data-filter="published" class="${filter === "published" ? "active" : ""}">已发布</button></div>
    <div class="task-list">${items.map((item) => `<button class="task-card ${item.id === state.selected ? "active" : ""}" data-task="${esc(item.id)}" aria-pressed="${item.id === state.selected}"><div class="task-card-top"><span class="task-number">NOTE ${item.index}</span>${statusBadge(item.status)}</div><h3>${esc(item.title)}</h3><small>${esc(item.type)} · v${item.version} · ${esc(item.profile.name)}</small><div class="mini-dots">${stages.map((_,i) => `<i class="${i < statuses[item.status][2] ? "done" : i === statuses[item.status][2] ? "current" : ""}"></i>`).join("")}</div></button>`).join("") || '<div class="empty">这里暂时没有创作。<p>换个筛选条件试试。</p></div>'}</div>`;
}
function workspace() {
  const current = task();
  return pageHeading(page === "archive" ? "COLLECTION / 内容归档" : "WORKSPACE / 创作工作台", page === "archive" ? "好内容，值得被留下。" : "把一个想法，写成一篇好内容。", "从你的真实需求出发，保留每一次创作的判断与选择。", `<button class="button primary" data-action="new">${icon("plus")}新建创作</button>`)
    + `<div class="workspace"><section class="task-column" aria-label="任务列表">${taskList()}</section><article class="editor-panel" aria-label="创作详情">${editor(current)}</article><aside class="context-column" aria-label="创作参考">${context(current)}</aside></div>`;
}
function editor(current) {
  const index = statuses[current.status][2];
  return `<div class="editor-heading"><div class="task-context"><span>小红书 · ${esc(current.type)}</span><span class="dot">/</span><span>当前版本 v${current.version}</span>${statusBadge(current.status)}</div><h2>${esc(current.title)}</h2><p class="task-goal">创作目标：${esc(current.goal)}</p></div>
    <div class="stages" aria-label="当前进度">${stages.map((name,i) => `<div class="stage ${i < index ? "done" : i === index ? "current" : ""}" ${i === index ? 'aria-current="step"' : ""}><span class="stage-dot">${i < index ? "✓" : i+1}</span>${name}</div>`).join("")}</div>
    ${["ideating", "researching", "failed", "cancelled"].includes(current.status) ? specialContent(current) : `<div class="editor-tabs"><button data-tab="edit" class="${tab === "edit" ? "active" : ""}">正文编辑</button><button data-tab="materials" class="${tab === "materials" ? "active" : ""}">素材与依据</button><button data-tab="history" class="${tab === "history" ? "active" : ""}">版本记录</button><span class="version-label">${current.history.length} 个版本</span></div><div class="editor-body">${editorContent(current)}</div>`}
    <div class="editor-footer"><span id="save-state" class="save-state">${["submitted","unknown"].includes(current.status) ? "结果核实前，请保留当前版本" : current.status === "approved" ? "批准已绑定当前版本" : current.status === "published" ? "发布记录已归档" : "所有修改都会保留为新版本"}</span><div class="footer-actions">${footer(current)}</div></div>`;
}
function editorContent(current) {
  if (tab === "materials") return `<div class="detail-block"><h3>你的素材</h3><p>${esc(current.materials || "尚未提供具体素材")}</p></div><div class="detail-block"><h3>本次约束</h3><p>${esc(current.constraints || "遵循创作画像中的表达边界")}</p></div><div class="detail-block"><h3>热点与外部资料</h3><p>${current.useTrends ? "已开启参考热点；正式接入时展示每条资料的标题、时间与来源链接。" : "本次围绕用户需求创作，不依赖热点。"}</p></div><div class="notice">原型中的正文用于展示编辑与审核流程；事实、路线和体验需由创作者核实。</div>`;
  if (tab === "history") return current.history.slice().reverse().map((entry) => `<div class="history-item"><time>VERSION ${String(entry.version).padStart(2,"0")}</time><h3>${esc(entry.title)}</h3><p>${esc(entry.note)}</p><button class="button text" data-history="${entry.version}">查看该版本 →</button></div>`).join("");
  const locked = !["pending_review","approved"].includes(current.status);
  const notice = current.status === "submitted" ? '<div class="notice">内容已提交，尚待核实实际发布结果。确认上线后再登记笔记链接。</div>' : current.status === "unknown" ? '<div class="notice">上次提交结果不明，请先到平台后台核实。当前任务不会再次提交。</div>' : current.status === "published" ? `<div class="notice success">已确认发布 · ${current.url ? '核实方式：人工确认' : '演示归档记录'}${current.url ? `<br><a class="inline-link" href="${esc(current.url)}" target="_blank" rel="noopener noreferrer">查看笔记链接 ↗</a>` : ""}</div>` : "";
  return `${notice}<div class="field-heading"><label for="draft-title">标题</label><span class="counter" id="title-counter">${current.title.length} / 20</span></div><input id="draft-title" class="title-input" value="${esc(current.title)}" ${locked ? "readonly" : ""}><label class="hidden" for="draft-body">正文</label><textarea id="draft-body" class="body-input" ${locked ? "readonly" : ""}>${esc(current.body)}</textarea><div class="field-heading"><span class="field-label">正文与话题</span><span id="body-counter" class="counter">${current.body.length + current.tags.join(" #").length + 2} / 1000</span></div><div class="tags">${current.tags.map((value) => `<span class="tag"># ${esc(value)}</span>`).join("")}</div><div class="media-row"><div class="media-thumb">${images.has(current.id) ? `<img src="${images.get(current.id)}" alt="当前上传图片预览">` : "图"}</div><div><h3>${esc(current.imageName)}</h3><p>1 张图片 · 发布前核对配图</p></div>${!locked ? '<button class="button small" data-action="upload">替换图片</button><input id="image-input" class="hidden" type="file" accept="image/jpeg,image/png,image/webp">' : ""}</div>`;
}
function footer(current) {
  if (current.status === "ideating") return '<button class="button text" data-action="cancel">取消任务</button><button class="button primary" data-action="choose">采用这个方向 '+icon("arrow")+'</button>';
  if (current.status === "researching" || current.status === "submitting") return '<button class="button" disabled>正在处理，请稍候…</button>';
  if (current.status === "pending_review") return '<button class="button" data-action="feedback">提出修改意见</button><button class="button" id="save-draft" data-action="save" disabled>保存修改</button><button class="button primary" id="approve-draft" data-action="approve">批准当前版本</button>';
  if (current.status === "approved") return '<button class="button" id="save-draft" data-action="save" disabled>保存修改</button><button class="button primary" id="publish-draft" data-action="publish">准备发布 '+icon("arrow")+'</button>';
  if (["submitted","unknown"].includes(current.status)) return '<button class="button primary" data-action="verify">核实发布结果</button>';
  if (current.status === "failed") return '<button class="button" data-page="settings">检查模型配置</button><button class="button primary" data-action="retry">重试任务</button>';
  return '<button class="button" data-action="export">导出文案</button>';
}
function specialContent(current) {
  if (current.status === "researching") return '<div class="editor-body progress-box"><div class="spinner"></div><h3>正在整理这个方向…</h3><p>原型正在演示资料整理、创作和审核进度。</p></div>';
  if (current.status === "failed") return '<div class="editor-body"><div class="notice error">模型配置未就绪。请检查模型 ID 和对应密钥环境变量，修复后可以重试。</div></div>';
  if (current.status === "cancelled") return '<div class="empty">这次创作已经取消。<p>新的想法，可以从「新建创作」开始。</p></div>';
  const options = [`${current.title}`, "从真实体验出发，整理一份实用清单", "把日常里的一个小发现，讲给同频的人"];
  return `<div class="editor-body"><p class="idea-intro">为你准备了 3 个方向。选一个你愿意写、也有素材支撑的。</p>${options.map((title,i) => `<label class="idea-card ${i === selectedIdea ? "selected" : ""}"><input type="radio" name="idea" value="${i}" ${i === selectedIdea ? "checked" : ""}><div class="idea-index">DIRECTION 0${i+1}</div><h3>${esc(title)}</h3><p>${["结合你的生活记录定位，以具体体验吸引有相同需求的读者。", "结构清楚，方便收藏；需要补充实际用过或走过的信息。", "表达负担更轻，适合从一个真实细节展开个人观点。"][i]}</p><span class="tag">${["生活分享", "实用清单", "个人观察"][i]}</span></label>`).join("")}</div>`;
}
function context(current) {
  return `<section class="side-card"><div class="section-label">这篇内容，为谁而写<button class="button text" data-page="profile" aria-label="编辑创作画像">↗</button></div><div class="profile-line"><span class="avatar">林</span><div><h3>${esc(current.profile.name)}</h3><p>城市生活 · 周末灵感</p></div></div><div class="profile-detail"><strong>读者</strong> ${esc(current.profile.audience)}<br><strong>语气</strong> ${esc(current.profile.style)}</div></section><section class="side-card"><div class="section-label">发布前，再看一眼<span class="count">02</span></div><div class="review-row"><span class="check">✓</span><p>内容结构完整<small>标题、正文和话题已齐备</small></p></div><div class="review-row"><span class="warning-mark">○</span><p>请核对实际路线和配图<small>将占位信息替换为真实素材</small></p></div><div class="review-note">自动检查提供参考，批准时请确认这篇内容代表你的真实表达。</div></section><section class="side-card preview-card"><div class="section-label">笔记预览<span class="count">封面方案</span></div>${images.has(current.id) ? `<img src="${images.get(current.id)}" alt="笔记封面预览" style="width:100%;max-height:240px;object-fit:cover;border-radius:7px">` : cover(current.title)}<p class="preview-subtitle">${esc(current.title)}</p><div class="preview-foot"><span>${esc(current.profile.name)}</span><span>♡ &nbsp; 收藏你的日常</span></div></section><p class="side-hint">任务按阶段保留进度。<button data-action="activity">查看本次执行记录</button></p>`;
}
function profilePage() {
  const p = state.profile;
  return pageHeading("IDENTITY / 创作画像", "先认识自己，再开始表达。", "画像决定内容的方向与语气。真实经历和表达偏好分开保存。") + `<div class="page-grid"><section class="surface"><h2>我的创作画像</h2><p>新任务使用保存后的画像快照，已有任务保留创建时的版本。</p><form id="profile-form"><div class="form-grid">${[["name","画像名称"],["positioning","账号定位"],["audience","目标读者"],["style","表达风格"]].map(([key,label]) => `<div><label for="profile-${key}">${label}</label><input id="profile-${key}" name="${key}" required value="${esc(p[key])}"></div>`).join("")}${[["experiences","真实经历","仅填写确实发生过、愿意用于创作的经历。"],["preferences","表达偏好","喜欢的内容类型、结构或表达方式。"],["boundaries","表达边界","不愿涉及的话题和不接受的表达方式。"]].map(([key,label,help]) => `<div class="full"><label for="profile-${key}">${label}</label><textarea id="profile-${key}" name="${key}">${esc(p[key])}</textarea><p class="form-help">${help}</p></div>`).join("")}</div><div class="form-bottom"><button class="button primary" type="submit">保存创作画像</button></div></form></section><aside class="surface profile-sample"><span class="label">YOUR OWN VOICE</span><h2>有自己的视角，<br>内容才会被记住。</h2><blockquote>“${esc(p.positioning)}”</blockquote><p>把真实的生活留给内容，<br>把表达的选择留给自己。</p></aside></div>`;
}
function accountsPage() {
  return pageHeading("CHANNELS / 发布账号", "每篇内容，都有明确的去处。", "创作画像定义表达，发布账号决定实际提交的目标。") + `<div class="page-grid"><section class="surface"><h2>我的发布账号</h2><p>当前展示的是独立的演示账号，与本机真实浏览器登录态无关。</p><div class="account-card"><div class="platform-logo">小红书</div><div class="account-description"><h3>周末手记 <span class="status neutral">演示账号</span></h3><p>xiaohongshu / demo-main</p></div><button class="button small" data-action="account-help">登录与身份校验</button></div><div class="capabilities"><span class="capability">图文发布</span><span class="capability">人工核实回执</span><span class="capability unsupported">视频 · 尚未接入</span><span class="capability unsupported">定时发布 · 尚未接入</span></div><hr><p>正式接入后，在本地浏览器完成扫码，再由服务端校验平台用户 ID。页面只展示绑定结果。</p></section><aside class="surface"><h2>清楚地知道，内容到了哪一步。</h2><div class="timeline"><p><strong>已批准</strong>当前版本通过人工确认，等待选择账号提交。</p><p><strong>已提交 / 结果待核实</strong>先到创作者后台检查，保留当前内容版本。</p><p><strong>已确认发布</strong>登记实际笔记链接与核实方式，再进入内容归档。</p></div></aside></div>`;
}
const skillSets = {
  ideation:[["热点发现","skill-trending-topics","发现和筛选相关热点"],["热点策划","skill-trend-rider","把已有热点转成适合账号的角度"],["选题评估","skill-topic-evaluator","评估选题价值和制作条件"],["资讯情报","skill-news-intelligence","补充行业动态和选题线索"]],
  research:[["资讯情报","skill-news-intelligence","整理背景信息和资料来源"]],
  creation:[["社媒内容创作","social-content","组织标题、正文和互动引导"]],
  review:[["质量审核","skill-quality-gate","检查内容质量与平台适配"]], publishing:[],
};
function settingsPage() {
  return pageHeading("CAPABILITIES / 技能与模型", "让每个助手，各司其职。", "按角色查看技能范围，并为模型驱动的阶段配置调用方式。") + `<div class="page-grid"><section class="surface"><h2>角色与技能</h2><p>模型按需读取已授权的技能。发布操作仍需单独人工确认。</p><div class="settings-tabs">${Object.entries({ideation:"选题",research:"资料",creation:"创作",review:"审核",publishing:"发布"}).map(([value,label]) => `<button data-role="${value}" class="${role === value ? "active" : ""}">${label}</button>`).join("")}</div>${skillSets[role].map(([name,id,description]) => `<div class="skill-row"><span class="skill-icon">✳</span><div><h3>${name}</h3><p>${description}</p><small style="color:var(--muted);font-size:8px">${id}</small></div><span class="status">已授权</span></div>`).join("") || '<div class="empty">发布角色当前没有技能授权。<p>实际发布由平台连接器和人工门禁控制。</p></div>'}<p class="form-help" style="margin-top:20px">原型展示当前 registry.json 的授权范围，管理能力将在正式接口中提供。</p></section><section class="surface"><h2>选题模型</h2><p>当前只有选题阶段接入真实模型；其他阶段暂用 Mock 或规则。</p><form id="model-form"><div class="form-grid"><div class="full"><label for="provider">模型服务商</label><select id="provider" name="provider">${["deepseek","openai","anthropic","qwen","zhipu","minimax","ollama"].map((value) => `<option ${state.model.provider === value ? "selected" : ""}>${value}</option>`).join("")}</select></div><div class="full"><label for="model-id">模型 ID</label><input id="model-id" name="model" placeholder="填写账号可用的模型 ID" value="${esc(state.model.model)}" required></div><div class="full"><label for="key-env">密钥环境变量名称</label><input id="key-env" name="api_key_env" value="${esc(state.model.api_key_env)}" required><p class="form-help">这里填写变量名；原型不接收或保存真实密钥。</p></div></div><div class="form-bottom"><button class="button primary" type="submit">保存演示配置</button></div></form></section></div>`;
}
function render() {
  main.innerHTML = page === "profile" ? profilePage() : page === "accounts" ? accountsPage() : page === "settings" ? settingsPage() : workspace();
  persist();
}
function openModal(title, body) {
  modal.innerHTML = `<div class="modal-heading"><h2 id="modal-title">${title}</h2><button class="icon-button" data-close aria-label="关闭弹窗">${icon("close")}</button></div><div class="modal-content">${body}</div>`;
  modal.showModal();
}
function newTask() {
  openModal("这次，想写些什么？", `<form id="new-task-form"><div class="form-field"><label for="new-goal">创作目标</label><textarea id="new-goal" name="goal" required placeholder="例如：分享一条适合周末放松的散步路线，让读者可以照着走。"></textarea></div><div class="form-grid"><div><label for="new-type">内容形式</label><select id="new-type" name="type"><option>分享</option><option>攻略</option><option>观点</option><option>生活记录</option><option>测评</option></select></div><div><label for="new-profile">创作画像</label><input id="new-profile" value="${esc(state.profile.name)}" readonly></div></div><div class="form-field" style="margin-top:18px"><label for="new-materials">已有素材 <small>选填</small></label><textarea id="new-materials" name="materials" placeholder="写下真实经历、观察或已有资料。"></textarea></div><label class="checkbox-line"><input type="checkbox" name="useTrends">结合我提供的相关热点</label><p class="form-help">首版以手动提供热点为主；没有热点也能围绕需求创作。</p><div class="modal-foot"><button type="button" class="button" data-close>稍后再写</button><button class="button primary" type="submit">开始选题 ${icon("arrow")}</button></div></form>`);
}
function saveDraft() {
  const current = task();
  const title = document.querySelector("#draft-title")?.value.trim();
  const body = document.querySelector("#draft-body")?.value.trim();
  if (!title || !body) return toast("标题和正文都需要填写。");
  current.title = title; current.body = body; current.version++; current.status = "pending_review";
  current.history.push({version:current.version,title,body,note:"人工保存修改，需重新审核"}); current.events.push(`已保存 v${current.version}，等待重新审核`);
  dirty = false; render(); toast("新版本已保存，旧批准已失效。");
}
function markDirty() {
  dirty = true;
  const save = document.querySelector("#save-draft"); if (save) save.disabled = false;
  for (const id of ["approve-draft","publish-draft"]) { const button = document.getElementById(id); if (button) button.disabled = true; }
  document.querySelector("#save-state").textContent = "有未保存修改 · 保存后重新审核";
  const titleLength = document.querySelector("#draft-title").value.length;
  const bodyLength = document.querySelector("#draft-body").value.length + task().tags.join(" #").length + 2;
  document.querySelector("#title-counter").textContent = `${titleLength} / 20`;
  document.querySelector("#title-counter").classList.toggle("over", titleLength > 20);
  document.querySelector("#body-counter").textContent = `${bodyLength} / 1000`;
  document.querySelector("#body-counter").classList.toggle("over", bodyLength > 1000);
}
function publishModal() {
  const current = task();
  if (current.title.length > 20 || current.body.length + current.tags.join(" #").length + 2 > 1000) return toast("请先调整标题或正文长度，保存新版本并重新审核。");
  openModal("确认本次发布", `<dl class="publish-summary"><dt>发布账号</dt><dd>小红书 · 周末手记 <span class="status neutral">演示账号</span></dd><dt>内容版本</dt><dd>v${current.version} · 已人工批准</dd><dt>笔记标题</dt><dd>${esc(current.title)}</dd><dt>图片</dt><dd>${esc(current.imageName)}</dd></dl><div class="notice">此处演示“已批准 → 已提交 → 人工核实”的过程，不会向真实平台发帖。</div><form id="publish-form"><label class="checkbox-line"><input type="checkbox" required>我已核对本次版本和目标账号</label><div class="modal-foot"><button type="button" class="button" data-close>返回检查</button><button class="button primary" type="submit">模拟提交一次</button></div></form>`);
}
function verifyModal() {
  openModal("核实发布结果", `<p style="font-size:11px;color:var(--muted);margin-bottom:18px">请先在创作者后台确认内容和账号，再登记实际笔记链接。登记不会再次提交内容。</p><form id="verify-form"><div class="form-field"><label for="note-url">小红书笔记链接</label><input id="note-url" name="url" type="url" placeholder="https://www.xiaohongshu.com/explore/…" required></div><label class="checkbox-line"><input type="checkbox" required>我已核实这篇笔记实际发布</label><p class="form-error" id="verify-error" aria-live="polite"></p><div class="modal-foot"><button type="button" class="button" data-close>继续等待</button><button class="button primary" type="submit">登记已发布</button></div></form>`);
}
function downloadNote() {
  const current = task();
  const url = URL.createObjectURL(new Blob([`# ${current.title}\n\n${current.body}\n\n${current.tags.map((t)=>"#"+t).join(" ")}`], {type:"text/markdown;charset=utf-8"}));
  const link = document.createElement("a"); link.href = url; link.download = "演示笔记.md"; link.click(); setTimeout(() => URL.revokeObjectURL(url), 1000); toast("演示文案已导出。");
}
document.addEventListener("click", (event) => {
  const close = event.target.closest("[data-close]"); if (close) return modal.close();
  const nav = event.target.closest("[data-page]"); if (nav) return navigate(nav.dataset.page);
  const taskButton = event.target.closest("[data-task]");
  if (taskButton) { if (dirty && !window.confirm("放弃尚未保存的编辑并切换任务？")) return; state.selected = taskButton.dataset.task; dirty = false; selectedIdea = 0; tab = "edit"; return render(); }
  const filterButton = event.target.closest("[data-filter]"); if (filterButton) { if (dirty) return toast("请先保存正文修改，再切换筛选。"); filter = filterButton.dataset.filter; return render(); }
  const tabButton = event.target.closest("[data-tab]"); if (tabButton) { if (dirty) return toast("请先保存正文修改，再切换标签。"); tab = tabButton.dataset.tab; return render(); }
  const roleButton = event.target.closest("[data-role]"); if (roleButton) { role = roleButton.dataset.role; return render(); }
  const history = event.target.closest("[data-history]");
  if (history) { const entry = task().history.find((item) => item.version === Number(history.dataset.history)); return openModal(`版本 v${entry.version} · 只读`, `<h3>${esc(entry.title)}</h3><div class="prose-preview" style="margin-top:18px">${esc(entry.body)}</div>`); }
  const action = event.target.closest("[data-action]")?.dataset.action;
  if (action === "new") { if (dirty) return toast("请先保存当前修改，再创建新任务。"); return newTask(); }
  if (action === "save") return saveDraft();
  if (action === "upload") { if (dirty) return toast("请先保存正文修改，再替换图片。"); return document.querySelector("#image-input").click(); }
  if (action === "approve") { task().status = "approved"; task().events.push(`人工批准 v${task().version}`); render(); return toast("当前版本已批准，可以准备发布。"); }
  if (action === "publish") return publishModal();
  if (action === "verify") return verifyModal();
  if (action === "export") return downloadNote();
  if (action === "activity") return openModal("本次执行记录", `<div class="timeline">${task().events.map((entry) => `<p><strong>${esc(entry)}</strong>演示记录</p>`).join("")}</div>`);
  if (action === "account-help") return openModal("登录与身份校验", '<div class="notice">正式接入时，此按钮通知服务端打开本地浏览器，扫码完成后返回身份校验结果。当前为交互原型。</div><p style="font-size:11px">账号 Cookie 由服务端浏览器会话维护。前端展示平台用户、绑定状态和可用能力。</p>');
  if (action === "feedback") return openModal("这篇内容，想怎样调整？", '<form id="feedback-form"><div class="form-field"><label for="feedback">修改意见</label><textarea id="feedback" name="feedback" required placeholder="例如：保留轻松的语气，补上交通方式和路线起点。"></textarea></div><div class="modal-foot"><button type="button" class="button" data-close>暂不修改</button><button class="button primary" type="submit">提交修改意见</button></div></form>');
  if (action === "cancel") { task().status = "cancelled"; render(); return toast("演示任务已取消。"); }
  if (action === "retry") { task().status = "ideating"; render(); return toast("已恢复到演示选题阶段。"); }
  if (action === "choose") {
    const current = task(); const titles = [current.title,"从真实体验出发，整理一份实用清单","把日常里的一个小发现，讲给同频的人"];
    current.title = titles[selectedIdea]; current.status = "researching"; current.events.push("已人工选择选题，开始演示资料整理"); render();
    setTimeout(() => { current.status = "pending_review"; current.history[1].title = current.title; current.events.push("演示草稿已完成，等待人工审核"); persist(); if (task().id === current.id) render(); }, 1300);
  }
});
document.addEventListener("input", (event) => {
  if (["draft-title","draft-body"].includes(event.target.id)) markDirty();
  if (event.target.id === "task-search") {
    query = event.target.value; const caret = event.target.selectionStart;
    document.querySelector(".task-column").innerHTML = taskList(); const input = document.querySelector("#task-search"); input.focus(); input.setSelectionRange(caret,caret);
  }
});
document.addEventListener("change", (event) => {
  if (event.target.name === "idea") { selectedIdea = Number(event.target.value); render(); }
  if (event.target.id === "image-input" && event.target.files[0]) {
    const file = event.target.files[0]; if (!["image/jpeg","image/png","image/webp"].includes(file.type) || file.size > 20*1024*1024) return toast("请选择 20 MB 以内的 JPG、PNG 或 WebP 图片。");
    const reader = new FileReader(); const current = task(); reader.onload = () => { images.set(current.id, reader.result); current.imageName = file.name; current.version++; current.status = "pending_review"; current.history.push({version:current.version,title:current.title,body:current.body,note:"替换图片，需重新审核"}); dirty = false; render(); toast("配图已替换为新版本，请重新审核；图片预览仅本次打开有效。"); }; reader.readAsDataURL(file);
  }
});
document.addEventListener("submit", (event) => {
  event.preventDefault(); const form = event.target; const values = Object.fromEntries(new FormData(form));
  if (form.id === "new-task-form") {
    if (!values.goal?.trim()) return;
    const current = makeTask("demo-"+Date.now(), values.goal.trim(), "ideating", String(state.tasks.length+1).padStart(2,"0"));
    Object.assign(current,{goal:values.goal.trim(),materials:values.materials,type:values.type,profile:clone(state.profile),useTrends:Boolean(values.useTrends),events:["已创建演示任务，等待人工选择方向"]});
    state.tasks.unshift(current); state.selected = current.id; page = "workspace"; filter = "all"; selectedIdea = 0; modal.close(); navigate("workspace"); toast("新的创作已经开始，请选择一个方向。");
  }
  if (form.id === "profile-form") { state.profile = values; render(); toast("画像已保存，将用于新创建的任务。"); }
  if (form.id === "model-form") { state.model = values; render(); toast("演示配置已保存，尚未连接模型服务。"); }
  if (form.id === "publish-form") {
    const current = task(); current.status = "submitting"; modal.close(); render();
    setTimeout(() => { current.status = "submitted"; current.events.push(`v${current.version} 演示提交完成，待人工核实`); persist(); render(); toast("模拟提交完成。核实发布结果后，再登记笔记链接。"); }, 1000);
  }
  if (form.id === "verify-form") {
    let parsed; try { parsed = new URL(values.url); } catch { parsed = null; }
    if (!parsed || parsed.protocol !== "https:" || parsed.host !== "www.xiaohongshu.com" || !/^\/(explore|discovery\/item)\/[a-f\d]{24}\/?$/i.test(parsed.pathname)) { document.querySelector("#verify-error").textContent = "请输入 https://www.xiaohongshu.com/explore/<24 位笔记 ID> 形式的链接。"; return; }
    task().url = parsed.origin + parsed.pathname; task().status = "published"; task().events.push("已登记演示人工回执，进入内容归档"); modal.close(); render(); toast("人工回执已登记，创作已归档。");
  }
  if (form.id === "feedback-form") {
    if (!values.feedback?.trim()) return;
    const current = task(); current.version++; current.status = "pending_review"; current.body += "\n\n【待落实的修改意见】\n"+values.feedback.trim(); current.history.push({version:current.version,title:current.title,body:current.body,note:"记录修改意见，需人工落实"}); current.events.push("已记录修改意见，生成新版本"); modal.close(); render(); toast("修改意见已记录；原型保留正文，等待人工落实。");
  }
});
document.querySelector("#reset-demo").addEventListener("click", () => {
  if (!window.confirm("重置此原型的演示数据？真实项目数据不受影响。")) return;
  state = initialState(); images.clear(); dirty = false; navigate("workspace"); toast("已恢复初始演示数据。");
});
window.addEventListener("beforeunload", (event) => { if (dirty) { event.preventDefault(); event.returnValue = ""; } });
document.querySelectorAll("[data-icon]").forEach((element) => { element.innerHTML = icon(element.dataset.icon); });
render();
