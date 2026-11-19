const bridge = window.AstrBotPluginView || window.AstrBotPluginPage;
if (!bridge) {
  document.body.innerHTML =
    '<p style="color:#ef4444;padding:24px">未检测到 AstrBotPluginView bridge，请确认 AstrBot 版本 >= 4.9.2 且插件已重载</p>';
  throw new Error("no bridge");
}

const $ = (id) => document.getElementById(id);
const PARAM_HINT =
  "默认入参（main 里按名字取用即可，用不到的不写）：\n" +
  "  args        聚合分组字典，形如 {\"分组名\": {\"变量名\": \"值\", ...}, ...}\n" +
  "  cmd         触发的指令名\n" +
  "  value       指令名后面的参数原文（字符串）\n" +
  "  vars        群自定义变量字典\n" +
  "  sender_id / sender_name / group_id";

let cfg = { script_timeout: 30, groups: {} };
let selGroup = "";
let selCmd = "";
let selAgg = "";
let dirty = false;

// ---------- 工具 ----------

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

// iframe 内 window.confirm 常被禁用，用页面内模态框
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

function gcur() {
  if (!selGroup) return null;
  const g = cfg.groups[selGroup];
  if (!g) return null;
  g.vars = g.vars || {};
  g.groups = g.groups || {};
  g.commands = g.commands || {};
  g.aggregate = true;
  return g;
}

function gids() {
  return Object.keys(cfg.groups || {});
}

// ---------- 加载 / 保存 ----------

async function loadCfg() {
  const data = await bridge.apiGet("config");
  cfg = { script_timeout: data.script_timeout ?? 30, groups: data.groups || {} };
  for (const g of Object.values(cfg.groups)) {
    g.vars = g.vars || {};
    g.groups = g.groups || {};
    g.commands = g.commands || {};
    g.aggregate = true;
  }
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

// ---------- 收割式键值表（避免闭包旧键导致的幽灵变量） ----------

function kvRow(k, v, phKey, phVal) {
  const tr = document.createElement("tr");
  const c1 = document.createElement("td");
  const c2 = document.createElement("td");
  const c3 = document.createElement("td");
  const ki = document.createElement("input");
  ki.value = k;
  ki.placeholder = phKey || "键";
  const vi = document.createElement("input");
  vi.value = v == null ? "" : typeof v === "object" ? JSON.stringify(v) : String(v);
  vi.placeholder = phVal || "值";
  ki.dataset.old = k;
  vi.dataset.old = k;
  const del = document.createElement("button");
  del.className = "btn small danger";
  del.textContent = "删除";
  c1.appendChild(ki);
  c2.appendChild(vi);
  c3.appendChild(del);
  tr.append(c1, c2, c3);
  tr._key = ki;
  tr._val = vi;
  tr._del = del;
  return tr;
}

// dict 与表格双向：每次 change/删除 从 DOM 行重建 dict，再重画
function kvTable(host, getDict, setDict) {
  host.innerHTML = "";
  const dict = getDict();
  const entries = Object.entries(dict);
  if (!entries.length) {
    host.innerHTML = '<div class="empty">暂无数据</div>';
    return;
  }
  const tb = document.createElement("table");
  tb.innerHTML = "<tr><th>键</th><th>值</th><th>操作</th></tr>";
  for (const [k, v] of entries) tb.appendChild(kvRow(k, v));
  host.appendChild(tb);

  const harvest = () => {
    const out = {};
    for (const tr of tb.querySelectorAll("tr")) {
      if (!tr._key) continue;
      const k = tr._key.value.trim();
      if (!k) continue;
      out[k] = tr._val.value;
    }
    setDict(out);
    markDirty();
    renderRaw();
  };
  tb.addEventListener("change", harvest);
  for (const tr of Array.from(tb.querySelectorAll("tr"))) {
    if (!tr._del) continue;
    tr._del.onclick = async () => {
      const k = tr._key.value.trim();
      tr.remove();
      harvest();
      if (k) toast(`已删除 ${k}（保存后生效）`);
    };
  }
}

function addKvRow(host, getDict, setDict, baseKey) {
  const dict = getDict();
  let k = baseKey || "key";
  let i = 2;
  while (dict[k] !== undefined) { k = "key" + i; i++; }
  dict[k] = "";
  setDict(dict);
  markDirty();
  renderAll();
}

// ---------- 渲染 ----------

function renderAll() {
  $("scriptTimeout").value = cfg.script_timeout;
  const gs = $("groupSelect");
  gs.innerHTML = "";
  if (!gids().length) gs.appendChild(new Option("（暂无群，请先添加）"));
  for (const gid of gids()) gs.appendChild(new Option(gid, gid));
  if (selGroup) gs.value = selGroup;

  const g = gcur();

  kvTable($("varsTable"), g ? () => g.vars : () => ({}), g ? (d) => { g.vars = d; } : () => {});

  const as = $("aggSelect");
  as.innerHTML = "";
  const aggNames = g ? Object.keys(g.groups) : [];
  for (const n of aggNames) as.appendChild(new Option(n, n));
  if (selAgg && !aggNames.includes(selAgg)) selAgg = "";
  if (!selAgg) selAgg = aggNames[0] || "";
  as.value = selAgg;
  $("btnRenameAgg").disabled = !(g && selAgg);
  $("btnDelAgg").disabled = !(g && selAgg);
  $("btnAddAggKv").disabled = !(g && selAgg);

  if (g && selAgg) {
    kvTable(
      $("aggTable"),
      () => g.groups[selAgg],
      (d) => { g.groups[selAgg] = d; }
    );
  } else {
    $("aggTable").innerHTML = '<div class="empty">请先添加分组</div>';
  }

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
  c.outputs = c.outputs || [];

  const hint = document.createElement("pre");
  hint.className = "hint paramhint";
  hint.textContent = PARAM_HINT;
  host.appendChild(hint);

  const lab = document.createElement("div");
  lab.className = "hint";
  lab.textContent = "Python 脚本（def main(...) 返回 dict，形参从默认入参里按名字取）";
  const ta = document.createElement("textarea");
  ta.className = "script";
  ta.spellcheck = false;
  ta.value = c.script;
  ta.addEventListener("input", () => {
    c.script = ta.value;
    markDirty();
    $("dirtyTip").textContent = "● 未保存";
  });
  host.append(lab, ta);

  // 输出变量表（name/label）
  const labOut = document.createElement("div");
  labOut.className = "hint";
  labOut.style.marginTop = "12px";
  labOut.textContent = "输出变量（按 main() 返回 dict 的键渲染回复；留空则全部键输出）";
  const tbOut = document.createElement("table");
  tbOut.innerHTML = "<tr><th>变量名</th><th>显示名</th><th>操作</th></tr>";
  const rows = [];
  c.outputs.forEach((o) => {
    const tr = document.createElement("tr");
    const a = document.createElement("td");
    const b = document.createElement("td");
    const e = document.createElement("td");
    const ni = document.createElement("input");
    ni.value = o.name || "";
    const li = document.createElement("input");
    li.value = o.label || "";
    li.placeholder = "留空用变量名";
    const del = document.createElement("button");
    del.className = "btn small danger";
    del.textContent = "删除";
    a.appendChild(ni);
    b.appendChild(li);
    e.appendChild(del);
    tr.append(a, b, e);
    rows.push({ tr, ni, li, del });
    tbOut.appendChild(tr);
  });
  const harvestOut = () => {
    const out = [];
    for (const r of rows) {
      const n = r.ni.value.trim();
      if (!n) continue;
      out.push({ name: n, label: r.li.value.trim() || n });
    }
    c.outputs = out;
    markDirty();
    renderRaw();
  };
  tbOut.addEventListener("change", harvestOut);
  for (const r of rows) {
    r.del.onclick = () => {
      r.tr.remove();
      rows.splice(rows.indexOf(r), 1);
      harvestOut();
    };
  }
  const btnAddOut = document.createElement("button");
  btnAddOut.className = "btn small";
  btnAddOut.style.marginTop = "8px";
  btnAddOut.textContent = "+ 添加输出";
  btnAddOut.onclick = () => {
    c.outputs.push({ name: "result", label: "结果" });
    markDirty();
    renderCmdBody();
    renderRaw();
  };
  host.append(labOut, tbOut, btnAddOut);
}

function renderRaw() {
  $("rawJson").value = JSON.stringify(cfg, null, 2);
}

// ---------- 事件绑定 ----------

$("groupSelect").addEventListener("change", (e) => {
  selGroup = e.target.value;
  selCmd = "";
  selAgg = "";
  renderAll();
});
$("btnAddGroup").onclick = () => {
  const gid = $("newGroup").value.trim();
  if (!gid) return toast("请输入群号", true);
  cfg.groups = cfg.groups || {};
  cfg.groups[gid] = cfg.groups[gid] || { aggregate: true, vars: {}, groups: {}, commands: {} };
  selGroup = gid;
  $("newGroup").value = "";
  markDirty();
  renderAll();
};
$("btnDelGroup").onclick = async () => {
  const gid = selGroup;
  if (!gid || !cfg.groups[gid]) return toast("请先选择要删除的群", true);
  if (!(await askConfirm(`确定删除群 ${gid} 的全部配置？（保存后生效）`))) return;
  delete cfg.groups[gid];
  selGroup = gids()[0] || "";
  markDirty();
  renderAll();
  toast(`已删除群 ${gid}（保存后生效）`);
};
$("scriptTimeout").addEventListener("change", (e) => {
  let v = parseInt(e.target.value || "30", 10);
  if (isNaN(v)) v = 30;
  cfg.script_timeout = Math.min(600, Math.max(1, v));
  e.target.value = cfg.script_timeout;
  markDirty();
  renderRaw();
});
$("aggSelect").addEventListener("change", (e) => {
  selAgg = e.target.value;
  renderAll();
});
$("btnAddAgg").onclick = () => {
  const g = gcur();
  if (!g) return toast("请先选择群", true);
  const n = $("newAgg").value.trim();
  if (!n) return toast("请输入分组名", true);
  if (g.groups[n]) return toast("分组已存在", true);
  g.groups[n] = {};
  selAgg = n;
  $("newAgg").value = "";
  markDirty();
  renderAll();
};
$("btnRenameAgg").onclick = () => {
  const g = gcur();
  if (!g || !selAgg) return toast("请先选择分组", true);
  const n = $("renameAgg").value.trim();
  if (!n) return toast("请输入新分组名", true);
  if (n === selAgg) return;
  if (g.groups[n]) return toast("同名分组已存在", true);
  g.groups[n] = g.groups[selAgg];
  delete g.groups[selAgg];
  selAgg = n;
  $("renameAgg").value = "";
  markDirty();
  renderAll();
  toast(`分组已改名（保存后生效）`);
};
$("btnDelAgg").onclick = async () => {
  const g = gcur();
  if (!g || !selAgg) return toast("请先选择分组", true);
  if (!(await askConfirm(`确定删除分组「${selAgg}」？（保存后生效）`))) return;
  delete g.groups[selAgg];
  selAgg = "";
  markDirty();
  renderAll();
  toast("分组已删除（保存后生效）");
};
$("btnAddAggKv").onclick = () => {
  const g = gcur();
  if (!g || !selAgg) return toast("请先选择分组", true);
  addKvRow($("aggTable"), () => g.groups[selAgg], (d) => { g.groups[selAgg] = d; });
};
$("btnAddVar").onclick = () => {
  const g = gcur();
  if (!g) return toast("请先选择群", true);
  addKvRow($("varsTable"), () => g.vars, (d) => { g.vars = d; });
};
$("cmdSelect").addEventListener("change", (e) => {
  selCmd = e.target.value;
  renderCmdBody();
});
$("btnAddCmd").onclick = () => {
  const g = gcur();
  if (!g) return toast("请先选择群", true);
  const n = $("newCmd").value.trim();
  if (!n) return toast("请输入指令名", true);
  if (/\s/.test(n)) return toast("指令名不能含空格", true);
  if (g.commands[n]) return toast("指令已存在", true);
  g.commands[n] = { script: "", outputs: [] };
  selCmd = n;
  $("newCmd").value = "";
  markDirty();
  renderAll();
};
$("btnDelCmd").onclick = async () => {
  const g = gcur();
  if (!g || !selCmd) return toast("请先选择要删除的指令", true);
  if (!(await askConfirm(`确定删除指令「${selCmd}」？（保存后生效）`))) return;
  delete g.commands[selCmd];
  selCmd = "";
  markDirty();
  renderAll();
  toast("指令已删除（保存后生效）");
};
$("btnSave").onclick = saveCfg;
$("btnReload").onclick = async () => {
  if (dirty && !(await askConfirm("有未保存修改，重新加载将丢弃。继续？"))) return;
  try {
    await loadCfg();
  } catch (e) {
    toast("加载失败: " + (e.message || e), true);
  }
};
$("btnRawRefresh").onclick = renderRaw;
$("btnRawApply").onclick = () => {
  let data;
  try {
    data = JSON.parse($("rawJson").value);
  } catch (e) {
    return toast("JSON 解析失败: " + e.message, true);
  }
  if (!data || typeof data !== "object" || typeof data.groups !== "object")
    return toast("JSON 必须包含 groups 对象", true);
  cfg = { script_timeout: data.script_timeout ?? 30, groups: data.groups };
  for (const g of Object.values(cfg.groups)) {
    g.vars = g.vars || {};
    g.groups = g.groups || {};
    g.commands = g.commands || {};
    g.aggregate = true;
    for (const c of Object.values(g.commands)) delete c.inputs;
  }
  if (!gids().includes(selGroup)) selGroup = gids()[0] || "";
  markDirty();
  renderAll();
  toast("已应用到页面，别忘了点保存");
};
window.addEventListener("beforeunload", (e) => {
  if (dirty) {
    e.preventDefault();
    e.returnValue = "";
  }
});

// ---------- 初始化 ----------

await bridge.ready();
try {
  await loadCfg();
} catch (e) {
  renderAll();
  toast("加载配置失败: " + (e.message || e), true);
}
