# SPDX-License-Identifier: GPL-3.0-or-later
"""控制中心扩展与主程序之间的 App Group 信箱（宿主一侧）。

扩展运行在沙盒里、没有米家凭据，所有设备操作都转给已登录的主程序做。
两边只靠组容器里的 MiHomeControls 目录（权限 0700）通信：

- host.lock：主程序运行期间一直持有它的排他 flock。扩展据此立刻分辨
  「MiHome 没运行」和「MiHome 正在处理」，不用干等超时。
- <UUID>.request：扩展原子写入的请求
  {"operation": "list"|"state"|"set", "device"?, "isOn"?, "expires"}
- <UUID>.reply：主程序原子写入的应答
  {"ok": true, ...} 或 {"ok": false, "error": "给用户看的原因"}

控件不会自己刷新。主程序在三种情况下请系统重载控件（系统随后向扩展要
最新状态）：登录就绪时、控件关心的设备开关状态和上次告诉扩展的不一致时
（在别处被开关、或上次读取失败后恢复）、主程序退出时（控件改显示「未运行」）。
"""

import base64
import ctypes
import fcntl
import json
import logging
import os
import plistlib
import re
import select
import sys
import time
from pathlib import Path

from PySide6.QtCore import QByteArray, QBuffer, QIODevice, QObject, QSize, QSocketNotifier, Qt, QTimer
from PySide6.QtGui import QImageReader

from app.core import icon_store

from app.core.control_center import DeviceControls
from app.core.jobs import PRIORITY_CONTROL
from app.core.service import ServiceError

logger = logging.getLogger(__name__)

_REQUEST = re.compile(r"[0-9A-Fa-f-]{36}\.request")
_GROUP = re.compile(r"[A-Za-z0-9.-]+")
# 目录变化（kqueue）是主通道；定时轮询只兜底事件丢失的情况。
# QFileSystemWatcher 实测会攒批，新请求要等 0.05~0.55 秒才被发现，kqueue 是即时的
_FALLBACK_POLL_MS = 1000
_O_EVTONLY = getattr(os, "O_EVTONLY", 0x8000)
# 一轮轮询会连续更新多台设备，合并成一次重载
_RELOAD_DEBOUNCE_MS = 400
_RELOADER = "libMiHomeControlBridge.dylib"
_NOT_READY = "MiHome 正在登录，请稍后再试。"


class ControlCenterBridge(QObject):
    @classmethod
    def attach(cls, window):
        """打包版 macOS 应用且带控制中心扩展时建立信箱，否则返回 None。"""
        if not getattr(sys, "frozen", False) or sys.platform != "darwin":
            return None
        contents = Path(sys.executable).resolve().parent.parent
        try:
            group = plistlib.loads((contents / "Info.plist").read_bytes()).get(
                "MiHomeControlGroup")
        except (OSError, ValueError, plistlib.InvalidFileException):
            return None
        if not isinstance(group, str) or not _GROUP.fullmatch(group):
            return None
        from app.core import app_group

        container = app_group.container_path(group)
        if container is None:
            logger.warning("控制中心：系统没有提供 App Group 容器，控件不可用")
            return None
        try:
            bridge = cls(window, container / "MiHomeControls",
                         cls._load_reloader(contents / "Frameworks" / _RELOADER))
        except OSError as exc:
            logger.warning("控制中心：无法建立通信目录：%s", exc)
            return None
        logger.info("控制中心：通信已就绪")
        return bridge

    def __init__(self, window, directory: Path, reloader=None):
        super().__init__(window)
        directory.mkdir(mode=0o700, parents=True, exist_ok=True)
        self._directory = directory
        self._window = window
        self._controls = DeviceControls(window._service)
        self._lock_fd: int | None = self._hold_lock(directory / "host.lock")
        self._busy = False
        self._was_ready = False
        # 控件问过的设备：主窗口轮询时一并刷新，状态变了才能通知控件
        self._tracked: set[str] = set()
        # 上次告诉扩展的开关状态；None 表示上次没能给出（读取失败等）
        self._reported: dict[str, bool | None] = {}
        self._reload = reloader
        self._reload_timer = QTimer(self)
        self._reload_timer.setSingleShot(True)
        self._reload_timer.setInterval(_RELOAD_DEBOUNCE_MS)
        self._reload_timer.timeout.connect(self._reload_now)
        self._timer = QTimer(self)
        self._timer.timeout.connect(self._poll)
        self._timer.start(_FALLBACK_POLL_MS)
        self._kqueue = None
        self._dir_fd: int | None = None
        self._notifier = None
        self._watch_directory()
        window._control_bridge = self
        window.power_state_updated.connect(self._on_power_state)

    # ---- 生命周期 ----

    @staticmethod
    def _hold_lock(path: Path) -> int:
        fd = os.open(path, os.O_RDWR | os.O_CREAT, 0o600)
        try:
            fcntl.flock(fd, fcntl.LOCK_EX | fcntl.LOCK_NB)
        except OSError:
            os.close(fd)
            raise
        return fd

    def _watch_directory(self) -> None:
        """目录里增删文件（扩展原子写入请求就是一次改名）时立即唤醒。"""
        try:
            self._dir_fd = os.open(self._directory, os.O_RDONLY | _O_EVTONLY)
            self._kqueue = select.kqueue()
            self._kqueue.control([select.kevent(
                self._dir_fd, filter=select.KQ_FILTER_VNODE,
                flags=select.KQ_EV_ADD | select.KQ_EV_CLEAR,
                fflags=select.KQ_NOTE_WRITE)], 0, 0)
        except OSError as exc:
            logger.warning("控制中心：无法监听通信目录，改为每秒检查：%s", exc)
            self._close_watch()
            return
        self._notifier = QSocketNotifier(self._kqueue.fileno(),
                                         QSocketNotifier.Type.Read, self)
        self._notifier.activated.connect(self._on_directory_event)

    def _on_directory_event(self, *_args) -> None:
        try:
            self._kqueue.control(None, 16, 0)
        except (OSError, AttributeError):
            pass
        self._poll()

    def _close_watch(self) -> None:
        if self._notifier is not None:
            self._notifier.setEnabled(False)
            self._notifier = None
        if self._kqueue is not None:
            self._kqueue.close()
            self._kqueue = None
        if self._dir_fd is not None:
            os.close(self._dir_fd)
            self._dir_fd = None

    @staticmethod
    def _load_reloader(path: Path):
        """加载随应用打包的小动态库，它调用 WidgetKit 的 ControlCenter 重载控件。"""
        if not path.is_file():
            logger.warning("控制中心：缺少 %s，控件状态只会在操作后刷新", path.name)
            return None
        try:
            reload_controls = ctypes.CDLL(str(path)).mihome_reload_controls
        except (OSError, AttributeError) as exc:
            logger.warning("控制中心：无法加载控件刷新库：%s", exc)
            return None
        reload_controls.restype = None
        reload_controls.argtypes = []
        return reload_controls

    def stop(self) -> None:
        """退出时先放开 host.lock 再重载，控件随即显示「MiHome 未运行」。"""
        self._timer.stop()
        self._reload_timer.stop()
        self._close_watch()
        if self._lock_fd is not None:
            os.close(self._lock_fd)
            self._lock_fd = None
            self._reload_now()

    def tracked_dids(self) -> set[str]:
        return set(self._tracked)

    # ---- 控件刷新 ----

    def _schedule_reload(self) -> None:
        if self._reload is not None:
            self._reload_timer.start()

    def _reload_now(self) -> None:
        if self._reload is None:
            return
        try:
            self._reload()
        except Exception:
            logger.warning("控制中心：请求刷新控件失败", exc_info=True)

    def _on_power_state(self, did: str, state) -> None:
        """主窗口拿到某台设备的开关状态（轮询或本地操作）。"""
        if did in self._tracked and type(state) is bool and self._reported.get(did) != state:
            self._schedule_reload()

    def _check_ready(self) -> None:
        ready = bool(self._window._session_ready) and not self._window._quitting
        if ready and not self._was_ready:
            # 刚登录好：之前显示「正在登录」或旧状态的控件全部重新取值
            self._schedule_reload()
        self._was_ready = ready

    # ---- 请求处理 ----

    def _poll(self) -> None:
        self._check_ready()
        if self._busy or self._lock_fd is None:
            return
        try:
            request = next((path for path in self._directory.iterdir()
                            if _REQUEST.fullmatch(path.name)), None)
        except OSError:
            return
        if request is None:
            return
        message = self._take(request)
        if message is None:
            # 已丢弃（损坏/过期），看看后面还有没有排队的
            QTimer.singleShot(0, self, self._poll)
            return
        self._handle(message, request.with_suffix(".reply"))

    @staticmethod
    def _take(request: Path) -> dict | None:
        """读出并删除请求；不合法或已过期返回 None。"""
        try:
            if request.is_symlink() or request.stat().st_size > 4096:
                request.unlink()
                return None
            message = json.loads(request.read_bytes())
            request.unlink()
            expires = message["expires"]
            now = time.time()
            if type(expires) not in (int, float) or not now < expires <= now + 10:
                return None
            return message
        except (OSError, ValueError, KeyError, TypeError):
            try:
                request.unlink(missing_ok=True)
            except OSError:
                pass
            return None

    def _handle(self, message: dict, reply: Path) -> None:
        expires = message["expires"]
        operation = message.get("operation")
        device_id = message.get("device")
        window = self._window
        if not window._session_ready or window._quitting:
            if operation in ("state", "set") and isinstance(device_id, str):
                self._tracked.add(device_id)
                self._reported[device_id] = None
            self._reply(reply, {"ok": False, "error": _NOT_READY}, expires)
            return

        devices = tuple(window._all_devices)
        if operation == "state" and isinstance(device_id, str):
            known = window._known_power.get(device_id)
            if type(known) is bool and any(
                device.did == device_id and device.online for device in devices
            ):
                self._tracked.add(device_id)
                self._reported[device_id] = known
                self._reply(reply, {"ok": True, "isOn": known,
                                    **self._controls.describe(devices, device_id)}, expires)
                QTimer.singleShot(0, self, self._poll)
                return
        started = time.monotonic()

        def active():
            return (time.time() < expires and window._session_ready
                    and not window._quitting)

        def work():
            if not active():
                raise ServiceError("控制请求已过期")
            if operation == "list":
                entries = self._controls.devices(devices)
                thumbnails = {
                    model: self._thumbnail(model)
                    for model in {entry["model"] for entry in entries}
                }
                for entry in entries:
                    thumbnail = thumbnails[entry.pop("model")]
                    if thumbnail is not None:
                        entry["thumbnail"] = thumbnail
                return {"devices": entries}
            if not isinstance(device_id, str):
                raise ServiceError("无效的设备")
            if operation == "state":
                return {"isOn": self._controls.state(devices, device_id),
                        **self._controls.describe(devices, device_id)}
            if operation == "set":
                return {"isOn": self._controls.set_state(
                    devices, device_id, message.get("isOn"), still_active=active)}
            raise ServiceError("无效的控制操作")

        def complete(result: dict) -> None:
            self._busy = False
            elapsed = time.monotonic() - started
            if operation in ("state", "set") and isinstance(device_id, str):
                self._tracked.add(device_id)
                self._reported[device_id] = result.get("isOn") if result["ok"] else None
                if result["ok"]:
                    # 比在途的轮询结果新：记版本号，免得旧结果把它盖回去
                    window._bump_power_gen(device_id)
                    window._apply_power_state(device_id, result["isOn"])
            if not result["ok"]:
                logger.warning("控制中心：%s 失败（%.1fs）：%s", operation, elapsed,
                               result.get("error"))
            elif operation == "set":
                logger.info("控制中心：已把设备切换为%s（%.1fs）",
                            "开" if result["isOn"] else "关", elapsed)
            elif operation == "list":
                logger.info("控制中心：设备选择列表 %d 台（%.1fs）",
                            len(result["devices"]), elapsed)
            self._reply(reply, result, expires)
            # 处理排在后面的请求（同时打开多个控件时会一起到）
            QTimer.singleShot(0, self, self._poll)

        self._busy = True
        window._jobs.submit(
            work, priority=PRIORITY_CONTROL,
            on_success=lambda result: complete({"ok": True, **result}),
            on_error=lambda exc: complete({"ok": False, "error": self._error_text(exc)}))

    @staticmethod
    def _thumbnail(model: str) -> str | None:
        """只读主窗口已经缓存的产品图，选设备时不等网络下载。"""
        if not model:
            return None
        reader = QImageReader(str(icon_store.icon_path(model)))
        size = reader.size()
        if not size.isValid() or size.isEmpty():
            return None
        size.scale(QSize(96, 96), Qt.AspectRatioMode.KeepAspectRatio)
        reader.setScaledSize(size)
        image = reader.read()
        if image.isNull():
            return None
        data = QByteArray()
        buffer = QBuffer(data)
        if buffer.open(QIODevice.OpenModeFlag.WriteOnly) and image.save(buffer, "PNG"):
            return base64.b64encode(bytes(data)).decode("ascii")
        return None

    @staticmethod
    def _error_text(exc: Exception) -> str:
        if isinstance(exc, ServiceError) and str(exc):
            return str(exc)
        return "设备暂时无法控制，请在 MiHome 查看状态。"

    @staticmethod
    def _reply(path: Path, result: dict, expires: float) -> None:
        if time.time() >= expires:
            return
        temp = path.with_suffix(".tmp")
        try:
            temp.write_text(json.dumps(result, ensure_ascii=False), encoding="utf-8")
            temp.replace(path)
        except OSError:
            pass
