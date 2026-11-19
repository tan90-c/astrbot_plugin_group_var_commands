# 群变量指令 (astrbot_plugin_group_var_commands)

AstrBot 插件：按群号配置变量与聚合分组，自定义指令绑定 Python 脚本执行，脚本可引用服务器配置变量。

## 安装

1. 将整个 `astrbot_plugin_group_var_commands` 文件夹放入 AstrBot 的 `data/plugins/` 目录（或通过插件仓库地址安装）。
2. 重启 AstrBot，在 WebUI「插件管理」中确认已加载。
3. 首次加载后会生成配置文件 `data/config/astrbot_plugin_group_var_commands_config.json`，可在 WebUI「插件设置」中编辑；也可用聊天管理指令配置（见下）。

要求 AstrBot >= 3.4.0，Python >= 3.10。脚本在独立 Python 子进程中运行，无需额外依赖。

## 配置结构（WebUI 插件设置 JSON）

```json
{
  "admins": ["12345678"],
  "admin_prefix": "/gvc",
  "script_timeout": 30,
  "groups": {
    "1122334455": {
      "aggregate": true,
      "vars": {
        "api_url": "http://10.0.0.5:8080"
      },
      "groups": {
        "Group1": {
          "role_id": "10001",
          "role_name": "小明",
          "urs": "abc123",
          "tongqian": 350
        }
      },
      "commands": {
        "查询": {
          "script": "def main(role_id, role_name, urs, tongqian):\n    result = f\"预存id: {role_id}\\n昵称: {role_name}\\nurs: {urs}(已冻结)\\n铜钱: {tongqian}\"\n    return {\"result\": result}",
          "inputs": [
            {"name": "role_id", "source": "gvar:Group1.role_id", "type": "string"},
            {"name": "role_name", "source": "gvar:Group1.role_name", "type": "string"},
            {"name": "urs", "source": "gvar:Group1.urs", "type": "string"},
            {"name": "tongqian", "source": "gvar:Group1.tongqian", "type": "number"}
          ],
          "outputs": [{"name": "result", "label": "result"}]
        }
      }
    }
  }
}
```

字段说明：

- `admins`：管理员 QQ 号列表（字符串或数字均可）。
- `admin_prefix`：管理指令前缀，默认 `/gvc`。
- `script_timeout`：脚本执行超时秒数。
- `groups.<群号>`：每个群一份配置。
  - `aggregate`：是否开启聚合分组。
  - `vars`：自定义变量（键值对），不开聚合时直接平铺使用。
  - `groups.<分组名>`：聚合分组内的变量（对应图1的 Group1 + 添加分组）。
  - `commands.<指令名>`：`script`（Python 源码，需定义 `main` 并 `return dict`）、`inputs`（入参绑定）、`outputs`（输出变量，对应图2）。

## 变量来源（inputs.source 语法）

| 来源 | 含义 |
| --- | --- |
| `var:<key>` | 群自定义变量 |
| `all:vars` | 全部自定义变量（dict） |
| `gvar:<分组名>.<key>` | 聚合分组内单个变量 |
| `gall:<分组名>` | 整个聚合分组（dict） |
| `arg:<n>` | 触发消息的第 n 个参数（`查询 10086` 中 `10086` 为 arg:1） |
| `sender_id` / `sender_name` / `group_id` | 触发者/群信息 |
| `literal:<文本>` | 固定文本 |

类型：`string`（默认）、`number`、`bool`、`json`。引用整个分组时用 `json` 或 `string`（自动序列化）。

## 聊天管理指令（管理员）

```
/gvc help                          帮助
/gvc add <群号>                    添加群配置
/gvc del <群号>                    删除群配置
/gvc agg on|off                    当前群开启/关闭聚合分组
/gvc set <key> <值>                设置自定义变量
/gvc unset <key>                   删除自定义变量
/gvc gadd <分组名>                 添加分组（自动开启聚合）
/gvc gdel <分组名>                 删除分组
/gvc gset <分组名> <key> <值>      设置分组变量
/gvc gunset <分组名> <key>         删除分组变量
/gvc cmd add <指令名>              创建指令（群成员直接发指令名触发）
/gvc cmd script <指令名>           随后发一条消息，内容即 Python 脚本
/gvc cmd bind <指令名> <参数名> <来源> [类型]
/gvc cmd unbind <指令名> <参数名>
/gvc cmd out <指令名> <变量名> [显示名]
/gvc cmd outdel <指令名> <变量名>
/gvc cmd list / cmd show <指令名>
/gvc show                          查看当前群完整配置
```

## 使用示例（对应图2）

1. 管理员在群内：`/gvc cmd add 查询`
2. `/gvc cmd script 查询`，然后发送：

```python
def main(role_id, role_name, urs, tongqian):
    result = f"预存id: {role_id}\n昵称: {role_name}\nurs: {urs}(已冻结)\n铜钱: {tongqian}"
    return {"result": result}
```

3. 绑定入参：

```
/gvc cmd bind 查询 role_id gvar:Group1.role_id string
/gvc cmd bind 查询 role_name gvar:Group1.role_name string
/gvc cmd bind 查询 urs gvar:Group1.urs string
/gvc cmd bind 查询 tongqian gvar:Group1.tongqian number
/gvc cmd out 查询 result
```

4. 群成员发送 `查询` → 机器人回复：

```
result: 预存id: 10001
昵称: 小明
urs: abc123(已冻结)
铜钱: 350
```

## 注意事项

- 脚本拥有与 AstrBot 进程相同的系统权限，只将 `cmd script` 权限授予可信管理员。
- 指令按名称最长优先匹配；指令名不要包含空格。
- 修改 `admins` 需先在 WebUI 插件设置中手动添加第一位管理员。
