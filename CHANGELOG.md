# 更新日志

本项目遵循 [Keep a Changelog](https://keepachangelog.com/zh-CN/1.1.0/)，
版本号遵循 [语义化版本](https://semver.org/lang/zh-CN/)。

## [Unreleased]

### 变更

- `取消关机` 去掉重复分支，行为不变（本地没记录时也照样尝试一次 `shutdown /a`）

### 计划中

- 手机端可调的关机倒计时
- 多电脑管理（一台手机记住多台已认证电脑）

## [1.3.0] - 2026-10-21

### 新增

- **Release 自动化**：推送 `v*` tag 时自动构建 APK 并创建 Release、上传安装包
  （`.github/workflows/release.yml`，用仓库自带 `GITHUB_TOKEN`，不需要另配密钥）
- 手机端连接成功后请求 MTU 247，长消息的通知条数更少

### 修复

- **通知按 MTU 安全分片**：服务端按 20 字节切分（不切断 UTF-8 字符），
  长中文消息不再丢包或出现替换字符；接收端本来就按 `\n` 拼接，新旧 App 都兼容
- `AUTH2` 的 HMAC 也像 `AUTH` 一样在日志里打码

### 工程

- CI 增加 `python -m compileall` 语法检查

## [1.2.0] - 2026-10-21

### 新增

- **协议 v2：nonce + HMAC 认证**。电脑端 `HELLO 2 AUTH <nonce>`，手机回
  `AUTH2 HMAC-SHA256(PIN, nonce)`，PIN 不再出现在蓝牙链路上；nonce 一次一换，旧应答不能重放
- 兼容旧版：旧 App 的明文 `AUTH <pin>` 仍可用，服务端记警告
- 手机端扫描 12 秒自动停止；认出目标后 1.5 秒自动停扫并触发连接
- Android 15 edge-to-edge 适配（根布局消费 system bars / 挖孔 insets）
- 版本号终于对上：`versionName 1.2.0`（v1.1.0 的 APK 里代码没改，显示还是 1.0）

### 安全

- 单次写入最多处理 4 条命令，堵住"一次写入塞几十条 AUTH"绕过失败计数的路
- `config.json` 做类型/范围校验：`"false"` 字符串、非法延时、非对象配置都有兜底
- 手机端从"附近的其他设备"切换连接时保留上次电脑地址，「连接电脑」不会在扫描后被误禁用

### 修复

- 扫描停止事件不再重复触发（只有真的在扫时才回调）

## [1.1.0] - 2026-10-21

### 新增

- App 图标：自适应图标（绿色电源 + 手机轮廓），补齐 mdpi–xxxhdpi 五套密度
- 英文 README（`README.en.md`），中文 README 顶部加语言切换
- `build-apk.ps1` 构建完成后自动把 APK 复制到 `dist\电脑关机-debug.apk`
- `SECURITY.md` 补充"降级模式"（Windows GATT 拿不到来源客户端）的威胁说明

### 安全

- 随机 PIN 改用 `secrets.randbelow`（密码学安全随机源），不再用 `random`
- PIN 比较改用 `hmac.compare_digest`，消除按字符提前返回的时序差异
- 认证边界收紧：能识别出会话时只认该会话自己的认证状态，不再借用全局授权；
  只有识别不出来源时才退回全局状态，且首次放行会在日志里警告
- 认证计数改为按连接（连错 5 次锁定 30 秒），不再一错就连坐所有设备
- PIN 不再写进 `pc_server.log`（日志经常被贴到 Issue 里）
- `android:allowBackup="false"`：避免手机备份把 PIN 等本地数据带走
- 手机端只有"和上次认证过的同一台设备（按蓝牙地址）"才自动回放 PIN，
  换了设备会要求确认，降低被伪造广播钓鱼的风险
- 手机端日志区对 `AUTH` 命令做脱敏
- `test_protocol.py` 新增认证边界测试（6 项）

### 修复

- **查询状态不再会取消关机**：原来 `QUERY` 和 `--self-test` 用 `shutdown /a` 探测，
  等于查一次就取消一次；现在只报本地待取消窗口
- 关机命令失败时回 `ERR` 而不是把「错误：...」包成 `OK`
- 写请求取 Deferral，标准 Write With Response 客户端不再超时
- 关机类命令改为线程池执行，不再阻塞 BLE 事件循环
- 协程异常不再被静默吞掉，会写进日志
- 会话身份优先从 `GattWriteRequestedEventArgs.session` 获取（原来只查了
  GattWriteRequest，永远拿不到来源客户端）
- 日志改为轮转（1 MB × 3），避免无限增长
- 修 CI：`android-actions/setup-android@v3` 默认安装的 `tools` 包已从 SDK 仓库移除
- 手机端：`onDestroy` 停扫描 / 取消自动试连 / 清延时任务；连接后再加 10 秒就绪超时；
  GATT 回调校验 `gatt` 代际，迟到回调不再影响新连接；通知按字节累积再整行解码，
  修复中文跨分片乱码；PIN 对话框防重复弹出、防 Activity 销毁后 `show`
- `scan_ble.py` 同时识别广播名和 `PSD1` 数据段（原来只认名字，永远报 ✗）
- 两个 `.bat` 用 `py -3` 兜底，不再硬编码 `C:\Python314\python.exe`
- 文档里 `dist\电脑关机.apk` 与实际产物名不一致
- `pc-server/README.md` 里写死的示例 PIN 和本机蓝牙名改为通用示例

## [1.0.0] - 2026-10-21

首个公开版本。

### 新增

- **电脑端**（Windows 10 1809+）
  - 基于 WinRT `GattServiceProvider` 的 BLE GATT 服务与广播
  - 自定义广播数据段 `PSD1` 标识，解决 Windows 无法设置广播名的问题
  - PIN 认证（随机 6 位 / `config.json` 固定）、单控制者、连错 5 次断开
  - 关机 / 重启 / 睡眠 / 取消关机 / 状态查询 / PING
  - 默认 10 秒关机倒计时，可撤销
  - `--dry-run` 演练模式、`--self-test` 自检、`--list-api` API 探测
  - 命令审计日志 `pc_server.log`
  - `test_protocol.py` 逻辑测试、`test_e2e.py` 端到端自测、`scan_ble.py` 扫描工具

- **手机端**（Android 8.0+）
  - Kotlin 原生 BLE：扫描、连接、服务发现、通知订阅、出站队列
  - PIN 输入与记忆、已认证电脑自动重连
  - 关机 / 重启二次确认，避免误触
  - Android 12+ 使用 `BLUETOOTH_SCAN` / `BLUETOOTH_CONNECT`（非定位用途），旧版本回退定位权限
  - 连接 15 秒超时保护，界面不卡在"握手中"

- **工程**
  - 文档：使用说明、协议与架构、故障排查、验证与自测
  - GitHub Actions：Python 逻辑测试 + Android 构建
  - MIT 许可证

[Unreleased]: https://github.com/jwutbt7137-lgtm/phone-shutdown-control/compare/v1.3.0...HEAD
[1.3.0]: https://github.com/jwutbt7137-lgtm/phone-shutdown-control/compare/v1.2.0...v1.3.0
[1.2.0]: https://github.com/jwutbt7137-lgtm/phone-shutdown-control/compare/v1.1.0...v1.2.0
[1.1.0]: https://github.com/jwutbt7137-lgtm/phone-shutdown-control/compare/v1.0.0...v1.1.0
[1.0.0]: https://github.com/jwutbt7137-lgtm/phone-shutdown-control/releases/tag/v1.0.0
