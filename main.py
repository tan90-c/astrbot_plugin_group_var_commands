# -*- coding: utf-8 -*-
from __future__ import annotations
"""
AstrBot 插件：群变量指令 (astrbot_plugin_group_var_commands)

配置方式（两种，等价）：
1. WebUI 控制页面（推荐）：插件详情页 → 「群变量指令 · 控制面板」可视化编辑
2. 直接编辑 data/config/ 下的插件设置 JSON

配置结构：
- groups.<群号>.vars      群自定义变量（可存服务器配置）
- groups.<群号>.aggregate 是否开启聚合分组
- groups.<群号>.groups    聚合分组（分组名 -> 键值对）
- groups.<群号>.commands  自定义指令（脚本 + 入参绑定 + 输出变量）
- script_timeout          脚本执行超时秒数

群成员发送「<指令名> <value>」或「/<指令名> <value>」即执行对应 Python 脚本的
main() 函数，入参从上述变量/分组/消息参数解析，返回 dict 渲染为回复。
"""

import asyncio
import copy
import json
import os
import re
import sys
import tempfile

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
# 用字面量切分，避免 Windows 下 \n 被转为 \r\n 导致匹配失败
RESULT_MARK = "###RES###"
# 入参来源合法前缀（服务端校验用）
SOURCE_PREFIXES = (
    "var:",
    "gvar:",
    "gall:",
    "arg:",
    "literal:",
    "all:vars",
    "sender_id",
    "sender_name",
    "group_id",
)

# 子进程运行器：加载用户脚本 -> 调用 main(**kwargs) -> 打印 JSON 结果
RUNNER = r"""
import json, sys, importlib.util
spec = importlib.util.spec_from_file_location("user_script", sys.argv[1])
mod = importlib.util.module_from_spec(spec)
spec.loader.exec_module(mod)
kwargs = json.loads(sys.argv[2])
ret = mod.main(**kwargs)
sys.stdout.write("\n###RES###\n" + json.dumps(ret, ensure_ascii=False, default=str))
"""


def _coerce(value, typ):
    """按声明类型转换变量值"""
    typ = (typ or "string").lower()
    if typ == "number":
        if isinstance(value, (int, float)) and not isinstance(value, bool):
            return value
        try:
            s = str(value).strip()
            return int(s) if re.fullmatch(r"[-+]?\d+", s) else float(s)
        except Exception:
            return 0
    if typ == "bool":
        if isinstance(value, bool):
            return value
        return str(value).strip().lower() in ("1", "true", "yes", "on", "是")
    if typ == "json":
        if isinstance(value, (dict, list)):
            return value
        try:
            return json.loads(str(value))
        except Exception:
            return {}
    # string / 默认
    if isinstance(value, (dict, list)):
        return json.dumps(value, ensure_ascii=False)
    return "" if value is None else str(value)


@register(
    PLUGIN_ID,
    "tan90-c",
    "按群配置变量/聚合分组，自定义指令绑定 Python 脚本执行，带 WebUI 控制面板",
    "1.2.1",
    "https://github.com/tan90-c/astrbot_plugin_group_var_commands",
)
class GroupVarCommands(Star):
    def __init__(self, context: Context, config: dict | None = None):
        super().__init__(context, config)
        # AstrBot 会按 _conf_schema.json 注入插件配置对象(AstrBotConfig, 继承自 dict)，
        # 保存路径为 data/config/<插件目录名>_config.json
        self.config: dict = config if config is not None else {}
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
            cmds: dict = {}
            for cname, c in cmds_in.items():
                cname = str(cname).strip()
                if not cname:
                    raise ValueError(f"群 {gid} 存在空的指令名")
                if not isinstance(c, dict):
                    raise ValueError(f"指令 {cname} 的配置必须是对象")
                inputs_raw = c.get("inputs", []) or []
                outputs_raw = c.get("outputs", []) or []
                if not isinstance(inputs_raw, list) or not isinstance(outputs_raw, list):
                    raise ValueError(f"指令 {cname} 的 inputs/outputs 必须是数组")
                inputs = []
                for i, item in enumerate(inputs_raw):
                    if not isinstance(item, dict):
                        raise ValueError(f"指令 {cname} 的第 {i + 1} 个入参必须是对象")
                    pname = str(item.get("name", "")).strip()
                    if not pname:
                        raise ValueError(f"指令 {cname} 的第 {i + 1} 个入参名字为空")
                    source = str(item.get("source", "")).strip()
                    if not source.startswith(SOURCE_PREFIXES):
                        raise ValueError(
                            f"指令 {cname} 入参 {pname} 来源非法：{source or '(空)'}，"
                            "须为 var:/gvar:分组.键/gall:分组/arg:n/literal:文本/"
                            "all:vars/sender_id/sender_name/group_id"
                        )
                    typ = str(item.get("type", "string")).lower()
                    if typ not in ("string", "number", "bool", "json"):
                        typ = "string"
                    inputs.append({"name": pname, "source": source, "type": typ})
                outputs = []
                for item in outputs_raw:
                    if not isinstance(item, dict):
                        continue
                    oname = str(item.get("name", "")).strip()
                    if not oname:
                        continue
                    outputs.append({"name": oname, "label": str(item.get("label", "") or oname)})
                cmds[cname] = {
                    "script": str(c.get("script", "") or ""),
                    "inputs": inputs,
                    "outputs": outputs,
                }
            groups[gid] = {
                "aggregate": bool(g.get("aggregate", False)),
                "vars": {str(k): v for k, v in vars_in.items()},
                "groups": {
                    str(k): (copy.deepcopy(v) if isinstance(v, dict) else {})
                    for k, v in agg_in.items()
                },
                "commands": cmds,
            }
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
        if not await self._save():
            return error_response("配置文件写入失败，详见后端日志", status_code=500)
        return json_response({"saved": True})

    # ---------------- 变量来源解析 ----------------

    def _resolve(self, source: str, gcfg: dict, args: list, event: AstrMessageEvent):
        """
        来源语法：
          var:<key>            群自定义变量
          all:vars             全部自定义变量(dict)
          gvar:<分组名>.<key>  聚合分组内单个变量
          gall:<分组名>        整个聚合分组(dict)
          arg:<n>              用户消息第 n 个参数(从1开始)
          sender_id / sender_name / group_id
          literal:<文本>
        """
        source = source or ""
        if source.startswith("arg:"):
            try:
                idx = int(source[4:]) - 1
            except ValueError:
                return ""
            return args[idx] if 0 <= idx < len(args) else ""
        if source.startswith("var:"):
            return gcfg.get("vars", {}).get(source[4:], "")
        if source == "all:vars":
            return dict(gcfg.get("vars", {}))
        if source.startswith("gvar:"):
            gname, _, key = source[5:].partition(".")
            return gcfg.get("groups", {}).get(gname, {}).get(key, "")
        if source.startswith("gall:"):
            return dict(gcfg.get("groups", {}).get(source[5:], {}))
        if source == "sender_id":
            return str(event.sender_id)
        if source == "sender_name":
            try:
                return event.get_sender_name()
            except Exception:
                return str(event.sender_id)
        if source == "group_id":
            return str(event.group_id)
        if source.startswith("literal:"):
            return source[8:]
        return ""

    # ---------------- 脚本执行 ----------------

    async def _run_script(self, script_text: str, kwargs: dict, timeout: int):
        """返回 (结果dict或None, 脚本stdout/错误信息)"""
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
                return None, f"无法启动 Python 子进程: {e}"
            try:
                out, _ = await asyncio.wait_for(proc.communicate(), timeout)
            except asyncio.TimeoutError:
                try:
                    proc.kill()
                except Exception:
                    pass
                return None, f"脚本执行超时（>{timeout}s）"
            text = out.decode("utf-8", "replace") if out else ""
        if RESULT_MARK in text:
            printed, res = text.split(RESULT_MARK, 1)
            try:
                return json.loads(res.strip()), printed.strip()
            except Exception:
                return None, "main() 返回值无法解析为 JSON:\n" + res[:1000]
        return None, "脚本执行出错（请检查 main 函数与入参）:\n" + text[-1500:]

    @staticmethod
    def _format_result(result, outputs) -> str:
        if not isinstance(result, dict):
            result = {"result": result}
        lines = []
        if outputs:
            for o in outputs:
                name = o.get("name")
                v = result.get(name)
                if isinstance(v, (dict, list)):
                    v = json.dumps(v, ensure_ascii=False)
                lines.append(f"{o.get('label') or name}: {v}")
        else:
            for k, v in result.items():
                if isinstance(v, (dict, list)):
                    v = json.dumps(v, ensure_ascii=False)
                lines.append(f"{k}: {v}")
        return "\n".join(lines) if lines else "(无输出)"

    # ---------------- 消息入口 ----------------

    @_ALL_MESSAGE
    async def on_message(self, event: AstrMessageEvent):
        if not event.is_group():
            return
        cfg = self._cfg()
        group_id = str(event.group_id or "")
        gcfg = cfg.get("groups", {}).get(group_id)
        if not gcfg:
            return
        cmds = gcfg.get("commands", {})
        if not cmds:
            return
        text = (event.get_message_str() or "").strip()
        if not text:
            return
        # 支持两种触发形式：「<指令名> <value>」与「/<指令名> <value>」
        body = text
        if body[:1] in ("/", "／"):
            body = body[1:].lstrip()
        for name in sorted(cmds.keys(), key=len, reverse=True):
            if body == name or body.startswith(name + " ") or body.startswith(name + "　"):
                args = body[len(name):].strip().split()
                async for r in self._execute(event, cfg, gcfg, name, cmds[name], args):
                    yield r
                return

    # ---------------- 指令执行 ----------------

    async def _execute(self, event, cfg, gcfg, name, cmd_cfg, args):
        script = (cmd_cfg.get("script") or "").strip()
        if not script:
            yield event.plain_result(
                f"指令「{name}」尚未配置脚本，请在 WebUI 插件控制面板或设置页中补充。"
            )
            return
        kwargs = {}
        for inp in cmd_cfg.get("inputs", []) or []:
            raw = self._resolve(inp.get("source", ""), gcfg, args, event)
            kwargs[inp.get("name")] = _coerce(raw, inp.get("type", "string"))
        timeout = int(cfg.get("script_timeout", 30) or 30)
        result, printed = await self._run_script(script, kwargs, timeout)
        if result is None:
            logger.warning(f"[{PLUGIN_ID}] 指令 {name} 执行失败: {printed[:300]}")
            yield event.plain_result(f"「{name}」执行失败：\n{printed[:800]}")
            return
        body = self._format_result(result, cmd_cfg.get("outputs"))
        if printed:
            body = printed + "\n──────\n" + body
        yield event.plain_result(body)
