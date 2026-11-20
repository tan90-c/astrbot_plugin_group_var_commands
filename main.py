# -*- coding: utf-8 -*-
from __future__ import annotations
"""
AstrBot 插件：自定义函数指令 (astrbot_plugin_group_var_commands)

配置方式（两种，等价）：
1. WebUI 控制页面（推荐）：插件详情页 → 「群变量指令 · 控制面板」可视化编辑
2. 直接编辑 data/config/ 下的插件设置 JSON

配置结构（groups 的键 = 会话来源 umo，群聊与私聊均可绑定，面板“绑定会话”自动填写）：
- groups.<会话>.groups    自定义变量分组（分组名 -> 键值对）
- groups.<会话>.note      备注名（面板左侧列表显示用，可空）
- groups.<会话>.bind      绑定的主会话 umo（非空=本会话只读跟随主会话，原内容保留）
- groups.<会话>.bind_mode 绑定模式：all=全量跟随；code=仅函数模块与指令跟随，变量分组用本会话自己的
- groups.<会话>.var_notes 变量说明备注 {分组名: {键: 说明}}（仅面板展示，不进脚本 args）
- groups.<会话>.modules   全局函数模块（模块名 -> Python 源码，指令脚本 import 即用）
- groups.<会话>.commands  自定义指令（script + aliases）
- script_timeout          脚本执行超时秒数
仅支持 QQ 官方机器人平台（qq_official / qq_official_webhook）；不兼容旧版配置字段。

群成员或私聊对象发送「<指令名或别名> <value>」或「/<指令名或别名> <value>」即执行对应 Python
脚本的 main() 函数。触发优先级：主指令名 > 别名；别名同主名重复→调主名指令；
别名间重复→后创建的指令赢。默认入参（main 按名取用，RUNNER 自动过滤）：
  args        自定义变量分组字典：{分组名: {变量名: 值}}
  cmd         主指令名
  alias       命中的别名（主名触发时为空串）
  value       指令名后的参数列表（按空格拆分）
脚本返回值直接转字符串回复（str 原样；dict/list 转 JSON；None/空不回复）；
脚本报错/超时默认无回复，仅记后端日志，不影响机器人运行。
"""

import asyncio
import copy
import json
import os
import re
import sys
import tempfile
import time

from astrbot.api import logger
from astrbot.api.event import filter, AstrMessageEvent
from astrbot.api.star import Context, Star, register

try:
    from astrbot.api.web import error_response, json_response, request
except ImportError:
    # 部分版本无 astrbot.api.web，基于 Quart 提供等价能力（同 mimo_tts 做法）
    from quart import Response as _QuartResponse
    from quart import request as _quart_request

    def json_response(data, status=200, **kwargs):
        return _QuartResponse(
            json.dumps(data, ensure_ascii=False, default=str),
            status=status,
            mimetype="application/json",
        )

    def error_response(message, status_code=400, **kwargs):
        return json_response({"ok": False, "message": message}, status=status_code)

    class _ReqProxy:
        @property
        def method(self):
            return _quart_request.method

        async def json(self, default=None):
            try:
                data = await _quart_request.get_json(silent=True)
            except Exception:
                data = None
            return default if data is None else data

    request = _ReqProxy()

PLUGIN_ID = "astrbot_plugin_group_var_commands"

def _build_all_message_filter():
    """兼容不同 AstrBot 版本的全量消息过滤器注册写法。"""
    if hasattr(filter, "event_message_type"):
        emt = getattr(filter, "EventMessageType", None)
        if emt is None:
            try:
                from astrbot.api.event import EventMessageType as emt  # noqa: F811
            except ImportError:
                emt = None
        if emt is not None:
            return filter.event_message_type(emt.ALL)
    if hasattr(filter, "event_message"):
        return filter.event_message()
    raise ImportError(
        f"[{PLUGIN_ID}] 当前 AstrBot 的 astrbot.api.event.filter 不支持已知事件过滤器，请升级 AstrBot"
    )

_ALL_MESSAGE = _build_all_message_filter()
_EM_RESOLVED = getattr(filter, "EventMessageType", None) is not None
# 用字面量切分，避免 Windows 下 \n 被转为 \r\n 导致匹配失败
RESULT_MARK = "###RES###"
# 子进程运行器：加载用户脚本 -> 按 main() 形参自动过滤 kwargs -> 调用 -> 打印 JSON 结果
RUNNER = r"""
import inspect, json, os, sys, importlib.util
spec = importlib.util.spec_from_file_location("user_script", sys.argv[1])
mod = importlib.util.module_from_spec(spec)
sys.path.insert(0, os.path.dirname(os.path.abspath(sys.argv[1])))
spec.loader.exec_module(mod)
candidates = json.loads(sys.argv[2])
try:
    params = inspect.signature(mod.main).parameters
except (ValueError, TypeError):
    params = {}
if any(p.kind == inspect.Parameter.VAR_KEYWORD for p in params.values()):
    kwargs = candidates
else:
    kwargs = {k: v for k, v in candidates.items() if k in params}
ret = mod.main(**kwargs)
sys.stdout.write("\n###RES###\n" + json.dumps(ret, ensure_ascii=False, default=str))
"""


@register(
    PLUGIN_ID,
    "tan90-c",
    "会话绑定+变量分组+全局函数模块+指令脚本执行，带 WebUI 可视化控制面板",
    "1.0.1",
    "https://github.com/tan90-c/astrbot_plugin_group_var_commands",
)
class GroupVarCommands(Star):
    def __init__(self, context: Context, config: dict | None = None):
        super().__init__(context, config)
        # AstrBot 会按 _conf_schema.json 注入插件配置对象(AstrBotConfig, 继承自 dict)，
        # 保存路径为 data/config/<插件目录名>_config.json
        self.config: dict = config if config is not None else {}
        # 已见过的群会话（供面板“绑定会话”选择）
        self._sessions_file = self._sessions_path()
        self._sessions = self._load_sessions()
        self._register_web_apis(context)

    def _cfg(self) -> dict:
        cfg = self.config
        if not isinstance(cfg, dict):
            return {}
        cfg.setdefault("script_timeout", 30)
        cfg.setdefault("reply_format", "markdown")
        cfg.setdefault("groups", {})
        return cfg

    # ---------------- WebUI 控制面板 API ----------------

    def _register_web_apis(self, context: Context):
        """注册控制面板（views/pages）使用的后端 Web API。"""
        if not hasattr(context, "register_web_api"):
            logger.warning(f"[{PLUGIN_ID}] 当前 AstrBot 版本不支持插件 Web API，控制面板不可用")
            return
        try:
            context.register_web_api(
                f"/{PLUGIN_ID}/config",
                self.web_get_config,
                ["GET"],
                "群变量指令：读取完整配置",
            )
            context.register_web_api(
                f"/{PLUGIN_ID}/config/save",
                self.web_save_config,
                ["POST"],
                "群变量指令：保存完整配置",
            )
            context.register_web_api(
                f"/{PLUGIN_ID}/sessions",
                self.web_get_sessions,
                ["GET"],
                "群变量指令：可选群会话列表",
            )
        except Exception as e:
            logger.error(f"[{PLUGIN_ID}] 注册 Web API 失败: {e}")

    def _export_config(self) -> dict:
        cfg = self._cfg()
        return {
            "script_timeout": cfg.get("script_timeout", 30),
            "reply_format": cfg.get("reply_format", "markdown"),
            "groups": copy.deepcopy(cfg.get("groups", {})),
        }

    def _validate_config(self, data: dict) -> dict:
        """校验并规范化前端提交的完整配置，非法则抛 ValueError。"""
        if not isinstance(data, dict):
            raise ValueError("配置必须是 JSON 对象")
        try:
            timeout = int(data.get("script_timeout", 30))
        except (TypeError, ValueError):
            raise ValueError("script_timeout 必须是数字")
        if not 1 <= timeout <= 600:
            raise ValueError("script_timeout 需在 1-600 秒之间")
        reply_format = str(data.get("reply_format", "markdown")).strip().lower()
        if reply_format not in ("markdown", "text"):
            raise ValueError("reply_format 须为 markdown 或 text")
        groups_in = data.get("groups")
        if not isinstance(groups_in, dict):
            raise ValueError("groups 必须是 JSON 对象（会话来源 umo -> 配置）")
        groups: dict = {}
        for gid, g in groups_in.items():
            gid = str(gid).strip()
            if not gid:
                raise ValueError("会话键(umo)不能为空")
            if not isinstance(g, dict):
                raise ValueError(f"群 {gid} 的配置必须是对象")
            agg_in = g.get("groups", {}) or {}
            cmds_in = g.get("commands", {}) or {}
            if not isinstance(agg_in, dict):
                raise ValueError(f"群 {gid} 的 groups 必须是对象")
            if not isinstance(cmds_in, dict):
                raise ValueError(f"群 {gid} 的 commands 必须是对象")
            notes_in = g.get("var_notes", {}) or {}
            if not isinstance(notes_in, dict):
                raise ValueError(f"会话 {gid} 的 var_notes 必须是对象")
            var_notes = {}
            for an, nd in notes_in.items():
                if not isinstance(nd, dict):
                    continue
                cleaned_n = {str(k): str(v).strip() for k, v in nd.items() if str(v).strip()}
                if cleaned_n:
                    var_notes[str(an)] = cleaned_n
            mods_in = g.get("modules", {}) or {}
            note = g.get("note", "") or ""
            if not isinstance(note, str):
                raise ValueError(f"会话 {gid} 的备注名必须是字符串")
            bind = g.get("bind", "") or ""
            if not isinstance(bind, str):
                raise ValueError(f"会话 {gid} 的 bind 必须是字符串")
            bind_mode = str(g.get("bind_mode", "all") or "all").strip().lower()
            if bind_mode not in ("all", "code"):
                raise ValueError(f"会话 {gid} 的 bind_mode 须为 all 或 code")
            if not isinstance(mods_in, dict):
                raise ValueError(f"群 {gid} 的 modules 必须是对象（模块名 -> Python 源码）")
            modules: dict = {}
            for mname, mcode in mods_in.items():
                mname = str(mname).strip()
                if not re.fullmatch(r"[A-Za-z_][A-Za-z0-9_]*", mname):
                    raise ValueError(f"模块名 {mname} 须为合法 Python 标识符（字母/下划线开头）")
                if not isinstance(mcode, str):
                    raise ValueError(f"模块 {mname} 的源码必须是字符串")
                modules[mname] = mcode
            cmds: dict = {}
            for cname, c in cmds_in.items():
                cname = str(cname).strip()
                if not cname:
                    raise ValueError(f"群 {gid} 存在空的指令名")
                if not isinstance(c, dict):
                    raise ValueError(f"指令 {cname} 的配置必须是对象")
                if not isinstance(c.get("script", ""), str):
                    raise ValueError(f"指令 {cname} 的 script 必须是字符串")
                aliases_in = c.get("aliases", []) or []
                if not isinstance(aliases_in, list):
                    raise ValueError(f"指令 {cname} 的 aliases 必须是数组")
                aliases = []
                for a in aliases_in:
                    a = str(a).strip()
                    if not a:
                        continue
                    if re.search(r"\s", a):
                        raise ValueError(f"指令 {cname} 的别名 {a} 不能含空格")
                    if "{" in a or "}" in a:
                        raise ValueError(f"指令 {cname} 的别名 {a} 含花括号：别名只能是固定文字（不支持模板占位写法），请删除此类别名")
                    if a == cname:
                        continue
                    if a not in aliases:
                        aliases.append(a)
                cmds[cname] = {
                    "script": str(c.get("script", "") or ""),
                    "aliases": aliases,
                }
            groups[gid] = {
                "note": note.strip(),
                "bind": bind.strip(),
                "bind_mode": bind_mode if bind.strip() else "all",
                "var_notes": var_notes,
                "modules": modules,
                "groups": {
                    str(k): (copy.deepcopy(v) if isinstance(v, dict) else {})
                    for k, v in agg_in.items()
                },
                "commands": cmds,
            }
        # 绑定关系检查：禁自绑、禁链式（目标本身是绑定会话）；悬空绑定允许（主会话可能被删除）
        for gid, g in groups.items():
            b = g.get("bind", "")
            if not b:
                continue
            if b == gid:
                raise ValueError(f"会话 {gid} 不能绑定自身")
            tgt = groups.get(b)
            if tgt is not None and tgt.get("bind"):
                raise ValueError(f"绑定目标 {b} 自身是绑定会话，不允许链式绑定")
        return {"script_timeout": timeout, "reply_format": reply_format, "groups": groups}

    async def _save(self) -> bool:
        try:
            if hasattr(self.config, "save_config_async"):
                await self.config.save_config_async()
            elif hasattr(self.config, "save_config"):
                self.config.save_config()
            return True
        except Exception as e:
            logger.error(f"[{PLUGIN_ID}] 保存配置失败: {e}")
            return False

    async def web_get_config(self):
        return json_response(self._export_config())

    async def web_save_config(self):
        payload = await request.json(default=None)
        if not isinstance(payload, dict):
            return error_response("请求体不是 JSON 对象")
        try:
            cleaned = self._validate_config(payload)
        except ValueError as e:
            return error_response(str(e), status_code=400)
        cfg = self._cfg()
        cfg["script_timeout"] = cleaned["script_timeout"]
        cfg["reply_format"] = cleaned["reply_format"]
        cfg["groups"] = cleaned["groups"]
        if not await self._save():
            return error_response("配置文件写入失败，详见后端日志", status_code=500)
        return json_response({"saved": True})

    async def web_get_sessions(self):
        items = sorted(self._sessions.values(), key=lambda x: x.get("last_seen", 0), reverse=True)
        for x in items:
            x.setdefault("type", "group")
        # 去重：同一会话可能因 umo 键变化/早期群号键缓存留下多条——
        # 按 (group_id,type) 或 (platform,group_name,type) 合并，保留最新并补全 umo
        merged = []
        seen_gid = {}
        seen_np = {}
        for x in items:
            gid = str(x.get("group_id") or "")
            typ = x.get("type") or "group"
            name = str(x.get("group_name") or "")
            np_key = (str(x.get("platform") or ""), name, typ)
            if gid and gid in seen_gid:
                prev = seen_gid[gid]
                if not prev.get("umo") and x.get("umo"):
                    prev["umo"] = x["umo"]
                continue
            if name and np_key in seen_np:
                continue
            merged.append(x)
            if gid:
                seen_gid[gid] = x
            if name:
                seen_np[np_key] = x
        return json_response({"sessions": merged})

    # ---------------- 已见群会话记录 ----------------

    def _sessions_path(self) -> str:
        base = ""
        try:
            from astrbot.api.util import get_astrbot_plugin_data_path
            base = str(get_astrbot_plugin_data_path())
        except Exception:
            try:
                from astrbot.core.utils.astrbot_path import get_astrbot_plugin_data_path as g2
                base = str(g2())
            except Exception:
                base = os.path.dirname(os.path.abspath(__file__))
        d = os.path.join(base, PLUGIN_ID)
        try:
            os.makedirs(d, exist_ok=True)
        except Exception:
            d = os.path.dirname(os.path.abspath(__file__))
        return os.path.join(d, "sessions.json")

    def _load_sessions(self) -> dict:
        try:
            with open(self._sessions_file, encoding="utf-8") as f:
                data = json.load(f)
            return data if isinstance(data, dict) else {}
        except Exception:
            return {}

    def _save_sessions(self):
        try:
            with open(self._sessions_file, "w", encoding="utf-8") as f:
                json.dump(self._sessions, f, ensure_ascii=False, indent=1)
        except Exception as e:
            logger.warning(f"[{PLUGIN_ID}] 会话缓存保存失败: {e}")

    async def _record_session(self, event, key: str, gid: str = ""):
        now = time.time()
        old = self._sessions.get(key)
        if old and old.get("group_name") and now - float(old.get("last_seen", 0)) < 3600:
            return
        try:
            platform = ""
            try:
                platform = event.get_platform_name() or ""
            except Exception:
                pass
            is_group = bool(gid)
            name = ""
            if is_group:
                try:
                    grp = await event.get_group()
                    if grp is not None:
                        name = str(getattr(grp, "group_name", "") or getattr(grp, "name", "") or "")
                except Exception:
                    pass
            else:
                try:
                    name = event.get_sender_name() or ""
                except Exception:
                    pass
            entry = {
                "umo": str(getattr(event, "unified_msg_origin", "") or ""),
                "group_id": gid or self._chat_id(event),
                "group_name": name or (old or {}).get("group_name") or key.split(":")[-1],
                "platform": platform or (old or {}).get("platform", ""),
                "type": "group" if is_group else "private",
                "last_seen": int(now),
            }
            if entry != old:
                self._sessions[key] = entry
                self._save_sessions()
        except Exception as e:
            logger.debug(f"[{PLUGIN_ID}] 记录会话失败: {e}")

    # ---------------- 跨平台事件适配 ----------------

    @staticmethod
    def _group_id(event) -> str:
        try:
            gid = event.get_group_id()
        except Exception:
            gid = ""
        if not gid:
            gid = (
                getattr(event, "group_id", "")
                or getattr(getattr(event, "message_obj", None), "group_id", "")
                or ""
            )
        return str(gid or "")

    # ---------------- 脚本执行 ----------------

    async def _run_script(self, script_text: str, kwargs: dict, timeout: int, modules: dict | None = None):
        """返回 (返回值, 脚本stdout, 错误信息或None)。modules 会写成同目录 .py 供脚本 import。"""
        with tempfile.TemporaryDirectory() as td:
            for mname, mcode in (modules or {}).items():
                try:
                    with open(os.path.join(td, f"{mname}.py"), "w", encoding="utf-8") as f:
                        f.write(mcode)
                except Exception:
                    pass
            path = os.path.join(td, "user_script.py")
            with open(path, "w", encoding="utf-8") as f:
                f.write(script_text)
            env = dict(os.environ)
            env["PYTHONIOENCODING"] = "utf-8"
            try:
                proc = await asyncio.create_subprocess_exec(
                    sys.executable,
                    "-c",
                    RUNNER,
                    path,
                    json.dumps(kwargs, ensure_ascii=False),
                    stdout=asyncio.subprocess.PIPE,
                    stderr=asyncio.subprocess.STDOUT,
                    env=env,
                    cwd=td,
                )
            except Exception as e:
                return None, "", f"无法启动 Python 子进程: {e}"
            try:
                out, _ = await asyncio.wait_for(proc.communicate(), timeout)
            except asyncio.TimeoutError:
                try:
                    proc.kill()
                except Exception:
                    pass
                return None, "", f"脚本执行超时（>{timeout}s）"
            text = out.decode("utf-8", "replace") if out else ""
        if RESULT_MARK in text:
            printed, res = text.split(RESULT_MARK, 1)
            try:
                return json.loads(res.strip()), printed.strip(), None
            except Exception:
                return None, printed.strip(), "main() 返回值无法序列化: " + res[:500]
        return None, "", "脚本执行出错: " + text[-1500:]

    @staticmethod
    def _sanitize_plain(body: str) -> str:
        """text 模式：转义 ** 避免 PC 端 QQ markdown 卡片吞内容；配合 use_markdown(False) 走纯文本 msg_type=0"""
        return body.replace("**", "*")

    @staticmethod
    def _result_to_text(result) -> str:
        """脚本返回值直接转字符串：str原样，dict/list转JSON，其他str()"""
        if result is None:
            return ""
        if isinstance(result, str):
            return result
        if isinstance(result, (dict, list)):
            return json.dumps(result, ensure_ascii=False)
        return str(result)


    # ---------------- 消息入口 ----------------


    def _trigger_index(self, cmds: dict):
        """构建触发词表（固定文字精确/前缀匹配）：主名优先；
        别名与其他主名重复→归主名指令；别名之间重复→后创建的指令赢；
        长触发词优先。"""
        trigger = {}
        for cname in cmds:
            trigger[cname] = cname
        for cname, c in cmds.items():
            for a in (c or {}).get("aliases", []) or []:
                a = str(a).strip()
                if not a or a in cmds:  # 与主名重复→主名指令赢，跳过
                    continue
                trigger[a] = cname  # 别名间重复→后创建者覆盖
        # 花括号别名非法（不支持模板写法），运行时直接忽略
        entries = [(tok, canon) for tok, canon in trigger.items()
                   if "{" not in tok and "}" not in tok]
        entries.sort(key=lambda e: len(e[0]), reverse=True)
        return entries

    @staticmethod
    def _plain_text(event) -> str:
        """提取纯文本指令：优先只拼接 Plain 组件（剔除 @机器人/回复/图片等组件），
        无组件链时退化为 message_str 并正则剥离 [At:xxx] 类标记。"""
        chain = None
        try:
            chain = getattr(getattr(event, "message_obj", None), "chain", None)
        except Exception:
            chain = None
        if chain:
            parts = [getattr(c, "text", "") or "" for c in chain
                     if c.__class__.__name__ == "Plain"]
            if any(parts):
                return "".join(parts)
        s = event.get_message_str() or ""
        return re.sub(r"\[[A-Za-z]+:[^\]]*\]", "", s)

    @staticmethod
    def _chat_id(event) -> str:
        """私聊会话的对端标识（openid 等）"""
        sid = ""
        try:
            sid = event.get_sender_id() or ""
        except Exception:
            sid = ""
        if not sid:
            sender = getattr(getattr(event, "message_obj", None), "sender", None)
            sid = str(getattr(sender, "user_id", "") or "")
        return sid

    @_ALL_MESSAGE
    async def on_message(self, event: AstrMessageEvent):
      try:
        dbg = bool(os.environ.get("GVC_DEBUG"))
        if dbg and not _EM_RESOLVED:
            logger.warning(f"[{PLUGIN_ID}] GVC_DEBUG: EventMessageType 未解析，全量消息钩子可能根本不会触发")
        group_id = self._group_id(event)
        umo = str(getattr(event, "unified_msg_origin", "") or "")
        # 会话键：umo 优先，退化为群号/私聊对端 id
        key = umo or group_id or self._chat_id(event)
        if not key:
            return
        cfg = self._cfg()
        groups = cfg.get("groups", {}) or {}
        gcfg = groups.get(key)
        await self._record_session(event, key, group_id)
        if dbg:
            logger.info(f"[{PLUGIN_ID}] DBG in: key={key!r} bound={key in groups} msg={self._plain_text(event)!r} raw={event.get_message_str()!r}")
        if not gcfg:
            if dbg:
                logger.info(f"[{PLUGIN_ID}] DBG: key={key!r} 未绑定任何配置（现有键: {list(groups)[:6]}）")
            return
        bind = str(gcfg.get("bind", "") or "").strip()
        if bind:
            master = groups.get(bind)
            if not master:
                logger.warning(f"[{PLUGIN_ID}] 会话 {key} 绑定的主会话 {bind!r} 不存在，指令已全部失效（静默）。请解绑或改绑")
            master = master or {}
            if str(gcfg.get("bind_mode", "all")) == "code":
                # 仅函数与指令跟随主会话，变量分组用本会话自己的
                gcfg = {
                    "commands": master.get("commands", {}) or {},
                    "modules": master.get("modules", {}) or {},
                    "groups": gcfg.get("groups", {}) or {},
                }
            else:
                gcfg = master
        cmds = gcfg.get("commands", {})
        if not cmds:
            return
        text = self._plain_text(event).strip()
        if not text:
            if dbg:
                logger.info(f"[{PLUGIN_ID}] DBG: 剥离组件后文本为空 raw={event.get_message_str()!r}")
            return
        # 支持两种触发形式：「<指令名/别名> <value>」与「/<指令名/别名> <value>」
        body = text
        if body[:1] in ("/", "／"):
            body = body[1:].lstrip()
        trigger_entries = self._trigger_index(cmds)
        if dbg:
            logger.info(f"[{PLUGIN_ID}] DBG body={body!r} entries={trigger_entries[:8]}")
        for tok, canon in trigger_entries:
            if body == tok or body.startswith(tok + " ") or body.startswith(tok + "　"):
                value_list = body[len(tok):].split()
                async for r in self._execute(event, cfg, gcfg, canon, cmds[canon], value_list, tok):
                    yield r
                return
        if dbg:
            logger.info(f"[{PLUGIN_ID}] DBG: 未命中任何触发词 body={body!r}")
      except Exception:
        logger.exception(f"[{PLUGIN_ID}] on_message 内部异常（请反馈此堆栈）")

    # ---------------- 指令执行 ----------------

    async def _execute(self, event, cfg, gcfg, name, cmd_cfg, value_list, triggered=None):
        script = (cmd_cfg.get("script") or "").strip()
        if not script:
            yield event.plain_result(
                f"指令「{name}」尚未配置脚本，请在 WebUI 插件控制面板或设置页中补充。"
            )
            return
        # 默认入参：RUNNER 会按脚本 main() 实际声明的形参自动筛选
        kwargs = {
            "args": copy.deepcopy(gcfg.get("groups", {}) or {}),
            "cmd": name,
            "alias": "" if (triggered is None or triggered == name) else str(triggered),
            "value": value_list,
        }
        timeout = int(cfg.get("script_timeout", 30) or 30)
        modules = {}
        for mname, mcode in (gcfg.get("modules", {}) or {}).items():
            if isinstance(mcode, str):
                modules[str(mname)] = mcode
        result, printed, err = await self._run_script(script, kwargs, timeout, modules)
        if err:
            # 脚本报错：默认无回复，仅记日志，不影响机器人
            logger.warning(f"[{PLUGIN_ID}] 指令 {name} 执行出错（已静默）: {err[:300]}")
            return
        body = self._result_to_text(result)
        if printed:
            body = (printed + "\n──────\n" + body) if body else printed
        if not body.strip():
            return
        if str(cfg.get("reply_format", "markdown")).lower() == "text":
            res = event.plain_result(self._sanitize_plain(body))
            # 强制纯文本：qq_official 适配器 use_markdown_ is False 时走 msg_type=0
            if hasattr(res, "use_markdown"):
                try:
                    res.use_markdown(False)
                except Exception:
                    pass
            yield res
        else:
            yield event.plain_result(body)
