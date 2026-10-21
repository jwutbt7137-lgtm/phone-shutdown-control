#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""
BLE 广播探针 —— 只用来看"电脑到底有没有把 BLE 广播发出去"。

用途：在另一个窗口运行 pc_server.py 之后，跑这个脚本，
      如果能看到 PC-Shutdown，说明电脑端服务确实在广播，手机就能扫到。

用法：
    python scan_ble.py            # 扫 12 秒
    python scan_ble.py --seconds 20
    python scan_ble.py --all      # 连没名字的设备也列出来

这个脚本只读，不连接任何设备，也不改任何东西。
"""

from __future__ import annotations

import argparse
import asyncio
import sys
import time
from collections import OrderedDict

for _name in ("stdout", "stderr"):
    _s = getattr(sys, _name, None)
    _r = getattr(_s, "reconfigure", None)
    if _r is not None:
        try:
            _r(encoding="utf-8", errors="replace")
        except Exception:                               # noqa: BLE001
            pass


def main() -> int:
    parser = argparse.ArgumentParser(description="扫描本机周围 BLE 广播")
    parser.add_argument("--seconds", type=float, default=12.0, help="扫描时长（秒）")
    parser.add_argument("--all", action="store_true", help="无名字的设备也列出")
    parser.add_argument("--watch-name", default="PC-Shutdown", help="重点关注的广播名")
    args = parser.parse_args()

    try:
        from winrt.windows.devices.bluetooth.advertisement import (
            BluetoothLEAdvertisementWatcher,
            BluetoothLEScanningMode,
        )
    except ImportError as exc:
        print(f"缺少 WinRT 组件：{exc}")
        print("请先按 README 安装 requirements.txt 里的依赖")
        return 2
    except Exception as exc:                            # noqa: BLE001
        print(f"加载 WinRT 失败：{exc!r}")
        return 2

    found: "OrderedDict[int, tuple]" = OrderedDict()
    target_hits = 0
    loop = asyncio.new_event_loop()

    def on_received(_sender, ev: BluetoothLEAdvertisementReceivedEventArgs) -> None:
        nonlocal target_hits
        try:
            address = int(ev.bluetooth_address)
            name = ev.advertisement.local_name or ""
            rssi = ev.raw_signal_strength_in_dbm
        except Exception:                               # noqa: BLE001
            return

        if name and args.watch_name.lower() in name.lower():
            target_hits += 1
            print(f"  ★ 命中目标广播：{name}  RSSI={rssi}dBm  addr={address:012X}")

        if address not in found or (name and not found[address][0]):
            found[address] = (name, rssi)

    watcher = BluetoothLEAdvertisementWatcher()
    try:
        watcher.scanning_mode = BluetoothLEScanningMode.ACTIVE
    except Exception:                                   # noqa: BLE001
        pass
    watcher.add_received(on_received)

    print("=" * 62)
    print(f"扫描 BLE 广播，持续 {args.seconds:.0f} 秒…")
    print("=" * 62)

    try:
        watcher.start()
    except Exception as exc:                            # noqa: BLE001
        print(f"启动扫描失败：{exc!r}")
        print("请确认蓝牙已打开。")
        return 1

    deadline = time.monotonic() + args.seconds
    try:
        while time.monotonic() < deadline:
            loop.run_until_complete(asyncio.sleep(0.25))
    except KeyboardInterrupt:
        print("\n（手动中断）")
    finally:
        try:
            watcher.stop()
        except Exception:                               # noqa: BLE001
            pass

    print()
    print(f"共发现 {len(found)} 个 BLE 设备：")
    for address, (name, rssi) in found.items():
        label = name if name else "(无名字)"
        if not name and not args.all:
            continue
        print(f"  {address:012X}  {rssi:>5}dBm  {label}")

    print()
    if target_hits:
        print(f"结果：✓ 检测到「{args.watch_name}」广播 {target_hits} 次 —— 电脑端服务在正常工作")
        return 0
    print(f"结果：✗ 没有检测到「{args.watch_name}」广播")
    print("      请确认 pc_server.py 正在运行（另开一个窗口跑它）")
    return 1


if __name__ == "__main__":
    sys.exit(main())
