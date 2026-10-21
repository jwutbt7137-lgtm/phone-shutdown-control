#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""
电脑端不依赖蓝牙的逻辑自测。

跑法：  python test_protocol.py
覆盖：  配置合并 / PIN 规则 / 命令→动作映射 / 演练模式不真的执行命令。
不碰蓝牙，也不会关机，可以随便跑。
"""

from __future__ import annotations

import hashlib
import hmac
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
check("协议版本为 2", srv.PROTOCOL_VERSION == 2)
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


# ---------------------------------------------------------------- 认证边界
print("\n[9] 认证边界")


class _Recorder:
    """把服务发给手机的应答收集起来，代替 tx_char。"""

    def __init__(self) -> None:
        self.lines: list[str] = []

    async def __call__(self, line: str, session: object = None) -> None:  # noqa: ANN001
        self.lines.append(line.strip())


class _FakeSession:
    def __init__(self, authenticated: bool = False) -> None:
        self.authenticated = authenticated
        self.failures = 0
        self.device_id = "AA:BB:CC:DD:EE:FF"

    def label(self) -> str:
        return "手机[测试]"


async def _auth_flow() -> list[str]:
    svc = srv.ShutdownService(dict(srv.DEFAULT_CONFIG), pin="135790", dry_run=True)
    rec = _Recorder()
    svc._send_line = rec                                # type: ignore[method-assign]

    await svc._handle_command("PING", None)             # 未认证 + 匿名
    await svc._handle_command("PING", _FakeSession(False))   # 已识别但未认证
    await svc._do_auth("000000", None)                  # 错误 PIN
    await svc._do_auth("135790", None)                  # 正确 PIN
    await svc._handle_command("PING", None)             # 全局已认证 + 匿名
    await svc._handle_command("PING", _FakeSession(False))   # 不能借用全局授权
    return rec.lines


import asyncio  # noqa: E402

_auth_lines = asyncio.run(_auth_flow())
check("未认证的匿名请求被拒", _auth_lines[0].startswith("ERR NEED_AUTH"), _auth_lines[0])
check("未认证的已识别会话被拒", _auth_lines[1].startswith("ERR NEED_AUTH"), _auth_lines[1])
check("错误 PIN 返回认证失败", _auth_lines[2].startswith("ERR AUTH"), _auth_lines[2])
check("正确 PIN 返回 OK AUTH", _auth_lines[3] == "OK AUTH", _auth_lines[3])
check("全局认证后匿名请求放行", _auth_lines[4] == "PONG", _auth_lines[4])
check("已识别但未认证的会话仍被拒", _auth_lines[5].startswith("ERR NEED_AUTH"), _auth_lines[5])


async def _hmac_flow() -> list[str]:
    svc = srv.ShutdownService(dict(srv.DEFAULT_CONFIG), pin="135790", dry_run=True)
    rec = _Recorder()
    svc._send_line = rec                                # type: ignore[method-assign]

    nonce = svc._nonce_for(None)
    good = hmac.new(b"135790", nonce.encode("utf-8"), hashlib.sha256).hexdigest()
    await svc._do_auth_hmac("deadbeef", None)           # 错误应答
    await svc._do_auth_hmac(good, None)                 # 正确应答
    await svc._do_auth_hmac(good, None)                 # 重放：nonce 已轮换
    hello = svc._hello_line(None)
    return [*rec.lines, hello.strip()]


_hmac_lines = asyncio.run(_hmac_flow())
check("HMAC 错误应答被拒", _hmac_lines[0].startswith("ERR AUTH"), _hmac_lines[0])
check("HMAC 正确应答通过", _hmac_lines[1] == "OK AUTH", _hmac_lines[1])
check("HMAC 重放被拒（nonce 已轮换）", _hmac_lines[2].startswith("ERR AUTH"), _hmac_lines[2])
check("HELLO 带 nonce 且版本为 2",
      _hmac_lines[3].startswith("HELLO 2 AUTH ") and len(_hmac_lines[3].split()) == 4,
      _hmac_lines[3])


# ---------------------------------------------------------------- 配置校验
print("\n[10] config.json 类型校验")
_v_ok = srv.load_config()
check("布尔字符串 false 转 False",
      srv._as_bool("false", True) is False and srv._as_bool("true", False) is True)
check("非法延时回退默认值", srv._as_int("abc", 10, 0, 600) == 10)
check("延时范围被夹紧",
      srv._as_int(9999, 10, 0, 600) == 600 and srv._as_int(-5, 10, 0, 600) == 0)
check("配置返回完整键",
      all(k in _v_ok for k in ("pin", "shutdown_delay_seconds", "allow_restart")))


# ---------------------------------------------------------------- 通知分片
print("\n[11] 通知分片（BLE 单包上限）")
_payload = "OK 关机: 10 秒后执行（可取消）\n".encode("utf-8")
_chunks = srv._chunk_utf8(_payload, srv.NOTIFY_CHUNK_BYTES)
check("每片不超过单包上限",
      all(len(c) <= srv.NOTIFY_CHUNK_BYTES for c in _chunks),
      str([len(c) for c in _chunks]))
check("拼回后与原文一致", b"".join(_chunks) == _payload)
check("每一片都是合法 UTF-8",
      all(c.decode("utf-8") for c in _chunks))
check("空数据返回空列表", srv._chunk_utf8(b"", 20) == [])


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
