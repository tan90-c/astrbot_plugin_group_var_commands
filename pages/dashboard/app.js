const bridge = window.AstrBotPluginView || window.AstrBotPluginPage;
if (!bridge) {
  document.body.innerHTML =
    '<p style="color:#ef4444;padding:24px">未检测到 AstrBotPluginView bridge，请确认 AstrBot 版本 >= 4.9.2 且插件已重载</p>';
  throw new Error("no bridge");
}

const $ = (id) => document.getElementById(id);
const PERM_OPTS = [
  ["member", "群成员（包含管理和群主）"],
  ["admin", "群管理（包含群主）"],
  ["owner", "群主"],
  ["super", "超级管理员（指定QQ）"],
];
const PARAM_HINT =
  "默认入参（main 里按名字取用即可，用不到的不写）：\n" +
  "  args        自定义变量分组字典，形如 {\"分组名\": {\"变量名\": \"值\", ...}, ...}\n" +
  "  cmd         主指令名\n" +
  "  alias       命中的别名（用主指令名触发时为空串）\n" +
  "  value       指令名后面的参数原文（字符串）\n" +
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
  g.groups = g.groups || {};
  g.commands = g.commands || {};
  g.super_admins = g.super_admins || "";
  g.aggregate = true;
  return g;
}

function gids() {
  return Object.keys(cfg.groups || {});
}

// ---------- 加载 / 保存 ----------

async function loadCfg() {
  const data = await bridge.apiGet("config");
  cfg = {
    script_timeout: data.script_timeout ?? 30,
    groups: data.groups || {},
  };
  delete cfg.super_admins;
  for (const g of Object.values(cfg.groups)) {
    g.groups = g.groups || {};
    g.commands = g.commands || {};
    g.aggregate = true;
    g.super_admins = g.super_admins || "";
    for (const c of Object.values(g.commands)) {
      c.aliases = Array.isArray(c.aliases) ? c.aliases : [];
      delete c.outputs;
    }
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

// dict 与表格双向：每次 change/移动/删除 从 DOM 行按顺序重建 dict
function kvTable(host, getDict, setDict, opts = {}) {
  const sortable = !!opts.sortable;
  host.innerHTML = "";
  const dict = getDict();
  const entries = Object.entries(dict);
  if (!entries.length) {
    host.innerHTML = '<div class="empty">暂无数据</div>';
    return;
  }
  const tb = document.createElement("table");
  tb.innerHTML = sortable
    ? "<tr><th>键</th><th>值</th><th>排序</th><th>操作</th></tr>"
    : "<tr><th>键</th><th>值</th><th>操作</th></tr>";
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
    tr._del.onclick = () => {
      const k = tr._key.value.trim();
      tr.remove();
      harvest();
      if (k) toast(`已删除 ${k}（保存后生效）`);
    };
    if (sortable) {
      const cell = document.createElement("td");
      const up = document.createElement("button");
      up.className = "btn small secondary";
      up.textContent = "↑";
      up.title = "上移";
      const down = document.createElement("button");
      down.className = "btn small secondary";
      down.textContent = "↓";
      down.title = "下移";
      down.style.marginLeft = "4px";
      up.onclick = () => {
        const p = tr.previousElementSibling;
        if (p && p._key) { tb.insertBefore(tr, p); harvest(); }
      };
      down.onclick = () => {
        const n = tr.nextElementSibling;
        if (n && n._key) { tb.insertBefore(n, tr); harvest(); }
      };
      up.addEventListener("click", (e) => e.preventDefault());
      down.addEventListener("click", (e) => e.preventDefault());
      cell.append(up, down);
      tr.insertBefore(cell, tr.children[2]);
    }
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
  const gg = gcur();
  $("groupSuperAdmins").value = gg ? gg.super_admins : "";
  $("groupSuperAdmins").disabled = !gg;
  const gs = $("groupSelect");
  gs.innerHTML = "";
  if (!gids().length) gs.appendChild(new Option("（暂无群，请先添加）"));
  for (const gid of gids()) gs.appendChild(new Option(gid, gid));
  if (selGroup) gs.value = selGroup;

  const g = gcur();

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
      (d) => { g.groups[selAgg] = d; },
      { sortable: true }
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
  c.aliases = Array.isArray(c.aliases) ? c.aliases : [];
  delete c.outputs;
  if (!PERM_OPTS.some((p) => p[0] === c.perm)) c.perm = "member";

  // 执行权限下拉
  const permRow = document.createElement("div");
  permRow.className = "row";
  permRow.style.margin = "0 0 10px";
  const permLab = document.createElement("span");
  permLab.className = "hint";
  permLab.textContent = "执行权限";
  const permSel = document.createElement("select");
  for (const [v, t] of PERM_OPTS) permSel.appendChild(new Option(t, v));
  permSel.value = c.perm;
  permSel.addEventListener("change", () => {
    c.perm = permSel.value;
    markDirty();
    renderRaw();
  });
  const permNote = document.createElement("span");
  permNote.className = "hint";
  permNote.textContent = "（超级管理员无条件可用；群管理/群主需 OneBot 适配器支持查询）";
  permRow.append(permLab, permSel, permNote);
  host.appendChild(permRow);

  const hint = document.createElement("pre");
  hint.className = "hint paramhint";
  hint.textContent = PARAM_HINT;
  host.appendChild(hint);

  const lab = document.createElement("div");
  lab.className = "hint";
  lab.textContent = "Python 脚本（def main(...)；返回值转字符串直接回复：str原样/dict、list转JSON/None空则不回复；报错超时静默不崩）";
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

  // 附属指令名（别名）
  const labAlias = document.createElement("div");
  labAlias.className = "hint";
  labAlias.style.marginTop = "12px";
  labAlias.textContent = "附属指令名（别名，可多个）：发送别名同等于发送主指令名；与其他指令主名重复时主名指令优先，别名间重复时后创建的指令生效";
  const tbAl = document.createElement("table");
  tbAl.innerHTML = "<tr><th>别名</th><th>操作</th></tr>";
  const alRows = [];
  c.aliases.forEach((a) => {
    const tr = document.createElement("tr");
    const td1 = document.createElement("td");
    const td2 = document.createElement("td");
    const ai = document.createElement("input");
    ai.value = a;
    ai.placeholder = "别名";
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
    markDirty();
    renderCmdBody();
    renderRaw();
  };
  host.append(labAlias, tbAl, btnAddAl);
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
  cfg.groups[gid] = cfg.groups[gid] || { aggregate: true, super_admins: "", groups: {}, commands: {} };
  selGroup = gid;
  $("newGroup").value = "";
  markDirty();
  renderAll();
};
$("btnAddGroupCopy").onclick = () => {
  const gid = $("newGroup").value.trim();
  if (!gid) return toast("请输入新群号", true);
  const src = gcur();
  if (!src) return toast("请先选择要复制的源群", true);
  const srcId = selGroup;
  if (gid === selGroup) return toast("新群号与当前群相同", true);
  if (cfg.groups[gid]) {
    askConfirm(`群 ${gid} 已存在，其配置将被源群 ${selGroup} 覆盖。继续？`).then((yes) => {
      if (!yes) return;
      cfg.groups[gid] = JSON.parse(JSON.stringify({
        aggregate: true,
        super_admins: src.super_admins || "",
        groups: src.groups || {},
        commands: src.commands || {},
      }));
      selGroup = gid;
      $("newGroup").value = "";
      markDirty();
      renderAll();
      toast(`已覆盖群 ${gid} 配置（复制自 ${srcId}，保存后生效）`);
    });
    return;
  }
  cfg.groups[gid] = JSON.parse(JSON.stringify({
    aggregate: true,
    super_admins: src.super_admins || "",
    groups: src.groups || {},
    commands: src.commands || {},
  }));
  selGroup = gid;
  $("newGroup").value = "";
  markDirty();
  renderAll();
  toast(`已添加群 ${gid} 并复制自 ${srcId}（保存后生效）`);
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
$("groupSuperAdmins").addEventListener("change", (e) => {
  const g = gcur();
  if (!g) return;
  g.super_admins = e.target.value.trim();
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
  const n = $("newAgg").value.trim();
  if (!n) return toast("请先在「分组名」输入框填写新名称", true);
  if (n === selAgg) return;
  if (g.groups[n]) return toast("同名分组已存在", true);
  g.groups[n] = g.groups[selAgg];
  delete g.groups[selAgg];
  selAgg = n;
  $("newAgg").value = "";
  markDirty();
  renderAll();
  toast(`分组已改名为 ${n}（保存后生效）`);
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
  g.commands[n] = { script: "", perm: "member", aliases: [] };
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
  cfg = {
    script_timeout: data.script_timeout ?? 30,
    groups: data.groups,
  };
  for (const g of Object.values(cfg.groups)) {
    g.groups = g.groups || {};
    g.commands = g.commands || {};
    g.aggregate = true;
    g.super_admins = Array.isArray(g.super_admins)
      ? g.super_admins.join(",")
      : g.super_admins || "";
    delete g.vars;
    for (const c of Object.values(g.commands)) {
      delete c.inputs;
      delete c.outputs;
      c.aliases = Array.isArray(c.aliases) ? c.aliases : [];
      if (!PERM_OPTS.some((p) => p[0] === c.perm)) c.perm = "member";
    }
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
