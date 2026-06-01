# CodexTrafficLight 功能说明

CodexTrafficLight 是一个面向 macOS 的 Codex 会话状态指示工具。它读取本机 Codex 会话日志，推断当前任务状态，并同时通过菜单栏和可选 ESP32-C3 BLE 实体红绿灯展示状态。

## 1. 核心功能

### 1.1 Codex 会话状态监控

程序会轮询以下本地文件：

```text
~/.codex/sessions/**/*.jsonl
~/.codex/session_index.jsonl
```

它从 session JSONL 中读取：

- 会话元信息：项目路径、会话 ID、模型信息
- 用户输入事件
- 工具调用事件
- 工具调用结果
- assistant final 回复
- 需要权限确认的工具调用

然后把 Codex 的原始事件转换成更容易理解的任务状态。

### 1.2 macOS 菜单栏状态灯

启动后，菜单栏会显示三个状态灯：

```text
🔴 ⚫ ⚫
⚫ 🟡 ⚫
⚫ ⚫ 🟢
```

菜单栏状态含义：

| 菜单栏状态 | 含义 |
| --- | --- |
| 🟢 绿灯 | Codex 会话进行中，或最近任务成功 |
| 🟡 黄灯闪烁 | Codex 正在等待用户确认权限 |
| 🔴 红灯 | 任务异常、会话结束或空闲 |

菜单栏菜单中还会展示：

- 当前选中的 Codex 项目
- 模型信息
- 硬件状态
- 硬件 mode
- 当前项目路径
- 状态说明

### 1.3 多项目支持

程序会按 Codex session 的 `cwd` 自动分组项目。

例如：

```text
/Users/soleilcc/Projects/PlaywithCodex
/Users/soleilcc/Projects/Claude_task/codex-traffic-light
```

如果存在多个 Codex 项目，菜单栏中可以切换要监控的项目。

选中的项目会记录到：

```text
~/.codex/traffic_light/selected_project
```

下次启动会优先恢复上次选择。

## 2. 状态判断规则

CodexTrafficLight 会把 Codex session 转换成以下硬件状态：

```text
thinking
generating
review_request
success
error
idle
off
```

### 2.1 thinking

判断条件：

- 新回合刚开始
- 尚未看到助手输出或工具调用
- 会话仍在活跃宽限时间内

含义：

```text
AI 正在分析
```

### 2.2 generating

判断条件：

- 助手已开始输出非 final 消息
- 或存在未完成的普通工具调用
- 会话仍在活跃宽限时间内

含义：

```text
AI 正在生成，硬件显示跑马灯
```

### 2.3 review_request

判断条件：

- 检测到未完成的 `require_escalated` 工具调用
- 或工具参数中包含 approval / permission 等权限相关字段

含义：

```text
Codex 正在等待用户审查或授权，硬件显示黄灯闪烁
```

典型场景：

- 需要访问网络
- 需要写入受限目录
- 需要执行沙盒外命令
- 需要用户批准高权限操作

### 2.4 success

判断条件：

- 最新 assistant 消息为 `phase=final`
- final 之前没有检测到明显错误输出

含义：

```text
本轮 Codex 任务正常完成
```

硬件行为：

```text
红灯闪烁 5 次，然后切回 traffic
```

### 2.5 error

判断条件：

本轮工具输出中包含明显错误特征，例如：

```text
Process exited with code 非 0
exec_command failed
Traceback
PermissionError
Rejected(...)
rejected by user
fatal:
ERROR:
```

并且随后进入 final。

含义：

```text
本轮任务出现异常或被拒绝
```

### 2.6 idle

判断条件：

- 会话超过活跃宽限时间无新事件

默认活跃宽限时间：

```text
20 秒
```

含义：

```text
Codex 会话结束、空闲或暂无活动
```

### 2.7 off

判断条件：

- 没有找到可监控的 Codex 会话

含义：

```text
关闭硬件灯效
```

## 3. 硬件状态灯功能

### 3.1 支持的硬件

当前硬件输出层兼容 `JasonLam08/cursor_agent_status_light` 项目的 ESP32-C3 BLE 固件。

默认 BLE 配置：

```text
Device Name: CursorLight
Service UUID: b8b7e001-7a6b-4f4f-9a8b-11c0ffee0001
Mode Characteristic UUID: b8b7e002-7a6b-4f4f-9a8b-11c0ffee0001
```

电脑端通过 BLE 写入字符串 mode 控制灯效。

### 3.2 硬件状态映射

| Codex 硬件状态 | 发送到 ESP32-C3 的 mode | 当前灯效 |
| --- | --- | --- |
| 程序启动 | `demo` | 自动展示多种灯效 |
| `thinking` | `green` | 绿灯常亮 |
| `generating` | `thinking` | 连贯跑马灯 |
| `review_request` | `busy` | 黄灯慢闪 |
| `success` | `red_blink_5` | 电脑端发送 `red` / `off` 闪 5 次，然后 `traffic` |
| `error` | `error` | 红灯快闪 |
| `idle` | `traffic` | 模拟红绿灯 |
| `off` | `off` | 全灭 |

说明：

- 思考状态用参考固件现有 `green` mode 表示绿灯常亮。
- 生成状态用参考固件 `thinking` mode 表示跑马灯。
- 请求审查用参考固件 `busy` mode 表示黄灯慢闪。
- 任务完成红灯闪 5 次由电脑端连续写入 `red` / `off` 实现，最后回到 `traffic`。
- 空闲状态使用参考固件的展示模式 `traffic`。

### 3.3 BLE 发送策略

硬件输出采用 best-effort 策略：

- 后台线程异步发送 BLE 指令
- 发送失败不会影响菜单栏
- 相同 mode 会防抖，避免频繁扫描 BLE
- 同一时刻只允许一个 BLE 发送任务
- 找不到设备或没有蓝牙权限时写日志

硬件日志位置：

```text
~/.codex/traffic_light/hardware.log
```

## 4. Codex notify 自动配置

CodexTrafficLight 启动时会临时配置 Codex 的 `notify`：

```text
~/.codex/config.toml
```

它会先备份原配置：

```text
~/.codex/traffic_light/config_backup.toml
```

然后把 `notify` 指向桥接脚本：

```text
~/.codex/traffic_light/codex_notify_bridge.py
```

桥接脚本会：

1. 写入 turn-ended 状态文件
2. 尝试继续调用原来的 Codex notify 命令

程序正常退出时会自动还原：

```text
~/.codex/config.toml
```

## 5. 配置和数据文件

| 路径 | 用途 |
| --- | --- |
| `~/.codex/config.toml` | Codex 主配置 |
| `~/.codex/traffic_light/config_backup.toml` | Codex 配置备份 |
| `~/.codex/traffic_light/codex_notify_bridge.py` | notify 桥接脚本 |
| `~/.codex/traffic_light/state/turn_ended.json` | turn-ended 状态文件 |
| `~/.codex/traffic_light/selected_project` | 当前选中项目 |
| `~/.codex/traffic_light/hardware.log` | BLE 硬件日志 |
| `~/.codex/sessions/**/*.jsonl` | Codex 会话日志来源 |
| `~/.codex/session_index.jsonl` | Codex 会话名称索引 |

## 6. 项目文件说明

| 文件 | 说明 |
| --- | --- |
| `traffic_light.py` | 主程序，包含状态解析、菜单栏 UI、BLE 硬件输出 |
| `requirements.txt` | Python 依赖 |
| `build.sh` | macOS App 打包脚本 |
| `FUNCTIONS.md` | 本功能说明文档 |
| `tests/test_status_parser.py` | 状态解析测试 |
| `traffic_light.icns` | App 图标 |

## 7. 运行方式

源码运行：

```bash
cd /Users/soleilcc/Projects/Claude_task/codex-traffic-light
source ./venv/bin/activate
python traffic_light.py
```

如果当前终端的 `python` 没有指向项目 venv，可以直接运行：

```bash
./venv/bin/python traffic_light.py
```

构建 App：

```bash
./build.sh
```

打开 App：

```bash
open dist/CodexTrafficLight.app
```

## 8. 测试能力

项目包含状态解析单元测试，覆盖：

- `thinking -> green`
- `generating -> thinking`
- `review_request -> busy`
- `success -> red_blink_5`
- `error -> error`
- `idle -> traffic`

运行测试：

```bash
python3 -m unittest tests/test_status_parser.py
```

语法检查：

```bash
python3 -m py_compile traffic_light.py tests/test_status_parser.py
```

构建检查：

```bash
./build.sh
test -d dist/CodexTrafficLight.app && echo app-build-ok
```

## 9. 当前限制

### 9.1 Codex 没有完整 hook 矩阵

CodexTrafficLight 主要依赖 session JSONL 轮询和 notify 桥接，不像 Claude Code 或 Cursor Hooks 那样拥有完整的事件 hook。

因此状态判断是基于日志推断，而不是强事件回调。

### 9.2 成功/异常是启发式判断

异常判断依赖工具输出文本特征，例如非零退出码、Traceback、PermissionError 等。

如果工具失败但输出没有明显错误文本，可能被判断为 success。

### 9.3 Codex 阶段判断是启发式

参考固件支持完整的 `demo / thinking / ai / busy / success / error / alarm / traffic / off` 模式，但 Codex 本身没有完整 hook 矩阵，因此以下阶段来自 session 日志推断：

- `thinking`：新回合开始但尚未看到助手输出或工具调用。
- `generating`：检测到助手非 final 输出或未完成普通工具调用。
- `review_request`：检测到未完成权限请求。
- `success`：检测到 final 且本回合无明显错误输出。

### 9.4 BLE 连接受 macOS 权限影响

macOS 需要给 Terminal、iTerm 或 `CodexTrafficLight.app` 蓝牙权限。

权限路径：

```text
系统设置 -> 隐私与安全性 -> 蓝牙
```

## 10. 后续改进建议

1. 在菜单中增加“硬件启用/禁用”开关。
2. 在菜单中增加“发送测试模式”子菜单，例如 success、error、traffic。
3. 缓存 BLE 设备地址，减少每次发送前的扫描时间。
4. 增加 USB 串口输出模式，作为 BLE 不稳定场景的替代方案。
5. 将状态解析和硬件输出拆分成独立模块，降低 `traffic_light.py` 单文件复杂度。
