# -*- coding: utf-8 -*-
from __future__ import annotations
"""
AstrBot 插件：群变量指令 (group-var-commands)

功能：
1. 配置页面（AstrBot WebUI 插件设置 JSON，或聊天内管理指令）：
   - 按群号添加配置
   - 每个群可添加自定义变量并赋值（对应服务器配置）
   - 可开启"聚合分组"，开启后按分组管理变量
2. 指令配置：
   - 自定义指令绑定 Python 脚本，触发时执行脚本内 main() 函数
   - 脚本入参可引用：单个变量 / 整个分组 / 单个分组变量 / 用户消息参数等
   - 脚本返回 dict，按输出变量渲染回复
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
    "1.0.2",
    "https://github.com/tan90-c/astrbot_plugin_group_var_commands",
)
class GroupVarCommands(Star):
    def __init__(self, context: Context, config: dict | None = None):
        super().__init__(context, config)
        # AstrBot 会按 _conf_schema.json 注入插件配置对象(AstrBotConfig, 继承自 dict)，
        # 保存路径为 data/config/<插件目录名>_config.json
        self.config: dict = config if config is not None else {}
        # (发送者id, 群id) -> 指令名，等待下一条消息作为脚本内容
        self._pending_script = {}

    # ---------------- 配置读写 ----------------

    def _cfg(self) -> dict:
        cfg = self.config
        if not isinstance(cfg, dict):
            cfg = {}
            self.config = cfg
        cfg.setdefault("admins", [])
        cfg.setdefault("admin_prefix", "/gvc")
        cfg.setdefault("script_timeout", 30)
        cfg.setdefault("groups", {})
        return cfg

    async def _save(self):
        try:
            if hasattr(self.config, "save_config_async"):
                await self.config.save_config_async()
            elif hasattr(self.config, "save_config"):
                self.config.save_config()
        except Exception as e:
            logger.error(f"[{PLUGIN_ID}] 保存配置失败: {e}")

    @staticmethod
    def _ensure_group(cfg: dict, group_id: str) -> dict:
        g = cfg["groups"].setdefault(
            str(group_id),
            {"aggregate": False, "vars": {}, "groups": {}, "commands": {}},
        )
        g.setdefault("vars", {})
        g.setdefault("groups", {})
        g.setdefault("commands", {})
        return g

    def _is_admin(self, cfg: dict, sender_id: str) -> bool:
        return str(sender_id) in {str(a) for a in cfg.get("admins", [])}

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

    @filter.event_message()
    async def on_message(self, event: AstrMessageEvent):
        text = (event.get_message_str() or "").strip()
        if not text:
            return
        cfg = self._cfg()
        prefix = (cfg.get("admin_prefix") or "/gvc").strip()
        sender = str(event.sender_id)
        group_id = str(event.group_id or "")
        is_admin = self._is_admin(cfg, sender)

        # 1) 等待脚本内容的消息
        pend_key = (sender, group_id)
        if pend_key in self._pending_script:
            cmd_name = self._pending_script.pop(pend_key)
            gcfg = self._ensure_group(cfg, group_id)
            cmds = gcfg.setdefault("commands", {})
            entry = cmds.setdefault(cmd_name, {"script": "", "inputs": [], "outputs": []})
            entry["script"] = text
            await self._save()
            yield event.plain_result(
                f"已保存指令「{cmd_name}」的 Python 脚本（{len(text)} 字符）。\n"
                f"绑定入参: {prefix} cmd bind {cmd_name} <参数名> <来源> [类型]"
            )
            return

        # 2) 管理指令
        parts = text.split()
        if parts and parts[0].lower() == prefix.lower():
            reply = await self._admin(event, cfg, parts, is_admin, group_id)
            if reply:
                yield event.plain_result(reply)
            return

        # 3) 群内触发已配置的指令
        if not event.is_group():
            return
        gcfg = cfg.get("groups", {}).get(group_id)
        if not gcfg:
            return
        cmds = gcfg.get("commands", {})
        if not cmds:
            return
        for name in sorted(cmds.keys(), key=len, reverse=True):
            if text == name or text.startswith(name + " ") or text.startswith(name + "　"):
                args = text[len(name):].strip().split()
                async for r in self._execute(event, cfg, gcfg, name, cmds[name], args):
                    yield r
                return

    # ---------------- 指令执行 ----------------

    async def _execute(self, event, cfg, gcfg, name, cmd_cfg, args):
        script = (cmd_cfg.get("script") or "").strip()
        if not script:
            yield event.plain_result(
                f"指令「{name}」尚未配置脚本，管理员请发送: "
                f"{cfg.get('admin_prefix', '/gvc')} cmd script {name}"
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

    # ---------------- 管理指令 ----------------

    async def _admin(self, event, cfg, parts, is_admin, group_id):
        def no_admin():
            return "你不是本插件管理员。管理员列表可在 WebUI 插件设置的 admins 中配置。"

        sub = parts[1].lower() if len(parts) > 1 else "help"

        if sub == "help":
            p = cfg.get("admin_prefix", "/gvc")
            return (
                f"群变量指令 管理命令（前缀 {p}）：\n"
                f"add <群号> / del <群号>  添加或删除群配置\n"
                f"agg <on|off>  当前群开启/关闭聚合分组\n"
                f"set <key> <值> / unset <key>  自定义变量\n"
                f"gadd <分组名> / gdel <分组名>  聚合分组\n"
                f"gset <分组名> <key> <值> / gunset <分组名> <key>\n"
                f"cmd add|del <指令名>  自定义指令\n"
                f"cmd script <指令名>  随后发一条消息作为 Python 脚本\n"
                f"cmd bind <指令名> <参数名> <来源> [string|number|bool|json]\n"
                f"cmd unbind <指令名> <参数名>\n"
                f"cmd out <指令名> <变量名> [显示名] / cmd outdel <指令名> <变量名>\n"
                f"cmd list / cmd show <指令名>\n"
                f"show  查看当前群配置\n"
                f"来源: var:key | all:vars | gvar:分组.key | gall:分组 | arg:1 | sender_id | sender_name | group_id | literal:文本"
            )

        # ---- 群增删（任意会话均可操作）----
        if sub == "add":
            if not is_admin:
                return no_admin()
            if len(parts) < 3:
                return "用法: add <群号>"
            self._ensure_group(cfg, parts[2])
            await self._save()
            return f"已添加群 {parts[2]} 的配置。"

        if sub == "del":
            if not is_admin:
                return no_admin()
            if len(parts) < 3:
                return "用法: del <群号>"
            removed = cfg["groups"].pop(parts[2], None)
            await self._save()
            return f"已删除群 {parts[2]} 配置。" if removed else f"群 {parts[2]} 不存在。"

        # ---- 以下均作用于当前群 ----
        if not event.is_group():
            return "该命令需在群聊内使用（或用 add <群号> 指定）。"
        if not is_admin:
            return no_admin()

        gcfg = self._ensure_group(cfg, group_id)

        if sub == "agg":
            if len(parts) < 3 or parts[2].lower() not in ("on", "off", "true", "false"):
                return "用法: agg <on|off>"
            gcfg["aggregate"] = parts[2].lower() in ("on", "true")
            await self._save()
            return "聚合分组已" + ("开启" if gcfg["aggregate"] else "关闭") + "。"

        if sub == "set":
            if len(parts) < 3:
                return "用法: set <key> <值>"
            key = parts[2]
            value = text_join(parts, 3)
            gcfg["vars"][key] = value
            await self._save()
            return f"变量 {key} = {value}"

        if sub == "unset":
            if len(parts) < 3:
                return "用法: unset <key>"
            gcfg["vars"].pop(parts[2], None)
            await self._save()
            return f"已删除变量 {parts[2]}。"

        if sub == "gadd":
            if len(parts) < 3:
                return "用法: gadd <分组名>"
            gcfg["groups"].setdefault(parts[2], {})
            gcfg["aggregate"] = True
            await self._save()
            return f"已添加分组「{parts[2]}」（聚合分组已自动开启）。"

        if sub == "gdel":
            if len(parts) < 3:
                return "用法: gdel <分组名>"
            removed = gcfg["groups"].pop(parts[2], None)
            await self._save()
            return f"已删除分组「{parts[2]}」。" if removed else f"分组「{parts[2]}」不存在。"

        if sub == "gset":
            if len(parts) < 4:
                return "用法: gset <分组名> <key> <值>"
            gname, key = parts[2], parts[3]
            value = text_join(parts, 4)
            gcfg["groups"].setdefault(gname, {})[key] = value
            await self._save()
            return f"分组「{gname}」变量 {key} = {value}"

        if sub == "gunset":
            if len(parts) < 4:
                return "用法: gunset <分组名> <key>"
            gcfg["groups"].get(parts[2], {}).pop(parts[3], None)
            await self._save()
            return f"已删除分组「{parts[2]}」的变量 {parts[3]}。"

        if sub == "cmd":
            return await self._cmd(event, cfg, gcfg, parts, group_id)

        if sub == "show":
            return json.dumps(gcfg, ensure_ascii=False, indent=2)

        return f"未知命令: {sub}，发送 help 查看用法。"

    async def _cmd(self, event, cfg, gcfg, parts, group_id):
        p = cfg.get("admin_prefix", "/gvc")
        if len(parts) < 3:
            return "用法: cmd add|del|script|bind|unbind|out|outdel|list|show <指令名> ..."
        action = parts[2].lower()
        cmds = gcfg["commands"]

        if action == "add":
            if len(parts) < 4:
                return "用法: cmd add <指令名>"
            name = parts[3]
            cmds.setdefault(name, {"script": "", "inputs": [], "outputs": []})
            await self._save()
            return (
                f"已创建指令「{name}」。群内成员发送「{name}」即触发。\n"
                f"下一步: {p} cmd script {name}"
            )

        if action == "del":
            if len(parts) < 4:
                return "用法: cmd del <指令名>"
            removed = cmds.pop(parts[3], None)
            await self._save()
            return f"已删除指令「{parts[3]}」。" if removed else "指令不存在。"

        if action == "script":
            if len(parts) < 4:
                return "用法: cmd script <指令名>（随后发送一条消息，内容即 Python 脚本）"
            name = parts[3]
            entry = cmds.setdefault(name, {"script": "", "inputs": [], "outputs": []})
            self._pending_script[(str(event.sender_id), group_id)] = name
            return (
                f"请直接发送 Python 脚本内容（需定义 def main(...) 并 return dict），"
                f"作为指令「{name}」的脚本。下一条消息将被保存。"
            )

        if action == "bind":
            # cmd bind <指令名> <参数名> <来源> [类型]
            if len(parts) < 6:
                return "用法: cmd bind <指令名> <参数名> <来源> [string|number|bool|json]"
            name, pname, source = parts[3], parts[4], parts[5]
            typ = parts[6] if len(parts) > 6 else "string"
            entry = cmds.setdefault(name, {"script": "", "inputs": [], "outputs": []})
            entry["inputs"] = [i for i in entry.get("inputs", []) if i.get("name") != pname]
            entry["inputs"].append({"name": pname, "source": source, "type": typ})
            await self._save()
            return f"指令「{name}」参数 {pname} <- {source} ({typ})"

        if action == "unbind":
            if len(parts) < 5:
                return "用法: cmd unbind <指令名> <参数名>"
            name, pname = parts[3], parts[4]
            entry = cmds.get(name)
            if entry:
                entry["inputs"] = [i for i in entry.get("inputs", []) if i.get("name") != pname]
                await self._save()
                return f"已解绑 {name}.{pname}"
            return "指令不存在。"

        if action == "out":
            if len(parts) < 5:
                return "用法: cmd out <指令名> <变量名> [显示名]"
            name, vname = parts[3], parts[4]
            label = parts[5] if len(parts) > 5 else vname
            entry = cmds.setdefault(name, {"script": "", "inputs": [], "outputs": []})
            entry["outputs"] = [o for o in entry.get("outputs", []) if o.get("name") != vname]
            entry["outputs"].append({"name": vname, "label": label})
            await self._save()
            return f"指令「{name}」输出变量 {vname} 已登记。"

        if action == "outdel":
            if len(parts) < 5:
                return "用法: cmd outdel <指令名> <变量名>"
            entry = cmds.get(parts[3])
            if entry:
                entry["outputs"] = [o for o in entry.get("outputs", []) if o.get("name") != parts[4]]
                await self._save()
                return "已移除输出变量。"
            return "指令不存在。"

        if action == "list":
            if not cmds:
                return "当前群没有配置指令。"
            lines = []
            for name, c in cmds.items():
                ins = ", ".join(f"{i.get('name')}<-{i.get('source')}" for i in c.get("inputs", []))
                lines.append(
                    f"「{name}」 脚本:{'已配置' if (c.get('script') or '').strip() else '未配置'}"
                    f" 入参:[{ins}] 输出:{len(c.get('outputs', []))}个"
                )
            return "\n".join(lines)

        if action == "show":
            if len(parts) < 4:
                return "用法: cmd show <指令名>"
            entry = cmds.get(parts[3])
            if not entry:
                return "指令不存在。"
            return json.dumps(entry, ensure_ascii=False, indent=2)

        return f"未知 cmd 子命令: {action}"


def text_join(parts: list, start: int) -> str:
    return " ".join(parts[start:]) if len(parts) > start else ""
