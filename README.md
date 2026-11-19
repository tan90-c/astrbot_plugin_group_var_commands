# 群变量指令 (astrbot_plugin_group_var_commands)

AstrBot 插件：按群号配置变量与聚合分组，自定义指令绑定 Python 脚本执行，脚本可引用服务器配置变量。

所有配置都在 WebUI 完成：v1.2.0 起插件自带「控制面板」可视化编辑页（基于 AstrBot 插件 Pages 能力，需 AstrBot >= 4.9.2，保存时服务端校验）；旧版 AstrBot 仍可在「插件设置」JSON 中编辑。群内没有任何管理指令，群成员只需直接发送 `<指令名> <值>` 或 `/<指令名> <值>` 即可触发脚本。

## 安装

1. 将整个 `astrbot_plugin_group_var_commands` 文件夹放入 AstrBot 的 `data/plugins/` 目录（或通过插件仓库地址安装）。
2. 重启 AstrBot，在 WebUI「插件管理」中确认已加载。
3. 打开 WebUI 插件详情页 → 「控制面板」页面，可视化管理群、变量、聚合分组与指令；也可在「插件设置」JSON（对应 `data/config/astrbot_plugin_group_var_commands_config.json`）中直接编辑。保存后即时生效，无需重启。

要求 AstrBot >= 3.4.0（控制面板需 >= 4.9.2），Python >= 3.10。脚本在独立 Python 子进程中运行，无需额外依赖。

## 配置结构

```json
{
  "script_timeout": 30,
  "groups": {
    "1122334455": {
      "aggregate": true,
      "vars": {
        "api_url": "http://10.0.0.5:8080"
      },
      "groups": {
        "Group1": {
          "ip": "192.168.1.10",
          "port": "25565",
          "name": "示例服务器"
        }
      },
      "commands": {
        "状态": {
          "script": "def main(ip, port, name):\n    result = f\"{name} ({ip}:{port}) 运行中\"\n    return {\"result\": result}",
          "inputs": [
            {"name": "ip", "source": "gvar:Group1.ip", "type": "string"},
            {"name": "port", "source": "gvar:Group1.port", "type": "string"},
            {"name": "name", "source": "gvar:Group1.name", "type": "string"}
          ],
          "outputs": [{"name": "result", "label": "result"}]
        }
      }
    }
  }
}
```

字段说明：

- `script_timeout`：脚本执行超时秒数。
- `groups.<群号>`：每个群一份配置。
  - `aggregate`：是否开启聚合分组。
  - `vars`：自定义变量（键值对），不开聚合时直接平铺使用。
  - `groups.<分组名>`：聚合分组内的变量（每个分组一组键值对）。
  - `commands.<指令名>`：
    - `script`：Python 源码（JSON 字符串，`\n` 换行），需定义 `def main(...)` 并 `return dict`；
    - `inputs`：入参绑定，`name` 与 `main()` 形参一一对应；
    - `outputs`：输出变量列表（省略则输出 `main()` 返回 dict 的全部键）。

提示：脚本源码中的换行在 JSON 里写 `\n`，双引号写 `\"`。可以先在本地编辑器写好，用在线 JSON 转义工具粘贴进来。

## 变量来源（inputs.source 语法）

| 来源 | 含义 |
| --- | --- |
| `var:<key>` | 群自定义变量 |
| `all:vars` | 全部自定义变量（dict） |
| `gvar:<分组名>.<key>` | 聚合分组内单个变量 |
| `gall:<分组名>` | 整个聚合分组（dict） |
| `arg:<n>` | 触发消息的第 n 个参数（`状态 1区` 中 `1区` 为 arg:1） |
| `sender_id` / `sender_name` / `group_id` | 触发者/群信息 |
| `literal:<文本>` | 固定文本 |

类型：`string`（默认）、`number`、`bool`、`json`。引用整个分组时用 `json` 或 `string`（自动序列化）。

## 触发方式

群成员在已配置的群内发送：

```
状态
/状态
状态 1区        ← "1区" 作为 arg:1 传给脚本
/状态 1区
```

按指令名最长优先匹配；指令名不要包含空格。未配置的群、未配置的指令不会有任何响应。

## Hello World 示例

`groups.<群号>.commands` 里加：

```json
"问候": {
  "script": "def main(name):\n    return {\"result\": \"Hello, \" + name + \"!\"}",
  "inputs": [{"name": "name", "source": "arg:1", "type": "string"}],
  "outputs": [{"name": "result"}]
}
```

群成员发送 `问候 World` 或 `/问候 World` → 回复 `result: Hello, World!`

## 注意事项

- 脚本拥有与 AstrBot 进程相同的系统权限，配置页面仅交给可信管理员访问。
- WebUI 中编辑 JSON 时若群号被自动识别为数字，请以字符串为键（JSON 键天然是字符串，无影响）。
- 指令执行出错时，机器人会回复截断后的报错信息（含 traceback），便于在配置页直接修脚本。
