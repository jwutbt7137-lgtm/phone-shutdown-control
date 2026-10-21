#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""
端到端自测 —— 在本机扮演"手机"的角色，用 BLE 客户端连上本机的关机服务。

这解决了一个实际问题：Windows 的 BLE 扫描器扫不到自己发出的广播，
所以光看"广播已启动"的日志并不能证明服务真的可用。这个脚本绕开扫描，
直接用蓝牙地址连接 GATT 服务，把整条链路走一遍：

    连接 → 发现服务 → 订阅通知 → 握手 → 认证 → 发命令 → 收应答

用法：
    # 窗口 1：用演练模式启动服务（不会真的关机）
    python pc_server.py --dry-run

    # 窗口 2：开始自测
    python test_e2e.py                 # 自动读本机蓝牙地址
    python test_e2e.py --pin 123456    # 指定 PIN（默认会自动去日志里找）
    python test_e2e.py --address F0D41577E411

安全性：脚本只做只读查询、PING，以及（在演练模式下）SHUTDOWN。
        如果检测到服务不是演练模式，它会拒绝发送关机类命令。
"""

from __future__ import annotations

import argparse
import asyncio
import os
import re
import sys
import time
import uuid
from pathlib import Path
from typing import List, Optional

for _n in ("stdout", "stderr"):
    _s = getattr(sys, _n, None)
    _r = getattr(_s, "reconfigure", None)
    if _r is not None:
        try:
            _r(encoding="utf-8", errors="replace")
        except Exception:                               # noqa: BLE001
            pass

sys.path.insert(0, str(Path(__file__).resolve().parent))

SERVICE_UUID = uuid.UUID("0000a001-1e4b-4f6a-9c3d-2b7e5a8f0c11")
RX_UUID = uuid.UUID("0000a002-1e4b-4f6a-9c3d-2b7e5a8f0c11")
TX_UUID = uuid.UUID("0000a003-1e4b-4f6a-9c3d-2b7e5a8f0c11")

LOG_PATH = Path(__file__).resolve().parent / "pc_server.log"

PASSED: List[str] = []
FAILED: List[str] = []


def check(name: str, ok: bool, detail: str = "") -> bool:
    if ok:
        PASSED.append(name)
        print(f"  ✓ {name}" + (f"  ({detail})" if detail else ""))
    else:
        FAILED.append(name)
        print(f"  ✗ {name}" + (f"  ({detail})" if detail else ""))
    return ok


def print_local_limit_hint() -> None:
    """本机自连失败时的准确结论（已实测确认，不是猜测）。"""
    print()
    print("  结论：Windows 不支持 BLE 客户端连接本机的 GATT 服务。")
    print("        实测 BluetoothLEDevice.from_bluetooth_address_async(本机地址)")
    print("        直接返回 None —— 这是操作系统层面的限制，不是服务的问题。")
    print()
    print("  这条自测路走不通时，验证手段有两条：")
    print("    1. 用手机装 App 实测（唯一能验证真实使用场景的办法）")
    print("    2. 插一个 USB 蓝牙适配器，把它的地址传给 --address，")
    print("       让它当客户端连内置蓝牙上的服务")


def read_last_config_line() -> Optional[str]:
    """读出日志里最近一次「配置 ...」行（由 pc_server.py 启动时写入，不含 PIN）。"""
    if not LOG_PATH.exists():
        return None
    try:
        text = LOG_PATH.read_text(encoding="utf-8", errors="replace")
    except Exception:                                   # noqa: BLE001
        return None
    hits = re.findall(r"配置[^\n]*模式=([\w-]+)", text)
    return hits[-1] if hits else None


def log_says_dry_run() -> bool:
    return read_last_config_line() == "dry-run"


async def get_local_address() -> Optional[str]:
    from winrt.windows.devices.bluetooth import BluetoothAdapter

    adapter = await BluetoothAdapter.get_default_async()
    if adapter is None:
        return None
    return f"{int(adapter.bluetooth_address):012X}"


async def find_service_uuid(address: str) -> Optional[str]:
    """先解析地址，确认这个地址上确实有我们的服务 UUID。"""
    from winrt.windows.devices.bluetooth import BluetoothLEDevice

    device = await BluetoothLEDevice.from_bluetooth_address_async(int(address, 16))
    if device is None:
        return None
    try:
        result = await device.get_gatt_services_async()
        for svc in result.services:
            if str(svc.uuid).lower() == str(SERVICE_UUID).lower():
                return str(svc.uuid)
    finally:
        device.close()
    return None


class Client:
    def __init__(self) -> None:
        self.device = None
        self.service = None
        self.rx = None
        self.tx = None
        self.lines: List[str] = []
        self.session_id: Optional[str] = None
        self._buffer = ""

    # ---------------------------------------------------------------- 连接

    async def connect(self, address: str) -> bool:
        from winrt.windows.devices.bluetooth import BluetoothLEDevice

        self.device = await BluetoothLEDevice.from_bluetooth_address_async(int(address, 16))
        if self.device is None:
            return False
        try:
            self.session_id = str(self.device.device_id)
        except Exception:                               # noqa: BLE001
            self.session_id = None

        result = await self.device.get_gatt_services_async()
        for svc in result.services:
            if str(svc.uuid).lower() == str(SERVICE_UUID).lower():
                self.service = svc
                break
        if self.service is None:
            return False

        chars = await self.service.get_characteristics_async()
        for ch in chars:
            u = str(ch.uuid).lower()
            if u == str(RX_UUID).lower():
                self.rx = ch
            elif u == str(TX_UUID).lower():
                self.tx = ch
        return self.rx is not None and self.tx is not None

    # ---------------------------------------------------------------- 收发

    def _on_notify(self, _sender, args) -> None:
        try:
            from winrt.windows.storage.streams import DataReader

            reader = DataReader.from_buffer(args.characteristic_value)
            size = int(reader.unconsumed_buffer_length)
            text = bytes(reader.read_buffer(size)).decode("utf-8", errors="replace") if size else ""
        except Exception as exc:                        # noqa: BLE001
            text = f"<解码失败 {exc!r}>"
        self._buffer += text
        while "\n" in self._buffer:
            line, self._buffer = self._buffer.split("\n", 1)
            line = line.strip()
            if line:
                self.lines.append(line)
                print(f"    ← {line}")

    async def subscribe(self) -> bool:
        from winrt.windows.devices.bluetooth.genericattributeprofile import (
            GattClientCharacteristicConfigurationDescriptorValue,
        )

        self.tx.add_value_changed(self._on_notify)
        status = await self.tx.write_client_characteristic_configuration_descriptor_async(
            GattClientCharacteristicConfigurationDescriptorValue.NOTIFY
        )
        # 0 == SUCCESS
        return int(status) == 0

    async def send(self, command: str) -> bool:
        from winrt.windows.storage.streams import DataWriter

        writer = DataWriter()
        writer.write_bytes((command + "\n").encode("utf-8"))
        payload = writer.detach_buffer()
        result = await self.rx.write_value_with_result_async(payload)
        # 0 == SUCCESS
        return int(result.status) == 0

    def close(self) -> None:
        try:
            if self.tx is not None:
                self.tx.remove_value_changed(self._on_notify)
        except Exception:                               # noqa: BLE001
            pass
        try:
            if self.device is not None:
                self.device.close()
        except Exception:                               # noqa: BLE001
            pass


async def run(address: str, pin: Optional[str], allow_power: bool) -> int:
    print("=" * 66)
    print("端到端自测：本机同时扮演「电脑端服务」与「手机端客户端」")
    print("=" * 66)

    client = Client()

    print("\n[1] 连接 GATT 服务")
    try:
        ok = await client.connect(address)
    except Exception as exc:                            # noqa: BLE001
        print(f"  连接抛异常：{exc!r}")
        print_local_limit_hint()
        return 3
    if not check("连上并找到服务与两个特征", ok):
        print_local_limit_hint()
        return 3

    try:
        print("\n[2] 订阅通知")
        if not check("通知订阅成功", await client.subscribe()):
            return 1

        print("\n[3] 握手")
        await client.send("HELLO")
        await asyncio.sleep(2.0)
        hello = next((l for l in client.lines if l.upper().startswith("HELLO")), None)
        check("收到 HELLO 应答", hello is not None, hello or "无应答")
        need_auth = bool(hello and "AUTH" in hello.upper())

        print("\n[4] 认证")
        if need_auth:
            if not pin:
                check("能拿到 PIN 用于认证", False, "日志里没找到，请用 --pin 指定")
                return 1
            await client.send(f"AUTH {pin}")
            await asyncio.sleep(2.0)
            auth_ok = any(l.upper().startswith("OK AUTH") for l in client.lines)
            if not check("PIN 正确时认证通过", auth_ok):
                return 1

            # 错误 PIN 必须被拒
            before = len(client.lines)
            await client.send("AUTH 000000" if pin != "000000" else "111111")
            await asyncio.sleep(2.0)
            wrong_rejected = any(
                l.upper().startswith("ERR AUTH") for l in client.lines[before:]
            )
            check("错误 PIN 被拒绝", wrong_rejected)
            # 重新认证回来
            await client.send(f"AUTH {pin}")
            await asyncio.sleep(1.5)
        else:
            print("  （电脑端未启用认证，跳过）")

        print("\n[5] 命令")
        before = len(client.lines)
        await client.send("PING")
        await asyncio.sleep(1.5)
        check("PING 得到 PONG", any(l.upper().startswith("PONG") for l in client.lines[before:]))

        before = len(client.lines)
        await client.send("QUERY")
        await asyncio.sleep(1.5)
        check("QUERY 得到 STATE", any(l.upper().startswith("STATE") for l in client.lines[before:]))

        before = len(client.lines)
        await client.send("BOGUS_COMMAND")
        await asyncio.sleep(1.5)
        check("未知命令被拒绝", any(l.upper().startswith("ERR UNKNOWN_CMD")
                                for l in client.lines[before:]))

        if allow_power:
            print("\n[6] 关机命令（演练模式下不会真的关机）")
            before = len(client.lines)
            await client.send("SHUTDOWN")
            await asyncio.sleep(2.5)
            reply = next((l for l in client.lines[before:] if l.upper().startswith("OK")), None)
            check("SHUTDOWN 得到 OK 应答", reply is not None, reply or "无应答")

            before = len(client.lines)
            await client.send("CANCEL")
            await asyncio.sleep(2.0)
            reply = next((l for l in client.lines[before:] if l.upper().startswith("OK")), None)
            check("CANCEL 得到 OK 应答", reply is not None, reply or "无应答")
        else:
            print("\n[6] 跳过关机命令（服务不是演练模式，避免真关机）")
            print("    想完整测试请用：python pc_server.py --dry-run")
    finally:
        client.close()

    print("\n" + "=" * 66)
    print(f"通过 {len(PASSED)} 项，失败 {len(FAILED)} 项")
    for name in FAILED:
        print(f"  - {name}")
    print("=" * 66)
    return 0 if not FAILED else 1


def main() -> int:
    parser = argparse.ArgumentParser(description="本机 BLE 端到端自测")
    parser.add_argument("--address", default=None, help="目标蓝牙地址，默认取本机")
    parser.add_argument("--pin", default=None,
                        help="认证 PIN；也可以用环境变量 PC_SHUTDOWN_PIN")
    parser.add_argument("--allow-power", action="store_true",
                        help="即使不是演练模式也测试关机命令（危险，默认关闭）")
    args = parser.parse_args()

    address = args.address
    if address is None:
        try:
            address = asyncio.run(get_local_address())
        except Exception as exc:                        # noqa: BLE001
            print(f"读取本机蓝牙地址失败：{exc!r}")
            return 2
    if not address:
        print("拿不到本机蓝牙地址，请用 --address 指定")
        return 2

    print(f"目标蓝牙地址：{address}")
    pin = args.pin or os.environ.get("PC_SHUTDOWN_PIN")
    if pin:
        print(f"使用的 PIN  ：{pin}（来源：{'命令行' if args.pin else '环境变量 PC_SHUTDOWN_PIN'}）")
    else:
        print("未找到 PIN：用 --pin 指定，或设置环境变量 PC_SHUTDOWN_PIN")
        print("（PIN 不再写进 pc_server.log，看服务窗口里那 6 位数）")

    dry = log_says_dry_run()
    allow_power = args.allow_power or dry
    if dry:
        print("服务当前是演练模式 —— 关机命令不会真的生效，可以放心测")

    try:
        return asyncio.run(run(address, pin, allow_power))
    except KeyboardInterrupt:
        print("\n已中断")
        return 130


if __name__ == "__main__":
    sys.exit(main())
