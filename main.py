# -*- coding: utf-8 -*-
from __future__ import annotations
"""
AstrBot 插件：群变量指令 (astrbot_plugin_group_var_commands)

配置方式（两种，等价）：
1. WebUI 控制页面（推荐）：插件详情页 → 「群变量指令 · 控制面板」可视化编辑
2. 直接编辑 data/config/ 下的插件设置 JSON

配置结构：
- groups.<群号>.super_admins 本群超级管理员QQ（逗号分隔，不跨群）
- groups.<群号>.groups    自定义变量分组（分组名 -> 键值对）
- groups.<群号>.commands  自定义指令（script + perm + aliases）
- script_timeout          脚本执行超时秒数

群成员发送「<指令名或别名> <value>」或「/<指令名或别名> <value>」即执行对应 Python
脚本的 main() 函数。触发优先级：主指令名 > 别名；别名同主名重复→调主名指令；
别名间重复→后创建的指令赢。默认入参（main 按名取用，RUNNER 自动过滤）：
  args        自定义变量分组字典：{分组名: {变量名: 值}}
  cmd         主指令名
  alias       命中的别名（主名触发时为空串）
  value       指令名后的参数列表（模板占位内容在首位）
  sender_id / sender_name / group_id
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
# 指令权限级别：下拉框四选项
PERM_LEVELS = ("member", "admin", "owner", "super")
PERM_LABELS = {
    "member": "群成员（包含管理和群主）",
    "admin": "群管理（包含群主）",
    "owner": "群主",
    "super": "超级管理员",
}
ROLE_RANK = {"member": 0, "admin": 1, "owner": 2}
# 用字面量切分，避免 Windows 下 \n 被转为 \r\n 导致匹配失败
RESULT_MARK = "###RES###"

# 子进程运行器：加载用户脚本 -> 按 main() 形参自动过滤 kwargs -> 调用 -> 打印 JSON 结果
RUNNER = r"""
import inspect, json, sys, importlib.util
spec = importlib.util.spec_from_file_location("user_script", sys.argv[1])
mod = importlib.util.module_from_spec(spec)
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
    "按群配置变量/聚合分组，自定义指令绑定 Python 脚本执行，带 WebUI 控制面板",
    "1.8.2",
    "https://github.com/tan90-c/astrbot_plugin_group_var_commands",
)
class GroupVarCommands(Star):
    def __init__(self, context: Context, config: dict | None = None):
        super().__init__(context, config)
        # AstrBot 会按 _conf_schema.json 注入插件配置对象(AstrBotConfig, 继承自 dict)，
        # 保存路径为 data/config/<插件目录名>_config.json
        self.config: dict = config if config is not None else {}
        # (群号, QQ号) -> (role, 时间戳)，60 秒角色缓存
        self._role_cache = {}
        self._register_web_apis(context)

    def _cfg(self) -> dict:
        cfg = self.config
        if not isinstance(cfg, dict):
            return {}
        cfg.setdefault("script_timeout", 30)
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
        except Exception as e:
            logger.error(f"[{PLUGIN_ID}] 注册 Web API 失败: {e}")

    def _export_config(self) -> dict:
        cfg = self._cfg()
        return {
            "script_timeout": cfg.get("script_timeout", 30),
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
        groups_in = data.get("groups")
        if not isinstance(groups_in, dict):
            raise ValueError("groups 必须是 JSON 对象（群号 -> 配置）")
        groups: dict = {}
        for gid, g in groups_in.items():
            gid = str(gid).strip()
            if not gid:
                raise ValueError("群号不能为空")
            if not isinstance(g, dict):
                raise ValueError(f"群 {gid} 的配置必须是对象")
            vars_in = g.get("vars", {}) or {}
            agg_in = g.get("groups", {}) or {}
            cmds_in = g.get("commands", {}) or {}
            if not isinstance(vars_in, dict):
                raise ValueError(f"群 {gid} 的 vars 必须是对象")
            if not isinstance(agg_in, dict):
                raise ValueError(f"群 {gid} 的聚合分组必须是对象")
            if not isinstance(cmds_in, dict):
                raise ValueError(f"群 {gid} 的 commands 必须是对象")
            # 群级超级管理员：字符串或列表都接受，统一规范为逗号分隔字符串
            sa_in = g.get("super_admins", "") or ""
            if isinstance(sa_in, (list, tuple, set)):
                sa_items = [str(x).strip() for x in sa_in]
            elif isinstance(sa_in, str):
                sa_items = [
                    x.strip()
                    for x in sa_in.replace("，", ",").replace("；", ",").replace(";", ",").split(",")
                ]
            else:
                raise ValueError(f"群 {gid} 的 super_admins 必须是字符串或列表")
            g_super = ",".join(x for x in sa_items if x)
            cmds: dict = {}
            for cname, c in cmds_in.items():
                cname = str(cname).strip()
                if not cname:
                    raise ValueError(f"群 {gid} 存在空的指令名")
                if not isinstance(c, dict):
                    raise ValueError(f"指令 {cname} 的配置必须是对象")
                if not isinstance(c.get("script", ""), str):
                    raise ValueError(f"指令 {cname} 的 script 必须是字符串")
                inputs_raw = c.get("inputs", []) or []
                if not isinstance(inputs_raw, list):
                    raise ValueError(f"指令 {cname} 的 inputs 必须是数组")
                # 旧配置的 inputs/outputs 字段已废弃，保存时自动丢弃
                perm = str(c.get("perm", "member") or "member").strip().lower()
                if perm not in PERM_LEVELS:
                    raise ValueError(f"指令 {cname} 的 perm 非法：{perm}，须为 member/admin/owner/super")
                aliases_in = c.get("aliases", []) or []
                if not isinstance(aliases_in, list):
                    raise ValueError(f"指令 {cname} 的 aliases 必须是数组")
                aliases = []
                for a in aliases_in:
                    a = self._norm_token(str(a).strip())
                    if not a:
                        continue
                    if re.search(r"\s", a):
                        raise ValueError(f"指令 {cname} 的别名 {a} 不能含空格")
                    tmp = a.replace("{*}", "")
                    if "{" in tmp or "}" in tmp:
                        raise ValueError(f"指令 {cname} 的别名 {a} 只允许使用 {{*}} 占位符")
                    if "{*}" in a and not tmp:
                        raise ValueError(f"指令 {cname} 的别名 {a} 不能只是占位符，需有固定文字")
                    if a == cname:
                        continue
                    if a not in aliases:
                        aliases.append(a)
                cmds[cname] = {
                    "script": str(c.get("script", "") or ""),
                    "perm": perm,
                    "aliases": aliases,
                }
            groups[gid] = {
                "aggregate": True,
                "super_admins": g_super,
                "groups": {
                    str(k): (copy.deepcopy(v) if isinstance(v, dict) else {})
                    for k, v in agg_in.items()
                },
                "commands": cmds,
            }  # 旧 vars 字段保存时自动丢弃
        return {"script_timeout": timeout, "groups": groups}

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
        cfg["groups"] = cleaned["groups"]
        cfg.pop("super_admins", None)  # 旧的全局字段已废弃，清理掉
        if not await self._save():
            return error_response("配置文件写入失败，详见后端日志", status_code=500)
        return json_response({"saved": True})

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

    @staticmethod
    def _sender_id(event) -> str:
        sid = ""
        try:
            sid = event.get_sender_id() or ""
        except Exception:
            sid = ""
        if not sid:
            sender = getattr(getattr(event, "message_obj", None), "sender", None)
            sid = str(getattr(sender, "user_id", "") or getattr(event, "sender_id", "") or "")
        return sid

    @staticmethod
    def _sender_name(event, fallback: str) -> str:
        try:
            return event.get_sender_name() or fallback
        except Exception:
            return fallback

    # ---------------- 脚本执行 ----------------

    async def _run_script(self, script_text: str, kwargs: dict, timeout: int):
        """返回 (返回值, 脚本stdout, 错误信息或None)"""
        with tempfile.TemporaryDirectory() as td:
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
    def _result_to_text(result) -> str:
        """脚本返回值直接转字符串：str原样，dict/list转JSON，其他str()"""
        if result is None:
            return ""
        if isinstance(result, str):
            return result
        if isinstance(result, (dict, list)):
            return json.dumps(result, ensure_ascii=False)
        return str(result)

    @staticmethod
    def _format_result(result, outputs) -> str:  # 已废弃，保留兼容
        return GroupVarCommands._result_to_text(result)

    # ---------------- 消息入口 ----------------

    @staticmethod
    def _norm_token(tok: str) -> str:
        """统一模板写法：旧 {value} 自动转为 {*}"""
        return tok.replace("{value}", "{*}")

    def _trigger_index(self, cmds: dict):
        """构建触发项表：主名优先；别名与其他主名重复→归主名指令；
        别名之间重复→后创建的指令赢。模板别名支持多个 {*} 占位，
        每个 {*} 按顺序捕获为 value 的一个元素（如 查{*}玩{*}家{*}）。"""
        trigger = {}
        for cname in cmds:
            trigger[cname] = cname
        for cname, c in cmds.items():
            for a in (c or {}).get("aliases", []) or []:
                a = self._norm_token(str(a).strip())
                if not a or a in cmds:  # 与主名重复→主名指令赢，跳过
                    continue
                trigger[a] = cname  # 别名间重复→后创建者覆盖
        entries = []
        for tok, canon in trigger.items():
            tok = self._norm_token(tok)
            if "{*}" in tok:
                parts = tok.split("{*}")
                pattern = "^" + "(.+?)".join(re.escape(p) for p in parts) + "$"
                entries.append((tok, canon, "tpl", re.compile(pattern)))
            else:
                entries.append((tok, canon, "fixed", None))
        # 固定文字部分长的优先；同长时非模板优先
        entries.sort(key=lambda e: (len(e[0].replace("{*}", "")), "{*}" not in e[0]), reverse=True)
        return entries

    @_ALL_MESSAGE
    async def on_message(self, event: AstrMessageEvent):
        group_id = self._group_id(event)
        if not group_id:
            return
        cfg = self._cfg()
        gcfg = cfg.get("groups", {}).get(group_id)
        if not gcfg:
            return
        cmds = gcfg.get("commands", {})
        if not cmds:
            return
        text = (event.get_message_str() or "").strip()
        if not text:
            return
        # 支持两种触发形式：「<指令名/别名> <value>」与「/<指令名/别名> <value>」
        body = text
        if body[:1] in ("/", "／"):
            body = body[1:].lstrip()
        trigger_entries = self._trigger_index(cmds)
        tokens = body.split()
        for tok, canon, kind, pat in trigger_entries:
            if kind == "fixed":
                if body == tok or body.startswith(tok + " ") or body.startswith(tok + "　"):
                    value_list = body[len(tok):].split()
                    async for r in self._execute(event, cfg, gcfg, canon, cmds[canon], value_list, tok):
                        yield r
                    return
            else:
                # 模板：先整句匹配，再逐个去掉尾部参数 token 重试（尾部 token 并入 value 尾部）
                for i in range(len(tokens), 0, -1):
                    prefix = " ".join(tokens[:i])
                    m = pat.match(prefix)
                    if m:
                        caps = [g.strip() for g in m.groups()]
                        if not all(caps):
                            continue
                        value_list = caps + [t for t in tokens[i:] if t.strip()]
                        async for r in self._execute(event, cfg, gcfg, canon, cmds[canon], value_list, tok):
                            yield r
                        return

    # ---------------- 权限校验 ----------------

    def _super_set(self, gcfg) -> set:
        raw = (gcfg or {}).get("super_admins", "") or ""
        if isinstance(raw, (list, tuple, set)):
            items = [str(x) for x in raw]
        else:
            items = str(raw).replace("，", ",").replace("；", ",").replace(";", ",").split(",")
        return {x.strip() for x in items if x.strip()}

    async def _get_role(self, event):
        """尽力获取发送者群角色: member/admin/owner，失败返回 None"""
        gid, uid = self._group_id(event), self._sender_id(event)
        key = (gid, uid)
        now = time.time()
        hit = self._role_cache.get(key)
        if hit and now - hit[1] < 60:
            return hit[0]
        role = None
        raw = getattr(getattr(event, "message_obj", None), "raw_message", None)
        if isinstance(raw, dict):
            r = raw.get("member_role") or raw.get("role")
            if isinstance(r, str) and r in ROLE_RANK:
                role = r
            else:
                # QQ 官方机器人 raw 结构 member_roles: 1=群主 2=管理
                author = raw.get("author") if isinstance(raw.get("author"), dict) else raw
                roles = author.get("member_roles") if isinstance(author, dict) else None
                if isinstance(roles, list):
                    rset = {str(x) for x in roles}
                    if "1" in rset:
                        role = "owner"
                    elif "2" in rset:
                        role = "admin"
                    elif rset:
                        role = "member"
        if role is None:
            bot = getattr(event, "bot", None)
            if bot is not None and hasattr(bot, "call_api"):
                try:
                    data = await bot.call_api(
                        "get_group_member_info",
                        group_id=gid,
                        user_id=uid,
                        no_cache=False,
                    )
                    if isinstance(data, dict) and data.get("role") in ROLE_RANK:
                        role = data["role"]
                except Exception as e:
                    logger.warning(f"[{PLUGIN_ID}] get_group_member_info 失败: {e}")
        if role is not None:
            self._role_cache[key] = (role, now)
        return role

    async def _check_perm(self, event, gcfg, perm):
        """返回 (allowed, 拒绝原因或 None)；本群超级管理员无条件放行"""
        perm = perm if perm in PERM_LEVELS else "member"
        if self._sender_id(event) in self._super_set(gcfg):
            return True, None
        if perm == "member":
            return True, None
        if perm == "super":
            return False, "此指令仅限本群超级管理员，请把你的 QQ 号加入控制面板【群超级管理员】"
        role = await self._get_role(event)
        if role is None:
            return False, f"无法识别你的群身份，暂不能执行「{PERM_LABELS[perm]}」级指令"
        need = 2 if perm == "owner" else 1
        if ROLE_RANK[role] < need:
            return False, f"权限不足：此指令需要「{PERM_LABELS[perm]}」"
        return True, None

    # ---------------- 指令执行 ----------------

    async def _execute(self, event, cfg, gcfg, name, cmd_cfg, value_list, triggered=None):
        ok, why = await self._check_perm(event, gcfg, cmd_cfg.get("perm", "member"))
        if not ok:
            yield event.plain_result(f"⛔ {why}")
            return
        script = (cmd_cfg.get("script") or "").strip()
        if not script:
            yield event.plain_result(
                f"指令「{name}」尚未配置脚本，请在 WebUI 插件控制面板或设置页中补充。"
            )
            return
        try:
            sender_name = self._sender_name(event, self._sender_id(event))
        except Exception:
            sender_name = self._sender_id(event)
        # 默认入参：RUNNER 会按脚本 main() 实际声明的形参自动筛选
        kwargs = {
            "args": copy.deepcopy(gcfg.get("groups", {}) or {}),
            "cmd": name,
            "alias": "" if (triggered is None or triggered == name) else str(triggered),
            "value": value_list,
            "vars": copy.deepcopy(gcfg.get("vars", {}) or {}),
            "sender_id": self._sender_id(event),
            "sender_name": str(sender_name),
            "group_id": self._group_id(event),
        }
        timeout = int(cfg.get("script_timeout", 30) or 30)
        result, printed, err = await self._run_script(script, kwargs, timeout)
        if err:
            # 脚本报错：默认无回复，仅记日志，不影响机器人
            logger.warning(f"[{PLUGIN_ID}] 指令 {name} 执行出错（已静默）: {err[:300]}")
            return
        body = self._result_to_text(result)
        if printed:
            body = (printed + "\n──────\n" + body) if body else printed
        if not body.strip():
            return
        yield event.plain_result(body)
