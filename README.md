# 群变量指令 (astrbot_plugin_group_var_commands)

AstrBot 插件：按群号管理聚合分组变量，自定义指令绑定 Python 脚本执行。自带 WebUI 可视化控制面板，群内零管理指令。

## 安装

1. WebUI「扩展功能」从仓库地址安装 `https://github.com/tan90-c/astrbot_plugin_group_var_commands`，或将整个文件夹放入 `data/plugins/`。
2. 重启/重载插件。
3. 打开 WebUI 插件详情页 → 「控制面板」进行全部配置；保存即生效，无需重启。

要求 AstrBot >= 4.9.2（控制面板；核心功能 >= 3.4），Python >= 3.10。脚本在独立 Python 子进程中运行，无额外依赖。

## 控制面板

1. 群与全局：添加/删除群号（删除有确认弹窗）、脚本超时
2. 群自定义变量（vars）：键值对增删
3. 聚合分组：默认形式，可添加多个分组，支持分组改名、删除（均有确认弹窗）与每组内变量增删改
4. 指令配置：创建/删除指令、Python 脚本编辑器、输出变量表
5. 高级 JSON：双向同步，可整段粘贴导入

v1.3.0 变化：删除了“入参绑定”——脚本不再需要声明来源，直接按名取用默认入参；面板删除操作全部改为页面内确认弹窗（修复 iframe 中 confirm 失效导致“删除无效”），变量表改为收割式重建（修复重复添加/删除出现幽灵 var1/var2 的问题）。

## 脚本默认入参

`main()` 形参按需取用，运行器按签名自动过滤（用不到的不写，多余的不传）：

| 参数 | 类型 | 含义 |
| --- | --- | --- |
| `args` | dict | 聚合分组全量：`{"分组名": {"变量名": 值, ...}, ...}` |
| `cmd` | str | 触发的指令名 |
| `value` | str | 指令名后面的参数原文（如 `状态 1区` 中的 `1区`） |
| `vars` | dict | 群自定义变量 |
| `sender_id` | str | 触发者 QQ 号 |
| `sender_name` | str | 触发者昵称 |
| `group_id` | str | 群号 |

脚本需 `return dict`（也可 `return` 其他值，会包装成 `{"result": ...}`；脚本内 `print` 的内容会附带在回复前面）。

## 触发方式

群成员在已配置的群内发送：

```
状态
/状态
状态 1区          ← cmd="状态"，value="1区"
```

按指令名最长优先匹配；指令名不含空格；未配置的群/指令一律静默。

## 配置结构（WebUI 插件设置 JSON）

```json
{
  "script_timeout": 30,
  "groups": {
    "1122334455": {
      "aggregate": true,
      "vars": { "api_url": "http://10.0.0.5:8080" },
      "groups": {
        "Group1": { "ip": "192.168.1.10", "port": "25565", "name": "示例服务器" }
      },
      "commands": {
        "状态": {
          "script": "def main(args, cmd, value):\n    g = args.get(\"Group1\", {})\n    result = f\"{g.get('name')} ({g.get('ip')}:{g.get('port')}) 运行中\"\n    return {\"result\": result}",
          "outputs": [{ "name": "result", "label": "结果" }]
        }
      }
    }
  }
}
```

## Hello World 示例

```python
def main(cmd, value):
    return {"result": "Hello, " + (value or "World") + "!"}
```

群成员发 `问候` → `Hello, World!`；发 `问候 AstrBot` → `Hello, AstrBot!`

## 注意事项

1. 脚本拥有与 AstrBot 进程相同的系统权限，控制面板/配置页仅交给可信管理员。
2. 旧配置里的 `inputs` 字段已废弃：保存时自动丢弃，不影响加载。
3. 聚合分组 `aggregate` 字段保留仅为兼容，现在恒为启用。
