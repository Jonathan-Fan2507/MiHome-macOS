# SPDX-License-Identifier: GPL-3.0-or-later
# MiHome-Windows: 米家设备的 Windows 桌面控制端
# Copyright (C) 2026 MiHome-Windows contributors
"""mijiaAPI 适配层——全项目唯一允许 import mijiaAPI 的模块。

上游库只从 PyPI 安装升级（版本锁 5.x，见 pyproject.toml），接口一旦变化
只需要修改这里；界面层与线程层完全不感知第三方类型的存亡。

依赖的上游半公开方法说明：扫码登录拆成 _get_qr_login_data 与
_complete_qr_login 两步使用，是因为上游的 login() 会把二维码打印到
终端，图形界面拿不到；官方 MCP server 也采用同样的两步组合。

设备规格自上游 5.x 起按 spec_type（云端下发的规格 URN）获取，
取代旧版按 model 在 home.miot-spec.com 抓页解析的方式。
"""

import contextlib
import functools
import json
import logging
import re
import threading
import time
from concurrent.futures import ThreadPoolExecutor
from pathlib import Path
from typing import Any
from urllib.parse import urlsplit

import mijiaAPI as _mijia_api_pkg
import requests
from mijiaAPI import (
    DeviceNotFoundError,
    GetDeviceInfoError,
    LoginError,
    get_device_info,
    mijiaAPI,
    mijiaDevice,
)
from mijiaAPI.devices import DevAction, DevProp
from mijiaAPI.miutils import generate_enc_params, gen_nonce, get_signed_nonce

from . import icon_store, name_store, spec_zh
from .models import ActionInfo, DeviceDetail, DeviceInfo, PropInfo, SceneInfo
from .net_guard import install_default_timeout

# 上游多处 HTTP 调用没有 timeout（登录取二维码、刷新 token、设备列表），
# 网络不通时会无限等待、界面永远转圈。在所有网络调用发生前补上默认超时。
install_default_timeout()

logger = logging.getLogger(__name__)

# 单次批量属性读取的设备数上限，防止请求体过大被网关拒绝。
# 32 台在线设备时：15 → 3 次请求，20 → 2 次请求；每次往返约 150~600ms，
# 少一次就少几百毫秒的显示延迟。
_BATCH_SIZE = 20
# 虚拟子设备的 did 后缀：<父did>.sN，N 为该路对应的 siid
_SUB_DEVICE_SUFFIX = re.compile(r"\.s(\d+)$")

# 云端对「规格里有、设备实际不支持」的属性返回的错误码。这类响应属于
# 规格与实际能力不符，按调试信息记录即可，不必当成读取故障
_UNSUPPORTED_PROP_CODES = {-704002000}
# spec 并发拉取线程数；目标是 miot-spec.org 的独立请求，无会话竞争
_SPEC_WORKERS = 8


class ServiceError(Exception):
    """携带用户可直接阅读的中文信息，界面层直接展示 message 即可。"""


class NetworkError(ServiceError):
    """链路故障：超时、连接被重置、代理或 TLS 握手失败、服务器 5xx。

    与「凭据失效」严格区分——网络失败时本地凭据原样保留，界面提示
    重试，而不是把用户赶回扫码页。stage/host/kind 只用于日志与提示
    定位，不含任何 URL 参数、Cookie 或 Token。
    """

    def __init__(self, message: str, *, stage: str = "", host: str = "",
                 kind: str = "", scan_confirmed: bool = False) -> None:
        super().__init__(message)
        self.stage = stage
        self.host = host
        self.kind = kind
        # 手机上已确认扫码、只是换取会话时网络失败：凭据已落盘，重试走
        # passToken 刷新即可，不必再出新二维码
        self.scan_confirmed = scan_confirmed


class AuthRequiredError(ServiceError):
    """本地没有可用凭据，或凭据已被服务器明确拒绝：只有这种情况才需要扫码。"""


# 登录校验结论（verify_login 的返回值）
LOGIN_OK = "ok"
LOGIN_REQUIRED = "login_required"

# 只把传输层故障算作网络问题：拿到了 HTTP 响应（哪怕是错误码）说明链路
# 是通的。SSLError / ProxyError / ConnectTimeout 都是 ConnectionError 的子类。
_TRANSPORT_ERRORS = (
    requests.exceptions.ConnectionError,
    requests.exceptions.Timeout,
    requests.exceptions.ChunkedEncodingError,
)

# 只读接口：在「用户主动发起」的操作里，传输层失败允许重发一次。
# 写接口（属性写入、动作、场景）一律不自动重发：超时时请求可能已经送达
# 设备，重发会重复触发（例如开机卡的「按电源键」被按两次）。
_IDEMPOTENT_URIS = frozenset({
    "/v2/message/v2/check_new_msg",
    "/v2/homeroom/gethome_merged",
    "/home/home_device_list",
    "/v2/home/device_list_page",
    "/miotspec/prop/get",
    "/appgateway/miot/appsceneservice/AppSceneService/GetSimpleSceneList",
    "/v2/home/standard_consumable_items",
    "/v2/user/statistics",
})

# URI -> 日志与提示里的阶段名
_STAGE_BY_URI = {
    "/v2/message/v2/check_new_msg": "会话校验",
    "/v2/homeroom/gethome_merged": "获取家庭列表",
    "/home/home_device_list": "获取设备列表",
    "/v2/home/device_list_page": "获取共享设备列表",
    "/miotspec/prop/get": "读取设备属性",
    "/miotspec/prop/set": "写入设备属性",
    "/miotspec/action": "执行设备动作",
    "/appgateway/miot/appsceneservice/AppSceneService/GetSimpleSceneList": "获取场景列表",
    "/appgateway/miot/appsceneservice/AppSceneService/NewRunScene": "执行场景",
    "/v2/home/standard_consumable_items": "获取耗材列表",
    "/v2/user/statistics": "获取统计数据",
}

# 会话凭据的必备字段。ua 由本层每次启动重建（见 _normalize_api_locale），
# passToken 是换取会话的长期凭据，单独判断
_SESSION_KEYS = ("ssecurity", "userId", "cUserId", "serviceToken")


def _host_of(exc: BaseException) -> str:
    """从 requests 异常里只取主机名；绝不返回路径与查询参数（含票据）。"""
    request = getattr(exc, "request", None)
    url = getattr(request, "url", None)
    if url:
        try:
            host = urlsplit(url).hostname
        except ValueError:
            host = None
        if host:
            return host
    matched = re.search(r"host='([^']+)'", str(exc))
    return matched.group(1) if matched else ""


def _transport_kind(exc: BaseException) -> str:
    if isinstance(exc, requests.exceptions.ConnectTimeout):
        return "连接超时"
    if isinstance(exc, requests.exceptions.ReadTimeout):
        # urllib3 把 TLS 握手阶段的超时也报成 read timeout（数值是连接超时）
        return "超时（建立加密连接或等待响应）"
    if isinstance(exc, requests.exceptions.ProxyError):
        return "代理连接失败"
    if isinstance(exc, requests.exceptions.SSLError):
        return "TLS 握手失败"
    if isinstance(exc, requests.exceptions.ChunkedEncodingError):
        return "响应中途断开"
    if isinstance(exc, requests.exceptions.ConnectionError):
        return "连接中断"
    if isinstance(exc, requests.exceptions.Timeout):
        return "超时"
    return type(exc).__name__


def _network_error(exc: BaseException, stage: str) -> NetworkError:
    host = _host_of(exc)
    kind = _transport_kind(exc)
    where = f"（{host}）" if host else ""
    return NetworkError(f"{stage}{kind}{where}", stage=stage, host=host, kind=kind)


def _safe_reason(exc: BaseException) -> str:
    """日志用的异常摘要：传输层异常只给类型与主机，不把带票据的 URL 写进日志。"""
    if isinstance(exc, _TRANSPORT_ERRORS):
        return f"{type(exc).__name__} host={_host_of(exc)}"
    if isinstance(exc, NetworkError):
        return f"{exc.kind} host={exc.host} 阶段={exc.stage}"
    text = "; ".join(str(a) for a in exc.args)[:160]
    return f"{type(exc).__name__}: {text}"


def _http_status_of(exc: BaseException) -> int | None:
    """上游 LoginError 把 HTTP 状态码拼进了文案（code: 502, message: ...）。"""
    matched = re.match(r"code: (\d{3}),", str(exc))
    return int(matched.group(1)) if matched else None


def _normalize_api_locale(api) -> None:
    """把系统 locale 规范成上游认识的语言标记。

    Windows 的 locale.getlocale() 返回 "Chinese (Simplified)_China"
    这类显示名，上游只校验是否含下划线就拼进 User-Agent，服务端
    认不出语言，设备列表的 name 会退回英文商品名。这里统一映射成
    标准的 zh_CN / en_US，并清掉按旧值缓存的 UA 让它重建。
    """
    raw = str(getattr(api, "locale", "") or "").strip()
    lowered = raw.lower()
    if "chinese" in lowered or lowered.startswith("zh"):
        # 繁体系统要保留 zh_TW，不能一律当简体
        if ("traditional" in lowered or "taiwan" in lowered
                or lowered.endswith("_tw") or lowered.endswith("_hk")):
            normalized = "zh_TW"
        else:
            normalized = "zh_CN"
    elif "english" in lowered or lowered.startswith("en"):
        normalized = "en_US"
    else:
        normalized = "zh_CN"
    api.locale = normalized
    # 不比较新旧值：历史会话可能把错误语言拼进 UA 并留在认证文件里，
    # 每次启动都重建一次请求头，才能保证服务端拿到正确语言。
    # 注意必须立刻读一次 user_agent 让它重新生成：available 会检查
    # auth_data 里有没有 ua 字段，pop 掉不补会被判成未登录。
    try:
        api.auth_data.pop("ua", None)
        _ = api.user_agent
    except Exception:
        pass


def _wrap_error(exc: Exception, context: str, *, write: bool = False) -> ServiceError:
    """把底层异常转成界面可读的 ServiceError，并保留网络/凭据两类语义。

    传输层异常的原文里可能带完整 URL（含 sts 票据），一律只保留阶段、
    故障类型与主机名。write=True 的失败要明确告诉用户「没有自动重发」，
    避免对开机卡这类「按一下」的设备连点两次。
    """
    if isinstance(exc, _TRANSPORT_ERRORS):
        exc = _network_error(exc, "网络请求")
    if isinstance(exc, NetworkError):
        hint = "。指令未自动重发，请先确认设备状态再操作" if write else ""
        return NetworkError(f"{context}：{exc}{hint}", stage=exc.stage,
                            host=exc.host, kind=exc.kind)
    if isinstance(exc, AuthRequiredError):
        return AuthRequiredError(f"{context}：{exc}")
    # 上游异常的 args 结构各不相同，原样拼接足够定位问题又不至于暴露噪音；
    # 调用方统一用 raise ... from exc 保持原始堆栈链
    detail = "; ".join(str(a) for a in exc.args)
    return ServiceError(f"{context}: {detail}")


def _serialized(method):
    """把服务方法串到实例锁上，避免多线程同时碰上游客户端。"""
    @functools.wraps(method)
    def wrapper(self, *args, **kwargs):
        with self._lock:
            return method(self, *args, **kwargs)

    return wrapper


def _log_network_failure(err: NetworkError, started: float, attempt: int,
                         attempts: int, quiet: bool = False) -> None:
    """记录一次传输层失败（只含阶段、主机、类型与用时）。

    quiet 用于后台轮询：任务执行器已经会记一条带阶段与主机的警告，
    这里再按警告记会把日志刷成两倍，降为调试级别。
    """
    action = "将重试" if attempt < attempts else "放弃"
    logger.log(
        logging.DEBUG if quiet else logging.WARNING,
        "网络失败（%s）: 阶段=%s host=%s 类型=%s 用时=%.1fs 第 %d/%d 次",
        action, err.stage, err.host or "?", err.kind,
        time.monotonic() - started, attempt, attempts)


class _MijiaClient(mijiaAPI):
    """上游客户端 + 「网络失败 ≠ 未登录」的区分。

    上游 `available` 把会话校验时的**任何**异常（包括代理链路超时）都当成
    凭据失效，随后用 passToken 走 sts 刷新，刷新再超时就只剩扫码一条路——
    这正是「明明有凭据，重开却又弹二维码」的根因（实测：凭据字段齐全，
    api.mijia.tech 握手 10 秒超时被吞成 available=False）。这里改为：

    - 传输层失败（超时/重置/代理/TLS）与服务端 5xx 抛 NetworkError，凭据不动；
    - 服务器给了响应却不认会话，才用 passToken 刷新；
    - 刷新被服务器明确拒绝才抛 AuthRequiredError，由界面进入扫码。

    另外两处减负：成功的接口调用顺延会话校验缓存（轮询期间不再每分钟
    额外校验一次）；同一轮设备加载复用家庭列表，不再每翻一页就重拉一遍。
    """

    # 用户主动发起的操作里，只读请求遇到传输层失败的总尝试次数；后台
    # 轮询保持 1——下一轮自会再来，重发只会把控制队列堵得更久
    _read_attempts = 1
    # 本轮操作内复用的家庭列表快照（见 homes_snapshot）
    _homes_snapshot: list | None = None
    # 最近一次响应的 HTTP 状态码（会话钩子记录），用来识别服务端 5xx
    _last_status: int | None = None
    # 只取二维码时带给 serviceLogin 的设备身份字段（见 fresh_qr_login_data）
    _QR_IDENTITY_KEYS = ("deviceId", "pass_o", "ua")
    # 取码探针进行中：此时 auth_data 只是探针，绝不能落盘
    _qr_probe_active = False

    def _init_session(self) -> None:
        try:
            super()._init_session()
        except KeyError as exc:
            # 认证文件缺会话字段（例如扫码回调中断后只落了 passToken）：
            # 先给一个空会话，稍后由 refresh_session 用 passToken 补齐，
            # 而不是把整份认证文件当成损坏隔离掉、逼用户重新扫码
            logger.warning("认证文件缺少会话字段 %s，将尝试用 passToken 刷新", exc)
            self.session = requests.Session()
        self.session.hooks["response"].append(self._remember_status)

    def _remember_status(self, response, *args, **kwargs) -> None:
        self._last_status = response.status_code

    def missing_session_keys(self) -> list[str]:
        """缺失的会话字段名（只返回字段名，不涉及取值）。"""
        return [key for key in _SESSION_KEYS if not self.auth_data.get(key)]

    @contextlib.contextmanager
    def read_retries(self, attempts: int = 2):
        previous = self._read_attempts
        self._read_attempts = max(previous, attempts)
        try:
            yield
        finally:
            self._read_attempts = previous

    @contextlib.contextmanager
    def homes_snapshot(self, homes: list):
        previous = self._homes_snapshot
        self._homes_snapshot = homes
        try:
            yield
        finally:
            self._homes_snapshot = previous

    @contextlib.contextmanager
    def temporary_session(self):
        """首次登录时还没有会话，而刷新/取码流程要跟随登录跳转：临时给一个。

        流程里若建立了正式会话（_init_session 替换了 session）就保留它；
        否则撤掉临时会话，保持上游「有会话 ⇔ 有凭据」的约定。
        """
        if hasattr(self, "session"):
            yield
            return
        bootstrap = self.session = requests.Session()
        try:
            yield
        finally:
            if getattr(self, "session", None) is bootstrap:
                del self.session
            bootstrap.close()

    def mark_session_valid(self) -> None:
        self._available_cache = True
        self._available_cache_time = int(time.time())

    def forget_session_check(self) -> None:
        self._available_cache = None
        self._available_cache_time = 0

    @property
    def available(self) -> bool:
        if "ua" not in self.auth_data or self.missing_session_keys():
            return False
        if (self._available_cache
                and int(time.time()) - self._available_cache_time < 60):
            return True
        try:
            self.check_new_msg(refresh_token=False)
        except NetworkError:
            self.forget_session_check()
            raise
        except Exception as exc:
            self.forget_session_check()
            logger.info("会话校验未通过（%s），需要用 passToken 刷新", _safe_reason(exc))
            return False
        self.mark_session_valid()
        return True

    def fresh_qr_login_data(self) -> dict:
        """不拿本地 passToken 试刷新，直接取一张新的登录二维码。

        上游 _get_qr_login_data 先带着 passToken 走 serviceLogin，凭据有效就
        直接换会话、不出二维码。macOS 首次登录要求扫码确认，所以取码时让
        serviceLogin 只看到设备身份（deviceId/pass_o/ua，与之后长轮询用的是
        同一套），看不到 passToken/userId，它就给出扫码登录的跳转——与上游
        「本机没有认证文件」时的首次登录同一条路。真实凭据只在内存里暂时
        让位、取码结束立即还原，认证文件全程不写。
        """
        saved = self.auth_data
        probe = {key: saved[key] for key in self._QR_IDENTITY_KEYS if key in saved}
        self.auth_data = probe
        self._qr_probe_active = True
        try:
            data = self._get_qr_login_data()
        finally:
            self._qr_probe_active = False
            self.auth_data = saved
            # 没有认证文件时设备身份是取码途中才生成的：并回正式数据，
            # 保证长轮询与取码用同一套身份
            for key in self._QR_IDENTITY_KEYS:
                if key in probe and key not in saved:
                    saved[key] = probe[key]
        if data.get("refreshed") or not data.get("loginUrl"):
            raise ServiceError("服务器没有返回登录二维码")
        return data

    def _save_auth_data(self) -> None:
        if self._qr_probe_active:
            # 探针只含设备身份，写下去会冲掉与 CLI 共用的认证文件
            raise ServiceError("取二维码过程中拒绝写入认证文件")
        super()._save_auth_data()

    def _refresh_token(self) -> dict:
        if self.available:
            return self.auth_data
        return self.refresh_session()

    def refresh_session(self) -> dict:
        """用 passToken 走上游 serviceLogin → sts 流程换新会话。

        传输层失败或服务端 5xx 抛 NetworkError（凭据不动，可重试）；
        passToken 被拒抛 AuthRequiredError。sts 地址带一次性票据，重试时
        重新走 serviceLogin 拿新地址，而不是重放旧地址。
        """
        if not self.auth_data.get("passToken"):
            raise AuthRequiredError("本地没有可用于刷新的登录凭据，请扫码登录")
        attempts = max(1, self._read_attempts)
        attempt = 0
        while True:
            attempt += 1
            started = time.monotonic()
            try:
                refreshed = self._refresh_once()
            except NetworkError as err:
                _log_network_failure(err, started, attempt, attempts,
                                     quiet=attempts <= 1)
                if attempt < attempts:
                    continue
                raise
            break
        if not refreshed:
            logger.info("刷新登录凭据被拒绝：passToken 已失效")
            raise AuthRequiredError("登录凭据已失效，请重新扫码登录")
        self.mark_session_valid()
        logger.info("刷新登录凭据成功（%.1fs，第 %d/%d 次）",
                    time.monotonic() - started, attempt, attempts)
        return self.auth_data

    def _refresh_once(self) -> bool:
        try:
            with self.temporary_session():
                location = self._get_location()
                if (location.get("code", -1) != 0
                        or location.get("message", "") != "刷新Token成功"):
                    return False
                self._save_auth_data()
                self._init_session()
                return True
        except _TRANSPORT_ERRORS as exc:
            raise _network_error(exc, "刷新登录凭据") from exc
        except LoginError as exc:
            status = _http_status_of(exc)
            if status is not None and status >= 500:
                raise NetworkError(
                    f"刷新登录凭据失败：服务器暂时不可用（HTTP {status}）",
                    stage="刷新登录凭据", host=urlsplit(self.login_url).hostname or "",
                    kind=f"HTTP {status}") from exc
            logger.info("刷新登录凭据被拒绝（%s）", _safe_reason(exc))
            raise AuthRequiredError("登录凭据已失效，请重新扫码登录") from exc

    def _request(self, uri: str, data: dict, refresh_token: bool = True) -> dict:
        stage = _STAGE_BY_URI.get(uri, "请求米家接口")
        idempotent = uri in _IDEMPOTENT_URIS
        attempts = self._read_attempts if idempotent else 1
        attempt = 0
        while True:
            attempt += 1
            started = time.monotonic()
            self._last_status = None
            try:
                result = super()._request(uri, data, refresh_token=refresh_token)
            except (NetworkError, AuthRequiredError):
                # 会话校验/刷新阶段已经分类并记录过，原样上抛
                raise
            except _TRANSPORT_ERRORS as exc:
                cause: BaseException = exc
                err = _network_error(exc, stage)
            except Exception as exc:
                status = self._last_status
                if status is None or status < 500:
                    # 拿到了正常响应却解析/校验失败：多半是会话被拒，
                    # 下一次请求前重新校验，而不是继续信任 60 秒缓存
                    self.forget_session_check()
                    raise
                cause = exc
                err = NetworkError(
                    f"{stage}失败：服务器暂时不可用（HTTP {status}）", stage=stage,
                    host=urlsplit(self.api_base_url).hostname or "",
                    kind=f"HTTP {status}")
            else:
                # 任何一次成功的接口调用都证明会话有效：顺延上游 60 秒的
                # 校验缓存，轮询期间不必每分钟额外做一次会话校验
                self.mark_session_valid()
                return result
            _log_network_failure(err, started, attempt, attempts,
                                 quiet=idempotent and attempts <= 1)
            if attempt >= attempts:
                raise err from cause

    def _get_home_owner(self, home_id) -> int:
        for home in self._homes_snapshot or ():
            if str(home.get("id")) == str(home_id):
                return int(home["uid"])
        return super()._get_home_owner(home_id)


class MijiaService:
    def __init__(self):
        # 服务实例被多个后台队列共享（控制队列与网络队列各一个线程），
        # 内部的 mijiaAPI 客户端与各类缓存都不是线程安全的：requests 的
        # 连接池与 SSL 上下文并发使用会导致 C 层访问越界，表现为进程
        # 直接闪退。这里用一把可重入锁把会发网络请求的方法串起来，
        # 重入是因为部分方法内部会互相调用。
        # 只读缓存的方法不加锁：它们被主线程高频调用，一旦排队到慢
        # 请求后面会把界面卡住，而字典读取本身有 GIL 保护。
        self._lock = threading.RLock()
        # 认证文件沿用上游默认位置 ~/.config/mijia-api/auth.json，
        # 这样 CLI 里扫过的码在 GUI 直接生效，反之亦然
        self._api = self._init_api()
        self._device_cache: dict[str, mijiaDevice] = {}
        # did -> (model, spec_type, name) 索引，批量读状态与共享设备组装时
        # 避免反复拉设备列表；自有与共享设备都在其中
        self._device_index: dict[str, tuple[str, str, str]] = {}
        # spec_type -> spec 内存缓存，轮询命中后不再读文件/打网络
        self._spec_cache: dict[str, dict | None] = {}
        # 产品页中文名缓存：model -> 中文名或 None（已确认无中文名，不再重查）。
        # 成功解析的名字从磁盘恢复，跨会话复用；失败项只在本会话记忆
        self._product_page_names: dict[str, str | None] = dict(name_store.load())
        # model -> 图标 URL 或 None（拉取失败，会话内不再重试）；
        # 启动即从磁盘加载，此后只增不删（见 icon_store）
        self._icon_cache: dict[str, str | None] = icon_store.load_urls()
        # 家庭名 -> 家庭 ID 映射，场景列表按家庭过滤用；懒加载缓存
        self._home_ids: dict[str, str] | None = None

    def _init_api(self) -> "_MijiaClient":
        """构造上游客户端；认证文件损坏时隔离坏文件并降级为未登录。

        上游构造函数同步读取并解析 auth.json 且写入非原子，进程被杀
        可能留下半截文件；不在这里接住，异常会穿透到 QApplication
        之前，用户只会看到进程无声退出。缺会话字段不算损坏（见
        _MijiaClient._init_session），留给 passToken 刷新补齐。
        """
        try:
            api = _MijiaClient()
            _normalize_api_locale(api)
            return api
        except (json.JSONDecodeError, KeyError, TypeError, OSError) as exc:
            auth_path = Path.home() / ".config" / "mijia-api" / "auth.json"
            try:
                auth_path.replace(auth_path.with_name("auth.json.corrupt"))
            except OSError:
                pass
            logger.warning("认证文件损坏已隔离为 auth.json.corrupt，请重新扫码登录: %s",
                           type(exc).__name__)
            # 文件已移除，上游按「无认证文件」处理，available 恒为 False
            api = _MijiaClient()
            _normalize_api_locale(api)
            return api

    @staticmethod
    def api_version() -> str:
        """上游 mijiaAPI 的实际安装版本（关于页展示）。

        运行时从包元信息读取而非从依赖清单抄写，包升级/降级后
        界面展示自动跟随；取不到时返回空串由调用方决定是否展示。
        """
        return getattr(_mijia_api_pkg, "__version__", "")

    # ---------- 登录 ----------

    @_serialized
    def verify_login(self) -> str:
        """启动登录校验：严格区分「需要扫码」与「网络暂时不通」。

        返回 LOGIN_OK（会话可用，必要时已用 passToken 刷新）或
        LOGIN_REQUIRED（本地无凭据，或凭据被服务器明确拒绝）；链路故障抛
        NetworkError，凭据原样保留，由界面提示重试——绝不因为超时就让用户
        重新扫码。日志逐阶段记录，只写字段名、阶段、主机与用时。
        """
        api = self._api
        started = time.monotonic()
        if not api.auth_data_path.exists():
            logger.info("登录校验: 未找到本地认证文件 → 需要扫码")
            return LOGIN_REQUIRED
        missing = api.missing_session_keys()
        if missing and not api.auth_data.get("passToken"):
            logger.info("登录校验: 本地凭据不完整（缺少 %s）且没有 passToken → 需要扫码",
                        "、".join(missing))
            return LOGIN_REQUIRED
        try:
            with api.read_retries(2):
                if not missing:
                    # 启动时总要真实校验一次，不信任进程内缓存
                    api.forget_session_check()
                    if api.available:
                        logger.info("登录校验: 会话有效（%.1fs）",
                                    time.monotonic() - started)
                        return LOGIN_OK
                    logger.info("登录校验: 会话未通过服务器校验 → 用 passToken 刷新")
                else:
                    logger.info("登录校验: 会话字段不全（缺少 %s）→ 用 passToken 刷新",
                                "、".join(missing))
                api.refresh_session()
        except NetworkError as exc:
            logger.warning(
                "登录校验: 网络失败，本地凭据已保留，等待重试"
                "（阶段=%s host=%s 类型=%s 总用时 %.1fs）",
                exc.stage, exc.host or "?", exc.kind, time.monotonic() - started)
            raise NetworkError(f"无法连接米家服务器：{exc}", stage=exc.stage,
                               host=exc.host, kind=exc.kind) from exc
        except AuthRequiredError as exc:
            logger.info("登录校验: %s → 需要扫码", exc)
            return LOGIN_REQUIRED
        logger.info("登录校验: 已用 passToken 刷新会话（总用时 %.1fs）",
                    time.monotonic() - started)
        return LOGIN_OK

    def has_saved_credentials(self) -> bool:
        """本机认证文件里是否已有登录凭据（只看字段在不在，不读取值）。"""
        api = self._api
        return api.auth_data_path.exists() and bool(
            api.auth_data.get("passToken") or not api.missing_session_keys())

    @_serialized
    def qr_login_begin(self, force_new: bool = False) -> dict | None:
        """获取扫码登录数据。

        返回 None 表示本地凭据已自动刷新、无需扫码；
        否则返回含 loginUrl 的 dict，交由界面渲染二维码。
        force_new=True（macOS 首次登录）不试刷新，一定返回新二维码：本机
        已有的 CLI 凭据不参与也不被改动，扫码确认后才写入共用的认证文件。
        """
        api = self._api
        try:
            # 上游取码前会先用 passToken 试刷新。mijiaAPI 5.0 只为已有凭据
            # 创建会话，但服务端仍可能返回需要跟随的登录跳转：临时给一个会话
            with api.temporary_session():
                data = (api.fresh_qr_login_data() if force_new
                        else api._get_qr_login_data())
        except _TRANSPORT_ERRORS as exc:
            # sts 只在「本地已有 passToken、上游先试刷新」时才会被访问
            stage = ("刷新已有登录凭据" if _host_of(exc).startswith("sts.")
                     else "获取登录二维码")
            err = _network_error(exc, stage)
            logger.warning("登录: 取码阶段网络失败（阶段=%s host=%s 类型=%s）",
                           err.stage, err.host or "?", err.kind)
            raise NetworkError(f"获取登录二维码失败：{err}。本地凭据已保留，可点击重试",
                               stage=err.stage, host=err.host, kind=err.kind) from exc
        except Exception as exc:
            raise _wrap_error(exc, "获取登录二维码失败") from exc
        if data.get("refreshed"):
            api.mark_session_valid()
            return None
        return data

    @_serialized
    def qr_login_wait(self, login_data: dict) -> None:
        """长轮询等待用户扫码，阻塞可达两分钟，必须放在后台线程调用。

        上游先把扫码结果（含 passToken）写进内存，再访问 sts 回调换会话。
        回调在慢链路上最容易超时；此时扫码其实已经确认，就用新 passToken
        走一次标准刷新补拿会话，仍失败则把已确认的凭据落盘，之后重试直接
        刷新、无需重扫。扫码本身没完成（长轮询失败）才恢复本轮之前的状态，
        避免下一次拿半套凭据尝试刷新登录。
        """
        api = self._api
        auth_before = api.auth_data.copy()
        try:
            api._complete_qr_login(login_data)
        except _TRANSPORT_ERRORS as exc:
            failure: BaseException = exc
        except Exception as exc:
            api.auth_data = auth_before
            raise _wrap_error(exc, "扫码登录未完成") from exc
        else:
            api.mark_session_valid()
            return

        pass_token = api.auth_data.get("passToken")
        if not pass_token or pass_token == auth_before.get("passToken"):
            # 扫码确认之前链路就断了：没有新凭据，恢复原状
            api.auth_data = auth_before
            err = _network_error(failure, "等待扫码结果")
            raise NetworkError(f"扫码登录未完成：{err}，请重新获取二维码",
                               stage=err.stage, host=err.host,
                               kind=err.kind) from failure

        logger.warning("登录: 已扫码确认，但回调换取会话失败（%s），改用 passToken 刷新",
                       _safe_reason(failure))
        try:
            with api.read_retries(2):
                api.refresh_session()
        except NetworkError as exc:
            # 已确认的扫码结果落盘：之后的重试直接走 passToken 刷新
            api._save_auth_data()
            logger.warning("登录: 补全会话仍失败，扫码凭据已保存，等待重试"
                           "（阶段=%s host=%s 类型=%s）",
                           exc.stage, exc.host or "?", exc.kind)
            raise NetworkError(
                f"已扫码确认，但向米家服务器换取会话时网络失败：{exc}。"
                "登录凭据已保存，点击下方按钮重试即可，无需重新扫码",
                stage=exc.stage, host=exc.host, kind=exc.kind,
                scan_confirmed=True) from exc
        except AuthRequiredError as exc:
            api.auth_data = auth_before
            raise AuthRequiredError(f"扫码登录未完成：{exc}") from exc
        logger.info("登录: 扫码后经 passToken 补全会话成功")

    # ---------- 设备列表 ----------

    @_serialized
    def list_devices(self) -> list[DeviceInfo]:
        """拉取全部家庭和共享设备并补齐房间归属。

        设备信息本身不含房间字段，参照 CLI 的实现思路：
        遍历每个家庭的 roomlist[].dids[] 反查出 did -> (家庭, 房间)。
        """
        started = time.monotonic()
        homes, devices = self._fetch_all_devices("获取设备列表失败")

        location: dict[str, tuple[str, str]] = {}
        for home in homes:
            for room in home.get("roomlist", []):
                for did in room.get("dids", []) or []:
                    location[str(did)] = (home["name"], room["name"])

        result = []
        for d in devices:
            did = str(d["did"])
            home_name, room_name = location.get(did, ("未知", "未知"))
            result.append(DeviceInfo(
                did=did,
                name=d.get("name", did),
                model=d.get("model", ""),
                home_name=home_name,
                room_name=room_name,
                online=bool(d.get("isOnline", False)),
                # 云端对无公开规格的设备（手表、第三方牙刷等）不下发该字段
                spec_type=d.get("spec_type") or "",
            ))
        logger.info("设备加载: 完成，%d 个家庭、%d 台设备（在线 %d），总用时 %.1fs",
                    len(homes), len(result), sum(1 for d in result if d.online),
                    time.monotonic() - started)
        return sorted(result, key=lambda x: (x.home_name, x.room_name, x.name))

    def _fetch_all_devices(self, context: str) -> tuple[list[dict], list[dict]]:
        """家庭列表 → 各家庭设备 → 共享设备，逐阶段记录结果与用时。

        上游 get_devices_list() 先拉一遍家庭列表，翻每一页设备时又各拉一遍
        （查房主），慢链路上每多一次请求就多一次超时机会。这里家庭列表只拉
        一次，并顺手更新 did 索引与家庭 ID 映射：紧随其后的开关轮询不必
        再整轮拉一次设备列表。用户主动刷新，只读请求允许重试一次。
        """
        api = self._api
        with api.read_retries(2):
            homes = self._load_stage("家庭列表", api.get_homes_list, context)
            devices: list[dict] = []
            with api.homes_snapshot(homes):
                for index, home in enumerate(homes, 1):
                    devices.extend(self._load_stage(
                        f"设备列表（家庭 {index}/{len(homes)}）",
                        lambda home_id=home["id"]: api.get_devices_list(home_id),
                        context))
            devices.extend(self._load_stage(
                "共享设备列表", api.get_shared_devices_list, context))
        self._home_ids = {
            str(h.get("name") or ""): str(h.get("id") or "") for h in homes}
        for d in devices:
            self._device_index[str(d["did"])] = (
                d.get("model", ""), d.get("spec_type") or "", d.get("name", ""))
        return homes, devices

    @staticmethod
    def _load_stage(stage: str, fetch, context: str) -> list:
        started = time.monotonic()
        try:
            result = fetch()
        except Exception as exc:
            logger.warning("设备加载: %s 失败（%.1fs）: %s",
                           stage, time.monotonic() - started, _safe_reason(exc))
            raise _wrap_error(exc, context) from exc
        logger.info("设备加载: %s 成功，%d 项（%.1fs）",
                    stage, len(result), time.monotonic() - started)
        return result

    # ---------- 设备控制 ----------

    @_serialized
    def device_detail(self, did: str) -> DeviceDetail:
        # 打开详情是用户主动操作：构造设备对象的只读请求允许重试一次
        with self._api.read_retries(2):
            dev = self._get_device(did)
        # prop_list 对属性名里的 '-' 额外注册了一份 '_' 别名键，
        # 两者指向同一对象，按对象身份去重避免面板出现重复控件
        seen: set[int] = set()
        props: list[PropInfo] = []
        for prop in dev.prop_list.values():
            if id(prop) in seen:
                continue
            seen.add(id(prop))
            props.append(PropInfo(
                name=prop.name,
                desc=prop.desc or prop.name,
                type=prop.type,
                readable="r" in prop.rw,
                writable="w" in prop.rw,
                range=tuple(prop.range) if prop.range else None,
                value_list=prop.value_list,
            ))
        actions = [
            ActionInfo(name=a.name, desc=a.desc or a.name)
            for a in dev.action_list.values()
        ]
        return DeviceDetail(did=dev.did, name=dev.name, model=dev.model,
                            props=props, actions=actions)

    @_serialized
    def read_prop(self, did: str, name: str):
        dev = self._get_device(did)
        try:
            return dev.get(name)
        except Exception as exc:
            raise _wrap_error(exc, f"读取属性 {name} 失败") from exc

    @_serialized
    def read_props(self, did: str, names: list[str]) -> dict[str, Any | None]:
        """批量读取同一台设备的多个属性，一次请求替代逐个轮询。

        逐个读取时上游每个属性固定 sleep 0.5 秒，详情面板十几项
        属性要等近十秒；合并为批量请求后整面板一次往返即可完成。
        """
        dev = self._get_device(did)
        queries: list[dict] = []
        key_to_name: dict[tuple, str] = {}
        for name in names:
            prop = dev.prop_list.get(name)
            if prop is None or "r" not in prop.rw:
                continue
            method = prop.method.copy()
            method["did"] = dev.did
            queries.append(method)
            key_to_name[(method["siid"], method["piid"])] = name

        result: dict[str, Any | None] = {name: None for name in key_to_name.values()}
        for start in range(0, len(queries), _BATCH_SIZE):
            batch = queries[start:start + _BATCH_SIZE]
            try:
                rets = self._api.get_devices_prop(batch)
            except Exception as exc:
                raise _wrap_error(exc, "批量读取属性失败") from exc
            for item in rets:
                key = (item.get("siid"), item.get("piid"))
                name = key_to_name.get(key)
                if name is None:
                    continue
                result[name] = item["value"] if item.get("code") == 0 else None
        return result

    @_serialized
    def write_prop(self, did: str, name: str, value) -> None:
        # 写入前的只读准备（设备对象、会话校验）可重试；写请求本身只发一次
        with self._api.read_retries(2):
            dev = self._get_device(did)
            try:
                dev.set(name, value)
            except Exception as exc:
                raise _wrap_error(exc, f"设置属性 {name} 失败", write=True) from exc

    @_serialized
    def run_action(self, did: str, name: str, params=None) -> None:
        with self._api.read_retries(2):
            self._run_action(did, name, params)

    def _run_action(self, did: str, name: str, params=None) -> None:
        dev = self._get_device(did)
        try:
            if params is not None:
                # 小爱类文本指令需走 _in 通道（见 mijiaAPI __main__.py:523
                # wifispeaker.run_action('execute-text-directive', _in=[prompt, quiet])）
                if name in ("execute-text-directive", "play-text", "play-music", "play-radio"):
                    # 统一按 _in 传递，兼容单字符串与列表
                    in_val = params if isinstance(params, (list, tuple)) else [params]
                    # execute-text-directive 为 [文本, 是否静默](0/1)，缺省按非静默补齐
                    if name == "execute-text-directive" and len(in_val) == 1:
                        in_val = [in_val[0], 0]
                    dev.run_action(name, _in=in_val)
                elif isinstance(params, (list, tuple)):
                    dev.run_action(name, params)
                else:
                    dev.run_action(name, [params])
            else:
                # 文本类动作无参必报 -704220025，这里直接引导上层弹输入
                if name in ("execute-text-directive", "play-text"):
                    raise ServiceError(f"动作 {name} 需要文本参数")
                dev.run_action(name)
        except ServiceError:
            raise
        except Exception as exc:
            raise _wrap_error(exc, f"执行动作 {name} 失败", write=True) from exc

    def _get_device(self, did: str) -> mijiaDevice:
        # 构造 mijiaDevice 本身要发两次网络请求（设备列表 + spec 拉取），
        # 按 did 缓存实例，面板切换时才不会反复打接口
        if did not in self._device_cache:
            if did not in self._device_index:
                self._refresh_device_index()
            entry = self._device_index.get(did)
            if entry is not None and not entry[1]:
                # 云端未下发 spec_type（手表、第三方牙刷等无公开规格的
                # 设备），上游 5.x 会拿空值拉规格抛 ValueError；这里提前
                # 拦截给出用户可读提示，仅对确实存在于列表的无规格设备生效
                raise ServiceError(
                    "该设备无公开的功能规格（常见于仅蓝牙连接的产品），"
                    "无法提供控制面板")
            try:
                self._device_cache[did] = mijiaDevice(self._api, did=did)
            except DeviceNotFoundError:
                # 上游构造只查自有设备列表（不含共享设备），共享设备
                # 必然在此失败；按上游 __init__ 的字段手工组装，后续
                # get/set/run_action 与自有设备走完全相同的代码路径
                self._device_cache[did] = self._build_shared_device(did)
            except GetDeviceInfoError as exc:
                # 无公开功能规格的设备（常见于仅蓝牙连接的产品）：
                # 没有属性/动作可控制，给出用户可读的明确提示
                raise ServiceError(
                    "该设备无公开的功能规格（常见于仅蓝牙连接的产品），"
                    "无法提供控制面板") from exc
            except Exception as exc:
                raise _wrap_error(exc, "加载设备信息失败") from exc
            self._patch_device_zh(self._device_cache[did])
        return self._device_cache[did]

    @staticmethod
    def _patch_device_zh(dev: mijiaDevice) -> None:
        """给 mijiaDevice 的属性/动作描述补齐中文兜底。

        上游构造从磁盘缓存读原始 spec，部分品类（音箱、温湿度计等）
        缺中文 comment 会退化为纯英文；缓存文件不归适配层管、无法在
        源头补，只能在对象组装后按名补一条，与 _fetch_spec 的内存
        dict 路径保持同源逻辑（见 spec_zh）。
        """
        seen: set[int] = set()
        for prop in dev.prop_list.values():
            if id(prop) in seen:
                continue  # 连字符与下划线两个键指向同一对象，去重防双写
            seen.add(id(prop))
            prop.desc = spec_zh.prop_desc_zh(prop.name, prop.desc)
        for act in dev.action_list.values():
            act.desc = spec_zh.action_desc_zh(act.name, act.desc)

    def _build_shared_device(self, did: str) -> mijiaDevice:
        """为共享设备手工组装 mijiaDevice（上游构造器不支持共享设备）。"""
        if did not in self._device_index:
            self._refresh_device_index()
        model, spec_type, name = self._device_index.get(did, ("", "", ""))
        if not model:
            raise ServiceError(f"未找到设备 {did}")
        if not spec_type:
            raise ServiceError(
                "该设备无公开的功能规格（常见于仅蓝牙连接的产品），"
                "无法提供控制面板")
        try:
            dev_info = get_device_info(
                spec_type, cache_path=self._api.auth_data_path.parent)
        except GetDeviceInfoError as exc:
            raise ServiceError(
                "该设备无公开的功能规格（常见于仅蓝牙连接的产品），"
                "无法提供控制面板") from exc
        except Exception as exc:
            raise _wrap_error(exc, "加载设备信息失败") from exc

        dev = mijiaDevice.__new__(mijiaDevice)
        dev.api = self._api
        dev.did = did
        dev.model = model
        dev.name = name or dev_info.get("name", did)
        dev.sleep_time = 0.5
        # prop_list/action_list 必须最后赋值：上游 __setattr__ 在
        # prop_list 存在后会拦截同名属性写入并转发为设备控制
        prop_list: dict[str, DevProp] = {}
        for prop in dev_info.get("properties", []):
            prop_obj = DevProp(prop)
            prop_list[prop["name"]] = prop_obj
            if "-" in prop["name"]:
                prop_list[prop["name"].replace("-", "_")] = prop_obj
        dev.prop_list = prop_list
        dev.action_list = {
            act["name"]: DevAction(act) for act in dev_info.get("actions", [])
        }
        return dev

    # ---------- 开关状态（卡片快速控制用） ----------

    @_serialized
    def power_state(self, did: str) -> bool | None:
        """读取单台设备开关状态；无可写开关属性的设备返回 None。

        走 power_states 的 spec 批量路径：model/spec 全部命中缓存时
        只剩一次属性请求，避免为单个开关构造 mijiaDevice（两次请求）。
        """
        return self.power_states([did]).get(did)

    @_serialized
    def toggle_power(self, did: str, current: bool | None = None) -> bool:
        """切换到开关的反面，返回新状态。

        `current` 是调用方已知的当前状态（界面层的开关记忆）：给了就
        直接写反值，**省掉一次读往返**——米家云端单次往返 0.3~0.8 秒，
        少一次就能让点击明显更跟手。

        不给 `current` 时退回"先读后写"，读与写串在同一任务里完成
        （调用方经单线程队列提交），否则两次轮询之间会出现读后写的竞态。
        """
        # 用户点击：写入前的读（当前状态、规格、会话校验）可重试一次，
        # 写请求本身只发一次（见 set_power_state）
        with self._api.read_retries(2):
            if current is None:
                current = self.power_state(did)
            if current is None:
                raise ServiceError("设备不支持开关控制或已离线")
            new_state = not current
            self.set_power_state(did, new_state)
        return new_state

    @staticmethod
    def _parent_did(did: str) -> str | None:
        """虚拟子设备 did → 父设备 did；不是子设备返回 None。

        `1234567890.s3` → `1234567890`；普通 did 原样返回 None。
        """
        m = _SUB_DEVICE_SUFFIX.search(str(did))
        return str(did)[:m.start()] if m else None

    @_serialized
    def set_power_state(self, did: str, state: bool) -> None:
        """写入开关状态：spec method 直接构造批量写请求。

        不经 mijiaDevice.set——那条路要构造设备对象并逐属性写入，
        首次点击要等两三次网络往返；直写批量接口一次请求完成。
        子设备的请求实际发往父 did（见 `_switch_target`），且不再试探
        子 did：那样每次都会白等约 6 秒才拿到拒绝。

        写请求**不自动重发**：超时可能发生在指令已送达之后，开机卡这类
        「模拟按一下电源键」的设备被按两次就是开了又关。写入前的只读准备
        （规格、会话校验）允许重试一次。
        """
        with self._api.read_retries(2):
            info = self._specs_for([did]).get(did)
            method = self._find_on_method(info, did) if info else None
            if method is None:
                raise ServiceError("设备不支持开关控制")
            self._write_power(self._switch_target(did), method, state)

    def _write_power(self, did: str, method: dict, state: bool) -> None:
        started = time.monotonic()
        try:
            rets = self._api.set_devices_prop(
                [{"did": did, **method, "value": state}]
            )
        except Exception as exc:
            logger.warning("开关写入失败（%.1fs，未重发）: %s",
                           time.monotonic() - started, _safe_reason(exc))
            raise _wrap_error(exc, "设置开关状态失败", write=True) from exc
        logger.info("开关写入已送达云端（%.1fs）", time.monotonic() - started)
        # 写接口的 code 语义与读不同：上游把 code∈(0,1) 的响应统一
        # 改写为「成功」（apis.py set_devices_prop 出口），其余一律失败
        items = rets if isinstance(rets, list) else [rets]
        for item in items:
            if not isinstance(item, dict):
                continue
            code = item.get("code")
            if code in (0, 1):
                continue
            raise ServiceError(
                f"设置失败（{item.get('message', '未知错误')}，错误码 {code}）"
            )

    @_serialized
    def power_states(self, dids: list[str]) -> dict[str, bool | None]:
        """批量读取多台设备的开关状态。

        三段式压缩网络开销：did->model 映射增量补齐（一次设备列表）、
        spec 线程池并发拉取（本地文件缓存后零开销）、属性按批合并
        读取。全部命中缓存时整批设备只剩一两次属性请求。

        虚拟子设备（`<父did>.sN`）的请求目标见 `_switch_target`：
        实测米家云端**只认父 did + 该路 siid**，子 did 一律返回
        -704083036，所以学到之后直接用父 did，不再每次白跑一次失败请求。
        """
        spec_map = self._specs_for(dids)

        queries: list[dict] = []
        owners: list[str] = []          # 与 queries 一一对应：发起请求的设备 did
        result: dict[str, bool | None] = {}
        method_of: dict[str, dict] = {}
        target_of: dict[str, str] = {}
        for did in dids:
            info = spec_map.get(did)
            method = self._find_on_method(info, did) if info else None
            if method is None:
                # spec 缺失或无开关属性，直接记为无能力。
                # 这是常态（温度计、门磁、摄像头），按 debug 记：
                # 按 WARNING 会在 debug.log 里造出一堆假故障
                result[did] = None
                entry = self._device_index.get(did, ("", "", ""))
                self._log_switch_failure(
                    did, None, None,
                    "规格无 on 属性或规格未取到（spec_type=%r）"
                    % (entry[1] if len(entry) > 1 else ""),
                    level=logging.DEBUG)
                continue
            method_of[did] = method
            target = self._switch_target(did)
            target_of[did] = target
            queries.append({"did": target, **method})
            owners.append(did)

        # 记录失败项及其原始响应：响应为空通常意味着 spec 声称有该属性、
        # 云端实际不认（路由器这类），按调试信息记即可；有明确错误码的
        # 才是真正需要关注的读取失败
        failed: list[tuple[str, dict | None]] = []
        for start in range(0, len(queries), _BATCH_SIZE):
            batch = queries[start:start + _BATCH_SIZE]
            batch_owners = owners[start:start + _BATCH_SIZE]
            try:
                rets = self._api.get_devices_prop(batch)
            except Exception as exc:
                raise _wrap_error(exc, "批量读取设备状态失败") from exc
            # 响应按 (did, siid) 回填：同一父设备的多路共用一个 did，
            # 只按 did 匹配会把三路结果全算到最后一路
            by_key = {(str(item.get("did")), item.get("siid")): item
                      for item in rets if isinstance(item, dict)}
            for index, owner in enumerate(batch_owners):
                method = method_of[owner]
                item = by_key.get((target_of[owner], method.get("siid")))
                if item is None and index < len(rets) and isinstance(rets[index], dict):
                    item = rets[index]          # 响应缺 siid 时按顺序对齐
                if item is not None and item.get("code") == 0:
                    result[owner] = bool(item["value"])
                else:
                    # 读失败（离线 / 该路被拒）不能记成 None——None 的语义是
                    # "确认没有开关能力"，一旦落进记忆，这台设备就再也不会
                    # 被轮询（force=True 会跳过 None），按钮永久消失。
                    # 这里只把它列进待兜底清单，兜底仍失败就不写进结果。
                    failed.append((owner, item))

        # 读不到的逐台留一次线索（不按轮询刷屏）
        for did, item in failed:
            if result.get(did) is None:
                # 规格声明了但设备实际不支持的属性不算故障，按调试级别记：
                # 响应缺 code、或返回「属性不存在」这类明确表示不支持的错误码。
                # 其余错误码（离线、鉴权、限流）才值得按警告记。
                code = item.get("code") if isinstance(item, dict) else None
                unsupported = code in (None, 0) or code in _UNSUPPORTED_PROP_CODES
                level = logging.DEBUG if unsupported else logging.WARNING
                self._log_switch_failure(
                    did, method_of.get(did), item, "读取失败", level=level)
        return result

    # ---------- 虚拟子设备的请求目标 ----------

    def _switch_target(self, did: str) -> str:
        """这条开关请求实际发给谁：子设备一律发给父设备。

        实测（2026-10-18，真实账号）：云端对子 did（`<父did>.sN`）的读取
        **不是快速报错，而是拖到 ~6 秒才返回 -704083036**；而父 did + 该路
        siid 只要 ~50ms。所以"先试子 did、失败再退回父 did"的做法会让
        每次启动后的首轮轮询白等 6 秒——这是子设备状态显示延迟的主因。

        既然父 did 才是唯一可用入口，就直接走父 did，不再试探。
        若将来某厂商云端只认子 did，只需把这里改成返回 `did`。
        """
        parent = self._parent_did(did)
        return parent if parent is not None else did

    def _log_switch_failure(self, did: str, method: dict | None,
                            item: dict | None, stage: str,
                            level: int = logging.WARNING) -> None:
        """同一台设备只记一次失败详情，避免 5 秒轮询把日志刷满。

        `level` 供"本来就没有开关属性"用：温度计、门磁这类设备不是失败，
        按 WARNING 记会让 debug.log 里全是假故障，真正的问题反而被淹掉。
        """
        logged = getattr(self, "_switch_diag_logged", None)
        if logged is None:
            logged = set()
            self._switch_diag_logged = logged
        if did in logged:
            return
        logged.add(did)
        entry = self._device_index.get(did, ("", "", ""))
        method = method or {}
        code = item.get("code") if isinstance(item, dict) else None
        message = item.get("message") if isinstance(item, dict) else None
        logger.log(
            level,
            "开关状态读取失败 did=%s name=%r model=%r spec_type=%r "
            "siid=%s piid=%s 阶段=%s code=%s message=%s",
            did, entry[2] if len(entry) > 2 else "", entry[0] if entry else "",
            entry[1] if len(entry) > 1 else "",
            method.get("siid"), method.get("piid"), stage, code, message)

    def _fetch_spec(self, spec_type: str, cache_dir) -> dict | None:
        if not spec_type:
            return None
        try:
            info = get_device_info(spec_type, cache_path=cache_dir)
        except (json.JSONDecodeError, UnicodeDecodeError):
            # 上游缓存文件损坏（多见于并发写坏）。不删掉重拉的话，
            # 该规格的能力信息将永久缺失，开关与温湿度控件随之消失；
            # 缓存文件按 spec_type 命名，冒号替换为下划线（与上游一致）
            try:
                (Path(cache_dir)
                 / f"{spec_type.replace(':', '_')}.json").unlink(missing_ok=True)
            except OSError:
                return None
            try:
                info = get_device_info(spec_type, cache_path=cache_dir)
            except Exception:
                return None
        except Exception:
            # 个别规格在 miot-spec.org 不存在（如第三方牙刷），按无能力处理
            return None
        # 部分品类（音箱、温湿度计等）官方数据缺中文 comment，
        # 按属性/动作名兜底补齐，避免界面退化为纯英文（见 spec_zh）
        spec_zh.apply_fallback(info)
        return info

    @staticmethod
    def _gang_from_did(did: str) -> int | None:
        """从虚拟子设备的 did 里取出它对应的通道号（siid）。

        多键面板在云端会派生出虚拟设备，did 形如 `<父did>.s2`、`.s3`、
        `.s4`——后缀的 N 就是这一路在规格里对应的 siid。单键设备与父
        设备本身没有后缀，返回 None（按第一条 on 属性处理）。
        """
        m = _SUB_DEVICE_SUFFIX.search(str(did))
        return int(m.group(1)) if m else None

    @classmethod
    def _find_on_method(cls, info: dict, did: str | None = None) -> dict | None:
        """挑出该设备可写的 on 属性，返回其 method(siid/piid)。

        多键面板的规格里有 `on-2`(siid 2)、`on-3`(siid 3)、`on-4`(siid 4)
        等多路属性。**必须按 did 后缀选对应的那一路**：三个虚拟设备都
        取第一条的话，后两路永远在控制第一路（用户表现为"虚拟设备点不
        动/开关错灯"）。
        """
        props = [
            prop for prop in info.get("properties", [])
            if prop.get("type") == "bool" and "w" in prop.get("rw", "")
            and (prop.get("name") == "on"
                 or prop["name"].replace("_", "-").startswith("on-"))
        ]
        if not props:
            return None
        gang = cls._gang_from_did(did) if did else None
        if gang is not None:
            for prop in props:
                if prop.get("method", {}).get("siid") == gang:
                    return prop["method"]
            # 后缀编号与 siid 对不上时按顺序兜底：s2→第 1 路、s3→第 2 路…
            index = gang - 2
            if 0 <= index < len(props):
                return props[index]["method"]
        return props[0]["method"]

    # ---------- 环境读数（卡片副标题展示用） ----------

    @_serialized
    def read_metrics(self, dids: list[str],
                     selections: dict[str, list[str]] | None = None,
                     climate_out: dict[str, dict[str, float]] | None = None,
                     climate_candidates: list[str] | None = None
                     ) -> dict[str, str | None]:
        """批量读取卡片副标题要显示的属性，拼成展示文案。

        默认读 temperature 与 relative-humidity（个别型号叫 humidity），
        插座这类设备更关心功率，可由 selections 指定每个 did 要显示的
        属性名（最多两个）。读不到的设备返回 None，副标题维持纯房间名。
        多通道设备的同名属性取第一个通道。

        climate_out 传入字典时，会把每台设备读到的温湿度原始值一并
        写进去：顶部环境胶囊固定展示温湿度，不能从用户自定义过的
        副标题文案里反推。climate_candidates 是没有副标题需求、但仍要
        为胶囊读温湿度的设备（比如用户把外显改成不外显或换成功率），
        规格里没有温湿度属性的会被自动跳过。
        """
        selections = selections or {}
        # 温湿度候选设备也一起取 spec：同一批请求内完成，不额外发网络请求。
        # 先按原顺序去重再排除已有副标题需求的设备，避免重复查询同一 did
        base = set(dids)
        extra = [d for d in dict.fromkeys(climate_candidates or [])
                 if d not in base]
        spec_map = self._specs_for(list(dids) + extra)
        queries: list[dict] = []
        # (did, siid, piid) -> (did, 属性名)
        key_to_metric: dict[tuple, tuple[str, str]] = {}
        result: dict[str, str | None] = {}
        for did in dids:
            if did in selections:
                # 键存在就用它的值：空列表代表用户明确选了不外显，
                # 不能拿真假值判断，否则会退回默认温湿度
                wanted = list(selections[did])
            else:
                wanted = list(_DEFAULT_METRICS)
            # 温湿度始终顺带读一次：顶部环境胶囊要固定展示它们，
            # 而卡片的副标题可能被用户改成别的属性
            for name in _DEFAULT_METRICS:
                if name not in wanted:
                    wanted.append(name)
            info = spec_map.get(did)
            # 属性名按优先级排序：用户选择顺序即显示顺序
            found: dict[str, dict] = {}
            if info:
                by_name = {p.get("name"): p for p in info.get("properties", [])
                           if "r" in p.get("rw", "")}
                for name in wanted:
                    prop = by_name.get(name)
                    if prop is None and name in ("humidity", "relative-humidity"):
                        # 两种写法在规格里叫法不一，互相兜底
                        prop = by_name.get("relative-humidity") or by_name.get("humidity")
                    if prop is not None and name not in found:
                        found[name] = prop["method"]
            if not found:
                result[did] = None
                continue
            for metric, method in found.items():
                queries.append({"did": did, **method})
                key_to_metric[(did, method["siid"], method["piid"])] = (did, metric)

        # 只为胶囊读温湿度的设备：规格里没有这两项的直接跳过。
        # 必须在下面发请求之前补齐查询，否则这些读数永远不会被读取
        for did in extra:
            info = spec_map.get(did)
            if not info:
                continue
            by_name = {p.get("name"): p for p in info.get("properties", [])
                       if "r" in p.get("rw", "")}
            picked: dict[str, dict] = {}
            temp_prop = by_name.get("temperature")
            if temp_prop is not None:
                picked["temperature"] = temp_prop["method"]
            # 湿度在规格里有 relative-humidity 与 humidity 两种写法
            hum_prop = by_name.get("relative-humidity") or by_name.get("humidity")
            if hum_prop is not None:
                picked["relative-humidity"] = hum_prop["method"]
            for name, method in picked.items():
                queries.append({"did": did, **method})
                key_to_metric[(did, method["siid"], method["piid"])] = (did, name)

        # did -> 属性名 -> 数值，最终按选择顺序拼成文案
        values: dict[str, dict[str, float]] = {}
        for start in range(0, len(queries), _BATCH_SIZE):
            batch = queries[start:start + _BATCH_SIZE]
            try:
                rets = self._api.get_devices_prop(batch)
            except Exception:
                # 读数失败只影响副标题展示，不值得打断主流程
                return {did: None for did in dids}
            for item in rets:
                entry = key_to_metric.get(
                    (str(item.get("did")), item.get("siid"), item.get("piid"))
                )
                if entry is None or item.get("code") != 0:
                    continue
                did, metric = entry
                try:
                    value = float(item["value"])
                except (KeyError, TypeError, ValueError):
                    continue
                values.setdefault(did, {})[metric] = value

        for did in dids:
            got = values.get(did, {})
            # 副标题只拼用户要的那几项；温湿度是顺带读的，不进文案
            wanted = (selections[did] if did in selections
                      else list(_DEFAULT_METRICS))
            shown = {name: got[name] for name in wanted if name in got}
            result[did] = format_metrics_text(shown)

        if climate_out is not None:
            # 胶囊数据要覆盖两类设备：有副标题需求的，以及只为胶囊读的
            for did in list(dids) + extra:
                got = values.get(did, {})
                climate_out[did] = {name: got[name]
                                    for name in _DEFAULT_METRICS if name in got}
        return result

    @_serialized
    def consumable_items(self) -> list[dict]:
        """耗材明细：设备名、耗材描述、当前值、不足阈值与更新时间。

        不足判定与米家 App 一致：details.value 低于 details.inadeq.val。
        接口异常返回空列表，顶栏隐藏该项即可，不影响主流程。
        """
        try:
            raw_items = self._api.get_consumable_items()
        except Exception:
            logger.debug("读取耗材列表失败", exc_info=True)
            return []
        result: list[dict] = []
        for item in raw_items or []:
            if item.get("is_ignore"):
                continue
            detail = item.get("details") or {}
            try:
                value = float(detail.get("value"))
            except (TypeError, ValueError):
                continue
            try:
                threshold = float(json.loads(detail.get("inadeq") or "{}").get("val"))
            except (TypeError, ValueError):
                threshold = None
            result.append({
                "did": str(item.get("did") or ""),
                "name": str(item.get("name") or ""),
                "model": str(item.get("model") or ""),
                "description": str(detail.get("description") or ""),
                "type": str(detail.get("type_name") or ""),
                "value": value,
                "threshold": threshold,
                "low": threshold is not None and value < threshold,
                "updated": int(detail.get("update_time") or 0),
            })
        return result

    @_serialized
    def consumable_low_count(self) -> tuple[int, int]:
        """耗材统计：返回 (耗材项数, 不足项数)。"""
        items = self.consumable_items()
        return len(items), sum(1 for item in items if item["low"])

    def _specs_for(self, dids: list[str]) -> dict[str, dict | None]:
        """did -> spec 映射，spec_type 增量补齐 + spec 并发拉取。

        与 power_states 共用设备索引、内存 spec 缓存与上游文件缓存，
        轮询同时刷开关和读数时不会产生额外请求。同一规格多台设备只拉
        一次：上游把 spec 缓存在按规格命名的同一个文件里，并发拉取
        同规格会互相覆盖写坏缓存。
        """
        if any(d not in self._device_index for d in dids):
            self._refresh_device_index()

        result: dict[str, dict | None] = {}
        to_fetch: list[str] = []
        for did in dids:
            spec_type = self._device_index.get(did, ("", "", ""))[1]
            if not spec_type:
                result[did] = None
            elif spec_type in self._spec_cache:
                result[did] = self._spec_cache[spec_type]
            else:
                to_fetch.append(spec_type)
        # 保序去重：同上循环里内存缓存未命中时会把同规格多台设备重复
        # 入列，并发线程拉同一规格会同时写同一个缓存文件互相覆盖
        to_fetch = list(dict.fromkeys(to_fetch))

        if to_fetch:
            cache_dir = self._api.auth_data_path.parent
            with ThreadPoolExecutor(max_workers=min(_SPEC_WORKERS, len(to_fetch))) as pool:
                fetched = pool.map(
                    lambda st: self._fetch_spec(st, cache_dir), to_fetch
                )
                for spec_type, spec in zip(to_fetch, fetched):
                    self._spec_cache[spec_type] = spec
            for did in dids:
                if did not in result:
                    result[did] = self._spec_cache.get(
                        self._device_index.get(did, ("", "", ""))[1]
                    )
        return result

    def _refresh_device_index(self) -> None:
        """重建 did -> (model, spec_type, name) 索引，自有与共享设备合并。

        采用 upsert 而非整体清空重建：列表接口偶发返回空（尤其共享
        列表）时，旧条目仍在才能继续组装共享设备；已移除设备的残留
        条目无害，UI 不会再给出入口。索引更新在 _fetch_all_devices 内完成。
        """
        self._fetch_all_devices("获取设备列表失败")

    # ---------- 场景（手动控制） ----------

    def _home_ids_map(self) -> dict[str, str]:
        """家庭名 -> 家庭 ID 映射，懒加载缓存。

        场景接口要家庭 ID 而界面层只有显示名；家庭结构变更概率
        极低，一次拉取后长期复用。
        """
        if self._home_ids is None:
            try:
                homes = self._api.get_homes_list()
            except Exception as exc:
                raise _wrap_error(exc, "获取家庭列表失败") from exc
            self._home_ids = {
                str(h.get("name") or ""): str(h.get("id") or "") for h in homes}
        return self._home_ids

    @_serialized
    def scene_list(self, home_name: str | None = None) -> list[SceneInfo]:
        """获取手动场景列表；home_name 为 None 时返回全部家庭的场景。"""
        home_id: str | None = None
        if home_name is not None:
            home_id = self._home_ids_map().get(home_name)
            if home_id is None:
                # 界面家庭名在映射里不存在（如「未知」），视为无场景
                return []
        api = self._api
        try:
            with api.read_retries(2):
                if home_id is not None:
                    scenes = api.get_scenes_list(home_id)
                else:
                    # 与设备列表同理：家庭列表只拉一次，逐家庭查场景时复用
                    homes = api.get_homes_list()
                    scenes = []
                    with api.homes_snapshot(homes):
                        for home in homes:
                            scenes.extend(api.get_scenes_list(home["id"]))
        except Exception as exc:
            raise _wrap_error(exc, "获取场景列表失败") from exc
        id_to_name = {v: k for k, v in self._home_ids_map().items()}
        return [
            SceneInfo(
                scene_id=str(s.get("scene_id") or ""),
                name=str(s.get("name") or "未命名场景"),
                # 上游返回的 home_id 与请求一致，兜底用请求时的家庭 ID
                home_id=str(s.get("home_id") or home_id or ""),
                # 家庭显示名反查映射（「全部」路径下跨家庭场景各自归属）
                home_name=id_to_name.get(
                    str(s.get("home_id") or home_id or ""), ""),
            )
            for s in scenes
        ]

    @_serialized
    def run_scene(self, scene: SceneInfo) -> None:
        """执行手动场景；上游返回值为假视为失败。执行请求只发一次。"""
        try:
            with self._api.read_retries(2):
                ok = self._api.run_scene(scene.scene_id, scene.home_id)
        except Exception as exc:
            raise _wrap_error(exc, f"执行场景「{scene.name}」失败", write=True) from exc
        if not ok:
            raise ServiceError(f"执行场景「{scene.name}」失败")

    # ---------- 设备图标 ----------

    @_serialized
    def icon_urls(self, models: list[str]) -> dict[str, str | None]:
        """model -> 图标 URL，增量拉取：只对缓存里没有的型号调接口。

        成功结果写入内存与磁盘缓存；失败仅记内存 None（本次会话不再
        重试，下次启动重新尝试），避免网络抖动时每轮刷新重复打接口。
        """
        if not models:
            return {}
        missing = [m for m in models if m not in self._icon_cache]
        if missing:
            for model in missing:
                self._icon_cache[model] = self._fetch_icon_url(model)
            icon_store.save_urls(
                {k: v for k, v in self._icon_cache.items() if v})
        return {m: self._icon_cache[m] for m in models if self._icon_cache.get(m)}

    def _fetch_icon_url(self, model: str) -> str | None:
        """调 productconfig/get_icon 取图标 CDN 地址（302 重定向的 Location）。

        与 _request 的差异：这个接口靠重定向返回图片地址而非 JSON 响应体，
        必须关闭自动跟随重定向自己读 Location；签名与刷新逻辑保持一致。
        """
        uri = "/v2/productconfig/get_icon"
        try:
            self._api._refresh_token()
            params = {"data": json.dumps(
                {"icon_name": "icon_real", "model": model},
                separators=(",", ":"))}
            nonce = gen_nonce()
            signed_nonce = get_signed_nonce(self._api.auth_data["ssecurity"], nonce)
            params = generate_enc_params(
                uri, "POST", signed_nonce, nonce, params,
                self._api.auth_data["ssecurity"])
            ret = self._api.session.post(
                self._api.api_base_url + uri, data=params,
                allow_redirects=False, timeout=30)
        except Exception as exc:
            logger.warning("获取设备图标失败 %s: %s", model, exc)
            return None
        if ret.status_code != 302:
            logger.info("获取设备图标未重定向 %s: HTTP %s", model, ret.status_code)
            return None
        return ret.headers.get("Location")

    @_serialized
    def icon_file(self, model: str, url: str) -> Path | None:
        """下载图标到本地缓存并返回文件路径；已存在直接返回，失败返回 None。"""
        path = icon_store.icon_path(model)
        if path.exists():
            return path
        import requests

        try:
            r = requests.get(url, timeout=15)
            if r.status_code != 200 or not r.content:
                logger.warning("下载设备图标失败 %s: HTTP %s", model, r.status_code)
                return None
            path.parent.mkdir(parents=True, exist_ok=True)
            path.write_bytes(r.content)
            return path
        except Exception as exc:
            logger.warning("下载设备图标失败 %s: %s", model, exc)
            return None

    def has_product_page_name(self, model: str) -> bool:
        """该型号的产品页中文名是否已解析过（含“确认无”的情况）。"""
        return model in self._product_page_names

    def cached_product_page_names(self, models: list[str]) -> dict[str, str]:
        """已解析且含中文的产品页名称（model -> name）。"""
        result: dict[str, str] = {}
        for model in models:
            name = self._product_page_names.get(model)
            if name and any("\u4e00" <= ch <= "\u9fff" for ch in name):
                result[model] = name
        return result

    @_serialized
    def product_page_name(self, model: str) -> str | None:
        """抓取 miot-spec 产品页（/p/<model>）的中文商品名。

        用于无公开 spec 的设备（如仅蓝牙类）：这类设备在 /spec/ 路径
        404、specSummary.available=False，没有 spec 产品名可读，只有
        产品页里有本地化商品名。任何失败都缓存 None（含网络超时）：
        该功能只是显示名锦上添花，而 jobs 是单线程串行队列，若失败不
        记忆，秒级的轮询会把超时请求反复排进队列，堵死后续设备控制。
        老站已基本不可用（响应超时或 404），失败记忆让每型号一个会
        话只踩一次坑。阻塞，须后台线程调用。
        """
        if model in self._product_page_names:
            return self._product_page_names[model]
        import requests

        def _fetch(path: str) -> str | None:
            """取某个详情页里的商品名，任何异常都当没有。"""
            try:
                resp = requests.get(
                    f"https://home.miot-spec.com/{path}/{model}", timeout=3,
                    headers={"User-Agent": "Mozilla/5.0"})
                if resp.status_code != 200:
                    return None
                matched = re.search(
                    r'<script data-page="app" type="application/json">(.*?)</script>',
                    resp.text, re.S)
                if not matched:
                    return None
                product = json.loads(matched.group(1)).get("props", {}).get("product", {})
                return product.get("name") or None
            except Exception as exc:
                logger.warning("产品页名称查询失败 %s/%s: %s", path, model, exc)
                return None

        name = None
        # 两个路径登记的信息并不一致：有的型号 /p/ 是 404 或超时，
        # 但 /spec/ 里有商品名，所以前一个拿不到时补查一次
        for path in ("p", "spec"):
            candidate = _fetch(path)
            if candidate and any("一" <= ch <= "鿿" for ch in candidate):
                name = candidate
                break
        self._product_page_names[model] = name
        if name:
            # 只把成功结果落盘：失败留给下次启动重试
            name_store.save({m: n for m, n in self._product_page_names.items() if n})
        return name

    def spec_has_published_functions(self, spec_type: str) -> bool | None:
        """该规格是否发布过含属性的功能 spec。

        True=有属性；False=无公开规格（云端未下发 spec_type）或无属性
        （无可控制功能）；None=spec 尚未拉取（未知，调用方应视为有并
        等待轮询证实）。
        """
        if not spec_type:
            # 云端不下发 spec_type 即官方确认无公开规格，无需等待轮询
            return False
        if spec_type not in self._spec_cache:
            return None
        spec = self._spec_cache[spec_type]
        return bool(spec and spec.get("properties"))

    def localized_product_names(self, dids: list[str], names: dict[str, str]) -> dict[str, str]:
        """did -> 中文产品名，用于替换未改名的英文默认设备名。

        米家 APP 对未改名设备显示的是产品库本地化商品名，而第三方
        设备列表接口的 name 字段只有英文默认名（国际版产品尤甚）。
        spec 数据里带有中文产品名，本方法从**已缓存的 spec**（不发起
        网络请求）读取：仅当云端名为纯 ASCII（未改名）且 spec 产品名
        含中文时返回，否则该 did 不在结果中（保持云端原名）。
        """
        def has_cjk(s: str) -> bool:
            return any("\u4e00" <= ch <= "\u9fff" for ch in s)

        result: dict[str, str] = {}
        for did in dids:
            name = names.get(did, "")
            if not name or has_cjk(name):
                continue  # 已是中文（用户改名或国内默认名），不替换
            spec_type = self._device_index.get(did, ("", "", ""))[1]
            spec = self._spec_cache.get(spec_type) if spec_type else None
            if not spec:
                continue
            spec_name = str(spec.get("name") or "")
            if spec_name and has_cjk(spec_name) and spec_name != name:
                result[did] = spec_name
        return result


# 卡片副标题默认显示的两项；插座的功率这类需求由用户自选覆盖
_DEFAULT_METRICS = ("temperature", "relative-humidity")

# 属性名 -> 展示单位。规格里的 unit 字段大量为 null，不能依赖，
# 这里按 SI 标准属性名兜底；未收录的属性只显示数值不带单位
_UNIT_BY_PROP = {
    "temperature": "°C",
    "relative-humidity": "%",
    "humidity": "%",
    "battery-level": "%",
    "power": "W",
    "electric-power": "W",
    "voltage": "V",
    "electric-current": "A",
    "power-consumption": "kWh",
    "pm2.5-density": "μg/m³",
    "co2-density": "ppm",
    "illumination": "lx",
}


def format_metrics_text(values: dict[str, float]) -> str | None:
    """把若干属性读数拼成卡片副标题文案；全无效时返回 None。

    values 的键是属性名，插入顺序即展示顺序。卡片副标题与详情面板
    回读共用，避免量纲启发式两处漂移。
    """
    parts: list[str] = []
    for name, value in values.items():
        try:
            parts.append(format_metric(name, float(value)))
        except (TypeError, ValueError):
            continue
    return " ".join(parts) if parts else None


def format_metric(name: str, value: float) -> str:
    """按属性名决定量纲换算与小数位。

    SI 规范里温度、湿度常用 0.1 步进的整数存储（283 = 28.3°C、
    683 = 68.3%），超出合理范围时按十分之一还原；功率这类量级
    差异大的直接保留一位小数。
    """
    if name == "temperature":
        if abs(value) > 60:
            value /= 10
        return f"{value:.1f}°C"
    if name in ("relative-humidity", "humidity"):
        if value > 100:
            value /= 10
        return f"{value:.0f}%"
    if name == "battery-level":
        return f"{round(value)}%"
    if name in ("power", "electric-power"):
        return f"{value:.1f}W"
    if name == "power-consumption":
        return f"{value:.2f}kWh"
    unit = _UNIT_BY_PROP.get(name, "")
    if not unit:
        return f"{value:g}"
    # 带单位的整数量纲（电压、电流等）保留一位小数足够
    return f"{value:.1f}{unit}"
