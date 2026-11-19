# -*- coding: utf-8 -*-
from __future__ import annotations
"""
AstrBot 插件：群变量指令 (astrbot_plugin_group_var_commands)

所有配置均在 WebUI「插件设置」页面（JSON）中完成：
- groups.<群号>.vars      群自定义变量（可存服务器配置）
- groups.<群号>.aggregate 是否开启聚合分组
- groups.<群号>.groups    聚合分组（分组名 -> 键值对）
- groups.<群号>.commands  自定义指令（脚本 + 入参绑定 + 输出变量）

群成员发送「<指令名> <值>」或「/<指令名> <值>」即执行对应 Python 脚本的
main() 函数，入参从上述变量/分组/消息参数解析，返回 dict 渲染为回复。
"""

import asyncio
import json
import os
import re
import sys
import tempfile

from astrbot.api import logger
from astrbot.api.event import filter, AstrMessageEvent
from astrbot.api.star import Context, Star, register

PLUGIN_ID = "astrbot_plugin_group_var_commands"
# 用字面量切分，避免 Windows 下 \n 被转为 \r\n 导致匹配失败
RESULT_MARK = "###RES###"

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
    "按群配置变量/聚合分组，自定义指令绑定 Python 脚本执行",
    "1.1.1",
    "https://github.com/tan90-c/astrbot_plugin_group_var_commands",
)
class GroupVarCommands(Star):
    def __init__(self, context: Context, config: dict | None = None):
        super().__init__(context, config)
        # AstrBot 会按 _conf_schema.json 注入插件配置对象(AstrBotConfig, 继承自 dict)，
        # 保存路径为 data/config/<插件目录名>_config.json
        self.config: dict = config if config is not None else {}

    def _cfg(self) -> dict:
        cfg = self.config
        if not isinstance(cfg, dict):
            return {}
        cfg.setdefault("script_timeout", 30)
        cfg.setdefault("groups", {})
        return cfg

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

    @filter.event_message_type(filter.EventMessageType.ALL)
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
                f"指令「{name}」尚未配置脚本，请在 WebUI 插件设置页的 groups.{event.group_id}.commands 中补充。"
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
