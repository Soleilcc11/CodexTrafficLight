# Codex Traffic Light

Codex 菜单栏 + ESP32-C3 BLE 硬件状态灯监控工具。它从本机 `~/.codex/sessions/**/*.jsonl` 会话日志推断 Codex 当前状态，在 macOS 菜单栏显示摘要，并可同步控制兼容 CursorLight 固件的实体红绿灯。

![macOS](https://img.shields.io/badge/macOS-supported-blue)
![Python](https://img.shields.io/badge/Python-3.9+-green)
![License](https://img.shields.io/badge/License-MIT-yellow)

## 功能特性

- **红绿灯状态指示**：在 macOS 菜单栏实时显示 Codex 会话状态，并同步到硬件
  - 🟢 绿灯常亮 — 任务成功 / Codex 会话进行中
  - 🟡 黄灯闪烁 — 请求审查 / 等待权限
  - 🔴 红灯 — 任务完成闪烁 / 异常 / 空闲
- **ESP32-C3 BLE 硬件输出**：兼容 `CursorLight` 固件协议，通过 BLE 写入灯效模式
- **多项目支持**：按 Codex 会话的 `cwd` 自动分组，同时监控多个项目，一键切换
- **自动配置**：启动时自动配置 Codex `notify` 桥接，退出时自动还原
- **配置备份**：安全备份原始 `~/.codex/config.toml`，避免覆盖现有配置

## 安装

### 从源码运行

```bash
git clone https://github.com/Soleilcc11/CodexTrafficLight.git
cd CodexTrafficLight

python3 -m venv venv
source venv/bin/activate
pip install -r requirements.txt

python traffic_light.py
```

启动后，菜单栏会出现红绿灯图标并自动开始监控 Codex 状态；如果附近存在已刷入 CursorLight 固件的 ESP32-C3，会同步发送硬件灯效。

### 构建 macOS App

```bash
./build.sh
```

构建完成后，应用位于 `dist/CodexTrafficLight.app`。

## 退出

- 点击菜单栏红绿灯图标，选择「退出」
- 或按 `Ctrl+C` 终止进程

退出时会自动还原 `~/.codex/config.toml`。

## 工作原理

1. **会话轮询**：读取 `~/.codex/sessions/**/*.jsonl`，根据最新事件推断会话活跃状态。
2. **阶段检测**：根据用户输入、助手输出、普通工具调用和权限请求推断思考、生成、请求审查。
3. **结果检测**：发现 `phase=final` 后，根据最近工具输出判断任务完成或异常。
4. **通知桥接**：启动时备份 `~/.codex/config.toml`，将 Codex `notify` 临时指向 `~/.codex/traffic_light/codex_notify_bridge.py`，并在桥接脚本中继续调用原始 `notify` 命令。
5. **硬件同步**：使用 `bleak` 扫描 `CursorLight` BLE 设备，并向 mode characteristic 写入模式字符串；连接失败只写日志，不影响菜单栏。

## 配置路径

- Codex 配置：`~/.codex/config.toml`
- 配置备份：`~/.codex/traffic_light/config_backup.toml`
- 通知桥接：`~/.codex/traffic_light/codex_notify_bridge.py`
- 项目选择：`~/.codex/traffic_light/selected_project`
- 硬件日志：`~/.codex/traffic_light/hardware.log`
- 会话来源：`~/.codex/sessions/**/*.jsonl`

## 状态映射

| Codex 事件/状态 | 菜单栏状态 | 硬件 mode |
| --- | --- |
| 新回合刚开始，尚未看到助手输出或工具调用 | 🟢 绿灯 | `green` 绿灯常亮 |
| 助手正在输出中，或存在未完成普通工具调用 | 🟢 绿灯 | `thinking` 连贯跑马灯 |
| 存在未完成的 `require_escalated` 权限请求 | 🟡 黄灯闪烁 | `busy` 黄灯慢闪 |
| 最新助手消息为 `phase=final`，且无明显错误输出 | 🔴 红灯 | `red` / `off` 闪 5 次，然后 `traffic` |
| 最新回合出现非零退出码、Traceback、PermissionError、拒绝授权等 | 🔴 红灯 | `error` 红灯快闪 |
| 会话超过活跃宽限时间无新事件 | 🔴 红灯 | `traffic` 模拟红绿灯 |
| 没有可监控会话 | 🔴 红灯 | `off` 全灭 |

> “任务完成红灯闪 5 次”由电脑端连续发送 `red` / `off` 实现，最后自动切回 `traffic`。

## ESP32-C3 BLE 硬件

本项目包含 ESP32-C3 参考固件：[ESP32C3/ESP32C3.ino](ESP32C3/ESP32C3.ino)，并兼容参考项目 `JasonLam08/cursor_agent_status_light` 的默认固件协议：

```text
Device Name: CursorLight
Service UUID: b8b7e001-7a6b-4f4f-9a8b-11c0ffee0001
Mode Characteristic UUID: b8b7e002-7a6b-4f4f-9a8b-11c0ffee0001
```

推荐开发板与接线：

| 灯位 | ESP32-C3 引脚 | 说明 |
| --- | --- | --- |
| 绿灯 | IO2 | 通过 220Ω 电阻接 L1 |
| 黄灯 | IO3 | 通过 220Ω 电阻接 L2 |
| 红灯 | IO4 | 通过 220Ω 电阻接 L3 |
| 公共正极 | 3.3V | 原灯板正极 |

原固件使用公共正极逻辑：`GPIO LOW = 灯亮`，电脑端无需关心高低电平。

### 手动测试

```bash
python3 - <<'PY'
import asyncio
from bleak import BleakClient, BleakScanner

MODE_CHAR_UUID = "b8b7e002-7a6b-4f4f-9a8b-11c0ffee0001"

async def main(mode="success"):
    device = await BleakScanner.find_device_by_name("CursorLight", timeout=6)
    if device is None:
        raise SystemExit("CursorLight not found")
    async with BleakClient(device) as client:
        await client.write_gatt_char(MODE_CHAR_UUID, mode.encode(), response=True)

asyncio.run(main())
PY
```

macOS 如果提示蓝牙不可用，请到「系统设置 -> 隐私与安全性 -> 蓝牙」授权 Terminal、iTerm 或打包后的 `CodexTrafficLight.app`。

## 开发板方案分析与改进

ESP32-C3 SuperMini 的优点是便宜、体积小、USB-C 供电、BLE 无线、与参考固件兼容，macOS 和 Windows 都能控制。主要缺点是 BLE 首次连接较慢，macOS 需要蓝牙权限，扫描失败时不如 USB 串口直观。

当前实现通过 mode 去重、防抖和后台线程发送降低 BLE 抖动；硬件不可用时只写 `hardware.log`，菜单栏继续工作。硬件模式沿用参考固件：`demo / thinking / ai / busy / success / error / alarm / traffic / off`。

## 系统要求

- macOS 10.15+
- Python 3.9+
- BLE 依赖：`bleak`
- Codex Desktop 或会写入 `~/.codex/sessions` 的 Codex CLI

## 注意

Codex 目前不像 Claude Code 那样暴露完整的 `SessionStart`、`PreToolUse`、`PostToolUse` hook 矩阵，因此本迁移版本主要依靠 Codex session JSONL 事件做实时推断，并用 `notify` 作为可恢复的结束信号桥接。
