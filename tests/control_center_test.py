# SPDX-License-Identifier: GPL-3.0-or-later
"""macOS 控制中心信箱（control_center_bridge）回归测试，离屏、不联网。

1. list：列出所有能读到开关状态的设备，带家庭与在线状态；开机卡也在其中，离线设备也在
2. state：回状态和当前名称/房间；记为控件关心的设备并同步给主窗口
3. set：状态不同才写入，相同不重复写；写入结果同步给主窗口
4. 不支持/离线/已删除的设备给出可读的原因；开机卡按普通开关处理
5. 未登录时立即回「正在登录」，不排队等超时
6. 过期、超前、损坏、超大、符号链接的请求直接删除、不应答
7. 运行期间持有 host.lock；stop() 放开锁并请系统重载控件
8. 控件重载：只在控件关心的设备状态和上次告诉扩展的不同时触发，且合并
9. 登录就绪时重载一次
10. 主窗口：落定开关状态时发 power_state_updated；轮询名单包含控件的设备
11. 新请求靠目录事件即时取走，不等 1 秒一次的兜底轮询

用法: .venv/bin/python -m tests.control_center_test
"""

import base64
import fcntl
import json
import os
import time
import uuid
from pathlib import Path
from tempfile import TemporaryDirectory
from types import SimpleNamespace

os.environ["QT_QPA_PLATFORM"] = "offscreen"

from tests._isolate import isolate  # noqa: E402

isolate()

from PySide6.QtCore import QObject, Signal  # noqa: E402
from PySide6.QtGui import QImage
from PySide6.QtWidgets import QApplication  # noqa: E402

app = QApplication([])

from app.core import cache as _device_cache, icon_store  # noqa: E402

_device_cache.save = lambda *a, **k: None

from app.core.jobs import JobExecutor  # noqa: E402
from app.core.models import DeviceInfo  # noqa: E402
from app.core.service import ServiceError  # noqa: E402
from app.ui import control_center_bridge as bridge_mod  # noqa: E402
from app.ui.control_center_bridge import ControlCenterBridge  # noqa: E402
from app.ui.main_window import MainWindow  # noqa: E402


def pump(seconds=1.0, until=None):
    deadline = time.time() + seconds
    while time.time() < deadline:
        app.processEvents()
        if until is not None and until():
            return True
        time.sleep(0.005)
    return False


class FakeService:
    """只实现 DeviceControls 用到的三个方法，记录写入。"""

    def __init__(self):
        self.power = {"d1": True, "d2": False, "pc-power-card": False, "d3": True}
        self.writes = []
        self.fail_reads = False

    def power_states(self, dids):
        if self.fail_reads:
            raise ServiceError("批量读取设备状态失败：测试模拟")
        return {did: self.power.get(did) for did in dids}

    def power_state(self, did):
        return self.power_states([did]).get(did)

    def set_power_state(self, did, state):
        self.writes.append((did, state))
        self.power[did] = state


class FakeWindow(QObject):
    power_state_updated = Signal(str, object)

    def __init__(self, service, devices):
        super().__init__()
        self._service = service
        self._jobs = JobExecutor(self)
        self._session_ready = True
        self._quitting = False
        self._all_devices = devices
        self._known_power = {}
        self._control_bridge = None
        self.applied = []
        self.gens = []

    def _bump_power_gen(self, did):
        self.gens.append(did)

    def _apply_power_state(self, did, state):
        self.applied.append((did, state))
        self.power_state_updated.emit(did, state)


DEVICES = [
    DeviceInfo("d1", "台灯", "m.plug", "家", "书房", True),
    DeviceInfo("d2", "热水器", "test.plug.heater", "家", "浴室", True),
    DeviceInfo("pc-power-card", "测试电脑开机卡", "pzg.plug.1127", "家", "主机", True),
    DeviceInfo("d3", "离线插座", "m.plug", "家", "卧室", False),
    DeviceInfo("sensor", "温湿度计", "m.sensor", "家", "卧室", True),
]

tmp = TemporaryDirectory(prefix="mihome-controls-")
mailbox = Path(tmp.name) / "MiHomeControls"
service = FakeService()
window = FakeWindow(service, DEVICES)
reloads = []
bridge = ControlCenterBridge(window, mailbox, reloader=lambda: reloads.append(time.time()))


def write_request(message, key=None):
    key = key or str(uuid.uuid4()).upper()
    temp = mailbox / (key + ".writing")
    temp.write_text(json.dumps(message), encoding="utf-8")
    temp.replace(mailbox / (key + ".request"))
    return key


def ask(operation, device=None, is_on=None, timeout=5.0):
    message = {"operation": operation, "expires": time.time() + 8}
    if device is not None:
        message["device"] = device
    if is_on is not None:
        message["isOn"] = is_on
    key = write_request(message)
    reply = mailbox / (key + ".reply")
    assert pump(timeout, reply.exists), f"{operation} 没有应答"
    data = json.loads(reply.read_text(encoding="utf-8"))
    reply.unlink()
    assert not (mailbox / (key + ".request")).exists(), "请求读完应删除"
    return data


assert oct(mailbox.stat().st_mode & 0o777) == "0o700", "信箱目录应为 0700"

# ---------- 1. list ----------
icon = icon_store.icon_path("pzg.plug.1127")
icon.parent.mkdir(parents=True, exist_ok=True)
image = QImage(200, 100, QImage.Format.Format_RGB32)
image.fill(0xFF40A060)
assert image.save(str(icon))
reply = ask("list")
assert reply["ok"] is True, reply
assert [d["id"] for d in reply["devices"]] == ["d1", "d2", "pc-power-card", "d3"], reply
assert reply["devices"][0] == {"id": "d1", "name": "台灯", "room": "书房", "home": "家",
                               "online": True, "symbol": "powerplug", "isOn": True}, reply
assert reply["devices"][1]["symbol"] == "drop"
assert reply["devices"][2]["symbol"] == "desktopcomputer"
thumbnail = QImage.fromData(base64.b64decode(reply["devices"][2]["thumbnail"]))
assert (thumbnail.width(), thumbnail.height()) == (96, 48)
assert reply["devices"][3]["online"] is False, "离线设备也要列出并标明离线"
from app.core.control_center import DeviceControls  # noqa: E402
assert DeviceControls.home_label("未知") == "共享设备" and DeviceControls.home_label("") == "共享设备"
print("1. list 列出全部有开关的设备（含开机卡、离线设备），带家庭 OK")

# ---------- 2. state ----------
reply = ask("state", "d1")
assert reply == {"ok": True, "isOn": True, "name": "台灯", "room": "书房", "home": "家",
                "symbol": "powerplug"}, reply
assert "d1" in bridge.tracked_dids()
assert window.applied[-1] == ("d1", True) and window.gens[-1] == "d1"
print("2. state 回状态与名称，并同步主窗口 OK")

# ---------- 3. set ----------
reply = ask("set", "d2", True)
assert reply == {"ok": True, "isOn": True}, reply
assert service.writes == [("d2", True)], service.writes
assert window.applied[-1] == ("d2", True)
reply = ask("set", "d2", True)
assert reply["ok"] is True and service.writes == [("d2", True)], "状态相同不应重复写入"
reply = ask("set", "d2", "yes")
assert reply["ok"] is False and "无效的开关状态" in reply["error"], reply
print("3. set 只在状态不同时写入 OK")

# ---------- 4. 不可控设备的原因 ----------
assert ask("state", "sensor")["error"] == "这台设备不支持控制中心开关"
assert ask("state", "d3")["error"] == "设备离线"
assert ask("state", "gone")["error"] == "这台设备已不在当前账号的设备列表里"
assert ask("set", "d3", False)["ok"] is False and ("d3", False) not in service.writes
assert ask("state", "pc-power-card") == {"ok": True, "isOn": False, "name": "测试电脑开机卡",
                                "room": "主机", "home": "家", "symbol": "desktopcomputer"}
assert ask("set", "pc-power-card", True) == {"ok": True, "isOn": True}
assert service.writes[-1] == ("pc-power-card", True), "开机卡应像普通开关一样写入相反状态"
print("4. 无开关/离线/已删除设备给出原因且不写入；开机卡按普通开关 OK")

# ---------- 5. 未登录 ----------
window._session_ready = False
started = time.time()
reply = ask("state", "d1")
assert reply == {"ok": False, "error": bridge_mod._NOT_READY}, reply
assert time.time() - started < 2, "未登录应立即应答"
window._session_ready = True
print("5. 未登录立即回「正在登录」 OK")

# ---------- 6. 不合法的请求 ----------
bad = {
    write_request({"operation": "list", "expires": time.time() - 1}): "过期",
    write_request({"operation": "list", "expires": time.time() + 60}): "超前",
    write_request({"operation": "list"}): "缺 expires",
}
key = str(uuid.uuid4()).upper()
(mailbox / (key + ".request")).write_text("{not json", encoding="utf-8")
bad[key] = "损坏"
key = str(uuid.uuid4()).upper()
(mailbox / (key + ".request")).write_text(" " * 5000, encoding="utf-8")
bad[key] = "超大"
key = str(uuid.uuid4()).upper()
target = Path(tmp.name) / "elsewhere.json"
target.write_text(json.dumps({"operation": "list", "expires": time.time() + 8}))
(mailbox / (key + ".request")).symlink_to(target)
bad[key] = "符号链接"
assert pump(3, lambda: not any((mailbox / (k + ".request")).exists(follow_symlinks=False)
                               for k in bad)), "不合法的请求应被删除"
pump(0.3)
for key, why in bad.items():
    assert not (mailbox / (key + ".reply")).exists(), f"{why}的请求不应应答"
assert target.exists(), "删除符号链接不能删到它指向的文件"
print("6. 过期/超前/损坏/超大/符号链接请求被丢弃 OK")

# ---------- 11. 即时处理 ----------
# 兜底轮询是 1 秒；新请求必须靠目录事件立即被取走，否则点控件要多等最多 1 秒
pickups = []
for gap in (0.35, 0.8, 0.15, 0.6, 0.45):
    pump(gap)
    key = write_request({"operation": "list", "expires": time.time() + 8})
    request = mailbox / (key + ".request")
    started = time.time()
    assert pump(2, lambda: not request.exists()), "请求没有被取走"
    pickups.append(time.time() - started)
    assert pump(2, (mailbox / (key + ".reply")).exists)
    (mailbox / (key + ".reply")).unlink()
assert max(pickups) < 0.2, f"请求应即时处理，实际 {['%.2f' % p for p in pickups]}"
print("11. 目录事件即时唤醒（%s 秒） OK" % "/".join("%.2f" % p for p in pickups))

# ---------- 8. 控件重载 ----------
# 第 5 步未登录时 d1 记成了「没能告诉扩展」，先正常取一次状态
assert ask("state", "d1") == {"ok": True, "isOn": True, "name": "台灯", "room": "书房", "home": "家",
                "symbol": "powerplug"}
pump(0.6)
reloads.clear()
window.power_state_updated.emit("d1", True)          # 与上次告诉扩展的一致
window.power_state_updated.emit("untracked", False)  # 没有控件问过的设备
pump(0.6)
assert reloads == [], "状态没变不应重载"
window.power_state_updated.emit("d1", False)
window.power_state_updated.emit("d2", False)
pump(0.8)
assert len(reloads) == 1, f"一轮变化应合并为一次重载：{reloads}"
service.fail_reads = True
assert ask("state", "d1")["ok"] is False
service.fail_reads = False
reloads.clear()
window.power_state_updated.emit("d1", True)
pump(0.8)
assert len(reloads) == 1, "读取失败后恢复应重载，让控件离开「不可用」"
print("8. 只在控件显示过时时合并重载 OK")

# ---------- 9. 登录就绪 ----------
reloads.clear()
window._session_ready = False
bridge._poll()
window._session_ready = True
bridge._poll()
pump(0.8)
assert len(reloads) == 1, "登录就绪应重载一次"
print("9. 登录就绪时重载 OK")

# ---------- 7. host.lock ----------
probe = os.open(mailbox / "host.lock", os.O_RDONLY)
try:
    fcntl.flock(probe, fcntl.LOCK_SH | fcntl.LOCK_NB)
    raise AssertionError("主程序运行期间 host.lock 应被占用")
except BlockingIOError:
    pass
reloads.clear()
bridge.stop()
fcntl.flock(probe, fcntl.LOCK_SH | fcntl.LOCK_NB)
fcntl.flock(probe, fcntl.LOCK_UN)
os.close(probe)
assert len(reloads) == 1, "退出时应重载，控件改显示未运行"
print("7. 运行期间占用 host.lock，退出放开并重载 OK")
window._jobs.shutdown(wait=1)

# ---------- 10. 主窗口集成 ----------
MainWindow._refresh_power_states = lambda self, force: None
MainWindow._refresh_metrics = lambda self: None
MainWindow._load_card_icons = lambda self: None
MainWindow._maybe_localize_names = lambda self: None
MainWindow._load_scenes = lambda self: None
win = MainWindow()
seen = []
win.power_state_updated.connect(lambda did, state: seen.append((did, state)))
win._all_devices = list(DEVICES)
win._apply_power_state("d1", True)
win._apply_power_state("d3", True)                   # 离线设备不落记忆，也不通知
assert seen == [("d1", True)], seen
stub = SimpleNamespace(
    _visible_devices=lambda: [DEVICES[0]], _board=None, _all_devices=DEVICES,
    _control_bridge=SimpleNamespace(tracked_dids=lambda: {"d2", "d3", "d1"}))
assert MainWindow._poll_dids(stub) == ["d1", "d2"], MainWindow._poll_dids(stub)
win._force_quit = True
win.close()
pump(0.2)
print("10. 主窗口发出状态信号，轮询名单含控件设备 OK")

tmp.cleanup()
print("CONTROL CENTER ALL PASS")
