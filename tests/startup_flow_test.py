# SPDX-License-Identifier: GPL-3.0-or-later
"""启动登录流程与菜单栏驻留/退出行为（离屏，不碰真实凭据与网络）。

1. 登录校验网络失败：不弹扫码；刷新按钮恢复可用；常驻提示带域名与
   「凭据已保留」；有缓存先展示缓存设备，且不发起任何后台网络任务
2. 点刷新重试：重新校验 → 在线拉列表 → 退出离线态
3. 设备列表网络失败：刷新按钮恢复可用，不弹扫码
4. 设备列表报凭据失效（AuthRequiredError）：这时才打开扫码
5. 校验结论为需要扫码：打开扫码
6. 驻留开启：关窗只隐藏，不退出、不收尾
7. Dock 重新激活：隐藏的主窗口被叫回；静默启动瞬间那次激活不响应；
   有其他窗口在前台时不抢
8. Quit 事件（⌘Q 等）开始后关窗：真正退出、收尾一次、不重入 quit
9. 驻留关闭：关窗即退出（quit 恰好一次）
10. macOS：Quit 事件经应用对象送达时打上退出标记；窗口隐藏时由
    aboutToQuit 兜底收尾
11. 菜单栏图标菜单：「显示主窗口」叫回隐藏的主窗口；窗口隐藏时点
    「退出」也会收尾并退出事件循环

用法: .venv/bin/python -m tests.startup_flow_test
"""

import os
import sys
import time
from pathlib import Path
from tempfile import TemporaryDirectory
from unittest.mock import patch

os.environ["QT_QPA_PLATFORM"] = "offscreen"

from tests._isolate import isolate  # noqa: E402

isolate()

import requests  # noqa: E402


def _no_network(*_args, **_kwargs):
    raise AssertionError("测试不应发出真实网络请求")


patch.object(requests.Session, "send", _no_network).start()

from PySide6.QtCore import QCoreApplication, QEvent, Qt  # noqa: E402
from PySide6.QtWidgets import QApplication, QMessageBox, QWidget  # noqa: E402

import run as entry  # noqa: E402  (入口模块：取 macOS 的 QApplication 子类)

IS_MAC = sys.platform == "darwin"
app = (entry._MacApplication if IS_MAC else QApplication)([])

from app.core import cache as device_cache  # noqa: E402
from app.core import service as service_mod  # noqa: E402
from app.core import settings_store  # noqa: E402
from app.core.models import DeviceInfo  # noqa: E402
from app.core.service import (  # noqa: E402
    LOGIN_OK,
    LOGIN_REQUIRED,
    AuthRequiredError,
    NetworkError,
    _MijiaClient,
)
from app.ui.main_window import MainWindow  # noqa: E402

# MainWindow 内部会构造 MijiaService：认证文件指到临时目录，绝不读真实凭据
_auth_dir = TemporaryDirectory()
patch.object(service_mod, "_MijiaClient",
             lambda: _MijiaClient(str(Path(_auth_dir.name) / "auth.json"))).start()
# 网络失败的弹框在离屏测试里不能阻塞
patch.object(QMessageBox, "exec", lambda self: 0).start()
patch.object(QMessageBox, "critical", lambda *a, **k: 0).start()
settings_store.set_check_update_enabled(False)
# 本文件覆盖的是「首次扫码之后」的启动流程；首次扫码门禁见 first_scan_test
from app.core import login_gate  # noqa: E402

login_gate.mark_first_scan_done()

# 会走网络的服务调用（离线态下一个都不该出现）
NETWORK_CALLS = {"list_devices", "power_states", "read_metrics", "icon_urls",
                 "consumable_items", "scene_list", "product_page_name"}
DEFAULTS = {"consumable_items": [], "scene_list": [], "has_product_page_name": True,
            "spec_has_published_functions": True, "api_version": ""}


class FakeService:
    def __init__(self):
        self.calls: list[str] = []
        self.verify: list = []
        self.lists: list = []

    def _next(self, name: str, queue: list):
        self.calls.append(name)
        result = queue.pop(0)
        if isinstance(result, BaseException):
            raise result
        return result

    def verify_login(self):
        return self._next("verify_login", self.verify)

    def list_devices(self):
        return self._next("list_devices", self.lists)

    def __getattr__(self, name):
        if name.startswith("__"):
            raise AttributeError(name)

        def record(*_args, **_kwargs):
            self.calls.append(name)
            return DEFAULTS.get(name, {})
        return record


def device(did: str, name: str) -> DeviceInfo:
    return DeviceInfo(did=did, name=name, model="lkrq.test.v1", home_name="家",
                      room_name="书房", online=True, spec_type="urn:test:1")


def pump(seconds: float, until=None) -> None:
    deadline = time.time() + seconds
    while time.time() < deadline:
        app.processEvents()
        if until is not None and until():
            return
        time.sleep(0.005)


def check(cond: bool, message: str) -> None:
    if not cond:
        raise AssertionError(message)


def make_window(fake: FakeService) -> tuple[MainWindow, list[str]]:
    win = MainWindow()
    win._service = fake
    opened: list[str] = []
    win._open_login_dialog = lambda: opened.append("login")
    return win, opened


network_down = NetworkError(
    "无法连接米家服务器：会话校验超时（建立加密连接或等待响应）（api.mijia.tech）",
    stage="会话校验", host="api.mijia.tech", kind="超时")

# ---------- 1. 登录校验网络失败 ----------
settings_store.set_minimize_to_tray(True)
device_cache.save([device("d1", "开机卡"), device("d2", "台灯")], {}, {})
fake = FakeService()
fake.verify = [network_down]
win, opened = make_window(fake)
win.show()
win.start()
pump(5, until=lambda: not win._checking_login)
pump(0.3)
check(opened == [], "网络失败不应打开扫码窗口")
check(win._refresh_btn.isEnabled(), "网络失败后刷新按钮必须恢复可用")
check(not win._session_ready and win._offline_view, "应进入离线态")
hint = win._status_hint.text()
check(win._status_hint.isVisible() and "凭据已保留" in hint and "api.mijia.tech" in hint,
      f"提示应常驻并说明凭据保留与域名: {hint!r}")
check(len(win._all_devices) == 2, "有缓存时应先展示缓存设备")
check(not NETWORK_CALLS.intersection(fake.calls),
      f"离线态不应发起后台网络任务: {fake.calls}")
check(win._auto_retry_timer.isActive() and "自动重试" in hint,
      "网络失败后应排定自动重试并告知用户")
first_delay = win._auto_retry_timer.interval()
# 自动重试再次失败：继续离线，退避拉长
fake.verify = [network_down]
win._auto_retry_login()
pump(5, until=lambda: not win._checking_login)
pump(0.2)
check(fake.calls.count("verify_login") == 2 and opened == [], "自动重试失败也不能弹扫码")
check(win._auto_retry_timer.isActive()
      and win._auto_retry_timer.interval() > first_delay, "自动重试应退避")
print("1. 校验网络失败 → 不扫码、按钮可用、展示缓存、无后台请求、自动重试退避 OK")

# ---------- 2. 点刷新重试 ----------
fake.verify = [LOGIN_OK]
fake.lists = [[device("d1", "开机卡"), device("d2", "台灯"), device("d3", "插座")]]
win._refresh_btn.click()
pump(5, until=lambda: "list_devices" in fake.calls and not win._loading_devices)
pump(0.3)
check(fake.calls.count("verify_login") == 3, "刷新应先重新校验登录")
check(win._session_ready and not win._offline_view, "重试成功后应退出离线态")
check(len(win._all_devices) == 3 and win._refresh_btn.isEnabled(), "应在线拉到最新列表")
check(not win._auto_retry_timer.isActive() and win._auto_retry_count == 0,
      "校验成功后应取消自动重试")
print("2. 刷新重试 → 重新校验并在线加载 OK")

# ---------- 3. 设备列表网络失败 ----------
fake.lists = [NetworkError("获取设备列表失败：获取家庭列表超时（api.mijia.tech）",
                           stage="获取家庭列表", host="api.mijia.tech", kind="超时")]
count = fake.calls.count("list_devices")
win._refresh_btn.click()
pump(5, until=lambda: fake.calls.count("list_devices") > count and not win._loading_devices)
pump(0.3)
check(win._refresh_btn.isEnabled() and not win._loading_devices, "失败后刷新按钮必须恢复")
check(opened == [], "网络失败不应打开扫码窗口")
check("凭据已保留" in win._status_hint.text(), "应提示凭据已保留")
print("3. 设备列表网络失败 → 按钮恢复、不扫码 OK")

# ---------- 4. 凭据被服务器拒绝 ----------
fake.lists = [AuthRequiredError("获取设备列表失败：登录凭据已失效，请重新扫码登录")]
count = fake.calls.count("list_devices")
win._refresh_btn.click()
pump(5, until=lambda: opened)
check(opened == ["login"], "凭据失效时应打开扫码窗口")
check(win._refresh_btn.isEnabled(), "刷新按钮必须恢复")
print("4. 凭据被拒 → 打开扫码 OK")

# ---------- 5. 校验结论需要扫码 ----------
opened.clear()
fake.verify = [LOGIN_REQUIRED]
win._session_ready = False
win.start()
pump(5, until=lambda: opened)
check(opened == ["login"], "无凭据时应打开扫码窗口")
print("5. 需要扫码 → 打开扫码 OK")

# ---------- 6~9. 关窗 / Dock / 退出 ----------
quit_calls: list[int] = []
patch.object(type(app), "quit", lambda *a, **k: quit_calls.append(1)).start()
# 离屏平台没有系统托盘；真实 macOS 上菜单栏图标总是可用
patch.object(MainWindow, "has_system_tray", lambda self: True).start()

win.show()
pump(0.2)
win.close()
pump(0.2)
check(win.isHidden() and not quit_calls and not win._shutdown_done,
      "驻留开启时关窗只应隐藏")
print("6. 驻留开启：关窗只隐藏 OK")

win._on_app_state_changed(Qt.ApplicationState.ApplicationActive)
pump(0.2)
check(win.isVisible(), "Dock 重新激活应叫回隐藏的主窗口")
win.close()
win.defer_dock_reopen()
win._on_app_state_changed(Qt.ApplicationState.ApplicationActive)
pump(0.2)
check(win.isHidden(), "静默启动瞬间的激活不应弹出主窗口")
# 启动宽限期过后（作为登录项静默启动时应用可能从未失活过），
# 第一次点 Dock 就要能叫出窗口
win._reopen_ignore_until = 0.0
other = QWidget()
other.setWindowTitle("另一个窗口")
other.show()
win._on_app_state_changed(Qt.ApplicationState.ApplicationActive)
pump(0.2)
check(win.isHidden(), "有别的窗口在前台时不应抢")
other.close()
win._on_app_state_changed(Qt.ApplicationState.ApplicationActive)
pump(0.2)
check(win.isVisible(), "切走一次后再激活应叫回主窗口")
print("7. Dock 重新激活规则 OK")

win.prepare_for_quit()
win.close()
pump(0.2)
check(win._shutdown_done and not quit_calls, "Quit 事件后的关窗应真正退出且不重入 quit")
print("8. ⌘Q 后关窗 → 真正退出、不重入 OK")

settings_store.set_minimize_to_tray(False)
win2, _ = make_window(FakeService())
win2.show()
pump(0.2)
win2.close()
pump(0.2)
check(win2._shutdown_done and len(quit_calls) == 1, "驻留关闭时关窗即退出")
print("9. 驻留关闭：关窗即退出 OK")

# ---------- 10. macOS：Quit 事件与隐藏态收尾 ----------
if IS_MAC:
    settings_store.set_minimize_to_tray(True)
    win3, _ = make_window(FakeService())
    win3.show()
    pump(0.2)
    win3.close()  # 隐藏到菜单栏
    pump(0.2)
    check(win3.isHidden() and not win3._shutdown_done, "应先隐藏到菜单栏")
    app.quit_hook = win3.prepare_for_quit
    QCoreApplication.sendEvent(app, QEvent(QEvent.Type.Quit))
    check(win3._quitting, "Quit 事件应给主窗口打上退出标记")
    app.aboutToQuit.emit()
    check(win3._shutdown_done, "窗口隐藏时 aboutToQuit 应兜底收尾")
    print("10. macOS Quit 事件 + aboutToQuit 收尾 OK")

# ---------- 11. 菜单栏图标菜单：「显示主窗口」/「退出」 ----------
# 直接触发菜单项的 QAction，与真实点击走同一个 triggered 槽
settings_store.set_minimize_to_tray(True)
win4, _ = make_window(FakeService())
check(win4._tray is not None, "应创建菜单栏图标控制器")
tray_actions = {a.text(): a for a in win4._tray._tray.contextMenu().actions()}
check({"显示主窗口", "退出"} <= set(tray_actions), "菜单栏菜单应有「显示主窗口」和「退出」")
win4.show()
pump(0.2)
win4.close()  # 红叉：隐藏到菜单栏
pump(0.2)
check(win4.isHidden() and not win4._shutdown_done, "红叉应只隐藏到菜单栏")
tray_actions["显示主窗口"].trigger()
pump(0.2)
check(win4.isVisible(), "「显示主窗口」应叫回隐藏的主窗口")
win4.close()
pump(0.2)
quits_before = len(quit_calls)
tray_actions["退出"].trigger()
pump(0.2)
check(win4._shutdown_done and len(quit_calls) > quits_before,
      "窗口隐藏时点「退出」也应收尾并退出事件循环")
print("11. 菜单栏「显示主窗口」/「退出」 OK")

print("STARTUP FLOW REGRESSION ALL PASS")
sys.stdout.flush()
# 直接退出：后台任务线程与离屏窗口不必逐个析构（同其他界面测试的做法）
os._exit(0)
