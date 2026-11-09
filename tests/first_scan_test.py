# SPDX-License-Identifier: GPL-3.0-or-later
"""macOS 首次登录必须扫码（全程离线：假的小米账号服务，不碰真实凭据与网络）。

服务层（真实 _MijiaClient + 假服务端）：
1. 本机已有有效 CLI 凭据、首次取码：serviceLogin 看不到 passToken/userId，
   返回新二维码；认证文件一字节不变，内存凭据原样还原，设备身份沿用
2. 对照：普通取码（非首次）拿同样的凭据会直接刷新、不出二维码
3. 探针保护：服务端即使意外回「刷新成功」，也绝不把探针写进认证文件
4. 首次扫码确认后：新凭据写进与 CLI 共用的认证文件，取码与长轮询同一身份
5. 本机没有认证文件时首次取码：取码途中生成的设备身份并回正式数据
6. 扫码已确认、换会话网络失败：错误带 scan_confirmed，凭据已落盘
界面层（MainWindow + 假服务 + 假扫码窗）：
7. 已有有效 CLI 凭据的首次启动：不校验登录、不摆缓存、不加载设备，
   直接打开「强制新二维码」的扫码窗
8. 首次扫码取消/失败：不记录完成状态、程序退出；下次启动仍需扫码
9. 首次扫码成功：记录完成状态（另起进程也读得到），跳过缓存在线加载
10. 完全退出再打开：凭据有效 → 自动登录，不弹扫码
11. 凭据被服务器明确拒绝 → 重新扫码（普通模式）；成功后照常加载
12. 凭据缺失 → 重新扫码
13. 网络超时 / 代理连接失败 → 沿用现有重试流程，不弹扫码、不动完成状态
扫码窗（真实 LoginDialog）：
14. 首次模式「手机已确认、换会话失败」后，重试改走刷新，不再强制新二维码
15. 门禁只在 macOS 生效（ENABLED=False 时恒不要求首次扫码、也不写文件）

用法: .venv/bin/python -m tests.first_scan_test
"""

import json
import os
import subprocess
import sys
import time
from pathlib import Path
from tempfile import TemporaryDirectory
from threading import RLock
from unittest.mock import patch

os.environ["QT_QPA_PLATFORM"] = "offscreen"

from tests._isolate import isolate  # noqa: E402

DATA_DIR = isolate()

import requests  # noqa: E402


def _no_network(*_args, **_kwargs):
    raise AssertionError("测试不应发出真实网络请求")


# 安全网：任何漏网的真实请求都直接判失败
patch.object(requests.Session, "send", _no_network).start()

from mijiaAPI import mijiaAPI  # noqa: E402
from PySide6.QtWidgets import QApplication, QMessageBox  # noqa: E402

import run as entry  # noqa: E402  (入口模块：取 macOS 的 QApplication 子类)

IS_MAC = sys.platform == "darwin"
app = (entry._MacApplication if IS_MAC else QApplication)([])

from app.core import cache as device_cache  # noqa: E402
from app.core import login_gate  # noqa: E402
from app.core import service as service_mod  # noqa: E402
from app.core import settings_store  # noqa: E402
from app.core.jobs import JobExecutor  # noqa: E402
from app.core.models import DeviceInfo  # noqa: E402
from app.core.service import (  # noqa: E402
    LOGIN_OK,
    LOGIN_REQUIRED,
    MijiaService,
    NetworkError,
    ServiceError,
    _MijiaClient,
)
from app.ui.login_dialog import LoginDialog as RealLoginDialog  # noqa: E402
from app.ui.main_window import MainWindow  # noqa: E402

# 门禁是 macOS 专属行为；在其他平台上跑本测试时显式打开，验证的是同一套逻辑
login_gate.ENABLED = True


def check(cond: bool, message: str) -> None:
    if not cond:
        raise AssertionError(message)


def pump(seconds: float, until=None) -> None:
    deadline = time.time() + seconds
    while time.time() < deadline:
        app.processEvents()
        if until is not None and until():
            return
        time.sleep(0.005)


# ---------------------------------------------------------------- 服务层


class Resp:
    def __init__(self, status: int, text: str):
        self.status_code = status
        self.text = text


def _xm(payload: dict) -> Resp:
    return Resp(200, "&&&START&&&" + json.dumps(payload))


class FakeXiaomi:
    """假的小米账号服务：Cookie 里有 passToken 就「刷新成功」，否则给二维码。"""

    def __init__(self):
        self.service_login_cookies: list[dict] = []
        self.qr_requests = 0
        self.qr_user_agent = None
        self.lp_user_agent = None
        # 探针保护测试用：服务端无视 Cookie 一律回「刷新成功」
        self.always_refresh = False

    @staticmethod
    def _cookie_fields(headers) -> dict:
        cookie = (headers or {}).get("Cookie", "")
        return dict(part.split("=", 1) for part in cookie.split(";") if "=" in part)

    def get(self, url, headers=None, **_kwargs):  # 替换 requests.get
        if url.startswith("https://account.xiaomi.com/pass/serviceLogin"):
            fields = self._cookie_fields(headers)
            self.service_login_cookies.append(fields)
            if fields.get("passToken") or self.always_refresh:
                return _xm({"code": 0, "ssecurity": "c3NlY3VyaXR5LXJlZnJlc2g=",
                            "location": "https://sts.api.mijia.tech/sts?ticket=refresh"})
            return _xm({"code": 70016,
                        "location": "https://account.xiaomi.com/fe/service/login"
                                    "?sid=mijia&_sign=test&qs=%253Fsid%253Dmijia"
                                    "&callback=https%3A%2F%2Fsts.api.mijia.tech%2Fsts"})
        if url.startswith("https://account.xiaomi.com/longPolling/loginUrl"):
            self.qr_requests += 1
            self.qr_user_agent = (headers or {}).get("User-Agent")
            return _xm({"code": 0, "qr": "https://qr.example.invalid/qr.png",
                        "loginUrl": "https://account.xiaomi.com/longPolling/login?ticket=t",
                        "lp": "https://lp.example.invalid/poll?ticket=t"})
        raise AssertionError(f"意外的请求: {url.split('?')[0]}")

    def session_get(self, session, url, headers=None, **_kwargs):  # 替换 Session.get
        if url.startswith("https://sts.api.mijia.tech/"):
            session.cookies.set("serviceToken", "service-from-sts")
            return Resp(200, "ok")
        if url.startswith("https://lp.example.invalid/"):
            self.lp_user_agent = (headers or {}).get("User-Agent")
            return _xm({"code": 0, "psecurity": "ps-new", "nonce": "n-new",
                        "ssecurity": "c3NlY3VyaXR5LW5ldw==", "passToken": "pass-new",
                        "userId": "42", "cUserId": "cuid-new",
                        "location": "https://sts.api.mijia.tech/sts?ticket=scan"})
        raise AssertionError(f"意外的会话请求: {url.split('?')[0]}")


def fake_server(server: FakeXiaomi):
    return (patch("requests.get", server.get),
            patch.object(requests.Session, "get",
                         lambda self, url, **kw: server.session_get(self, url, **kw)))


CLI_AUTH = {
    "psecurity": "ps-cli", "nonce": "n-cli", "ssecurity": "c2VjdXJpdHk=",
    "passToken": "pass-cli", "userId": "7", "cUserId": "cuid-cli",
    "serviceToken": "service-cli", "expireTime": 1893456000000,
    "ua": "Android-15-11.0.701-Xiaomi-TEST-CN-ua", "deviceId": "cliDevice0001",
    "pass_o": "0123456789abcdef", "saveTime": 1762500000000,
}


def make_service(directory: str, auth: dict | None = CLI_AUTH) -> tuple[MijiaService, Path]:
    path = Path(directory) / "auth.json"
    if auth is not None:
        path.write_text(json.dumps(auth, indent=2))
    service = object.__new__(MijiaService)
    service._lock = RLock()
    service._api = _MijiaClient(str(path))
    return service, path


def service_tests() -> None:
    # 1. 已有有效 CLI 凭据 → 首次取码不带凭据、拿到新二维码、文件不变
    with TemporaryDirectory() as d:
        service, path = make_service(d)
        api = service._api
        before_bytes = path.read_bytes()
        before = json.loads(json.dumps(api.auth_data))
        server = FakeXiaomi()
        p1, p2 = fake_server(server)
        with p1, p2:
            data = service.qr_login_begin(force_new=True)
        check(service.has_saved_credentials(), "应识别出本机已有凭据")
        check(data is not None and data.get("loginUrl"), "首次登录必须拿到新二维码")
        sent = server.service_login_cookies[-1]
        check(sent.get("passToken") == "" and sent.get("userId") == ""
              and sent.get("cUserId") == "", "取码请求不得带 passToken/userId/cUserId")
        check(sent.get("deviceId") == CLI_AUTH["deviceId"]
              and sent.get("pass_o") == CLI_AUTH["pass_o"], "应沿用本机设备身份")
        check(path.read_bytes() == before_bytes, "取码过程不应改动认证文件")
        check(api.auth_data == before, "内存凭据应原样还原")
        check(not api._qr_probe_active, "探针状态应复位")
    print("1. 已有 CLI 凭据的首次取码：不带凭据、出新二维码、认证文件不变 OK")

    # 2. 对照：普通取码会用 passToken 直接刷新，不出二维码
    with TemporaryDirectory() as d:
        service, path = make_service(d)
        server = FakeXiaomi()
        p1, p2 = fake_server(server)
        with p1, p2:
            data = service.qr_login_begin()
        check(data is None and server.qr_requests == 0, "普通取码在凭据有效时应直接刷新")
        check(server.service_login_cookies[-1].get("passToken") == "pass-cli",
              "普通取码会带上本地 passToken（这正是首次登录要绕开的）")
    print("2. 对照：普通取码凭据有效时直接刷新 OK")

    # 3. 探针保护：服务端意外回「刷新成功」也不写认证文件
    with TemporaryDirectory() as d:
        service, path = make_service(d)
        api = service._api
        before_bytes = path.read_bytes()
        before = json.loads(json.dumps(api.auth_data))
        server = FakeXiaomi()
        server.always_refresh = True
        p1, p2 = fake_server(server)
        with p1, p2:
            try:
                service.qr_login_begin(force_new=True)
            except ServiceError as exc:
                check("二维码" in str(exc), str(exc))
            else:
                raise AssertionError("服务端没给二维码时应报错")
        check(path.read_bytes() == before_bytes, "探针绝不能写进认证文件")
        check(api.auth_data == before and not api._qr_probe_active, "内存凭据应还原")
    print("3. 探针保护：意外刷新也不写认证文件 OK")

    # 4. 首次扫码确认后：新凭据写进共用的认证文件，身份一致
    with TemporaryDirectory() as d:
        service, path = make_service(d)
        server = FakeXiaomi()
        p1, p2 = fake_server(server)
        with p1, p2:
            data = service.qr_login_begin(force_new=True)
            service.qr_login_wait(data)
        saved = json.loads(path.read_text())
        check(saved["passToken"] == "pass-new" and saved["userId"] == "42"
              and saved["serviceToken"] == "service-from-sts", "新凭据应写入共用认证文件")
        check(saved["deviceId"] == CLI_AUTH["deviceId"] and saved["ua"] == CLI_AUTH["ua"]
              and saved["pass_o"] == CLI_AUTH["pass_o"], "设备身份应保持不变")
        check(server.qr_user_agent == server.lp_user_agent == CLI_AUTH["ua"],
              "取码与长轮询应使用同一身份")
        check(service._api.available, "扫码成功后会话应立即可用（命中校验缓存）")
    print("4. 扫码确认后写入共用认证文件、身份一致 OK")

    # 5. 本机没有认证文件：取码途中生成的设备身份并回正式数据
    with TemporaryDirectory() as d:
        service, path = make_service(d, auth=None)
        api = service._api
        server = FakeXiaomi()
        p1, p2 = fake_server(server)
        with p1, p2:
            data = service.qr_login_begin(force_new=True)
        check(data and data.get("loginUrl"), "无认证文件时也应出二维码")
        check(not path.exists(), "取码阶段不应创建认证文件")
        generated = server.service_login_cookies[-1].get("deviceId")
        check(generated and api.auth_data.get("deviceId") == generated,
              "取码时生成的 deviceId 应并回正式数据")
        check("passToken" not in api.auth_data, "不应凭空出现凭据字段")
    print("5. 无认证文件时首次取码：设备身份并回 OK")

    # 6. 已确认扫码、换会话网络失败 → scan_confirmed
    with TemporaryDirectory() as d:
        service, path = make_service(d, auth=None)

        def scanned_then_timeout(api, _login_data):
            api.auth_data.update({"psecurity": "ps", "nonce": "n",
                                  "ssecurity": "c2VjdXJpdHk=", "passToken": "pass-scan",
                                  "userId": "1", "cUserId": "c"})
            raise requests.exceptions.ReadTimeout(
                "HTTPSConnectionPool(host='sts.api.mijia.tech', port=443)")

        def refresh_timeout(_api):
            raise requests.exceptions.ReadTimeout(
                "HTTPSConnectionPool(host='sts.api.mijia.tech', port=443)")

        with (patch.object(mijiaAPI, "_complete_qr_login", scanned_then_timeout),
              patch.object(mijiaAPI, "_get_location", refresh_timeout)):
            try:
                service.qr_login_wait({})
            except NetworkError as exc:
                check(exc.scan_confirmed, "已确认扫码的网络失败应标记 scan_confirmed")
            else:
                raise AssertionError("换会话失败应报网络错误")
        check(json.loads(path.read_text())["passToken"] == "pass-scan", "已确认的凭据应落盘")
    print("6. 已确认扫码、换会话失败 → scan_confirmed 且凭据落盘 OK")


# ---------------------------------------------------------------- 界面层

DEFAULTS = {"consumable_items": [], "scene_list": [], "has_product_page_name": True,
            "spec_has_published_functions": True, "api_version": ""}


class FakeService:
    """MainWindow 用的假服务：记录调用，按队列给结论。"""

    def __init__(self, *, verify=(), lists=(), saved_credentials=True):
        self.calls: list[str] = []
        self.verify = list(verify)
        self.lists = list(lists)
        self.saved_credentials = saved_credentials

    def _next(self, name: str, queue: list):
        self.calls.append(name)
        result = queue.pop(0)
        if isinstance(result, BaseException):
            raise result
        return result

    def has_saved_credentials(self):
        return self.saved_credentials

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


class FakeLoginDialog:
    """替身扫码窗：记录打开方式与打开那一刻主窗口的状态，按队列给结果。"""

    Accepted = 1
    Rejected = 0
    opened: list["FakeLoginDialog"] = []
    results: list[int] = []
    # 扫码窗开着时发生的事（例如用户按 ⌘Q），在返回结果前执行
    during_exec = None

    def __init__(self, service, jobs, parent=None, *, force_new_qr=False):
        self.service = service
        self.window = parent
        self.force_new_qr = force_new_qr

    def exec_and_wait(self) -> int:
        self.devices_shown = len(self.window._all_devices)
        self.calls_before = list(self.service.calls)
        FakeLoginDialog.opened.append(self)
        if FakeLoginDialog.during_exec is not None:
            FakeLoginDialog.during_exec(self.window)
            FakeLoginDialog.during_exec = None
        return FakeLoginDialog.results.pop(0)


def device(did: str, name: str) -> DeviceInfo:
    return DeviceInfo(did=did, name=name, model="lkrq.test.v1", home_name="家",
                      room_name="书房", online=True, spec_type="urn:test:1")


def new_window(fake: FakeService) -> MainWindow:
    win = MainWindow()
    win._service = fake
    return win


def wait_dialog(count: int) -> None:
    pump(5, until=lambda: len(FakeLoginDialog.opened) >= count)
    check(len(FakeLoginDialog.opened) == count,
          f"应打开第 {count} 个扫码窗，实际 {len(FakeLoginDialog.opened)}")


def gate_in_new_process() -> bool:
    """另起一个 Python 进程读完成状态：模拟「完全退出再打开」后的读取。"""
    code = ("from app.core import login_gate; login_gate.ENABLED = True; "
            "print(login_gate.first_scan_required())")
    out = subprocess.run([sys.executable, "-c", code], capture_output=True, text=True,
                         env={**os.environ, "MIHOME_DATA_DIR": DATA_DIR},
                         cwd=Path(__file__).resolve().parents[1], check=True)
    return out.stdout.strip() == "True"


network_down = NetworkError(
    "无法连接米家服务器：会话校验代理连接失败（api.mijia.tech）",
    stage="会话校验", host="api.mijia.tech", kind="代理连接失败")


def window_tests() -> None:
    # MainWindow 内部会构造 MijiaService：认证文件指到临时目录，绝不读真实凭据
    auth_dir = TemporaryDirectory()
    patch.object(service_mod, "_MijiaClient",
                 lambda: _MijiaClient(str(Path(auth_dir.name) / "auth.json"))).start()
    patch.object(QMessageBox, "exec", lambda self: 0).start()
    patch.object(QMessageBox, "critical", lambda *a, **k: 0).start()
    info_calls: list[int] = []
    patch.object(QMessageBox, "information",
                 lambda *a, **k: info_calls.append(1) or 0).start()
    patch("app.ui.login_dialog.LoginDialog", FakeLoginDialog).start()
    quit_calls: list[int] = []
    patch.object(type(app), "quit", lambda *a, **k: quit_calls.append(1)).start()
    settings_store.set_check_update_enabled(False)
    settings_store.set_minimize_to_tray(True)
    cache_loads: list[int] = []
    real_cache_load = device_cache.load
    patch.object(device_cache, "load",
                 lambda: (cache_loads.append(1), real_cache_load())[1]).start()

    gate_file = Path(DATA_DIR) / "login_state.json"
    check(login_gate.first_scan_required() and not gate_file.exists(), "初始应需要首次扫码")
    # 上次留下的设备缓存（可能来自别的账号）：首次扫码前一个都不该摆出来
    device_cache.save([device("old1", "缓存设备A"), device("old2", "缓存设备B")], {}, {})

    # 7 + 8. 已有有效凭据的首次启动 → 强制扫码；取消 → 不记录、退出
    fake = FakeService(verify=[LOGIN_OK], saved_credentials=True)
    win = new_window(fake)
    win.show()
    FakeLoginDialog.results = [FakeLoginDialog.Rejected]
    cache_loads.clear()
    win.start()
    wait_dialog(1)
    dlg = FakeLoginDialog.opened[0]
    check(dlg.force_new_qr, "首次登录的扫码窗必须强制出新二维码")
    check("verify_login" not in fake.calls, "首次扫码前不应拿已有凭据做登录校验")
    check(dlg.devices_shown == 0 and not cache_loads, "首次扫码前不应展示设备缓存")
    check("list_devices" not in dlg.calls_before, "首次扫码前不应加载设备")
    print("7. 已有有效凭据的首次启动 → 不校验、不摆缓存、强制新二维码 OK")
    pump(0.3)
    check(login_gate.first_scan_required() and not gate_file.exists(),
          "取消/失败不应记录完成状态")
    check(win._shutdown_done and quit_calls, "未完成扫码应退出程序")
    check(len(info_calls) == 1, "取消扫码应提示「未完成扫码登录，程序即将退出」")
    check("list_devices" not in fake.calls and not win._all_devices, "取消后不应加载设备")
    check(gate_in_new_process(), "另起进程读到的状态也应是「仍需首次扫码」")
    # 下次启动：仍然先扫码
    fake = FakeService(verify=[LOGIN_OK])
    win = new_window(fake)
    win.show()
    FakeLoginDialog.results = [FakeLoginDialog.Rejected]
    win.start()
    wait_dialog(2)
    check(FakeLoginDialog.opened[1].force_new_qr and "verify_login" not in fake.calls,
          "取消后下次启动仍需首次扫码")
    pump(0.3)
    # 扫码窗开着时按 ⌘Q（Quit 事件先给主窗口打退出标记）：照样退出、不记录，
    # 也不再弹「即将退出」的提示
    fake = FakeService()
    win = new_window(fake)
    win.show()
    FakeLoginDialog.results = [FakeLoginDialog.Rejected]
    FakeLoginDialog.during_exec = lambda w: w.prepare_for_quit()
    infos_before = len(info_calls)
    win.start()
    wait_dialog(3)
    pump(0.3)
    check(len(info_calls) == infos_before, "退出过程中关掉扫码窗不应再弹提示")
    check(login_gate.first_scan_required(), "⌘Q 关掉首次扫码窗也不记录完成状态")
    print("8. 首次扫码取消/失败/⌘Q → 不记录、退出；下次启动仍需扫码 OK")

    # 9. 首次扫码成功 → 记录、跳过缓存在线加载
    online = [device("d1", "开机卡"), device("d2", "台灯"), device("d3", "插座")]
    fake = FakeService(lists=[online])
    win = new_window(fake)
    win.show()
    FakeLoginDialog.results = [FakeLoginDialog.Accepted]
    cache_loads.clear()
    quits_before = len(quit_calls)
    win.start()
    wait_dialog(4)
    check(FakeLoginDialog.opened[3].force_new_qr, "仍应是首次扫码窗")
    pump(5, until=lambda: "list_devices" in fake.calls and not win._loading_devices)
    pump(0.3)
    check(not login_gate.first_scan_required() and gate_file.exists(), "成功后应记录完成状态")
    state = json.loads(gate_file.read_text())
    check(state.get("first_qr_login_done") is True and set(state) == {
        "version", "first_qr_login_done", "completed_at"}, f"完成标记只含状态与时间: {state}")
    check(not gate_in_new_process(), "另起进程应读到「已完成首次扫码」")
    check("verify_login" not in fake.calls, "首次扫码成功后不必再做登录校验")
    check(not cache_loads, "首次扫码后应跳过本地缓存、直接在线加载")
    check([d.name for d in win._all_devices] == ["开机卡", "台灯", "插座"]
          or sorted(d.name for d in win._all_devices) == sorted(["开机卡", "台灯", "插座"]),
          "应展示在线拉到的设备")
    check(win._session_ready and len(quit_calls) == quits_before, "登录成功不应退出")
    print("9. 首次扫码成功 → 记录完成状态（跨进程可读）、跳过缓存在线加载 OK")

    # 10. 完全退出再打开：凭据有效 → 自动登录
    win.prepare_for_quit()
    win.close()
    pump(0.2)
    fake = FakeService(verify=[LOGIN_OK])
    win = new_window(fake)
    win.show()
    win.start()
    pump(5, until=lambda: win._session_ready)
    pump(0.3)
    check(len(FakeLoginDialog.opened) == 4, "凭据有效时重启不应弹扫码窗")
    check(fake.calls.count("verify_login") == 1 and win._session_ready, "应自动登录")
    check(len(win._all_devices) == 3, "自动登录后应加载设备（缓存先显示）")
    print("10. 重启且凭据有效 → 自动登录、不弹扫码 OK")

    # 11. 凭据被服务器明确拒绝 → 重新扫码（普通模式）
    win.prepare_for_quit()
    win.close()
    pump(0.2)
    fake = FakeService(verify=[LOGIN_REQUIRED], lists=[online])
    win = new_window(fake)
    win.show()
    FakeLoginDialog.results = [FakeLoginDialog.Accepted]
    win.start()
    wait_dialog(5)
    check(not FakeLoginDialog.opened[4].force_new_qr, "凭据失效的重新扫码走普通模式")
    check(fake.calls[0] == "verify_login", "已完成首次扫码后应先做登录校验")
    pump(1)
    check(win._session_ready and not login_gate.first_scan_required(),
          "重新扫码成功后照常使用，完成状态保持")
    print("11. 凭据失效 → 重新扫码（普通模式）、成功后照常加载 OK")

    # 12. 凭据缺失：服务层判为需要扫码 → 同样打开扫码窗
    with TemporaryDirectory() as d:
        svc, _ = make_service(d, auth=None)
        check(svc.verify_login() == LOGIN_REQUIRED, "没有认证文件应需要扫码")
    win.prepare_for_quit()
    win.close()
    pump(0.2)
    fake = FakeService(verify=[LOGIN_REQUIRED], saved_credentials=False)
    win = new_window(fake)
    win.show()
    FakeLoginDialog.results = [FakeLoginDialog.Rejected]
    win.start()
    wait_dialog(6)
    check(not FakeLoginDialog.opened[5].force_new_qr, "凭据缺失的重新扫码走普通模式")
    pump(0.3)
    check(not login_gate.first_scan_required(), "取消重新扫码不应清掉首次完成状态")
    print("12. 凭据缺失 → 重新扫码 OK")

    # 13. 网络超时 / 代理连接失败 → 沿用重试流程
    win.prepare_for_quit()
    win.close()
    pump(0.2)
    fake = FakeService(verify=[network_down])
    win = new_window(fake)
    win.show()
    win.start()
    pump(5, until=lambda: not win._checking_login)
    pump(0.3)
    check(len(FakeLoginDialog.opened) == 6, "网络失败不应弹扫码窗")
    check(win._auto_retry_timer.isActive() and "凭据已保留" in win._status_hint.text(),
          "网络失败应进入现有的自动重试流程")
    check(not login_gate.first_scan_required(), "网络失败不影响完成状态")
    win._auto_retry_timer.stop()
    print("13. 网络超时/代理失败 → 沿用重试流程 OK")


class DialogService:
    """真实扫码窗用的假服务：第一次长轮询报「已确认但换会话失败」。"""

    def __init__(self):
        self.begin_calls: list[bool] = []
        self.wait_calls = 0

    def qr_login_begin(self, force_new: bool = False):
        self.begin_calls.append(force_new)
        if len(self.begin_calls) == 1:
            return {"loginUrl": "https://account.xiaomi.com/longPolling/login?ticket=t",
                    "lp": "https://lp.example.invalid/poll"}
        return None  # 用刚落盘的凭据刷新成功

    def qr_login_wait(self, _data):
        self.wait_calls += 1
        raise NetworkError("已扫码确认，但向米家服务器换取会话时网络失败：刷新登录凭据超时"
                           "（sts.api.mijia.tech）。登录凭据已保存，点击下方按钮重试即可，无需重新扫码",
                           stage="刷新登录凭据", host="sts.api.mijia.tech", kind="超时",
                           scan_confirmed=True)


def dialog_tests() -> None:
    # 14. 真实扫码窗：首次模式 → 已确认但换会话失败 → 重试走刷新
    service = DialogService()
    jobs = JobExecutor(None)
    dialog = RealLoginDialog(service, jobs, None, force_new_qr=True)
    dialog.show()
    dialog._begin_login()
    # 首次渲染二维码要加载 qrcode/PIL，冷启动时较慢：等待放宽到 15 秒
    pump(15, until=lambda: service.wait_calls == 1 and dialog._retry_btn.isVisible())
    check(service.begin_calls == [True], "首次模式应强制取新二维码")
    check(dialog._retry_btn.isVisible() and dialog._retry_btn.text().strip() == "重试",
          "换会话失败应给出「重试」"
          f"（窗口可见={dialog.isVisible()} 按钮隐藏={dialog._retry_btn.isHidden()}"
          f" 文案={dialog._retry_btn.text()!r} 长轮询={service.wait_calls}"
          f" 状态={dialog._status_text[:30]!r}）")
    check(not dialog._force_new_qr, "已确认扫码后应退出强制模式")
    dialog._retry_btn.click()
    pump(15, until=lambda: dialog.result() == RealLoginDialog.Accepted)
    check(service.begin_calls == [True, False], "重试应走刷新而不是再出新二维码")
    check(dialog.result() == RealLoginDialog.Accepted, "刷新成功后应完成登录")
    print("14. 首次模式「已确认、换会话失败」→ 重试走刷新、无需重扫 OK")

    # 15. 门禁只在 macOS 生效
    with TemporaryDirectory() as d:
        with (patch.dict(os.environ, {"MIHOME_DATA_DIR": d}),
              patch.object(login_gate, "ENABLED", False)):
            check(not login_gate.first_scan_required(), "非 macOS 不要求首次扫码")
            login_gate.mark_first_scan_done()
            check(not (Path(d) / "login_state.json").exists(), "非 macOS 不写完成标记")
    print("15. 门禁仅 macOS 生效 OK")


service_tests()
window_tests()
dialog_tests()
print("FIRST SCAN REGRESSION ALL PASS")
sys.stdout.flush()
# 直接退出：后台任务线程与离屏窗口不必逐个析构（同其他界面测试的做法）
os._exit(0)
