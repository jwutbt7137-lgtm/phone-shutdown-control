# 电脑关机 · Phone Shutdown

**用手机把电脑关掉。** Android App + Windows 蓝牙 BLE 服务端，不依赖 Wi-Fi、不需要远程桌面、不经过任何云服务。

[English](README.en.md)

[![CI](https://github.com/jwutbt7137-lgtm/phone-shutdown-control/actions/workflows/ci.yml/badge.svg)](https://github.com/jwutbt7137-lgtm/phone-shutdown-control/actions/workflows/ci.yml)
[![Release](https://img.shields.io/github/v/release/jwutbt7137-lgtm/phone-shutdown-control?color=2ea44f)](https://github.com/jwutbt7137-lgtm/phone-shutdown-control/releases)
[![License: MIT](https://img.shields.io/badge/license-MIT-yellow.svg)](LICENSE)
[![Platform](https://img.shields.io/badge/platform-Windows%2010%2B%20%7C%20Android%208%2B-0078d4)](https://github.com/jwutbt7137-lgtm/phone-shutdown-control)

<img src="docs/images/app.png" alt="Android App 界面" width="360">

---

## 这是什么

躺床上、坐在客厅、手上正端着东西——不想走到电脑前按电源键的时候，掏出手机点一下：

| 按钮 | 电脑会做什么 |
| --- | --- |
| 关机 | `shutdown /s /t 10`，10 秒倒计时，可撤销 |
| 重启 | `shutdown /r /t 10` |
| 睡眠 | 进入睡眠 |
| 取消关机 | `shutdown /a`，撤销还没执行的关机 |
| 查询状态 | 读取电脑端待执行状态 |
| 测试连通 | 链路 ping |

**只有"关机"方向，没有"开机"。** 电脑断电后蓝牙跟着断，物理上无法用蓝牙唤醒——这是原理限制，不是功能没做完。

## 为什么用 BLE 而不是别的

| 方案 | 问题 |
| --- | --- |
| 远程桌面 / SSH | 要同一网络、要账号密码、手机端体验重 |
| Wake-on-LAN | 只管开机，不管关机；还要网卡支持 |
| 智能插座 | 断电式关机，有丢数据风险 |
| **蓝牙 BLE GATT** | **免网络、免账号、手机原生支持，只在本机 10 米内可用** |

本项目走 BLE GATT 外设模式：Windows 端用 `GattServiceProvider` 起服务并广播，手机作为 GATT Client 连上来发命令。不需要手动配 COM 口，不需要每次点配对确认。

## 工作原理

```
┌──────────────────┐          BLE (GATT)           ┌────────────────────┐
│   Android 手机    │  ──────────────────────────▶  │    Windows 电脑     │
│   「电脑关机」App │   AUTH <pin>                  │  pc_server.py      │
│                  │   SHUTDOWN / RESTART / ...    │  → shutdown /s     │
└──────────────────┘                               └────────────────────┘
```

1. 电脑端启动，开 GATT 服务 + 广播，窗口显示 6 位 PIN
2. 手机扫描到电脑（靠广播里的 `PSD1` 标识识别），连接、订阅通知
3. 输入 PIN → 认证通过 → 按钮点亮
4. 发命令 → 电脑执行 → 手机收到结果

协议细节（UUID、消息格式、广播标识）见 [docs/协议与架构.md](docs/协议与架构.md)。

## 快速开始

### 第 1 步：电脑端

要求：Windows 10 1809+ / Windows 11，带 BLE 的蓝牙适配器，Python 3.9+。

```powershell
cd pc-server
python -m pip install -r requirements.txt
python pc_server.py
```

窗口里会打印：

```
  蓝牙地址 : F0D41577E411
  广播标识 : 5053443102     ← 手机靠它认出本机（PSD1 + 协议版本）
  本次 PIN : 481920
```

也可以直接双击 `pc-server\一键启动（含依赖检查）.bat`，会自动装依赖、自检、再启动。

先不想真的关机？用演练模式：

```powershell
python pc_server.py --dry-run     # 只打印将要执行的命令
python pc_server.py --self-test   # 检查依赖与 WinRT API，然后退出
```

### 第 2 步：手机端

现成 APK 在 [Releases](https://github.com/jwutbt7137-lgtm/phone-shutdown-control/releases) 下载，传到手机安装。
或者开 USB 调试后：

```powershell
adb install -r "dist\电脑关机-debug.apk"
```

第一次打开要允许「附近的设备」权限（Android 12+ 是 `BLUETOOTH_SCAN` / `BLUETOOTH_CONNECT`，不索要定位权限）。

### 第 3 步：连上就能用

1. App 里点「扫描电脑」→ 出现 **★ 电脑** 条目 → 点「连接电脑」
2. 弹 PIN 输入框 → 填电脑窗口里的 6 位数
3. 状态变「已认证」→ 关机 / 重启按钮亮起

> 扫不到带 ★ 的条目？「附近的其他设备」里手动点那台电脑也能连。蓝牙名就是 Windows 设置里显示的电脑名。

## 安全设计

- **PIN 认证**：每次启动随机 6 位（也可在 `config.json` 固定），未认证只能收到 `ERR NEED_AUTH`
- **PIN 不上链路**：协议 v2 用 `HMAC-SHA256(PIN, nonce)` 认证，蓝牙旁听者拿不到 PIN；
  nonce 一次一换，旧应答不能重放（旧版 App 仍可用明文 `AUTH`，服务端会记警告）
- **单控制者**：同一时刻只允许一台手机处于已认证状态，新认证顶掉旧的
- **防爆破**：同一连接连续输错 5 次锁定 30 秒；能识别会话时按连接计数，不会误伤其它设备
- **可撤销**：关机 / 重启默认 10 秒倒计时，点「取消关机」还能救
- **审计**：所有命令写入 `pc_server.log`

威胁模型、不在防护范围内的场景、漏洞报告方式见 [SECURITY.md](SECURITY.md)。

## 项目结构

```
.
├── android/                  # 手机端：Kotlin + 原生 BLE
│   └── app/src/main/java/com/dsh/phoneshutdown/
│       ├── MainActivity.kt   # 界面、权限、命令按钮
│       ├── BleClient.kt      # 扫描 / 连接 / 重连 / 出站队列
│       └── BleProtocol.kt    # UUID、消息编解码
├── pc-server/                # 电脑端：Python + WinRT BLE GATT 服务
│   ├── pc_server.py          # 服务主体（含自检与演练模式）
│   ├── test_protocol.py      # 不碰蓝牙的逻辑测试
│   ├── test_e2e.py           # 本机扮演手机，端到端走一遍 GATT 链路
│   ├── scan_ble.py           # 扫描附近 BLE 设备（排查用）
│   └── requirements.txt      # winrt 分包依赖
├── docs/                     # 使用说明 / 协议与架构 / 故障排查 / 验证与自测
├── build-apk.ps1             # 离线工具链一键构建 APK
└── .github/                  # CI、Issue / PR 模板
```

## 开发

### 跑电脑端测试

不需要蓝牙硬件，不会真的关机：

```powershell
cd pc-server
python -m pip install -r requirements.txt
python test_protocol.py
```

端到端自测需要真实蓝牙适配器（窗口 1 开服务，窗口 2 跑自测）：

```powershell
python pc_server.py --dry-run
python test_e2e.py
```

### 编译 APK

标准 Gradle 工程（AGP 8.7.3 / Kotlin 2.0.21 / compileSdk 35）：

```powershell
cd android
.\gradlew.bat assembleDebug        # Linux / macOS: ./gradlew assembleDebug
```

产物：`android/app/build/outputs/apk/debug/app-debug.apk`

没有网络、只想用本地工具链？`build-apk.ps1` 支持指定工具链根目录：

```powershell
$env:PHONE_SHUTDOWN_TOOLS = 'D:\android-toolchain'   # 内含 jdk\ sdk\ gradle\bin\gradle.bat
powershell -ExecutionPolicy Bypass -File .\build-apk.ps1
```

### 改协议时

手机端和电脑端的消息格式、UUID 是成对的，改一端必须同步另一端，并更新：

- `docs/协议与架构.md`
- `pc-server/test_protocol.py` 里的协议契约断言
- `android/.../BleProtocol.kt`

## 已知限制

- 只能关机，不能开机（蓝牙随电脑断电）
- 睡眠后无法用蓝牙唤醒（需要在 Windows 电源设置里配键盘/鼠标/网卡唤醒）
- 关掉电脑端服务窗口 = 停服，手机侧会断开
- Windows 不允许第三方应用改蓝牙广播名，手机看到的是电脑系统蓝牙名
- 所以广播里额外放了自定义数据段 `PSD1` 作为服务标识，手机不靠名字也能认出这台电脑

## 常见问题

| 现象 | 先看 |
| --- | --- |
| 装不上 / 闪退 / 扫不到设备 | [docs/故障排查.md](docs/故障排查.md) |
| 想确认功能是否正常 | [docs/验证与自测.md](docs/验证与自测.md) |
| 想了解协议细节 | [docs/协议与架构.md](docs/协议与架构.md) |
| 第一次用，要一步步来 | [docs/使用说明.md](docs/使用说明.md) |

## 参与贡献

欢迎 Issue / PR。提交前请读 [CONTRIBUTING.md](CONTRIBUTING.md)，并跑一遍 `python pc-server/test_protocol.py`。
安全问题请走私密渠道：[SECURITY.md](SECURITY.md)。

## 许可证

[MIT](LICENSE) © 2026 jwutbt7137-lgtm

## 免责声明

这个程序**真的会关机**。请在能接受"电脑被关掉"的环境使用：
保存好文档、别在跑长任务 / 下载 / 编译的时候手滑。
作者不对数据丢失、业务中断或任何间接损失负责。
