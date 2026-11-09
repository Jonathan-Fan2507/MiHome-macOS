# SPDX-License-Identifier: GPL-3.0-or-later
"""扫码登录的失败恢复（全程离线，不碰真实凭据）。

1. 扫码已确认、sts 回调网络失败 → 用新 passToken 刷新补全会话，登录成功
2. 补全也网络失败 → NetworkError，已确认的凭据落盘，提示无需重新扫码；
   重开后直接走 passToken 刷新即可登录
3. 扫码确认之前链路就断了 → 恢复本轮之前的状态，提示重新获取二维码
4. 非网络失败（上游长轮询超时等）→ 恢复本轮之前的状态
5. 首次登录没有会话时取码：临时会话跟随登录跳转，用完撤掉

用法: .venv/bin/python -m tests.login_retry_test
"""

import json
from pathlib import Path
from tempfile import TemporaryDirectory
from threading import RLock
from unittest.mock import Mock, patch

from tests._isolate import isolate

isolate()

import requests  # noqa: E402
from mijiaAPI import LoginError, mijiaAPI  # noqa: E402

from app.core.service import (  # noqa: E402
    LOGIN_OK,
    MijiaService,
    NetworkError,
    ServiceError,
    _MijiaClient,
)


def _no_network(*_args, **_kwargs):
    raise AssertionError("测试不应发出真实网络请求")


# 安全网：任何漏网的真实请求都直接判失败
patch.object(requests.Session, "send", _no_network).start()

SCAN_RESULT = {"psecurity": "ps", "nonce": "n", "ssecurity": "c2VjdXJpdHk=",
               "passToken": "scanned-pass", "userId": "1", "cUserId": "c"}


def sts_timeout() -> requests.exceptions.ReadTimeout:
    return requests.exceptions.ReadTimeout(
        "HTTPSConnectionPool(host='sts.api.mijia.tech', port=443): "
        "Read timed out. (read timeout=25.0)")


def make_service(directory: str) -> MijiaService:
    service = object.__new__(MijiaService)
    service._lock = RLock()
    service._api = _MijiaClient(str(Path(directory) / "auth.json"))
    return service


def scanned_then(exc: BaseException):
    """模拟上游：长轮询拿到扫码结果写进内存，随后 sts 回调失败。"""
    def fake(api, _login_data):
        api.auth_data.update(SCAN_RESULT)
        raise exc
    return fake


def location_results(*results):
    """编排 _get_location（serviceLogin → sts）：异常或 "ok"。"""
    calls = []

    def fake(api):
        calls.append(1)
        result = results[len(calls) - 1]
        if isinstance(result, BaseException):
            raise result
        api.auth_data["serviceToken"] = "fresh-service"
        return {"code": 0, "message": "刷新Token成功"}
    return fake, calls


def main() -> None:
    # 1. 回调失败 → passToken 补全成功
    with TemporaryDirectory() as d:
        service = make_service(d)
        api = service._api
        refresh, calls = location_results("ok")
        with (patch.object(mijiaAPI, "_complete_qr_login", scanned_then(sts_timeout())),
              patch.object(mijiaAPI, "_get_location", refresh)):
            service.qr_login_wait({})
        saved = json.loads((Path(d) / "auth.json").read_text())
        assert len(calls) == 1, "回调失败后应改用 passToken 刷新一次"
        assert saved["passToken"] == "scanned-pass"
        assert saved["serviceToken"] == "fresh-service"
        assert api.available, "补全后会话应可用（命中校验缓存，不再发请求）"
    print("1. 扫码已确认、回调超时 → passToken 补全会话 OK")

    # 2. 补全也失败 → 凭据落盘，重开直接刷新，无需重扫
    with TemporaryDirectory() as d:
        service = make_service(d)
        api = service._api
        refresh, calls = location_results(sts_timeout(), sts_timeout())
        with (patch.object(mijiaAPI, "_complete_qr_login", scanned_then(sts_timeout())),
              patch.object(mijiaAPI, "_get_location", refresh)):
            try:
                service.qr_login_wait({})
            except NetworkError as exc:
                assert "无需重新扫码" in str(exc), str(exc)
            else:
                raise AssertionError("补全失败应提示重试")
        assert len(calls) == 2, "补全刷新应重试一次"
        saved = json.loads((Path(d) / "auth.json").read_text())
        assert saved["passToken"] == "scanned-pass", "已确认的扫码凭据应落盘"
        assert not hasattr(api, "session"), "失败后不应残留临时会话"

        # 重开：认证文件缺 serviceToken 也能构造，并直接走刷新完成登录
        service = make_service(d)
        refresh, calls = location_results("ok")
        with patch.object(mijiaAPI, "_get_location", refresh):
            assert service.verify_login() == LOGIN_OK, "落盘的扫码凭据应能直接刷新登录"
        assert len(calls) == 1
    print("2. 补全失败 → 凭据落盘，重开直接刷新 OK")

    # 3. 扫码确认前断线 → 恢复原状
    with TemporaryDirectory() as d:
        service = make_service(d)
        api = service._api
        before = dict(api.auth_data)

        def broken_poll(_api, _data):
            raise requests.exceptions.ConnectionError(
                "HTTPSConnectionPool(host='account.xiaomi.com', port=443)")

        with patch.object(mijiaAPI, "_complete_qr_login", broken_poll):
            try:
                service.qr_login_wait({})
            except NetworkError as exc:
                assert "重新获取二维码" in str(exc), str(exc)
            else:
                raise AssertionError("长轮询断线应提示重试")
        assert api.auth_data == before
        assert not (Path(d) / "auth.json").exists()
    print("3. 扫码确认前断线 → 恢复原状 OK")

    # 4. 非网络失败 → 恢复原状（不拿半套凭据去刷新）
    with TemporaryDirectory() as d:
        service = make_service(d)
        api = service._api
        with patch.object(mijiaAPI, "_complete_qr_login",
                          scanned_then(LoginError(-1, "超时，请重试"))):
            try:
                service.qr_login_wait({})
            except NetworkError:
                raise AssertionError("长轮询超时不是网络故障")
            except ServiceError:
                pass
            else:
                raise AssertionError("扫码未完成应提示重试")
        assert "passToken" not in api.auth_data
        assert not hasattr(api, "session")
    print("4. 非网络失败 → 恢复原状 OK")

    # 5. 首次登录取码：临时会话跟随登录跳转，用完撤掉
    with TemporaryDirectory() as d:
        service = make_service(d)
        api = service._api
        redirect = Mock(status_code=401)
        replies = [
            {"code": 0, "location": "https://account.xiaomi.com/example?sid=mijia"},
            {"loginUrl": "https://account.xiaomi.com/example-qr"},
        ]
        with (
            patch("requests.get"),
            patch.object(requests.Session, "get", return_value=redirect) as follow,
            patch.object(api, "_handle_ret", side_effect=replies),
        ):
            data = service.qr_login_begin()
        assert follow.call_count == 1
        assert data == replies[1]
        assert not hasattr(api, "session")
    print("5. 首次取码临时会话 OK")

    print("LOGIN RETRY REGRESSION ALL PASS")


if __name__ == "__main__":
    main()
