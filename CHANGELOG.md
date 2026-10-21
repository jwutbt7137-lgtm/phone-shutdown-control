# 更新日志

本项目遵循 [Keep a Changelog](https://keepachangelog.com/zh-CN/1.1.0/)，
版本号遵循 [语义化版本](https://semver.org/lang/zh-CN/)。

## [Unreleased]

### 计划中

- 支持自定义关机倒计时（手机端可调，当前只能改 `config.json`）
- 多电脑管理（一台手机记住多台已认证电脑）

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

[Unreleased]: https://github.com/jwutbt7137-lgtm/phone-shutdown-control/compare/v1.0.0...HEAD
[1.0.0]: https://github.com/jwutbt7137-lgtm/phone-shutdown-control/releases/tag/v1.0.0
