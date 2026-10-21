#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""
PC-Shutdown —— 电脑端蓝牙关机服务（Windows 10 1809+ / Windows 11）

作用：在电脑上开一个蓝牙低功耗(BLE) GATT 服务并持续广播。
      手机 App 连上来、输入本窗口显示的 PIN 之后，就能发送关机 / 重启 / 睡眠等命令。

安全设计：
  1. PIN 校验：每次启动随机生成 6 位 PIN（也可在 config.json 里固定），
     手机必须先用 AUTH2 <hmac>（协议 v2）或旧版 AUTH <pin> 通过校验，
     否则任何控制命令都会被拒绝。
  2. 关机类命令默认带 10 秒倒计时；发送 CANCEL 可在倒计时结束前取消。
  3. 同一时刻只允许一个手机处于"已认证"状态，新认证会把旧的踢下线。

用法：
    python pc_server.py                 # 正常运行
    python pc_server.py --dry-run       # 演练模式：只打印将要执行的命令，既不关机也不取消
    python pc_server.py --self-test     # 静态自检：检查依赖与 API 是否可用后退出
    python pc_server.py --list-api      # 打印本机 WinRT BLE API 可用性
"""

from __future__ import annotations

import argparse
import asyncio
import hashlib
import hmac
import json
import locale
import logging
import os
import secrets
import socket
import subprocess
import sys
import threading
import time
import uuid
from dataclasses import dataclass
from pathlib import Path
from typing import Any, Callable, Dict, List, Optional

# ---------------------------------------------------------------------------
# 常量与协议
# ---------------------------------------------------------------------------

SERVICE_UUID = "0000a001-1e4b-4f6a-9c3d-2b7e5a8f0c11"
RX_UUID = "0000a002-1e4b-4f6a-9c3d-2b7e5a8f0c11"   # 手机 -> 电脑（写入）
TX_UUID = "0000a003-1e4b-4f6a-9c3d-2b7e5a8f0c11"   # 电脑 -> 手机（通知）

ADVERTISED_NAME = "PC-Shutdown"
PROTOCOL_VERSION = 2

# 广播数据段里的自定义标识（AD type 0xFF）。
# 手机端解析到这段字节就说明"这台就是关机服务"。
# 布局：b"PSD1" + 协议版本
ADVERT_MARKER = b"PSD1" + bytes([PROTOCOL_VERSION])

MAX_AUTH_FAILURES = 5
AUTH_LOCKOUT_SECONDS = 30
RX_BUFFER_LIMIT = 512
MAX_LINES_PER_WRITE = 4

HERE = Path(__file__).resolve().parent
CONFIG_PATH = HERE / "config.json"
LOG_PATH = HERE / "pc_server.log"

DEFAULT_CONFIG: Dict[str, Any] = {
    "pin": None,                 # null = 每次启动随机生成；也可写死 "123456"
    "shutdown_delay_seconds": 10,
    "allow_restart": True,
    "allow_sleep": True,
    "allow_cancel": True,
    "advertised_name": ADVERTISED_NAME,
    "log_level": "INFO",
}

log = logging.getLogger("pc-shutdown")


# ---------------------------------------------------------------------------
# 配置
# ---------------------------------------------------------------------------

def _as_bool(value: Any, default: bool) -> bool:
    if isinstance(value, bool):
        return value
    if isinstance(value, (int, float)) and not isinstance(value, bool):
        return bool(value)
    if isinstance(value, str):
        text = value.strip().lower()
        if text in ("true", "1", "yes", "on"):
            return True
        if text in ("false", "0", "no", "off"):
            return False
    return default


def _as_int(value: Any, default: int, low: int, high: int) -> int:
    try:
        number = int(str(value).strip())
    except (TypeError, ValueError):
        return default
    return max(low, min(number, high))


def load_config() -> Dict[str, Any]:
    cfg = dict(DEFAULT_CONFIG)
    if CONFIG_PATH.exists():
        try:
            raw = json.loads(CONFIG_PATH.read_text(encoding="utf-8"))
            if isinstance(raw, dict):
                cfg.update({k: v for k, v in raw.items() if k in DEFAULT_CONFIG})
            else:
                log.warning("config.json 顶层不是对象，已忽略，使用默认配置")
        except Exception as exc:                      # noqa: BLE001
            log.warning("config.json 解析失败(%s)，使用默认配置", exc)

    # 类型和范围都收一遍：JSON 里写 "false" 不会变成 False，
    # 写 "abc" 当延时会直接把服务炸在启动阶段。
    cfg["shutdown_delay_seconds"] = _as_int(
        cfg.get("shutdown_delay_seconds"), DEFAULT_CONFIG["shutdown_delay_seconds"], 0, 600)
    for key in ("allow_restart", "allow_sleep", "allow_cancel"):
        cfg[key] = _as_bool(cfg.get(key), bool(DEFAULT_CONFIG[key]))
    if not isinstance(cfg.get("advertised_name"), str) or not cfg["advertised_name"].strip():
        cfg["advertised_name"] = ADVERTISED_NAME
    return cfg


def resolve_pin(cfg: Dict[str, Any]) -> str:
    configured = cfg.get("pin")
    if configured is None or str(configured).strip() == "":
        return f"{secrets.randbelow(1_000_000):06d}"
    text = str(configured).strip()
    if not text.isdigit() or not (4 <= len(text) <= 12):
        log.warning("config.json 里的 pin 必须是 4-12 位数字，已改为随机 PIN")
        return f"{secrets.randbelow(1_000_000):06d}"
    return text


# ---------------------------------------------------------------------------
# 电源动作
# ---------------------------------------------------------------------------

class PowerActions:
    """把命令翻译成 Windows 命令并执行。dry_run=True 时只打印。"""

    def __init__(self, cfg: Dict[str, Any], dry_run: bool = False) -> None:
        self.cfg = cfg
        self.dry_run = dry_run
        self.pending_cancel: Optional[Callable[[], None]] = None
        self._pending_lock = threading.Lock()

    # -- 内部工具 ---------------------------------------------------------

    @staticmethod
    def _creation_flags() -> int:
        return getattr(subprocess, "CREATE_NO_WINDOW", 0)

    def _run(self, argv: List[str], what: str) -> str:
        if self.dry_run:
            log.info("[dry-run] 本应执行：%s", " ".join(argv))
            return f"{what}: 演练模式未真正执行"
        try:
            proc = subprocess.run(
                argv,
                capture_output=True,
                timeout=20,
                creationflags=self._creation_flags(),
            )
        except FileNotFoundError:
            return f"错误：找不到命令 {argv[0]}"
        except subprocess.TimeoutExpired:
            return f"错误：{what} 执行超时"

        encoding = locale.getpreferredencoding(False) or "utf-8"
        out = (proc.stdout or b"").decode(encoding, errors="replace").strip()
        err = (proc.stderr or b"").decode(encoding, errors="replace").strip()
        if proc.returncode == 0:
            return f"{what}: 已下达"
        detail = err or out or f"退出码 {proc.returncode}"
        if proc.returncode == 1190:
            detail = "已有待执行的关机计划"
        elif proc.returncode == 5:
            detail = "被系统拒绝（可能缺少关机权限）"
        return f"错误：{what} 失败 - {detail}"

    def _schedule_cancel(self, seconds: int) -> None:
        """到点后清掉待执行标记（真正的关机由 Windows 自己完成）。"""
        def worker() -> None:
            time.sleep(seconds + 1)
            with self._pending_lock:
                self.pending_cancel = None
        t = threading.Thread(target=worker, name="cancel-window", daemon=True)
        t.start()

    # -- 供协议调用的动作 -------------------------------------------------

    def shutdown(self) -> str:
        delay = int(self.cfg.get("shutdown_delay_seconds", 10) or 0)
        delay = max(0, min(delay, 600))
        if delay == 0:
            return self._run(["shutdown", "/s", "/t", "0"], "关机")
        result = self._run(["shutdown", "/s", "/t", str(delay)], "关机")
        if result.endswith("已下达"):
            with self._pending_lock:
                self.pending_cancel = lambda: None
            self._schedule_cancel(delay)
            return f"关机: {delay} 秒后执行（可取消）"
        return result

    def restart(self) -> str:
        if not self.cfg.get("allow_restart", True):
            return "错误: 配置已禁用重启"
        delay = int(self.cfg.get("shutdown_delay_seconds", 10) or 0)
        delay = max(0, min(delay, 600))
        result = self._run(["shutdown", "/r", "/t", str(delay)], "重启")
        if result.endswith("已下达"):
            with self._pending_lock:
                self.pending_cancel = lambda: None
            self._schedule_cancel(delay)
            return f"重启: {delay} 秒后执行（可取消）"
        return result

    def cancel(self) -> str:
        if not self.cfg.get("allow_cancel", True):
            return "错误: 配置已禁用取消"
        with self._pending_lock:
            pending = self.pending_cancel is not None
        if not pending and not self.dry_run:
            # 仍然尝试一次，兼容用户手动执行过 shutdown 的情况
            return self._run(["shutdown", "/a"], "取消关机")
        result = self._run(["shutdown", "/a"], "取消关机")
        with self._pending_lock:
            self.pending_cancel = None
        return result

    def sleep(self) -> str:
        if not self.cfg.get("allow_sleep", True):
            return "错误: 配置已禁用睡眠"
        if self.dry_run:
            log.info("[dry-run] 本应让电脑进入睡眠")
            return "睡眠: 演练模式未真正执行"
        # 挂起需要系统支持；用 rundll32 调 SetSuspendState
        return self._run(
            ["rundll32.exe", "powrprof.dll,SetSuspendState", "0,1,0"],
            "睡眠",
        )

    def state(self) -> str:
        """
        只报本地记录的待取消窗口，**绝不**用 `shutdown /a` 去探测。

        早期版本用 `shutdown /a` 的返回码判断"有没有待执行的关机"，这等于
        查询一次就取消一次：用户在倒计时里点「查询状态」会把刚下达的关机撤掉，
        `--self-test` 也会误伤系统里已有的关机计划。
        """
        with self._pending_lock:
            local_pending = self.pending_cancel is not None
        return (
            f"pending={'yes' if local_pending else 'no'} "
            f"local_window={'yes' if local_pending else 'no'} "
            f"dry_run={'yes' if self.dry_run else 'no'}"
        )


# ---------------------------------------------------------------------------
# WinRT 装配（延迟导入，便于 --self-test 给出清晰提示）
# ---------------------------------------------------------------------------

class WinRT:
    """集中导入并暴露所需的 WinRT 类型，缺失时给出可操作的提示。"""

    def __init__(self) -> None:
        self.available = False
        self.error: Optional[str] = None
        self._load()

    def _load(self) -> None:
        try:
            from winrt.windows.devices.bluetooth import BluetoothAdapter, BluetoothError
            from winrt.windows.devices.bluetooth.advertisement import (
                BluetoothLEAdvertisementDataSection,
                BluetoothLEAdvertisementPublisher,
                BluetoothLEAdvertisementPublisherStatus,
            )
            from winrt.windows.devices.bluetooth.genericattributeprofile import (
                GattCharacteristicProperties,
                GattLocalCharacteristicParameters,
                GattProtectionLevel,
                GattServiceProvider,
                GattServiceProviderAdvertisingParameters,
                GattSubscribedClient,
                GattWriteOption,
            )
            from winrt.windows.foundation import Deferral
            from winrt.windows.storage.streams import DataReader, DataWriter

            self.BluetoothAdapter = BluetoothAdapter
            self.BluetoothError = BluetoothError
            self.BluetoothLEAdvertisementDataSection = BluetoothLEAdvertisementDataSection
            self.BluetoothLEAdvertisementPublisher = BluetoothLEAdvertisementPublisher
            self.BluetoothLEAdvertisementPublisherStatus = BluetoothLEAdvertisementPublisherStatus
            self.GattCharacteristicProperties = GattCharacteristicProperties
            self.GattLocalCharacteristicParameters = GattLocalCharacteristicParameters
            self.GattProtectionLevel = GattProtectionLevel
            self.GattServiceProvider = GattServiceProvider
            self.GattServiceProviderAdvertisingParameters = GattServiceProviderAdvertisingParameters
            self.GattSubscribedClient = GattSubscribedClient
            self.GattWriteOption = GattWriteOption
            self.Deferral = Deferral
            self.DataReader = DataReader
            self.DataWriter = DataWriter
            self.available = True
        except ImportError as exc:
            self.error = (
                f"缺少 WinRT 组件：{exc}\n"
                "请先安装依赖：\n"
                "  python -m pip install -i https://pypi.tuna.tsinghua.edu.cn/simple "
                "winrt-runtime winrt-Windows.Foundation winrt-Windows.Foundation.Collections "
                "winrt-Windows.Storage.Streams winrt-Windows.Devices.Bluetooth "
                "winrt-Windows.Devices.Bluetooth.GenericAttributeProfile "
                "winrt-Windows.Devices.Bluetooth.Advertisement"
            )
        except Exception as exc:                        # noqa: BLE001
            self.error = f"加载 WinRT 组件失败：{exc!r}"


def buffer_to_bytes(buffer: Any) -> bytes:
    """把 WinRT IBuffer 转成 Python bytes。"""
    if buffer is None:
        return b""
    from winrt.windows.storage.streams import DataReader  # noqa: PLC0415

    reader = DataReader.from_buffer(buffer)
    size = int(reader.unconsumed_buffer_length)
    if size <= 0:
        return b""
    return bytes(reader.read_buffer(size))


def bytes_to_buffer(payload: bytes) -> Any:
    """把 Python bytes 转成 WinRT IBuffer。"""
    from winrt.windows.storage.streams import DataWriter  # noqa: PLC0415

    writer = DataWriter()
    writer.write_bytes(payload)
    return writer.detach_buffer()


# ---------------------------------------------------------------------------
# 服务主体
# ---------------------------------------------------------------------------

@dataclass
class Session:
    """一个与手机相关的会话。

    Windows 上不一定能拿到客户端句柄，所以 session 可能是匿名兜底，
    见 `_handle_command` 里的认证判定。
    """
    client: Any
    device_id: Optional[str] = None
    authenticated: bool = False
    failures: int = 0
    locked_until: float = 0.0
    nonce: str = ""

    def label(self) -> str:
        if self.device_id:
            return f"手机[{self.device_id[-6:]}]"
        return "手机[未知]"


class ShutdownService:
    def __init__(self, cfg: Dict[str, Any], pin: str, dry_run: bool = False) -> None:
        self.cfg = cfg
        self.pin = pin
        self.dry_run = dry_run
        self.actions = PowerActions(cfg, dry_run=dry_run)

        self.loop: Optional[asyncio.AbstractEventLoop] = None
        self.rt = WinRT()
        self.provider: Any = None
        self.rx_char: Any = None
        self.tx_char: Any = None
        self.publisher: Any = None

        self.sessions: List[Session] = []
        self._sessions_lock = threading.Lock()
        self.stop_event: Optional[asyncio.Event] = None
        self.local_ident: dict = {}

        # 全局认证状态：会话识别不出来时的兜底（见 _do_auth 说明）
        self.authenticated: bool = False
        self.auth_failures: int = 0
        self._anonymous_warned: bool = False
        self.auth_locked_until: float = 0.0
        self.anon_nonce: str = ""
        # 动作串行化：所有关机类命令排队执行，避免两条命令交错
        self._action_lock: Optional[asyncio.Lock] = None

    # ------------------------------------------------------------ 生命周期

    async def start(self) -> None:
        self.loop = asyncio.get_running_loop()
        self.stop_event = asyncio.Event()
        self._action_lock = asyncio.Lock()

        rt = self.rt
        if not rt.available:
            raise RuntimeError(rt.error or "WinRT 不可用")

        await self._create_service()
        await self._start_advertising()

    async def _create_service(self) -> None:
        rt = self.rt
        result = await rt.GattServiceProvider.create_async(uuid.UUID(SERVICE_UUID))
        if result.error != rt.BluetoothError.SUCCESS:
            raise RuntimeError(f"创建 GATT 服务失败：{result.error}")
        self.provider = result.service_provider
        service = self.provider.service
        log.info("GATT 服务已创建：%s", SERVICE_UUID)

        # 手机 -> 电脑：可写
        rx_params = rt.GattLocalCharacteristicParameters()
        rx_params.characteristic_properties = (
            rt.GattCharacteristicProperties.WRITE
            | rt.GattCharacteristicProperties.WRITE_WITHOUT_RESPONSE
        )
        rx_params.write_protection_level = rt.GattProtectionLevel.PLAIN
        rx_params.user_description = "phone to pc"
        rx_result = await service.create_characteristic_async(uuid.UUID(RX_UUID), rx_params)
        if rx_result.error != rt.BluetoothError.SUCCESS:
            raise RuntimeError(f"创建接收特征失败：{rx_result.error}")
        self.rx_char = rx_result.characteristic
        self.rx_char.add_write_requested(self._on_write_requested)

        # 电脑 -> 手机：通知
        tx_params = rt.GattLocalCharacteristicParameters()
        tx_params.characteristic_properties = rt.GattCharacteristicProperties.NOTIFY
        tx_params.read_protection_level = rt.GattProtectionLevel.PLAIN
        tx_params.user_description = "pc to phone"
        tx_result = await service.create_characteristic_async(uuid.UUID(TX_UUID), tx_params)
        if tx_result.error != rt.BluetoothError.SUCCESS:
            raise RuntimeError(f"创建通知特征失败：{tx_result.error}")
        self.tx_char = tx_result.characteristic
        self.tx_char.add_subscribed_clients_changed(self._on_subscribed_changed)

        log.info("特征就绪：RX=%s  TX=%s", RX_UUID, TX_UUID)

    async def _start_advertising(self) -> None:
        rt = self.rt
        adv = rt.GattServiceProviderAdvertisingParameters()
        adv.is_discoverable = True
        adv.is_connectable = True
        self.provider.start_advertising_with_parameters(adv)
        log.info("GATT 服务已开始广播")

        self.local_ident = await self._read_local_adapter()
        self._start_name_publisher()

    async def _read_local_adapter(self) -> dict:
        """读出本机蓝牙名与地址，方便用户在手机扫描列表里认出这台电脑。"""
        info: dict = {}
        try:
            adapter = await self.rt.BluetoothAdapter.get_default_async()
            if adapter is not None:
                info["address"] = f"{int(adapter.bluetooth_address):012X}"
                info["peripheral"] = bool(adapter.is_peripheral_role_supported)
                log.info("本机蓝牙地址：%s（支持外设角色：%s）",
                         info["address"], info["peripheral"])
        except Exception as exc:                        # noqa: BLE001
            log.debug("读取蓝牙适配器信息失败：%r", exc)
        return info

    def _start_name_publisher(self) -> None:
        """
        尝试让手机在扫描结果里看到 "PC-Shutdown" 这个名字。

        实测（Windows 11 build 26200 + WinRT 3.2.1）：
          - 设 advertisement.local_name  → publisher.start() 抛 参数错误
          - 加 advertisement.service_uuids → 同样抛 参数错误
          - 只有 data_sections 能被接受
        所以这里用自定义数据段承载一个标识，广播内容由手机端解析识别；
        拿不到漂亮名字也不影响功能，手机端也有兜底（列出全部设备任选）。
        """
        try:
            publisher = self.rt.BluetoothLEAdvertisementPublisher()
            section = self.rt.BluetoothLEAdvertisementDataSection()
            section.data_type = 0xFF
            section.data = bytes_to_buffer(ADVERT_MARKER)
            publisher.advertisement.data_sections.append(section)
            # 盯住广播状态：如果被系统或驱动中止，必须立刻可见，
            # 否则"手机扫不到"会变成无头悬案。
            publisher.add_status_changed(self._on_publisher_status_changed)
            publisher.start()
            self.publisher = publisher
            log.info("广播中已加入自定义标识段（%s）", ADVERT_MARKER.hex().upper())
        except Exception as exc:                        # noqa: BLE001
            self.publisher = None
            log.info(
                "未能附加自定义广播标识（%s）。不影响使用："
                "手机端会列出附近的蓝牙设备，选中这台电脑即可。",
                exc,
            )

    def _on_publisher_status_changed(self, sender: Any, args: Any) -> None:
        try:
            status = int(sender.status)
            error = sender.error
        except Exception:                               # noqa: BLE001
            return
        # 0=Created 1=Started 2=Stopping 3=Stopped 4=Aborted
        if status == 4:
            log.error(
                "广播被中止了（error=%s）！手机将搜不到本机。"
                "常见原因：蓝牙适配器不支持外设模式、或蓝牙被关闭。",
                error,
            )
        else:
            log.debug("广播状态变化：status=%s error=%s", status, error)

    def request_stop(self) -> None:
        if self.loop is not None and self.stop_event is not None:
            self.loop.call_soon_threadsafe(self.stop_event.set)

    async def shutdown(self) -> None:
        try:
            if self.publisher is not None:
                self.publisher.stop()
        except Exception:                               # noqa: BLE001
            pass
        try:
            if self.provider is not None:
                self.provider.stop_advertising()
        except Exception:                               # noqa: BLE001
            pass

    # ------------------------------------------------------------ 事件桥接

    def _dispatch(self, coro) -> None:
        """把 WinRT 线程上的回调安全地丢回 asyncio 事件循环。"""
        loop = self.loop
        if loop is None or loop.is_closed():
            return
        try:
            future = asyncio.run_coroutine_threadsafe(coro, loop)
        except RuntimeError:
            return
        future.add_done_callback(self._log_future_error)

    @staticmethod
    def _log_future_error(future: Any) -> None:
        """协程里抛的异常以前会被静默吞掉，命令就"消失"了，这里至少留痕。"""
        try:
            future.result()
        except Exception:                               # noqa: BLE001
            log.exception("异步任务执行失败")

    def _on_write_requested(self, sender: Any, args: Any) -> None:
        # WinRT 要求在处理完写入前持有 Deferral，否则响应窗口一过请求就会被提交，
        # 后面再 respond() 只会抛"对象已提交"。拿不到 Deferral 也不致命（旧实现就是这么跑的）。
        deferral = None
        try:
            deferral = args.get_deferral()
        except Exception:                               # noqa: BLE001
            deferral = None
        self._dispatch(self._handle_write(args, deferral))

    def _on_subscribed_changed(self, sender: Any, args: Any) -> None:
        log.info("订阅事件触发：args 类型=%s", type(args).__name__)
        self._dispatch(self._handle_subscription(args))

    async def _handle_subscription(self, args: Any) -> None:
        # 该事件的参数在不同 SDK 版本里可能是 GattSubscribedClient 本身，
        # 也可能包一层；两种形态都兼容。
        client = args
        for attr in ("subscribed_client", "SubscribedClient"):
            inner = getattr(args, attr, None)
            if inner is not None:
                client = inner
                break
        if client is None:
            log.warning("订阅事件里拿不到 client，args=%r", args)
            return

        with self._sessions_lock:
            session = next((s for s in self.sessions if _same_client(s.client, client)), None)
            if session is None:
                session = Session(client=client)
                session.device_id = _device_key(client)
                self.sessions.append(session)
                log.info("客户端已订阅通知：%s（当前 %d 个会话）",
                         session.label(), len(self.sessions))

        await self._send_line(self._hello_line(session), session)

    async def _handle_write(self, args: Any, deferral: Any = None) -> None:
        try:
            await self._handle_write_inner(args)
        except Exception:                               # noqa: BLE001
            log.exception("处理写入请求时出现未捕获异常")
        finally:
            if deferral is not None:
                try:
                    deferral.complete()
                except Exception:                       # noqa: BLE001
                    pass

    async def _handle_write_inner(self, args: Any) -> None:
        request = None
        try:
            request = await args.get_request_async()
        except Exception as exc:                        # noqa: BLE001
            log.warning("读取写入请求失败：%r", exc)
            return
        if request is None:
            return

        text = ""
        try:
            payload = buffer_to_bytes(request.value)
            text = payload.decode("utf-8", errors="replace").strip()
        except Exception as exc:                        # noqa: BLE001
            log.warning("解码写入内容失败：%r", exc)

        # 每个请求只能应答一次。WriteWithoutResponse 类型的请求不需要（也不能）
        # 应答，直接调用会抛 "已提交对象"，所以失败就静默降级。
        try:
            request.respond()
        except Exception as exc:                        # noqa: BLE001
            log.debug("请求无需应答或已应答：%r", exc)

        if not text:
            return

        # WinRT 的 GattWriteRequestedEventArgs 上才有 session；GattWriteRequest 没有。
        # 两个都试，拿到客户端才能做"按连接"的认证。
        session = self._session_for(args, request)

        lines = [chunk.strip() for chunk in text.splitlines() if chunk.strip()]
        if len(lines) > MAX_LINES_PER_WRITE:
            # 一次写入塞几十条 AUTH 会绕过"连错 5 次"的节奏
            log.warning("单次写入 %d 条命令，只处理前 %d 条", len(lines), MAX_LINES_PER_WRITE)
            lines = lines[:MAX_LINES_PER_WRITE]

        for line in lines:
            try:
                await self._handle_command(line, session)
            except Exception:                           # noqa: BLE001
                log.exception("命令处理失败：%r", line)
                await self._send_line("ERR INTERNAL\n", session)

    def _extract_client(self, source: Any) -> Any:
        if source is None:
            return None
        for attr in ("session", "Session", "subscribed_client", "SubscribedClient"):
            try:
                value = getattr(source, attr, None)
            except Exception:                           # noqa: BLE001
                value = None
            if value is not None:
                return value
        return None

    def _session_for(self, args: Any, request: Any) -> Optional[Session]:
        """
        由一次写入事件反查（必要时新建）会话。

        优先看事件参数本身：WinRT 把来源客户端放在 GattWriteRequestedEventArgs.Session，
        而不是 GattWriteRequest 上。以前的实现只查后者，导致永远拿不到客户端、
        退化成一个全局认证开关。
        """
        client = self._extract_client(args) or self._extract_client(request)
        if client is None:
            log.debug("写入请求不含来源客户端，改用全局认证状态判定")
            return None

        with self._sessions_lock:
            session = next((s for s in self.sessions if _same_client(s.client, client)), None)
            if session is None:
                session = Session(client=client)
                session.device_id = _device_key(client)
                self.sessions.append(session)
                log.info("由写入请求建立会话：%s（当前 %d 个会话）",
                         session.label(), len(self.sessions))
            return session

    # ------------------------------------------------------------ 协议

    async def _send_line(self, line: str, session: Optional[Session] = None) -> None:
        """
        把一行应答发给手机。

        不再预检"有没有订阅者"：实测 Windows 11 + WinRT 3.2.1 上
        subscribed_clients / subscribed_clients_changed 都拿不到客户端，
        预检会把应答静默丢掉（手机就永远等不到 OK AUTH）。
        直接发，失败才记日志。
        """
        payload = line.encode("utf-8")[:RX_BUFFER_LIMIT]
        try:
            await self.tx_char.notify_value_async(bytes_to_buffer(payload))
            log.debug("已发送 %r", line.strip())
        except Exception as exc:                        # noqa: BLE001
            log.warning("通知发送失败：%r（手机可能已断开）", exc)

    async def _handle_command(self, line: str, session: Optional[Session]) -> None:
        parts = line.split()
        if not parts:
            return
        cmd = parts[0].upper()
        arg = parts[1] if len(parts) > 1 else ""
        who = session.label() if session else "未知来源"
        log.info("收到命令：%s %s（来自 %s）", cmd, "*" * len(arg) if cmd == "AUTH" else arg, who)

        if cmd == "HELLO":
            await self._send_line(self._hello_line(session), session)
            return

        if cmd == "AUTH":
            await self._do_auth(arg, session)
            return

        if cmd == "AUTH2":
            await self._do_auth_hmac(arg, session)
            return

        # 认证判定：
        # - 能识别出会话：只认这个会话自己的认证状态，不能借用别人的授权
        # - 识别不出会话（Windows 上有时候拿不到来源 client）：
        #   退回全局状态兜底，首次放行时打一条警告，说明当前处于降级模式
        if session is not None:
            authenticated = session.authenticated
        else:
            authenticated = self.authenticated
            if authenticated and not self._anonymous_warned:
                self._anonymous_warned = True
                log.warning("无法识别写入来源，改用全局认证状态放行（降级模式）")
        if not authenticated:
            await self._send_line("ERR NEED_AUTH\n", session)
            return

        if cmd == "PING":
            await self._send_line("PONG\n", session)

        elif cmd == "QUERY":
            await self._send_line(f"STATE {self.actions.state()}\n", session)

        elif cmd == "SHUTDOWN":
            await self._reply_result(await self._run_action(self.actions.shutdown), session)

        elif cmd == "RESTART":
            await self._reply_result(await self._run_action(self.actions.restart), session)

        elif cmd == "SLEEP":
            if not self.cfg.get("allow_sleep", True):
                await self._send_line("ERR 错误: 配置已禁用睡眠\n", session)
            else:
                # 先回包：真正进入睡眠后 BLE 就断了，等执行完再回会永远收不到
                await self._send_line("OK 睡眠: 即将进入睡眠\n", session)
                await asyncio.sleep(0.2)
                result = await self._run_action(self.actions.sleep)
                log.info("睡眠结果：%s", result)

        elif cmd == "CANCEL":
            await self._reply_result(await self._run_action(self.actions.cancel), session)

        else:
            await self._send_line(f"ERR UNKNOWN_CMD {cmd}\n", session)

    async def _reply_result(self, result: str, session: Optional[Session]) -> None:
        """成功回 OK，失败回 ERR —— 不能把「错误：...」包装成 OK 发出去。"""
        prefix = "ERR" if result.startswith("错误") else "OK"
        await self._send_line(f"{prefix} {result}\n", session)

    async def _run_action(self, func: Callable[[], str]) -> str:
        """关机类命令会拉起子进程，丢到线程里跑，别卡住 BLE 事件循环。"""
        if self._action_lock is not None:
            async with self._action_lock:
                return await asyncio.to_thread(func)
        return await asyncio.to_thread(func)

    async def _do_auth(self, arg: str, session: Optional[Session]) -> None:
        """
        兼容协议 v1 的明文 PIN 认证。

        只在旧版 App 上出现；建议升级到 v1.2.0+（走 AUTH2 的 HMAC 应答），
        PIN 就不会再出现在蓝牙链路上。
        """
        if await self._auth_rejected(session):
            return
        if hmac.compare_digest(arg.encode("utf-8"), self.pin.encode("utf-8")):
            log.warning("%s 使用旧版明文 PIN 认证（协议 v1），建议升级 App",
                        self._who(session))
            await self._auth_grant(session)
        else:
            await self._auth_deny(session)

    async def _do_auth_hmac(self, arg: str, session: Optional[Session]) -> None:
        """
        协议 v2：手机回 HMAC-SHA256(PIN, nonce)，PIN 本身不上链路。

        nonce 存在时要求 AUTH2；旧 App 发 AUTH 仍可用，但会记警告。
        """
        if await self._auth_rejected(session):
            return
        nonce = self._nonce_for(session, create=False)
        if nonce:
            expected = hmac.new(
                self.pin.encode("utf-8"), nonce.encode("utf-8"), hashlib.sha256
            ).hexdigest()
            if hmac.compare_digest(arg.strip().lower(), expected):
                await self._auth_grant(session)
                return
        await self._auth_deny(session)

    @staticmethod
    def _who(session: Optional[Session]) -> str:
        return session.label() if session else "未知来源"

    def _nonce_for(self, session: Optional[Session], create: bool = True) -> str:
        if session is not None:
            if not session.nonce and create:
                session.nonce = secrets.token_hex(16)
            return session.nonce
        if not self.anon_nonce and create:
            self.anon_nonce = secrets.token_hex(16)
        return self.anon_nonce

    def _hello_line(self, session: Optional[Session]) -> str:
        return f"HELLO {PROTOCOL_VERSION} AUTH {self._nonce_for(session)}\n"

    async def _auth_rejected(self, session: Optional[Session]) -> bool:
        now = time.monotonic()
        locked = session.locked_until if session is not None else self.auth_locked_until
        if locked > now:
            await self._send_line(
                f"ERR AUTH 已锁定，{int(locked - now) + 1} 秒后再试\n", session)
            return True
        return False

    async def _auth_grant(self, session: Optional[Session]) -> None:
        with self._sessions_lock:
            if session is not None:
                for other in self.sessions:
                    if other is not session:
                        other.authenticated = False
                session.authenticated = True
                session.failures = 0
                session.locked_until = 0.0
                session.nonce = ""
        self.authenticated = True
        self.auth_failures = 0
        self.auth_locked_until = 0.0
        self.anon_nonce = ""
        log.info("%s 认证成功，已授权控制", self._who(session))
        await self._send_line("OK AUTH\n", session)

    async def _auth_deny(self, session: Optional[Session]) -> None:
        now = time.monotonic()
        who = self._who(session)
        if session is not None:
            session.failures += 1
            if session.failures >= MAX_AUTH_FAILURES:
                session.failures = 0
                session.authenticated = False
                session.locked_until = now + AUTH_LOCKOUT_SECONDS
                session.nonce = ""      # 换 nonce：锁定期间的旧应答不能复用
                log.warning("%s 连续失败 %d 次，锁定 %d 秒",
                            who, MAX_AUTH_FAILURES, AUTH_LOCKOUT_SECONDS)
            else:
                log.warning("%s 认证失败（第 %d 次）", who, session.failures)
        else:
            self.auth_failures += 1
            if self.auth_failures >= MAX_AUTH_FAILURES:
                self.auth_failures = 0
                self.authenticated = False
                self.auth_locked_until = now + AUTH_LOCKOUT_SECONDS
                self.anon_nonce = ""
                log.warning("匿名来源连续失败 %d 次，锁定 %d 秒",
                            MAX_AUTH_FAILURES, AUTH_LOCKOUT_SECONDS)
            else:
                log.warning("%s 认证失败（第 %d 次）", who, self.auth_failures)

        # 不回具体第几次，减少给爆破者的计数 oracle
        await self._send_line("ERR AUTH 失败\n", session)


def _device_key(client: Any) -> Optional[str]:
    """
    取出蓝牙设备标识（BluetoothDeviceId 转字符串）。

    WinRT 里两种形态都可能是"客户端"：
      - GattSubscribedClient：.session.device_id
      - GattSession：.device_id
    """
    candidates = []
    try:
        candidates.append(getattr(client, "session", None))
    except Exception:                                   # noqa: BLE001
        pass
    candidates.append(client)

    for candidate in candidates:
        if candidate is None:
            continue
        try:
            device_id = candidate.device_id
        except Exception:                               # noqa: BLE001
            continue
        if device_id is None:
            continue
        raw = getattr(device_id, "id", None)
        return str(raw) if raw is not None else str(device_id)
    return None


def _same_client(a: Any, b: Any) -> bool:
    if a is b:
        return True
    ka, kb = _device_key(a), _device_key(b)
    return ka is not None and ka == kb


# ---------------------------------------------------------------------------
# 自检
# ---------------------------------------------------------------------------

def run_self_test(cfg: Dict[str, Any]) -> int:
    print("=" * 68)
    print("PC-Shutdown 自检")
    print("=" * 68)
    ok = True

    print(f"Python      : {sys.version.split()[0]}  ({sys.executable})")
    print(f"平台        : {sys.platform}")
    if sys.platform != "win32":
        print("  ✗ 本服务只能在 Windows 上运行")
        return 2
    if sys.getwindowsversion().build < 17763:
        print("  ! Windows 版本过低（需要 10 1809 / build 17763 及以上）")

    rt = WinRT()
    if rt.available:
        print("WinRT 组件  : ✓ 全部可用")
    else:
        ok = False
        print("WinRT 组件  : ✗ 不可用")
        print(rt.error)

    if rt.available:
        checks = [
            ("GattServiceProvider.create_async", hasattr(rt.GattServiceProvider, "create_async")),
            ("GattLocalCharacteristicParameters.user_description",
             hasattr(rt.GattLocalCharacteristicParameters(), "user_description")),
            ("GattLocalCharacteristicParameters.write_protection_level",
             hasattr(rt.GattLocalCharacteristicParameters(), "write_protection_level")),
            ("GattServiceProviderAdvertisingParameters.is_discoverable",
             hasattr(rt.GattServiceProviderAdvertisingParameters(), "is_discoverable")),
            ("BluetoothLEAdvertisementPublisher", rt.BluetoothLEAdvertisementPublisher is not None),
        ]
        for name, good in checks:
            print(f"  {'✓' if good else '✗'} {name}")
            ok = ok and good

        try:
            params = rt.GattLocalCharacteristicParameters()
            params.user_description = "probe"
            print("  ✓ 特征参数可写（user_description）")
        except Exception as exc:                        # noqa: BLE001
            ok = False
            print(f"  ✗ 特征参数不可写：{exc!r}")

        try:
            adv = rt.BluetoothLEAdvertisementPublisher()
            adv.advertisement.local_name = ADVERTISED_NAME
            print(f"  ✓ 可设置广播名 {ADVERTISED_NAME}")
        except Exception as exc:                        # noqa: BLE001
            print(f"  ! 无法设置广播名：{exc!r}（运行时将退回电脑原始蓝牙名）")

    # 关机命令是否可用（只读探测，不真正关机）
    probe = PowerActions(cfg, dry_run=True)
    print(f"演练关机    : {probe.shutdown()}")
    print(f"演练重启    : {probe.restart()}")
    print(f"演练取消    : {probe.cancel()}")
    print(f"状态查询    : {probe.state()}")

    host = socket.gethostname()
    print(f"主机名      : {host}")
    print(f"配置        : {CONFIG_PATH if CONFIG_PATH.exists() else '（未创建，将使用默认值）'}")
    print("=" * 68)
    print("自检结果    : " + ("通过 ✓" if ok else "存在问题 ✗"))
    print("=" * 68)
    return 0 if ok else 1


def list_api() -> int:
    rt = WinRT()
    if not rt.available:
        print(rt.error)
        return 1
    print("GattServiceProvider 成员：")
    for name in sorted(dir(rt.GattServiceProvider)):
        if not name.startswith("_"):
            print("   ", name)
    print("\nBluetoothLEAdvertisementPublisher 成员：")
    for name in sorted(dir(rt.BluetoothLEAdvertisementPublisher)):
        if not name.startswith("_"):
            print("   ", name)
    print("\nGattLocalCharacteristicParameters 成员：")
    for name in sorted(dir(rt.GattLocalCharacteristicParameters())):
        if not name.startswith("_"):
            print("   ", name)
    return 0


# ---------------------------------------------------------------------------
# 入口
# ---------------------------------------------------------------------------

BANNER = r"""
  ____   ____    ____  _                     _     _
 |  _ \ / ___|  / ___|| |__  _   _ _ __   __| | __| | ___ _ __
 | |_) | |     \___ \| '_ \| | | | '_ \ / _` |/ _` |/ _ \ '_ \
 |  __/| |___   ___) | | | | |_| | | | | (_| | (_| |  __/ | | |
 |_|    \____| |____/|_| |_|\__,_|_| |_|\__,_|\__,_|\___|_| |_|
"""


def setup_console_encoding() -> None:
    """Windows 控制台默认 GBK，中文日志容易花屏，统一切到 UTF-8。"""
    for stream_name in ("stdout", "stderr"):
        stream = getattr(sys, stream_name, None)
        reconfigure = getattr(stream, "reconfigure", None)
        if reconfigure is not None:
            try:
                reconfigure(encoding="utf-8", errors="replace")
            except Exception:                           # noqa: BLE001
                pass


def setup_logging(level: str) -> None:
    setup_console_encoding()
    numeric = getattr(logging, str(level).upper(), logging.INFO)
    log.setLevel(numeric)
    fmt = logging.Formatter("%(asctime)s %(levelname)-7s %(message)s", "%H:%M:%S")

    stream = logging.StreamHandler(sys.stdout)
    stream.setFormatter(fmt)
    log.addHandler(stream)

    try:
        from logging.handlers import RotatingFileHandler
        file_handler = RotatingFileHandler(
            LOG_PATH, maxBytes=1_000_000, backupCount=3, encoding="utf-8")
        file_handler.setFormatter(fmt)
        log.addHandler(file_handler)
    except Exception:                                   # noqa: BLE001
        pass


async def amain(cfg: Dict[str, Any], pin: str, dry_run: bool) -> int:
    service = ShutdownService(cfg, pin, dry_run=dry_run)

    try:
        await service.start()
    except Exception as exc:                            # noqa: BLE001
        log.error("启动失败：%s", exc)
        return 1

    print()
    print(" " + "=" * 62)
    ident = service.local_ident or {}
    print(f"  蓝牙地址 : {ident.get('address', '(读取失败)')}")
    if ident.get("peripheral") is False:
        print("  ！警告   : 该适配器未报告支持外设角色，手机可能搜不到本机")
    print(f"  广播标识 : {ADVERT_MARKER.hex().upper()}  （手机 App 靠它认出本机）")
    print(f"  本次 PIN : {pin}")
    print(f"  关机延时 : {cfg.get('shutdown_delay_seconds')} 秒（可用「取消关机」撤销）")
    mode_text = "演练（不会真的关机）" if dry_run else "正常（会真的关机）"
    print(f"  模式     : {mode_text}")
    print("  手机操作 : 打开「电脑关机」App → 扫描电脑 → 选中本机 → 连接 → 输入上面的 PIN")
    print("  退出     : 按 Ctrl+C")
    print(" " + "=" * 62)
    print()

    # PIN 只显示在控制台，不写进日志文件：日志经常被贴到 Issue 里排障。
    log.info(
        "配置 地址=%s 延时=%s秒 模式=%s 广播标识=%s（PIN 见控制台）",
        ident.get("address", "unknown"),
        cfg.get("shutdown_delay_seconds"),
        "dry-run" if dry_run else "normal",
        ADVERT_MARKER.hex().upper(),
    )

    try:
        await service.stop_event.wait()
    except asyncio.CancelledError:
        pass
    finally:
        await service.shutdown()
        log.info("服务已停止")
    return 0


def main(argv: Optional[List[str]] = None) -> int:
    parser = argparse.ArgumentParser(
        description="电脑端蓝牙关机服务（手机通过 BLE 控制本机关机）"
    )
    parser.add_argument("--dry-run", action="store_true",
                        help="演练模式：只打印将要执行的命令，不真的关机/重启")
    parser.add_argument("--self-test", action="store_true",
                        help="检查依赖与 WinRT API 可用性后退出")
    parser.add_argument("--list-api", action="store_true",
                        help="打印本机 WinRT BLE API 清单后退出")
    parser.add_argument("--pin", default=None,
                        help="指定本次使用的 PIN（覆盖 config.json）")
    parser.add_argument("--log-level", default=None,
                        help="日志级别：DEBUG/INFO/WARNING")
    args = parser.parse_args(argv)

    cfg = load_config()
    if args.log_level:
        cfg["log_level"] = args.log_level
    setup_logging(cfg.get("log_level", "INFO"))

    if args.self_test:
        return run_self_test(cfg)
    if args.list_api:
        return list_api()

    pin = args.pin.strip() if args.pin else resolve_pin(cfg)
    if not pin.isdigit() or not (4 <= len(pin) <= 12):
        log.error("PIN 必须是 4-12 位数字")
        return 1

    print(BANNER)
    try:
        return asyncio.run(amain(cfg, pin, dry_run=args.dry_run))
    except KeyboardInterrupt:
        print()
        log.info("已手动停止")
        return 0


if __name__ == "__main__":
    sys.exit(main())
