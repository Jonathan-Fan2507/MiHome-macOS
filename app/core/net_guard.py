# SPDX-License-Identifier: GPL-3.0-or-later
# MiHome-Windows: 米家设备的 Windows 桌面控制端
# Copyright (C) 2026 MiHome-Windows contributors
"""给上游 HTTP 调用补一个默认超时。

上游 mijiaAPI 里有几处 `requests.get(...)` 没有 timeout（登录取二维码、
刷新 token、设备列表都在其中）。requests 在不给 timeout 时会一直等下去，
在网络被墙、代理失效、IPv6 路由不通的机器上表现为**界面无限转圈**：
用户看到的是"一直在加载"，日志里一片安静，既没法判断也没法重试。

这里在会话层注入默认超时，只影响"没显式指定 timeout"的调用：

    install_default_timeout((30, 30))   # 连接 30s，读 30s

显式传了 timeout 的调用保持原样 —— 扫码长轮询自己传了 120s，不能被改短。

**连接超时为什么是 30 秒**：连接超时同时约束 TCP 建连、走 HTTP 代理时的
CONNECT 隧道和 TLS 握手；握手阶段超时会被 urllib3 报成
「Read timed out. (read timeout=<连接超时>)」——日志里的「10 秒读超时」
其实就是旧的 10 秒连接超时。实测（2026-11-07，macOS + 系统代理）
api.mijia.tech / sts.api.mijia.tech 走代理节点时，能成功的握手分布在
4~28 秒（应用内两次成功的会话校验分别用了 24.6 秒和 25.4 秒），10 秒
几乎必然失败；同一时段直连的 account.xiaomi.com 只要 0.05 秒。30 秒
覆盖了实测里全部「慢但能通」的握手；彻底不通时界面会提示并自动重试
（用户主动操作的只读请求另有一次重试，见 service._MijiaClient）。
"""

import logging

import requests
from requests.sessions import Session

logger = logging.getLogger(__name__)

DEFAULT_TIMEOUT = (30.0, 30.0)

_installed = False


def install_default_timeout(timeout: tuple[float, float] = DEFAULT_TIMEOUT) -> None:
    """给 requests 会话注入默认超时（幂等）。"""
    global _installed
    if _installed:
        return
    original_request = Session.request

    def request(self, method, url, **kwargs):
        if kwargs.get("timeout") is None:
            kwargs["timeout"] = timeout
        return original_request(self, method, url, **kwargs)

    Session.request = request
    _installed = True
    logger.debug("已注入 requests 默认超时 %s", timeout)
