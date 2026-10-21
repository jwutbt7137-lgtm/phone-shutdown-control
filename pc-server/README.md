# 电脑端蓝牙关机服务

手机 App 通过蓝牙低功耗（BLE）连到这台电脑，远程执行**关机 / 重启 / 睡眠 / 取消关机**。
开机不在控制范围内（蓝牙断开时电脑无法被唤醒，这是物理限制，不是软件缺陷）。

---

## 一、最快用法

```powershell
# 1) 安装依赖（只做一次）
python -m pip install -i https://pypi.tuna.tsinghua.edu.cn/simple `
  winrt-runtime winrt-Windows.Foundation winrt-Windows.Foundation.Collections `
  winrt-Windows.Storage.Streams winrt-Windows.Devices.Bluetooth `
  winrt-Windows.Devices.Bluetooth.GenericAttributeProfile `
  winrt-Windows.Devices.Bluetooth.Advertisement

# 2) 先自检（只读，不会关机）
python pc_server.py --self-test

# 3) 演练模式跑一遍（不会真的关机，适合第一次验证连通性）
python pc_server.py --dry-run

# 4) 正式运行
python pc_server.py
```

或者直接双击 `启动服务.bat`。

---

## 二、界面上会看到什么

```
  蓝牙地址 : F0D41577E411
  广播标识 : 5053443102  （手机 App 靠它辅助识别本机）
  本次 PIN : 481920
  关机延时 : 10 秒（可用「取消关机」撤销）
  模式     : 正常（会真的关机）
```

- **PIN 默认固定**：`config.json` 里的 `pin` 写死了就是固定的，不用每次看窗口。
  把它改成 `null` 就恢复"每次启动随机生成"（更安全，但要每次抄 PIN）。
- 手机首次认证成功后会把 PIN 记在自己那边，以后自动认证、自动重连，
  所以正常情况下你**根本不需要输 PIN**。
- 关机命令默认带 10 秒倒计时，用 App 里的「取消关机」可以撤销。

> **关于蓝牙名**：Windows 不允许第三方应用设置广播名（实测设 `local_name`
> 直接报"拒绝访问"），所以手机扫描时看到的是电脑的系统蓝牙名（例如本机是 `RAINBOW`）。
> App 会记住你连过的电脑名来识别，也可以靠广播标识辅助判断。

---

## 三、命令行参数

| 参数 | 作用 |
| --- | --- |
| `--dry-run` | 演练模式：只打印将要执行的命令，**不会真的关机/重启** |
| `--self-test` | 检查依赖与 WinRT API 是否齐全，然后退出 |
| `--list-api` | 打印本机 WinRT BLE API 清单（排查兼容性用） |
| `--pin 123456` | 本次使用指定 PIN，忽略 `config.json` |
| `--log-level DEBUG` | 输出更详细的日志 |

---

## 四、config.json

仓库里只有示例 `config.example.json`。要用固定 PIN 就复制一份改名 `config.json` 再改
（`config.json` 已在 `.gitignore` 里，不会被误提交）：

```json
{
  "pin": null,
  "shutdown_delay_seconds": 10,
  "allow_restart": true,
  "allow_sleep": true,
  "allow_cancel": true,
  "advertised_name": "PC-Shutdown",
  "log_level": "INFO"
}
```

- `pin`：`null` = 每次随机；填 `"123456"` 则固定（省得每次看窗口）。
- `shutdown_delay_seconds`：`0` = 立即关机，不带取消窗口。
- `allow_*`：把对应功能关掉，手机端会收到错误提示。

---

## 五、安全说明

1. **PIN 校验**：没通过 `AUTH2 <hmac>`（协议 v2）或 `AUTH <pin>`（旧版）的连接，发任何控制命令都会被拒绝。
2. **单控制者**：同一时刻只有一个手机处于已认证状态，新认证会把旧的踢下线。
3. **防爆破**：同一连接连续输错 5 次 PIN 会锁定 30 秒，之后可重试。
4. **可撤销**：默认 10 秒倒计时，发现点错了还能救。
5. 所有动作都会写进 `pc_server.log`（同目录）。

> 建议：只在你自己的家用/办公网络环境使用。蓝牙有效距离约 10 米。

---

## 六、排错

| 现象 | 处理 |
| --- | --- |
| `--self-test` 报缺组件 | 按提示跑那段 pip 安装命令 |
| 手机扫描不到 `PC-Shutdown` | 确认服务在运行；确认手机蓝牙已开；距离拉近到 2–3 米；Windows 设置里蓝牙开关打开 |
| 服务启动报「创建 GATT 服务失败」 | 蓝牙被关了，或驱动不支持 BLE 外设模式；看 `pc_server.log` |
| 手机连上但一直「未认证」 | 看窗口里的 PIN 有没有输错；比对 `pc_server.log` 里的认证记录 |
| 关机命令返回「被系统拒绝」 | 用管理员身份重新打开一次服务 |
| 关闭服务窗口后手机还连着 | 正常现象，Windows 会自己回收 GATT 服务 |

---

## 七、技术要求

- Windows 10 1809（build 17763）或更高，Windows 11 全部支持
- 电脑要有支持 BLE 外设模式的蓝牙适配器（近几年的笔记本/台式机基本都满足）
- Python 3.9+（开发环境实测 3.14 正常；CI 在 3.12 上跑测试）
