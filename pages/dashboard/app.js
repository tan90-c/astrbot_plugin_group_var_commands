const bridge = window.AstrBotPluginView || window.AstrBotPluginPage;
if (!bridge) {
  document.body.innerHTML =
    '<p style="color:#ef4444;padding:24px">未检测到 AstrBotPluginView bridge，请确认 AstrBot 版本 >= 4.9.2 且插件已重载</p>';
  throw new Error("no bridge");
}

const $ = (id) => document.getElementById(id);
const PARAM_HINT =
  "默认入参（main 里按名字取用即可，用不到的不写）：\n" +
  "  args        自定义变量分组字典，形如 {\"分组名\": {\"变量名\": \"值\", ...}, ...}\n" +
  "  cmd         主指令名\n" +
  "  alias       命中的别名（用主指令名触发时为空串）\n" +
  "  value       参数列表：触发词后的内容按空格拆分，如 \"状态 1区 2区\" → value=[\"1区\", \"2区\"]\n" +
  "  user_id     发言人唯一ID（QQ官方机器人为member_openid加密串，同机器人内稳定）\n" +
  "全局函数模块：脚本顶部 import 模块名 或 from 模块名 import 函数，直接调用\n" +
  "持久存储：import store → d=store.load()（普通字典用法），改完 d.save() 写回；store.load(name=\"用途\") 按用途另开文件；store.save(d, name=...) 亦可；跨重启保留";

let cfg = { script_timeout: 30, reply_format: "markdown", groups: {} };
let sessions = [];
let selGroup = "";   // "" = 首页
let selMod = "";
let selCmd = "";
let selAgg = "";
let dirty = false;
let saving = false;

// ---------- 工具 ----------

function toast(msg, bad = false) {
  const t = $("toast");
  t.textContent = msg;
  t.style.color = bad ? "#ef4444" : "#10b981";
  t.classList.add("show");
  clearTimeout(toast._h);
  toast._h = setTimeout(() => t.classList.remove("show"), 2200);
}

function markDirty() {
  dirty = true;
  $("dirtyTip").textContent = "● 待保存";
}

// 所有修改自动保存（串行防竞态）
async function saveNow(silent = false) {
  if (saving) { saveNow._again = true; return; }
  saving = true;
  try {
    await bridge.apiPost("config/save", cfg);
    dirty = false;
    $("dirtyTip").textContent = "";
    if (!silent) toast("已保存 ✔");
  } catch (e) {
    $("dirtyTip").textContent = "● 未保存";
    toast("保存失败: " + (e && e.message ? e.message : e), true);
  } finally {
    saving = false;
    if (saveNow._again) { saveNow._again = false; saveNow(true); }
  }
}

function askConfirm(msg) {
  return new Promise((resolve) => {
    $("modalMsg").textContent = msg;
    $("modalMask").style.display = "flex";
    const yes = $("modalYes"), no = $("modalNo");
    const done = (val) => {
      $("modalMask").style.display = "none";
      yes.removeEventListener("click", onYes);
      no.removeEventListener("click", onNo);
      resolve(val);
    };
    const onYes = () => done(true);
    const onNo = () => done(false);
    yes.addEventListener("click", onYes);
    no.addEventListener("click", onNo);
  });
}

function gids() {
  return Object.keys(cfg.groups || {});
}

function gkeyInfo(key) {
  const tail = key.includes(":") ? key.split(":").pop() : key;
  const s = sessions.find((x) => x.umo === key || x.group_id === tail);
  return {
    name: (s && s.group_name) || tail || key,
    sub: s && s.platform ? s.platform : key,
    type: s && s.type ? s.type : "group",
  };
}

function gcur() {
  if (!selGroup) return null;
  const g = cfg.groups[selGroup];
  if (!g) return null;
  g.note = typeof g.note === "string" ? g.note : "";
  g.bind = typeof g.bind === "string" ? g.bind : "";
  g.modules = g.modules || {};
  g.groups = g.groups || {};
  g.commands = g.commands || {};
  g.var_notes = g.var_notes || {};
  g.bind_mode = g.bind_mode === "code" ? "code" : "all";
  return g;
}

function gBound() {
  const g = gcur();
  return !!(g && g.bind);
}

// 生效配置：绑定会话取主会话；主会话不存在时视为空配置
function gEff() {
  const g = gcur();
  if (!g) return null;
  if (g.bind) {
    const m = cfg.groups[g.bind];
    if (!m) return { note: "", bind: "", modules: {}, groups: {}, commands: {} };
    return m;
  }
  return g;
}

function normGroup(g) {
  g.note = typeof g.note === "string" ? g.note : "";
  g.bind = typeof g.bind === "string" ? g.bind : "";
  g.var_notes = (g.var_notes && typeof g.var_notes === "object") ? g.var_notes : {};
  g.bind_mode = g.bind_mode === "code" ? "code" : "all";
  g.modules = g.modules || {};
  g.groups = g.groups || {};
  g.commands = g.commands || {};
  for (const c of Object.values(g.commands)) {
    c.aliases = Array.isArray(c.aliases) ? c.aliases : [];
    c.script = c.script || "";
  }
}

// ---------- 加载 / 会话列表 ----------

async function loadCfg() {
  const data = await bridge.apiGet("config");
  cfg = { script_timeout: data.script_timeout ?? 30, reply_format: data.reply_format ?? "markdown", llm_fallback: data.llm_fallback ?? true, groups: data.groups || {} };
  for (const g of Object.values(cfg.groups)) normGroup(g);
  if (selGroup && !cfg.groups[selGroup]) selGroup = "";
  renderAll();
}

async function loadSessions() {
  try {
    const data = await bridge.apiGet("sessions");
    sessions = Array.isArray(data.sessions) ? data.sessions : [];
  } catch (e) {
    sessions = [];
  }
  const sel = $("sessionSelect");
  sel.innerHTML = "";
  if (!sessions.length) {
    sel.appendChild(new Option("（暂无已见会话：机器人进群发言后自动出现）"));
    return;
  }
  for (const x of sessions) {
    const bound = gids().includes(x.umo || x.group_id) ? "（已绑定）" : "";
    const ic = x.type === "private" ? "💬私聊" : "🤖群聊";
    sel.appendChild(new Option(`${ic} ${x.group_name || x.group_id}（${x.platform || "?"}）${bound}`, x.group_id));
  }
}

// ---------- Python 代码编辑器 ----------

const PY_KW = new Set(("def class return if elif else for while in not and or import from as try except finally " +
  "with lambda None True False pass break continue global yield raise assert del is async await").split(/\s+/));
const PY_DEC = new Set(["def", "class", "import", "from", "return", "lambda", "async"]);

function escHtml(s) {
  return s.replace(/&/g, "&amp;").replace(/</g, "&lt;").replace(/>/g, "&gt;");
}

function pyHighlight(code) {
  let out = "", i = 0;
  const n = code.length;
  const isWord = (c) => c && /[A-Za-z0-9_]/.test(c);
  while (i < n) {
    const c = code[i];
    if (c === "#") {
      let j = code.indexOf("\n", i);
      if (j < 0) j = n;
      out += '<span class="c">' + escHtml(code.slice(i, j)) + "</span>";
      i = j;
    } else if (c === '"' || c === "'") {
      const triple = code.slice(i, i + 3) === c.repeat(3);
      const q = triple ? c.repeat(3) : c;
      let j = i + q.length;
      while (j < n) {
        if (code[j] === "\\") { j += 2; continue; }
        if (code.startsWith(q, j)) { j += q.length; break; }
        if (!triple && code[j] === "\n") break;
        j++;
      }
      out += '<span class="s">' + escHtml(code.slice(i, j)) + "</span>";
      i = j;
    } else if (/[A-Za-z_]/.test(c)) {
      let j = i;
      while (j < n && /[A-Za-z0-9_]/.test(code[j])) j++;
      const w = code.slice(i, j);
      if (PY_KW.has(w)) out += '<span class="' + (PY_DEC.has(w) ? "d" : "k") + '">' + w + "</span>";
      else out += escHtml(w);
      i = j;
    } else if (/[0-9]/.test(c) && !isWord(code[i - 1])) {
      let j = i;
      while (j < n && /[0-9a-fA-F.xX]/.test(code[j])) j++;
      out += '<span class="n">' + code.slice(i, j) + "</span>";
      i = j;
    } else {
      out += escHtml(c);
      i++;
    }
  }
  return out;
}

function buildCodeEditor(getValue, setValue, foldKey) {
  const box = document.createElement("div");
  box.className = "codebox";
  const bar = document.createElement("div");
  bar.className = "bar";
  bar.innerHTML = "<span>PYTHON 脚本</span><span>Tab 缩进 · 失焦自动保存</span>";
  const fk = foldKey || "default";
  const fb = document.createElement("button");
  fb.className = "fold-btn";
  fb.type = "button";
  fb.innerHTML = CHEVRON;
  const applyF = (f) => {
    box.classList.toggle("folded", f);
    fb.title = f ? "展开" : "收起";
  };
  applyF(!!editorFold.get(fk));
  fb.onclick = (e) => {
    e.preventDefault();
    e.stopPropagation();
    const f = !box.classList.contains("folded");
    editorFold.set(fk, f);
    applyF(f);
  };
  bar.appendChild(fb);
  const body = document.createElement("div");
  body.className = "body";
  const gutter = document.createElement("pre");
  gutter.className = "gutter";
  const wrap = document.createElement("div");
  wrap.className = "wrap";
  const hl = document.createElement("pre");
  hl.className = "hl";
  const ta = document.createElement("textarea");
  ta.className = "input";
  ta.spellcheck = false;
  ta.value = getValue();
  wrap.append(hl, ta);
  body.append(gutter, wrap);
  box.append(bar, body);

  const syncScroll = () => {
    hl.scrollTop = ta.scrollTop;
    hl.scrollLeft = ta.scrollLeft;
    gutter.scrollTop = ta.scrollTop;
  };
  const refresh = () => {
    const v = ta.value;
    hl.innerHTML = pyHighlight(v) + "\n";
    const lines = v.split("\n").length;
    gutter.textContent = Array.from({ length: lines }, (_, i) => i + 1).join("\n");
    syncScroll();
  };
  ta.addEventListener("input", () => {
    setValue(ta.value);
    markDirty();
    refresh();
  });
  ta.addEventListener("blur", () => {
    if (dirty) saveNow(true);
  });
  ta.addEventListener("scroll", syncScroll);
  ta.addEventListener("keydown", (e) => {
    if (e.key === "Tab") {
      e.preventDefault();
      document.execCommand("insertText", false, "    ");
    } else if (e.key === "Enter") {
      const pos = ta.selectionStart;
      const lineStart = ta.value.lastIndexOf("\n", pos - 1) + 1;
      const indent = (ta.value.slice(lineStart, pos).match(/^\s*/) || [""])[0];
      const extra = /:\s*$/.test(ta.value.slice(lineStart, pos)) ? "    " : "";
      e.preventDefault();
      document.execCommand("insertText", false, "\n" + indent + extra);
    }
  });
  refresh();
  return box;
}

// ---------- 折叠 ----------

const CHEVRON = '<svg width="12" height="12" viewBox="0 0 16 16" fill="none" stroke="currentColor" stroke-width="2" stroke-linecap="round" stroke-linejoin="round"><path d="M3 6l5 5 5-5"/></svg>';
const editorFold = new Map();

function initCardFolds() {
  for (const card of document.querySelectorAll(".card")) {
    const h2 = card.querySelector(":scope > h2");
    if (!h2 || h2.querySelector(".fold-btn")) continue;
    const b = document.createElement("button");
    b.className = "fold-btn";
    b.type = "button";
    b.innerHTML = CHEVRON;
    b.title = "收起";
    b.onclick = (e) => {
      e.preventDefault();
      e.stopPropagation();
      const f = card.classList.toggle("folded");
      b.title = f ? "展开" : "收起";
    };
    h2.appendChild(b);
  }
}

// ---------- 收割式键值表（DOM 顺序即数据顺序） ----------

function kvRow(k, v, note) {
  const tr = document.createElement("tr");
  const c1 = document.createElement("td");
  const c2 = document.createElement("td");
  const c3 = document.createElement("td");
  const ki = document.createElement("input");
  ki.value = k;
  ki.placeholder = "键";
  ki.style.width = "92%";
  const vi = document.createElement("input");
  vi.value = v == null ? "" : typeof v === "object" ? JSON.stringify(v) : String(v);
  vi.placeholder = "值";
  vi.style.width = "92%";
  const del = document.createElement("button");
  del.className = "btn small danger";
  del.textContent = "删除";
  c1.appendChild(ki);
  c2.appendChild(vi);
  c3.appendChild(del);
  tr.append(c1, c2, c3);
  const cN = document.createElement("td");
  const ni = document.createElement("input");
  ni.value = note || "";
  ni.placeholder = "说明（用途备注，不影响脚本）";
  ni.style.width = "96%";
  cN.appendChild(ni);
  tr.insertBefore(cN, c3);
  tr._note = ni;
  c1.style.width = "27%";
  c2.style.width = "27%";
  cN.style.width = "34%";
  tr._key = ki;
  tr._val = vi;
  tr._del = del;
  return tr;
}

function kvTable(host, getDict, setDict, getNotes, setNotes) {
  host.innerHTML = "";
  const dict = getDict();
  const entries = Object.entries(dict);
  if (!entries.length) {
    host.innerHTML = '<div class="empty">暂无数据</div>';
    return;
  }
  const notes = getNotes ? getNotes() : {};
  const tb = document.createElement("table");
  tb.innerHTML = '<tr><th style="width:27%">键</th><th style="width:27%">值</th><th style="width:34%">说明</th><th>排序</th><th>操作</th></tr>';
  for (const [k, v] of entries) tb.appendChild(kvRow(k, v, notes[k]));
  host.appendChild(tb);

  const harvest = () => {
    const out = {};
    const nd = {};
    for (const tr of tb.querySelectorAll("tr")) {
      if (!tr._key) continue;
      const k = tr._key.value.trim();
      if (!k) continue;
      out[k] = tr._val.value;
      if (tr._note) {
        const nv = tr._note.value.trim();
        if (nv) nd[k] = nv;
      }
    }
    if (setNotes) setNotes(nd);
    setDict(out);
    markDirty();
    renderRaw();
    saveNow(true);
  };
  tb.addEventListener("change", harvest);
  for (const tr of Array.from(tb.querySelectorAll("tr"))) {
    if (!tr._del) continue;
    tr._del.onclick = () => {
      const k = tr._key.value.trim();
      tr.remove();
      harvest();
      if (k) toast(`已删除并保存：${k}`);
    };
    {
      const cell = document.createElement("td");
      const up = document.createElement("button");
      up.className = "btn small secondary";
      up.textContent = "↑";
      const down = document.createElement("button");
      down.className = "btn small secondary";
      down.textContent = "↓";
      down.style.marginLeft = "4px";
      up.onclick = () => {
        const p = tr.previousElementSibling;
        if (p && p._key) { tb.insertBefore(tr, p); harvest(); }
      };
      down.onclick = () => {
        const nx = tr.nextElementSibling;
        if (nx && nx._key) { tb.insertBefore(nx, tr); harvest(); }
      };
      cell.append(up, down);
      tr.insertBefore(cell, tr.children[3]);
    }
  }
}

// ---------- 渲染 ----------

function renderSidebar() {
  const list = $("tabList");
  list.innerHTML = "";
  const kw = $("searchBox").value.trim().toLowerCase();
  const mk = (cls, avatar, name, sub, active, onclick) => {
    const d = document.createElement("div");
    d.className = "tab" + (cls ? " " + cls : "") + (active ? " active" : "");
    const av = document.createElement("div");
    av.className = "avatar";
    av.textContent = avatar;
    const meta = document.createElement("div");
    meta.className = "meta";
    const t1 = document.createElement("div");
    t1.className = "tname";
    t1.textContent = name;
    const t2 = document.createElement("div");
    t2.className = "tsub";
    t2.textContent = sub;
    meta.append(t1, t2);
    d.append(av, meta);
    d.onclick = onclick;
    list.appendChild(d);
    return d;
  };
  if (!kw || "首页".includes(kw)) mk("home", "🏠", "首页", "绑定群会话 / 全局设置", selGroup === "", () => {
    selGroup = ""; selCmd = ""; selAgg = ""; renderAll();
  });
  for (const key of gids()) {
    const info = gkeyInfo(key);
    const g = cfg.groups[key];
    const disp = (g && g.note) || info.name;
    const eff = (g && g.bind && cfg.groups[g.bind]) || g;
    const nCmd = Object.keys((eff && eff.commands) || {}).length;
    if (kw && !disp.toLowerCase().includes(kw) && !info.name.toLowerCase().includes(kw) && !key.toLowerCase().includes(kw)) continue;
    mk("", info.type === "private" ? "💬" : "🤖", disp, `${nCmd} 条指令${g && g.bind ? " · 🔗绑定" : ""} · ${info.sub}`, selGroup === key, () => {
      selGroup = key; selCmd = ""; selAgg = ""; selMod = ""; renderAll();
    });
  }
}

function renderViews() {
  $("homeView").style.display = selGroup ? "none" : "";
  $("groupView").style.display = selGroup ? "" : "none";
  if (!selGroup) return;
  const g = gcur();
  if (!g) { selGroup = ""; renderAll(); return; }
  const g0 = gcur();
  $("gTitle").textContent = gkeyInfo(selGroup).name;
  $("groupNote").value = g0 ? g0.note : "";
  const banner = $("bindBanner");
  if (g0 && g0.bind) {
    banner.style.display = "";
    if (!cfg.groups[g0.bind]) {
      banner.textContent = `⚠ 绑定的主会话已不存在，本会话指令不会触发。请点击「解绑会话」解除，或改绑其他主会话`;
    } else if (g0.bind_mode === "code") {
      banner.textContent = `🔗 已绑定到「${gkeyInfo(g0.bind).name}」（仅函数模块与指令跟随主会话，可预览复制、不可修改）；变量分组是本会话自己的内容，可正常编辑`;
    } else {
      banner.textContent = `🔗 已绑定到「${gkeyInfo(g0.bind).name}」：内容跟随主会话（可折叠/展开预览，代码可选中复制、滚动查看，不可修改）；修改主会话内容会即时反映到这里`;
    }
  } else {
    banner.style.display = "none";
    banner.textContent = "";
  }
}

function renderAll() {
  $("scriptTimeout").value = cfg.script_timeout;
  $("replyFormat").value = cfg.reply_format || "markdown";
  $("llmFallback").value = cfg.llm_fallback === false ? "0" : "1";
  renderSidebar();
  renderViews();
  if (!selGroup) return;
  const g = gEff();

  const as = $("aggSelect");
  as.innerHTML = "";
  const aggNames = Object.keys(g.groups);
  for (const n of aggNames) as.appendChild(new Option(n, n));
  if (selAgg && !aggNames.includes(selAgg)) selAgg = "";
  if (!selAgg) selAgg = aggNames[0] || "";
  as.value = selAgg;
  $("btnRenameAgg").disabled = !selAgg;
  $("btnCopyAgg").disabled = !selAgg;
  $("btnDelAgg").disabled = !selAgg;
  $("btnAddAggKv").disabled = !selAgg;

  if (selAgg) {
    kvTable($("aggTable"),
      () => g.groups[selAgg],
      (d) => { g.groups[selAgg] = d; },
      () => g.var_notes[selAgg] || {},
      (n) => {
        if (Object.keys(n).length) g.var_notes[selAgg] = n;
        else delete g.var_notes[selAgg];
      });
  } else {
    $("aggTable").innerHTML = '<div class="empty">请先添加分组</div>';
  }

  const ms = $("modSelect");
  ms.innerHTML = "";
  const modNames = g ? Object.keys(g.modules) : [];
  for (const n of modNames) ms.appendChild(new Option(n, n));
  if (selMod && !modNames.includes(selMod)) selMod = "";
  if (!selMod) selMod = modNames[0] || "";
  ms.value = selMod;
  $("btnDelMod").disabled = !(g && selMod);
  renderModBody();

  const cs = $("cmdSelect");
  cs.innerHTML = "";
  const cmdNames = Object.keys(g.commands);
  for (const n of cmdNames) cs.appendChild(new Option(n, n));
  if (selCmd && !cmdNames.includes(selCmd)) selCmd = "";
  if (!selCmd) selCmd = cmdNames[0] || "";
  cs.value = selCmd;
  renderCmdBody();

  renderCopySelect();
  renderRaw();
  applyBoundLock();
  $("dirtyTip").textContent = dirty ? "● 未保存" : "";
}

const RENDER_MANAGED = new Set(["btnRenameAgg", "btnCopyAgg", "btnDelAgg", "btnAddAggKv",
  "btnDelMod", "btnCopyAll", "btnBind", "btnBindCode", "copyFromSelect"]);

function applyBoundLock() {
  const g = gcur();
  const bound = !!(g && g.bind);
  const codeOnly = bound && g.bind_mode === "code";
  // 只读模式：灰罩纯视觉（pointer-events:none 不挡选复制/滚动/折叠），
  // 防修改靠 readOnly（可选中复制）与 disabled；折叠按钮恒可用；
  // 解锁时 render 管理的按钮由其自身状态负责，其余直接恢复
  const set = (id, lock) => {
    const el = $(id);
    if (!el) return;
    el.classList.toggle("locked", lock);
    for (const f of el.querySelectorAll("input, textarea")) f.readOnly = lock;
    for (const b of el.querySelectorAll("select, button")) {
      if (b.classList.contains("fold-btn")) continue;
      if (lock) { b.disabled = true; continue; }
      if (!RENDER_MANAGED.has(b.id)) b.disabled = false;
    }
  };
  set("varsCard", bound && !codeOnly);
  set("modCard", bound);
  set("cmdCard", bound);
  set("advCard", bound);
}

function renderCopySelect() {
  const sel = $("copyFromSelect");
  sel.innerHTML = "";
  const g = gcur();
  const bound = !!(g && g.bind);
  const others = gids().filter((k) => k !== selGroup);
  for (const k of others) {
    sel.appendChild(new Option(gkeyInfo(k).name + (cfg.groups[k].bind ? "（已是绑定会话）" : ""), k));
  }
  if (!others.length) sel.appendChild(new Option("（没有其他标签页）", ""));
  if (bound && cfg.groups[g.bind]) sel.value = g.bind;
  sel.disabled = bound;
  $("btnCopyAll").disabled = bound || !others.length;
  const codeMode = bound && g.bind_mode === "code";
  $("btnBind").textContent = bound ? "解绑会话" : "绑定会话";
  $("btnBind").disabled = bound ? false : !others.length;
  $("btnBindCode").textContent = bound ? (codeMode ? "改为全量绑定" : "改为仅绑定函数和指令") : "仅绑定函数和指令";
  $("btnBindCode").disabled = bound ? false : !others.length;
}

function renderModBody() {
  const host = $("modBody");
  host.innerHTML = "";
  const g = gEff();
  if (!g || !selMod || g.modules[selMod] === undefined) {
    host.innerHTML = '<div class="empty">请先创建或选择模块</div>';
    return;
  }
  const lab = document.createElement("div");
  lab.className = "hint";
  lab.textContent = "模块 " + selMod + ".py（定义若干函数，指令脚本 import 即用）";
  host.append(lab, buildCodeEditor(() => g.modules[selMod], (v) => { g.modules[selMod] = v; }, "mod:" + selMod));
}

function renderCmdBody() {
  const host = $("cmdBody");
  host.innerHTML = "";
  const g = gEff();
  if (!g || !selCmd || !g.commands[selCmd]) {
    host.innerHTML = '<div class="empty">请先创建或选择指令</div>';
    return;
  }
  const c = g.commands[selCmd];
  c.script = c.script || "";
  c.aliases = Array.isArray(c.aliases) ? c.aliases : [];

  const hint = document.createElement("pre");
  hint.className = "hint paramhint";
  hint.textContent = PARAM_HINT;
  host.appendChild(hint);

  const lab = document.createElement("div");
  lab.className = "hint";
  lab.textContent = "Python 脚本（def main(...)；返回值转字符串直接回复；报错超时静默不崩）";
  host.append(lab, buildCodeEditor(() => c.script, (v) => { c.script = v; }, "cmd:" + selCmd));

  const labAlias = document.createElement("div");
  labAlias.className = "hint";
  labAlias.style.marginTop = "12px";
  labAlias.textContent = "附属指令名（别名，可多个）：固定文字触发词，发送「别名 参数…」即可触发（别名不允许含花括号，不支持模板占位写法）；与其他指令主名重复时主名指令优先，别名间重复时后创建的指令生效；长触发词优先";
  const tbAl = document.createElement("table");
  tbAl.innerHTML = "<tr><th>别名</th><th>操作</th></tr>";
  const alRows = [];
  c.aliases.forEach((a) => {
    const tr = document.createElement("tr");
    const td1 = document.createElement("td");
    const td2 = document.createElement("td");
    const ai = document.createElement("input");
    ai.value = a;
    ai.placeholder = "别名，如 查玩家";
    const del = document.createElement("button");
    del.className = "btn small danger";
    del.textContent = "删除";
    td1.appendChild(ai);
    td2.appendChild(del);
    tr.append(td1, td2);
    alRows.push({ tr, ai, del });
    tbAl.appendChild(tr);
  });
  const harvestAl = () => {
    const out = [];
    for (const r of alRows) {
      const a = r.ai.value.trim();
      if (a && !out.includes(a)) out.push(a);
    }
    c.aliases = out;
    markDirty();
    renderRaw();
    saveNow(true);
  };
  tbAl.addEventListener("change", harvestAl);
  for (const r of alRows) {
    r.del.onclick = () => {
      r.tr.remove();
      alRows.splice(alRows.indexOf(r), 1);
      harvestAl();
    };
  }
  const btnAddAl = document.createElement("button");
  btnAddAl.className = "btn small";
  btnAddAl.style.marginTop = "8px";
  btnAddAl.textContent = "+ 添加别名";
  btnAddAl.onclick = () => {
    c.aliases.push("");
    renderCmdBody();
  };
  host.append(labAlias, tbAl, btnAddAl);
}

function renderRaw() {
  const g = selGroup && cfg.groups[selGroup] ? cfg.groups[selGroup] : {};
  $("rawJson").value = JSON.stringify(g, null, 2);
}

// ---------- 事件 ----------

$("searchBox").addEventListener("input", renderSidebar);
$("btnGoHome").onclick = () => { selGroup = ""; selCmd = ""; selAgg = ""; selMod = ""; renderAll(); };

$("replyFormat").addEventListener("change", (e) => {
  cfg.reply_format = e.target.value;
  markDirty();
  renderRaw();
  saveNow();
});
$("llmFallback").addEventListener("change", (e) => {
  cfg.llm_fallback = e.target.value === "1";
  markDirty();
  saveNow();
  toast(cfg.llm_fallback ? "报错/无输出时将交给大模型兜底" : "已拦截大模型兜底：静默就是真静默");
});
$("scriptTimeout").addEventListener("change", (e) => {
  let v = parseInt(e.target.value || "30", 10);
  if (isNaN(v)) v = 30;
  cfg.script_timeout = Math.min(600, Math.max(1, v));
  e.target.value = cfg.script_timeout;
  markDirty();
  saveNow();
});

$("btnReload").onclick = async () => {
  if (dirty && !(await askConfirm("有未保存修改（保存失败内容），重新加载将丢弃。继续？"))) return;
  try {
    await loadCfg();
    toast("已重新加载");
  } catch (e) {
    toast("加载失败: " + (e.message || e), true);
  }
};

$("btnRefreshSessions").onclick = async () => {
  await loadSessions();
  toast("会话列表已刷新");
};

$("btnAddFromSession").onclick = async () => {
  if (!sessions.length) {
    await loadSessions();
    if (!sessions.length) return toast("暂无可选会话，请先让机器人在目标群发一条消息", true);
  }
  const gid = $("sessionSelect").value;
  const x = sessions.find((v) => v.group_id === gid);
  if (!x) return toast("请选择会话", true);
  const key = x.umo || x.group_id;
  cfg.groups = cfg.groups || {};
  if (!cfg.groups[key]) {
    cfg.groups[key] = { groups: {}, commands: {} };
    markDirty();
    await saveNow(true);
    toast(`已新增标签页：${x.group_name || gid}`);
  } else {
    toast("该会话已绑定，切换到对应标签页");
  }
  selGroup = key;
  selCmd = "";
  selAgg = "";
  selMod = "";
  renderAll();
};

$("groupNote").addEventListener("change", async (e) => {
  const g = gcur();
  if (!g) return;
  g.note = e.target.value.trim();
  markDirty();
  renderSidebar();
  renderRaw();
  await saveNow(true);
  toast(g.note ? `备注已保存：${g.note}` : "备注已清空");
});
$("btnDelGroup").onclick = async () => {
  if (!selGroup) return;
  const info = gkeyInfo(selGroup);
  if (!(await askConfirm(`确定删除群「${info.name}」的全部配置？\n（立即生效，不可恢复）`))) return;
  delete cfg.groups[selGroup];
  selGroup = "";
  selCmd = "";
  selAgg = "";
  selMod = "";
  markDirty();
  renderAll();
  await saveNow();
};

$("aggSelect").addEventListener("change", (e) => { selAgg = e.target.value; renderAll(); });
$("btnAddAgg").onclick = async () => {
  const g = gcur();
  if (!g) return toast("请先进入群标签页", true);
  const n = $("newAgg").value.trim();
  if (!n) return toast("请输入分组名", true);
  if (g.groups[n]) return toast("分组已存在", true);
  g.groups[n] = {};
  selAgg = n;
  $("newAgg").value = "";
  markDirty();
  renderAll();
  await saveNow();
};
$("btnCopyAgg").onclick = async () => {
  const g = gcur();
  if (!g || !selAgg) return toast("请先选择要复制的分组", true);
  const src = selAgg;
  let n = $("newAgg").value.trim();
  if (!n) {
    n = src + " 副本";
    let i = 2;
    while (g.groups[n] !== undefined) n = src + " 副本" + i++;
  }
  if (g.groups[n]) return toast("同名分组已存在", true);
  g.groups[n] = JSON.parse(JSON.stringify(g.groups[src]));
  if (g.var_notes[src]) g.var_notes[n] = JSON.parse(JSON.stringify(g.var_notes[src]));
  selAgg = n;
  $("newAgg").value = "";
  markDirty();
  renderAll();
  await saveNow();
  toast(`已复制分组：${src} → ${n}`);
};

$("btnRenameAgg").onclick = async () => {
  const g = gcur();
  if (!g || !selAgg) return toast("请先选择分组", true);
  const n = $("newAgg").value.trim();
  if (!n) return toast("请先在「分组名」输入框填写新名称", true);
  if (n === selAgg) return;
  if (g.groups[n]) return toast("同名分组已存在", true);
  g.groups[n] = g.groups[selAgg];
  delete g.groups[selAgg];
  if (g.var_notes[selAgg]) { g.var_notes[n] = g.var_notes[selAgg]; delete g.var_notes[selAgg]; }
  selAgg = n;
  $("newAgg").value = "";
  markDirty();
  renderAll();
  await saveNow();
};
$("btnDelAgg").onclick = async () => {
  const g = gcur();
  if (!g || !selAgg) return toast("请先选择分组", true);
  if (!(await askConfirm(`确定删除分组「${selAgg}」？`))) return;
  delete g.groups[selAgg];
  delete g.var_notes[selAgg];
  selAgg = "";
  markDirty();
  renderAll();
  await saveNow();
};
$("btnAddAggKv").onclick = () => {
  const g = gcur();
  if (!g || !selAgg) return toast("请先选择分组", true);
  let k = "key";
  let i = 2;
  while (g.groups[selAgg][k] !== undefined) k = "key" + i++;
  g.groups[selAgg][k] = "";
  markDirty();
  renderAll();
  saveNow(true);
};

$("modSelect").addEventListener("change", (e) => { selMod = e.target.value; renderModBody(); });
$("btnAddMod").onclick = async () => {
  const g = gcur();
  if (!g) return toast("请先进入群标签页", true);
  const n = $("newMod").value.trim();
  if (!n) return toast("请输入模块名", true);
  if (!/^[A-Za-z_][A-Za-z0-9_]*$/.test(n)) return toast("模块名须为合法 Python 标识符", true);
  if (g.modules[n] !== undefined) return toast("模块已存在", true);
  g.modules[n] = "";
  selMod = n;
  $("newMod").value = "";
  markDirty();
  renderAll();
  await saveNow();
};
$("btnDelMod").onclick = async () => {
  const g = gcur();
  if (!g || !selMod) return toast("请先选择模块", true);
  if (!(await askConfirm(`确定删除模块「${selMod}」？引用它的指令脚本将报错（静默）。`))) return;
  delete g.modules[selMod];
  selMod = "";
  markDirty();
  renderAll();
  await saveNow();
};
$("cmdSelect").addEventListener("change", (e) => { selCmd = e.target.value; renderCmdBody(); });
$("btnAddCmd").onclick = async () => {
  const g = gcur();
  if (!g) return toast("请先进入群标签页", true);
  const n = $("newCmd").value.trim();
  if (!n) return toast("请输入指令名", true);
  if (/\s/.test(n)) return toast("指令名不能含空格", true);
  if (g.commands[n]) return toast("指令已存在", true);
  g.commands[n] = { script: "", aliases: [] };
  selCmd = n;
  $("newCmd").value = "";
  markDirty();
  renderAll();
  await saveNow();
};
$("btnDelCmd").onclick = async () => {
  const g = gcur();
  if (!g || !selCmd) return toast("请先选择要删除的指令", true);
  if (!(await askConfirm(`确定删除指令「${selCmd}」？`))) return;
  delete g.commands[selCmd];
  selCmd = "";
  markDirty();
  renderAll();
  await saveNow();
};

$("btnRawApply").onclick = async () => {
  const g = gcur();
  if (!g) return;
  if (g.bind) return toast("绑定会话不可编辑，请先解绑", true);
  let data;
  try {
    data = JSON.parse($("rawJson").value);
  } catch (e) {
    return toast("JSON 解析失败: " + e.message, true);
  }
  if (!data || typeof data !== "object" || Array.isArray(data)) {
    return toast("必须是 JSON 对象（当前群的配置）", true);
  }
  if (!(await askConfirm("将用文本框里的 JSON 覆盖当前会话的全部配置（变量分组/函数模块/指令）。继续？"))) return;
  cfg.groups[selGroup] = data;
  normGroup(cfg.groups[selGroup]);
  selAgg = "";
  selCmd = "";
  selMod = "";
  markDirty();
  renderAll();
  await saveNow();
  toast("已导入并覆盖当前会话配置");
};

async function copyAllFrom(srcKey) {
  const g = gcur();
  const src = cfg.groups[srcKey];
  if (!g || !src) return toast("请选择要复制的来源标签页", true);
  if (g.bind) return toast("绑定会话不可编辑，请先解绑", true);
  const name = gkeyInfo(srcKey).name;
  const hasTarget = Object.keys(g.groups).length || Object.keys(g.commands).length || Object.keys(g.modules).length;
  if (hasTarget && !(await askConfirm(`当前标签页的变量分组、函数模块和指令将被「${name}」的覆盖。继续？`))) return;
  g.groups = JSON.parse(JSON.stringify(src.groups || {}));
  g.modules = JSON.parse(JSON.stringify(src.modules || {}));
  g.commands = JSON.parse(JSON.stringify(src.commands || {}));
  g.var_notes = JSON.parse(JSON.stringify(src.var_notes || {}));
  selAgg = "";
  selCmd = "";
  selMod = "";
  markDirty();
  renderAll();
  await saveNow();
  toast(`已复制「${name}」的变量分组、函数模块和指令`);
}

$("btnCopyAll").onclick = async () => {
  const srcKey = $("copyFromSelect").value;
  if (!srcKey) return toast("请选择要复制的来源标签页", true);
  await copyAllFrom(srcKey);
};

$("btnBind").onclick = async () => {
  const g = gcur();
  if (!g) return toast("请先进入群标签页", true);
  if (g.bind) {
    if (!(await askConfirm("解绑后本标签页恢复可编辑，原有内容保留不变（绑定期未使用）。继续？"))) return;
    g.bind = "";
    g.bind_mode = "all";
    markDirty();
    renderAll();
    await saveNow();
    return toast("已解绑");
  }
  const srcKey = $("copyFromSelect").value;
  if (!srcKey || !cfg.groups[srcKey]) return toast("请选择要绑定的主会话标签页", true);
  if (cfg.groups[srcKey].bind) return toast("该标签页自身是绑定会话，不能作为主会话", true);
  const name = gkeyInfo(srcKey).name;
  if (!(await askConfirm(`绑定后本标签页变为只读，内容跟随「${name}」变动；原有内容保留，解绑后恢复。继续？`))) return;
  g.bind = srcKey;
  g.bind_mode = "all";
  markDirty();
  renderAll();
  await saveNow();
  toast(`已绑定到「${name}」`);
};

$("btnBindCode").onclick = async () => {
  const g = gcur();
  if (!g) return toast("请先进入群标签页", true);
  if (g.bind) {
    const toCode = g.bind_mode !== "code";
    const msg = toCode
      ? "切换为仅绑定函数和指令：变量分组恢复本会话自己的内容并解除锁定。继续？"
      : "切换为全量绑定：变量分组也将跟随主会话且不可编辑（本会话原变量保留，切回即恢复）。继续？";
    if (!(await askConfirm(msg))) return;
    g.bind_mode = toCode ? "code" : "all";
    markDirty();
    renderAll();
    await saveNow();
    return toast(toCode ? "已切换：仅函数和指令跟随主会话" : "已切换：全量跟随主会话");
  }
  const srcKey = $("copyFromSelect").value;
  if (!srcKey || !cfg.groups[srcKey]) return toast("请选择要绑定的主会话标签页", true);
  if (cfg.groups[srcKey].bind) return toast("该标签页自身是绑定会话，不能作为主会话", true);
  const name = gkeyInfo(srcKey).name;
  if (!(await askConfirm(`绑定后本标签页的函数模块与指令只读跟随「${name}」；变量分组仍是本会话自己的内容，可正常编辑。继续？`))) return;
  g.bind = srcKey;
  g.bind_mode = "code";
  markDirty();
  renderAll();
  await saveNow();
  toast(`已绑定「${name}」（仅函数和指令）`);
};

window.addEventListener("beforeunload", (e) => {
  if (dirty) {
    e.preventDefault();
    e.returnValue = "";
  }
});

// ---------- 初始化 ----------

initCardFolds();
await bridge.ready();
try {
  await loadSessions();
  await loadCfg();
  toast("已加载");
} catch (e) {
  renderAll();
  toast("加载失败: " + (e.message || e), true);
}
