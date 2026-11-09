# SPDX-License-Identifier: GPL-3.0-or-later
"""登录校验与网络失败分类回归测试（全程离线，不碰真实凭据）。

对应 2026-11 macOS 实测故障：凭据字段齐全，但 api.mijia.tech 的 TLS
握手 10 秒超时被上游 `available` 吞成「未登录」，重开就弹扫码；sts 超时
又让二维码也取不到。锁死以下行为：

1. 本地无认证文件 → 需要扫码，且不发任何请求
2. 会话校验遇到传输层超时 → NetworkError，认证文件一字不改，不去刷新
3. 会话校验第一次超时、重试成功 → 登录有效
4. 服务器不认会话 → 用 passToken 刷新；刷新成功即登录有效
5. passToken 被拒 → 需要扫码；刷新时网络失败 → NetworkError，凭据保留
6. 会话字段缺失但有 passToken → 走刷新而不是扫码；构造客户端不报错
7. 服务端 5xx → 按服务端故障处理，不触发刷新
8. 重试策略：用户操作的只读请求重试一次；后台轮询不重试；
   写请求（开关、场景）绝不重发，报错明确说明「未自动重发」
9. 设备加载：家庭列表只拉一次，did 索引随列表更新，失败点名阶段与域名
10. 报错与日志不含 URL 参数（sts 票据）

用法: .venv/bin/python -m tests.network_login_test
"""

import json
import logging
from pathlib import Path
from tempfile import TemporaryDirectory
from threading import RLock
from unittest.mock import patch

from tests._isolate import isolate

isolate()

import requests  # noqa: E402
from mijiaAPI import APIError, mijiaAPI  # noqa: E402

from app.core.models import SceneInfo  # noqa: E402
from app.core.service import (  # noqa: E402
    LOGIN_OK,
    LOGIN_REQUIRED,
    MijiaService,
    NetworkError,
    _MijiaClient,
    _safe_reason,
    _wrap_error,
)

CHECK = "/v2/message/v2/check_new_msg"
HOMES = "/v2/homeroom/gethome_merged"
DEVICES = "/home/home_device_list"
SHARED = "/v2/home/device_list_page"
PROP_SET = "/miotspec/prop/set"
RUN_SCENE = "/appgateway/miot/appsceneservice/AppSceneService/NewRunScene"
SECRET = "TICKET-MUST-NOT-LEAK"

FULL_AUTH = {
    "ua": "test-ua", "pass_o": "o", "deviceId": "d",
    "ssecurity": "c2VjdXJpdHk=", "userId": "1", "cUserId": "c",
    "serviceToken": "old-service", "passToken": "pass",
}


def _no_network(*_args, **_kwargs):
    raise AssertionError("测试不应发出真实网络请求")


# 安全网：任何漏网的真实请求都直接判失败
patch.object(requests.Session, "send", _no_network).start()


def timeout(host: str = "api.mijia.tech") -> requests.exceptions.ReadTimeout:
    return requests.exceptions.ReadTimeout(
        f"HTTPSConnectionPool(host='{host}', port=443): "
        "Read timed out. (read timeout=25.0)")


def ticket_error() -> requests.exceptions.ConnectionError:
    url = f"https://sts.api.mijia.tech/sts?d=x&ticket={SECRET}&nonce={SECRET}"
    return requests.exceptions.ConnectionError(
        "HTTPSConnectionPool(host='sts.api.mijia.tech', port=443): Max retries "
        f"exceeded with url: /sts?d=x&ticket={SECRET}",
        request=requests.Request("GET", url).prepare())


class Script:
    """按 URI 编排上游 _request 的结果；calls 记录真正「发出」的请求。"""

    def __init__(self):
        self.calls: list[str] = []
        self.plan: dict[str, list] = {}

    def on(self, uri: str, *actions) -> None:
        self.plan.setdefault(uri, []).extend(actions)

    def fake_request(self, api, uri, data, refresh_token=True):
        # 与上游一致：先做会话校验/刷新，再发请求
        if refresh_token:
            api._refresh_token()
        self.calls.append(uri)
        actions = self.plan.get(uri)
        action = actions.pop(0) if actions else {}
        if isinstance(action, BaseException):
            raise action
        if isinstance(action, tuple) and action[0] == "status":
            api._last_status = action[1]
            raise json.JSONDecodeError("Expecting value", "", 0)
        return action


class Location:
    """编排上游 _get_location（serviceLogin → sts）的结果。"""

    def __init__(self, *results):
        self.results = list(results)
        self.calls = 0

    def fake(self, api):
        self.calls += 1
        result = self.results.pop(0)
        if isinstance(result, BaseException):
            raise result
        if result == "ok":
            api.auth_data["serviceToken"] = "new-service"
            return {"code": 0, "message": "刷新Token成功"}
        return {"sid": "mijia"}  # serviceLogin 不认 passToken：返回登录页参数


def make_service(directory: str, auth: dict | None = FULL_AUTH):
    path = Path(directory) / "auth.json"
    if auth is not None:
        path.write_text(json.dumps(auth))
    svc = object.__new__(MijiaService)
    svc._lock = RLock()
    svc._device_index = {}
    svc._spec_cache = {}
    svc._home_ids = None
    svc._api = _MijiaClient(str(path))
    return svc, path


def run(directory: str, script: Script, location: Location | None = None):
    """在编排好的上游行为下执行，返回 patch 上下文。"""
    location = location or Location()
    patches = [
        patch.object(mijiaAPI, "_request",
                     lambda api, uri, data, refresh_token=True:
                     script.fake_request(api, uri, data, refresh_token)),
        patch.object(mijiaAPI, "_get_location", lambda api: location.fake(api)),
    ]
    return patches


class Patched:
    def __init__(self, patches):
        self.patches = patches

    def __enter__(self):
        for p in self.patches:
            p.start()

    def __exit__(self, *exc):
        for p in reversed(self.patches):
            p.stop()


def check(cond: bool, message: str) -> None:
    if not cond:
        raise AssertionError(message)


class Capture(logging.Handler):
    def __init__(self):
        super().__init__(logging.DEBUG)
        self.messages: list[str] = []

    def emit(self, record):
        self.messages.append(record.getMessage())


capture = Capture()
logging.getLogger().addHandler(capture)
logging.getLogger().setLevel(logging.DEBUG)


def test_no_auth_file() -> None:
    with TemporaryDirectory() as d:
        svc, _ = make_service(d, auth=None)
        script, location = Script(), Location()
        with Patched(run(d, script, location)):
            check(svc.verify_login() == LOGIN_REQUIRED, "无认证文件应需要扫码")
        check(script.calls == [] and location.calls == 0, "无认证文件不应发请求")
    print("1. 无认证文件 → 扫码，不发请求 OK")


def test_timeout_keeps_credentials() -> None:
    with TemporaryDirectory() as d:
        svc, path = make_service(d)
        before = path.read_bytes()
        script, location = Script(), Location()
        script.on(CHECK, timeout(), timeout())
        with Patched(run(d, script, location)):
            try:
                svc.verify_login()
            except NetworkError as exc:
                error = exc
            else:
                raise AssertionError("会话校验超时应抛 NetworkError，而不是判成未登录")
        check(script.calls == [CHECK, CHECK], f"校验应重试一次: {script.calls}")
        check(location.calls == 0, "网络失败时不应去刷新凭据")
        check(path.read_bytes() == before, "网络失败不能改动认证文件")
        check("api.mijia.tech" in str(error) and error.stage == "会话校验",
              f"报错应点名阶段与域名: {error}")
    print("2. 会话校验超时 → NetworkError，凭据不动，不刷新 OK")


def test_retry_then_ok() -> None:
    with TemporaryDirectory() as d:
        svc, _ = make_service(d)
        script = Script()
        script.on(CHECK, timeout(), {"ok": 1})
        with Patched(run(d, script)):
            check(svc.verify_login() == LOGIN_OK, "重试成功应判定登录有效")
        check(script.calls == [CHECK, CHECK], f"应恰好两次校验: {script.calls}")
    print("3. 第一次超时、重试成功 → 登录有效 OK")


def test_rejected_session_refreshes() -> None:
    with TemporaryDirectory() as d:
        svc, path = make_service(d)
        script, location = Script(), Location("ok")
        script.on(CHECK, APIError(3, "auth err"))
        with Patched(run(d, script, location)):
            check(svc.verify_login() == LOGIN_OK, "刷新成功应判定登录有效")
        saved = json.loads(path.read_text())
        check(location.calls == 1 and saved["serviceToken"] == "new-service",
              "应用 passToken 刷新并落盘新会话")

        svc, _ = make_service(d)
        script, location = Script(), Location("rejected")
        script.on(CHECK, APIError(3, "auth err"))
        with Patched(run(d, script, location)):
            check(svc.verify_login() == LOGIN_REQUIRED, "passToken 被拒应需要扫码")

        svc, path = make_service(d, FULL_AUTH)
        before = path.read_bytes()
        script = Script()
        location = Location(timeout("sts.api.mijia.tech"), timeout("sts.api.mijia.tech"))
        script.on(CHECK, APIError(3, "auth err"))
        with Patched(run(d, script, location)):
            try:
                svc.verify_login()
            except NetworkError as exc:
                check("sts.api.mijia.tech" in str(exc) and exc.stage == "刷新登录凭据",
                      f"应点名刷新阶段与 sts 域名: {exc}")
            else:
                raise AssertionError("刷新超时应抛 NetworkError")
        check(location.calls == 2, "刷新应重试一次（每次重新走 serviceLogin）")
        check(path.read_bytes() == before, "刷新网络失败不能改动认证文件")
    print("4/5. 会话被拒→刷新；passToken 被拒→扫码；刷新超时→保留凭据 OK")


def test_partial_credentials() -> None:
    with TemporaryDirectory() as d:
        partial = {k: v for k, v in FULL_AUTH.items() if k != "serviceToken"}
        svc, _ = make_service(d, partial)  # 不应因缺字段在构造时抛 KeyError
        check(svc._api.missing_session_keys() == ["serviceToken"], "缺失字段判断错误")
        script, location = Script(), Location("ok")
        with Patched(run(d, script, location)):
            check(svc.verify_login() == LOGIN_OK, "有 passToken 时应刷新而不是扫码")
        check(script.calls == [] and location.calls == 1, "缺会话字段应直接刷新")
    print("6. 会话字段缺失但有 passToken → 刷新 OK")


def test_server_error_is_not_auth() -> None:
    with TemporaryDirectory() as d:
        svc, _ = make_service(d)
        script, location = Script(), Location()
        script.on(CHECK, ("status", 502), ("status", 502))
        with Patched(run(d, script, location)):
            try:
                svc.verify_login()
            except NetworkError as exc:
                check(exc.kind == "HTTP 502", f"应标明 HTTP 502: {exc.kind}")
            else:
                raise AssertionError("5xx 应按服务端故障处理")
        check(location.calls == 0, "5xx 不应触发刷新")
    print("7. 服务端 5xx → 服务端故障，不刷新 OK")


def test_retry_policy() -> None:
    with TemporaryDirectory() as d:
        svc, _ = make_service(d)
        api = svc._api
        script = Script()
        with Patched(run(d, script)):
            api.mark_session_valid()
            script.on(HOMES, timeout(), {"homelist": []})
            with api.read_retries(2):
                api.get_homes_list()
            check(script.calls.count(HOMES) == 2, "用户操作的只读请求应重试一次")

            script.calls.clear()
            script.on(HOMES, timeout())
            try:
                api.get_homes_list()
            except NetworkError:
                pass
            check(script.calls.count(HOMES) == 1, "后台轮询不应重试")

            # 开机卡这类「按一下」的设备：写请求超时绝不重发
            did = "lkrq-1"
            svc._device_index[did] = ("lkrq.test.v1", "urn:test:1", "开机卡")
            svc._spec_cache["urn:test:1"] = {"properties": [{
                "name": "on", "type": "bool", "rw": "rw",
                "method": {"siid": 2, "piid": 1}}]}
            script.calls.clear()
            script.on(PROP_SET, timeout())
            try:
                svc.set_power_state(did, True)
            except NetworkError as exc:
                check("未自动重发" in str(exc), f"写失败应说明未重发: {exc}")
            else:
                raise AssertionError("写超时应报 NetworkError")
            check(script.calls.count(PROP_SET) == 1, f"写请求被重发了: {script.calls}")

            script.calls.clear()
            script.on(HOMES, {"homelist": [{"id": "1001", "uid": 9, "name": "家"}]})
            script.on(RUN_SCENE, timeout())
            scene = SceneInfo(scene_id="s1", name="开电脑", home_id="1001", home_name="家")
            try:
                svc.run_scene(scene)
            except NetworkError as exc:
                check("未自动重发" in str(exc), f"场景失败应说明未重发: {exc}")
            else:
                raise AssertionError("场景执行超时应报 NetworkError")
            check(script.calls.count(RUN_SCENE) == 1, "场景执行请求被重发了")
    print("8. 只读重试一次 / 轮询不重试 / 写请求不重发 OK")


def test_device_loading() -> None:
    homes = {"homelist": [
        {"id": "1001", "uid": 9, "name": "家",
         "roomlist": [{"name": "书房", "dids": ["d1"]}]},
        {"id": "1002", "uid": 9, "name": "公司",
         "roomlist": [{"name": "工位", "dids": ["d2"]}]},
    ]}
    with TemporaryDirectory() as d:
        svc, _ = make_service(d)
        script = Script()
        script.on(HOMES, homes)
        script.on(DEVICES,
                  {"device_info": [{"did": "d1", "name": "开机卡", "model": "lkrq.x",
                                    "isOnline": True, "spec_type": "urn:a"}]},
                  {"device_info": [{"did": "d2", "name": "台灯", "model": "y",
                                    "isOnline": False}]})
        script.on(SHARED, {"list": [{"did": "d3", "name": "共享插座", "model": "z",
                                     "owner": True, "isOnline": True}]})
        with Patched(run(d, script)):
            svc._api.mark_session_valid()
            devices = svc.list_devices()
        check(script.calls.count(HOMES) == 1,
              f"家庭列表应只拉一次: {script.calls.count(HOMES)} 次")
        by_did = {dev.did: dev for dev in devices}
        check(set(by_did) == {"d1", "d2", "d3"}, f"设备不全: {set(by_did)}")
        check((by_did["d1"].home_name, by_did["d1"].room_name) == ("家", "书房"),
              "房间归属错误")
        check(set(svc._device_index) == {"d1", "d2", "d3"}, "did 索引未随列表更新")

        svc, _ = make_service(d)
        script = Script()
        script.on(HOMES, homes)
        script.on(DEVICES, timeout(), timeout())
        with Patched(run(d, script)):
            svc._api.mark_session_valid()
            try:
                svc.list_devices()
            except NetworkError as exc:
                check("获取设备列表" in str(exc) and "api.mijia.tech" in str(exc),
                      f"失败应点名阶段与域名: {exc}")
            else:
                raise AssertionError("设备列表超时应报 NetworkError")
    print("9. 家庭列表只拉一次、索引更新、失败点名阶段 OK")


def test_no_ticket_leak() -> None:
    check(SECRET not in str(_wrap_error(ticket_error(), "x")), "报错泄露了 URL 参数")
    check(SECRET not in _safe_reason(ticket_error()), "日志摘要泄露了 URL 参数")
    with TemporaryDirectory() as d:
        svc, _ = make_service(d)
        script, location = Script(), Location(ticket_error(), ticket_error())
        script.on(CHECK, APIError(3, "auth err"))
        capture.messages.clear()
        with Patched(run(d, script, location)):
            try:
                svc.verify_login()
            except NetworkError as exc:
                check(SECRET not in str(exc), "NetworkError 泄露了票据")
        leaked = [m for m in capture.messages if SECRET in m]
        check(not leaked, f"日志泄露了票据: {leaked}")
        check(any("sts.api.mijia.tech" in m for m in capture.messages),
              "日志应保留主机名以便定位")
    print("10. 报错与日志不含 URL 参数 OK")


def main() -> None:
    test_no_auth_file()
    test_timeout_keeps_credentials()
    test_retry_then_ok()
    test_rejected_session_refreshes()
    test_partial_credentials()
    test_server_error_is_not_auth()
    test_retry_policy()
    test_device_loading()
    test_no_ticket_leak()
    print("NETWORK LOGIN REGRESSION ALL PASS")


if __name__ == "__main__":
    main()
