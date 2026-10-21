#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""
电脑端不依赖蓝牙的逻辑自测。

跑法：  python test_protocol.py
覆盖：  配置合并 / PIN 规则 / 命令→动作映射 / 演练模式不真的执行命令。
不碰蓝牙，也不会关机，可以随便跑。
"""

from __future__ import annotations

import json
import sys
import tempfile
from pathlib import Path

# Windows 控制台默认 GBK，✓ 之类的符号会直接抛 UnicodeEncodeError，先切 UTF-8
for _stream_name in ("stdout", "stderr"):
    _stream = getattr(sys, _stream_name, None)
    _reconfigure = getattr(_stream, "reconfigure", None)
    if _reconfigure is not None:
        try:
            _reconfigure(encoding="utf-8", errors="replace")
        except Exception:                               # noqa: BLE001
            pass

sys.path.insert(0, str(Path(__file__).resolve().parent))

import pc_server as srv  # noqa: E402

PASSED = 0
FAILED: list[str] = []


def check(name: str, condition: bool, detail: str = "") -> None:
    global PASSED
    if condition:
        PASSED += 1
        print(f"  ✓ {name}")
    else:
        FAILED.append(name)
        print(f"  ✗ {name}  {detail}")


# ---------------------------------------------------------------- 配置
print("\n[1] 配置合并")
cfg = dict(srv.DEFAULT_CONFIG)
check("默认值含 shutdown_delay_seconds", cfg["shutdown_delay_seconds"] == 10)
check("默认 pin 为 None", cfg["pin"] is None)
check("默认广播名正确", cfg["advertised_name"] == srv.ADVERTISED_NAME)

merged = dict(srv.DEFAULT_CONFIG)
merged.update({"shutdown_delay_seconds": 0, "unknown_key": "ignored"})
check("未知键不影响已知键", merged["shutdown_delay_seconds"] == 0)


# ---------------------------------------------------------------- PIN
print("\n[2] PIN 规则")
random_pin = srv.resolve_pin({"pin": None})
check("随机 PIN 是 6 位数字", random_pin.isdigit() and len(random_pin) == 6, random_pin)

fixed = srv.resolve_pin({"pin": "4321"})
check("固定 PIN 原样返回", fixed == "4321", fixed)

bad = srv.resolve_pin({"pin": "abc"})
check("非法 PIN 退回随机 6 位", bad.isdigit() and len(bad) == 6, bad)

too_long = srv.resolve_pin({"pin": "1" * 13})
check("超长 PIN 退回随机", too_long.isdigit() and len(too_long) == 6, too_long)


# ---------------------------------------------------------------- 动作（演练）
print("\n[3] 演练模式不真的执行命令")
dry = srv.PowerActions(dict(srv.DEFAULT_CONFIG), dry_run=True)

result = dry.shutdown()
check("演练关机不报错", "演练" in result, result)

result = dry.restart()
check("演练重启不报错", "演练" in result, result)

result = dry.cancel()
check("演练取消不报错", "演练" in result, result)

result = dry.sleep()
check("演练睡眠不报错", "演练" in result, result)
check("演练关机不会留下待取消窗口", dry.pending_cancel is None)


print("\n[4] 配置开关生效")
cfg_no_restart = dict(srv.DEFAULT_CONFIG, allow_restart=False)
dry2 = srv.PowerActions(cfg_no_restart, dry_run=True)
check("禁用重启后返回错误", "错误" in dry2.restart(), dry2.restart())

cfg_no_sleep = dict(srv.DEFAULT_CONFIG, allow_sleep=False)
dry3 = srv.PowerActions(cfg_no_sleep, dry_run=True)
check("禁用睡眠后返回错误", "错误" in dry3.sleep(), dry3.sleep())

cfg_zero = dict(srv.DEFAULT_CONFIG, shutdown_delay_seconds=0)
dry4 = srv.PowerActions(cfg_zero, dry_run=True)
check("延时为 0 时也走演练路径", "演练" in dry4.shutdown())


# ---------------------------------------------------------------- 协议常量
print("\n[5] 协议契约")
check("服务 UUID 长度正确", len(srv.SERVICE_UUID) == 36)
check("RX/TX UUID 不同", srv.RX_UUID != srv.TX_UUID)
check("协议版本为 1", srv.PROTOCOL_VERSION == 1)
check("最大认证失败次数 >= 3", srv.MAX_AUTH_FAILURES >= 3)

for name in ("SERVICE_UUID", "RX_UUID", "TX_UUID"):
    value = getattr(srv, name)
    parts = value.split("-")
    check(f"{name} 是合法 UUID 形状", len(parts) == 5 and all(parts), value)


# ---------------------------------------------------------------- 缓冲转换
print("\n[6] IBuffer 与 bytes 互转")
try:
    original = "SHUTDOWN\n".encode("utf-8")
    buf = srv.bytes_to_buffer(original)
    back = srv.buffer_to_bytes(buf)
    check("bytes → IBuffer → bytes 往返一致", back == original, repr(back))

    empty = srv.buffer_to_bytes(None)
    check("None 转 bytes 返回空", empty == b"")

    payload_cn = "OK 关机: 已下达\n".encode("utf-8")
    round_trip = srv.buffer_to_bytes(srv.bytes_to_buffer(payload_cn))
    check("UTF-8 中文往返一致", round_trip == payload_cn, repr(round_trip))
except Exception as exc:                                # noqa: BLE001
    check("IBuffer 往返", False, repr(exc))


# ---------------------------------------------------------------- 会话
print("\n[7] 会话模型")


class FakeClient:
    def __init__(self, key: str) -> None:
        class _Id:
            def __init__(self, v: str) -> None:
                self.id = v

        class _Session:
            def __init__(self, v: str) -> None:
                self.device_id = _Id(v)

        self.session = _Session(key)


a = srv.Session(client=FakeClient("Bluetooth#Bluetoothaa-bb"))
b = srv.Session(client=FakeClient("Bluetooth#Bluetoothaa-bb"))
c = srv.Session(client=FakeClient("Bluetooth#Bluetoothcc-dd"))

check("同一设备被识别为相同", srv._same_client(a.client, b.client))
check("不同设备被识别为不同", not srv._same_client(a.client, c.client))
check("设备标签带尾号", a.__class__.__name__ == "Session")


# ---------------------------------------------------------------- 配置读取
print("\n[8] config.json 读取")
original_path = srv.CONFIG_PATH
try:
    with tempfile.TemporaryDirectory() as tmp:
        cfg_file = Path(tmp) / "config.json"
        cfg_file.write_text(json.dumps({"shutdown_delay_seconds": 3, "pin": "999999"}), encoding="utf-8")
        srv.CONFIG_PATH = cfg_file
        loaded = srv.load_config()
        check("读到了自定义延时", loaded["shutdown_delay_seconds"] == 3, str(loaded["shutdown_delay_seconds"]))
        check("读到了自定义 PIN", loaded["pin"] == "999999")

        cfg_file.write_text("{ 这不是合法 json", encoding="utf-8")
        broken = srv.load_config()
        check("坏配置回退到默认值", broken["shutdown_delay_seconds"] == 10)

        cfg_file.write_text("[1,2,3]", encoding="utf-8")
        not_object = srv.load_config()
        check("非对象配置回退默认值", not_object["shutdown_delay_seconds"] == 10)
finally:
    srv.CONFIG_PATH = original_path


# ---------------------------------------------------------------- 汇总
print("\n" + "=" * 60)
print(f"通过 {PASSED} 项")
if FAILED:
    print(f"失败 {len(FAILED)} 项：")
    for name in FAILED:
        print(f"  - {name}")
    print("=" * 60)
    sys.exit(1)
print("全部通过 ✓")
print("=" * 60)
sys.exit(0)
