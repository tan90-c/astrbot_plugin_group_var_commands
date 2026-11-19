const bridge = window.AstrBotPluginView || window.AstrBotPluginPage;
if (!bridge) {
  document.body.innerHTML =
    '<p style="color:#ef4444;padding:24px">未检测到 AstrBotPluginView bridge，请确认 AstrBot 版本 >= 4.9.2 且插件已重载</p>';
  throw new Error("no bridge");
}

const $ = (id) => document.getElementById(id);
const TYPES = ["string", "number", "bool", "json"];
const SRC_HINT = "var:键 / gvar:分组.键 / gall:分组 / arg:1 / all:vars / sender_id / sender_name / group_id / literal:文本";

let cfg = { script_timeout: 30, groups: {} };
let selGroup = "";
let selCmd = "";
let selAgg = "";
let dirty = false;

function toast(msg, bad = false) {
  const t = $("toast");
  t.textContent = msg;
  t.style.color = bad ? "#ef4444" : "#10b981";
  t.classList.add("show");
  clearTimeout(toast._h);
  toast._h = setTimeout(() => t.classList.remove("show"), 2600);
}

function markDirty() {
  dirty = true;
  $("dirtyTip").textContent = "● 未保存";
}

function gcur() {
  if (!selGroup) return null;
  const g = cfg.groups[selGroup];
  if (!g) return null;
  g.vars = g.vars || {};
  g.groups = g.groups || {};
  g.commands = g.commands || {};
  if (g.aggregate === undefined) g.aggregate = false;
  return g;
}

function gids() {
  return Object.keys(cfg.groups || {});
}

function uniqueKey(dict, base) {
  let k = base, i = 2;
  while (k in dict) { k = base + i; i++; }
  return k;
}

function renameKey(dict, oldK, newK) {
  newK = (newK || "").trim();
  if (!newK || newK === oldK) return;
  dict[newK] = dict[oldK];
  delete dict[oldK];
  markDirty();
  renderRaw();
}

// ---------- 加载 / 保存 ----------

async function loadCfg() {
  const data = await bridge.apiGet("config");
  cfg = { script_timeout: data.script_timeout ?? 30, groups: data.groups || {} };
  if (!gids().includes(selGroup)) selGroup = gids()[0] || "";
  selCmd = "";
  selAgg = "";
  dirty = false;
  renderAll();
  toast("已加载");
}

async function saveCfg() {
  try {
    await bridge.apiPost("config/save", cfg);
    dirty = false;
    $("dirtyTip").textContent = "";
    toast("保存成功 ✔");
  } catch (e) {
    toast("保存失败: " + (e && e.message ? e.message : e), true);
  }
}

// ---------- 渲染 ----------

function inputEl(value, onChange, ph) {
  const inp = document.createElement("input");
  inp.value = value == null ? "" : typeof value === "object" ? JSON.stringify(value) : String(value);
  if (ph) inp.placeholder = ph;
  inp.addEventListener("change", () => { onChange(inp.value); markDirty(); });
  return inp;
}

function kvTable(host, dict, onDelExtra) {
  host.innerHTML = "";
  const keys = Object.keys(dict);
  if (!keys.length) { host.innerHTML = '<div class="empty">暂无数据</div>'; return; }
  const tb = document.createElement("table");
  tb.innerHTML = "<tr><th>键</th><th>值</th><th>操作</th></tr>";
  for (const k of keys) {
    const tr = document.createElement("tr");
    const a = document.createElement("td");
    a.appendChild(inputEl(k, (v) => renameKey(dict, k, v)));
    const b = document.createElement("td");
    b.appendChild(inputEl(dict[k], (v) => { dict[k] = v; }, "值"));
    const c = document.createElement("td");
    const del = document.createElement("button");
    del.className = "btn small danger";
    del.textContent = "删除";
    del.onclick = () => { delete dict[k]; markDirty(); renderAll(); if (onDelExtra) onDelExtra(); };
    c.appendChild(del);
    tr.append(a, b, c);
    tb.appendChild(tr);
  }
  host.appendChild(tb);
}

function renderAll() {
  $("scriptTimeout").value = cfg.script_timeout;
  const gs = $("groupSelect");
  gs.innerHTML = "";
  if (!gids().length) gs.appendChild(new Option("（暂无群，请先添加）"));
  for (const gid of gids()) gs.appendChild(new Option(gid, gid));
  if (selGroup) gs.value = selGroup;

  const g = gcur();

  kvTable($("varsTable"), g ? g.vars : {});

  const as = $("aggSelect");
  as.innerHTML = "";
  const aggNames = g ? Object.keys(g.groups) : [];
  for (const n of aggNames) as.appendChild(new Option(n, n));
  if (selAgg && !aggNames.includes(selAgg)) selAgg = "";
  if (!selAgg) selAgg = aggNames[0] || "";
  as.value = selAgg;
  $("aggEnable").checked = !!(g && g.aggregate);
  $("aggEnable").disabled = !g;
  kvTable($("aggTable"), g && selAgg ? (g.groups[selAgg] ||= {}) : {});

  const cs = $("cmdSelect");
  cs.innerHTML = "";
  const cmdNames = g ? Object.keys(g.commands) : [];
  for (const n of cmdNames) cs.appendChild(new Option(n, n));
  if (selCmd && !cmdNames.includes(selCmd)) selCmd = "";
  if (!selCmd) selCmd = cmdNames[0] || "";
  cs.value = selCmd;
  renderCmdBody();

  renderRaw();
  $("dirtyTip").textContent = dirty ? "● 未保存" : "";
}

function renderCmdBody() {
  const host = $("cmdBody");
  host.innerHTML = "";
  const g = gcur();
  if (!g || !selCmd || !g.commands[selCmd]) {
    host.innerHTML = '<div class="empty">请先创建或选择指令</div>';
    return;
  }
  const c = g.commands[selCmd];
  c.script = c.script || "";
  c.inputs = c.inputs || [];
  c.outputs = c.outputs || [];

  const lab = document.createElement("div");
  lab.className = "hint";
  lab.textContent = "Python 脚本（def main(**kwargs) 返回 dict）";
  const ta = document.createElement("textarea");
  ta.className = "script";
  ta.spellcheck = false;
  ta.value = c.script;
  ta.addEventListener("input", () => { c.script = ta.value; markDirty(); $("dirtyTip").textContent = "● 未保存"; });
  host.append(lab, ta);

  // 入参
  const labIn = document.createElement("div");
  labIn.className = "hint";
  labIn.style.marginTop = "12px";
  labIn.textContent = "入参绑定（脚本 main() 的关键字参数）";
  const tbIn = document.createElement("table");
  tbIn.innerHTML = "<tr><th>参数名</th><th>来源</th><th>类型</th><th>操作</th></tr>";
  c.inputs.forEach((it, idx) => {
    const tr = document.createElement("tr");
    const a = document.createElement("td");
    a.appendChild(inputEl(it.name, (v) => { it.name = v.trim(); }));
    const b = document.createElement("td");
    b.appendChild(inputEl(it.source, (v) => { it.source = v.trim(); }, SRC_HINT));
    const d = document.createElement("td");
    const se = document.createElement("select");
    for (const t of TYPES) se.appendChild(new Option(t, t));
    se.value = TYPES.includes((it.type || "string").toLowerCase()) ? (it.type || "string") : "string";
    se.addEventListener("change", () => { it.type = se.value; markDirty(); });
    d.appendChild(se);
    const e = document.createElement("td");
    const del = document.createElement("button");
    del.className = "btn small danger";
    del.textContent = "删除";
    del.onclick = () => { c.inputs.splice(idx, 1); markDirty(); renderCmdBody(); renderRaw(); };
    e.appendChild(del);
    tr.append(a, b, d, e);
    tbIn.appendChild(tr);
  });
  const btnAddIn = document.createElement("button");
  btnAddIn.className = "btn small";
  btnAddIn.style.marginTop = "8px";
  btnAddIn.textContent = "+ 添加入参";
  btnAddIn.onclick = () => { c.inputs.push({ name: "value", source: "arg:1", type: "string" }); markDirty(); renderCmdBody(); renderRaw(); };

  // 输出
  const labOut = document.createElement("div");
  labOut.className = "hint";
  labOut.style.marginTop = "12px";
  labOut.textContent = "输出变量（按 result dict 的键渲染回复）";
  const tbOut = document.createElement("table");
  tbOut.innerHTML = "<tr><th>变量名</th><th>显示名</th><th>操作</th></tr>";
  c.outputs.forEach((o, idx) => {
    const tr = document.createElement("tr");
    const a = document.createElement("td");
    a.appendChild(inputEl(o.name, (v) => { o.name = v.trim(); }));
    const b = document.createElement("td");
    b.appendChild(inputEl(o.label || "", (v) => { o.label = v; }, "留空用变量名"));
    const e = document.createElement("td");
    const del = document.createElement("button");
    del.className = "btn small danger";
    del.textContent = "删除";
    del.onclick = () => { c.outputs.splice(idx, 1); markDirty(); renderCmdBody(); renderRaw(); };
    e.appendChild(del);
    tr.append(a, b, e);
    tbOut.appendChild(tr);
  });
  const btnAddOut = document.createElement("button");
  btnAddOut.className = "btn small";
  btnAddOut.style.marginTop = "8px";
  btnAddOut.textContent = "+ 添加输出";
  btnAddOut.onclick = () => { c.outputs.push({ name: "result", label: "结果" }); markDirty(); renderCmdBody(); renderRaw(); };

  host.append(labIn, tbIn, btnAddIn, labOut, tbOut, btnAddOut);
}

function renderRaw() {
  $("rawJson").value = JSON.stringify(cfg, null, 2);
}

// ---------- 事件绑定 ----------

$("groupSelect").addEventListener("change", (e) => {
  selGroup = e.target.value; selCmd = ""; selAgg = ""; renderAll();
});
$("btnAddGroup").onclick = () => {
  const gid = $("newGroup").value.trim();
  if (!gid) return toast("请输入群号", true);
  cfg.groups = cfg.groups || {};
  cfg.groups[gid] = cfg.groups[gid] || { aggregate: false, vars: {}, groups: {}, commands: {} };
  selGroup = gid; $("newGroup").value = "";
  markDirty(); renderAll();
};
$("btnDelGroup").onclick = () => {
  if (!gcur()) return;
  if (!confirm(`删除群 ${selGroup} 的全部配置？`)) return;
  delete cfg.groups[selGroup];
  selGroup = gids()[0] || "";
  markDirty(); renderAll();
};
$("scriptTimeout").addEventListener("change", (e) => {
  let v = parseInt(e.target.value || "30", 10);
  if (isNaN(v)) v = 30;
  cfg.script_timeout = Math.min(600, Math.max(1, v));
  e.target.value = cfg.script_timeout;
  markDirty(); renderRaw();
});
$("aggSelect").addEventListener("change", (e) => { selAgg = e.target.value; renderAll(); });
$("aggEnable").addEventListener("change", (e) => {
  const g = gcur(); if (!g) return;
  g.aggregate = e.target.checked;
  markDirty(); renderRaw();
});
$("btnAddAgg").onclick = () => {
  const g = gcur(); if (!g) return toast("请先选择群", true);
  const n = $("newAgg").value.trim(); if (!n) return toast("请输入分组名", true);
  g.groups[n] = g.groups[n] || {};
  g.aggregate = true;
  selAgg = n; $("newAgg").value = "";
  markDirty(); renderAll();
};
$("btnDelAgg").onclick = () => {
  const g = gcur(); if (!g || !selAgg) return;
  if (!confirm(`删除分组「${selAgg}」？`)) return;
  delete g.groups[selAgg];
  selAgg = "";
  markDirty(); renderAll();
};
$("btnAddAggKv").onclick = () => {
  const g = gcur(); if (!g || !selAgg) return toast("请先选择分组", true);
  g.groups[selAgg][uniqueKey(g.groups[selAgg], "key")] = "";
  markDirty(); renderAll();
};
$("btnAddVar").onclick = () => {
  const g = gcur(); if (!g) return toast("请先选择群", true);
  g.vars[uniqueKey(g.vars, "var1")] = "";
  markDirty(); renderAll();
};
$("cmdSelect").addEventListener("change", (e) => { selCmd = e.target.value; renderCmdBody(); });
$("btnAddCmd").onclick = () => {
  const g = gcur(); if (!g) return toast("请先选择群", true);
  const n = $("newCmd").value.trim();
  if (!n) return toast("请输入指令名", true);
  if (g.commands[n]) return toast("指令已存在", true);
  g.commands[n] = { script: "", inputs: [], outputs: [] };
  selCmd = n; $("newCmd").value = "";
  markDirty(); renderAll();
};
$("btnDelCmd").onclick = () => {
  const g = gcur(); if (!g || !selCmd) return;
  if (!confirm(`删除指令「${selCmd}」？`)) return;
  delete g.commands[selCmd];
  selCmd = "";
  markDirty(); renderAll();
};
$("btnSave").onclick = saveCfg;
$("btnReload").onclick = () => {
  if (dirty && !confirm("有未保存修改，重新加载将丢弃。继续？")) return;
  loadCfg().catch((e) => toast("加载失败: " + (e.message || e), true));
};
$("btnRawRefresh").onclick = renderRaw;
$("btnRawApply").onclick = () => {
  let data;
  try { data = JSON.parse($("rawJson").value); }
  catch (e) { return toast("JSON 解析失败: " + e.message, true); }
  if (!data || typeof data !== "object" || typeof data.groups !== "object")
    return toast("JSON 必须包含 groups 对象", true);
  cfg = { script_timeout: data.script_timeout ?? 30, groups: data.groups };
  if (!gids().includes(selGroup)) selGroup = gids()[0] || "";
  markDirty();
  renderAll();
  toast("已应用到页面，别忘了点保存");
};
window.addEventListener("beforeunload", (e) => {
  if (dirty) { e.preventDefault(); e.returnValue = ""; }
});

// ---------- 初始化 ----------

await bridge.ready();
try { await loadCfg(); }
catch (e) { renderAll(); toast("加载配置失败: " + (e.message || e), true); }
