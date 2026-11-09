# SPDX-License-Identifier: GPL-3.0-or-later
# MiHome-Windows: 米家设备的 Windows 桌面控制端
# Copyright (C) 2026 MiHome-Windows contributors
"""macOS 首次登录必须扫码：记录「首次扫码登录已完成」。

本机可能早就有 mijiaAPI CLI 扫码留下的凭据（~/.config/mijia-api/auth.json，
与 CLI 共用，不另建文件）。macOS 版第一次登录时不直接沿用它，而是要求
用手机米家 App 扫码并确认一次；成功后在应用数据目录记一笔，之后只要
凭据有效就自动登录。扫码被取消或失败时不记录，下次启动仍需扫码。

标记单独放一个文件，不并进 settings.json：设置文件版本不符时会整份
回到默认值，那会让用户莫名其妙地被要求重新扫码。文件里只有完成时间，
不含账号信息或任何凭据。Windows 版不受影响（ENABLED 为 False）。
"""

from datetime import datetime

from app import IS_MACOS
from app.core import _json_store

# 测试可改写；Windows 上恒为关闭，登录流程与原来一致
ENABLED = IS_MACOS

_FILENAME = "login_state.json"
_VERSION = 1


def _path():
    return _json_store.data_file(_FILENAME)


def first_scan_required() -> bool:
    """还没有完成过一次扫码登录：这次登录不能沿用本机已有的凭据。"""
    if not ENABLED:
        return False
    raw = _json_store.read_json(_path(), {})
    return not (raw.get("version") == _VERSION and raw.get("first_qr_login_done") is True)


def mark_first_scan_done() -> None:
    """扫码登录成功后调用；写入失败时 _json_store 会留日志，下次启动仍会要求扫码。"""
    if not ENABLED:
        return
    _json_store.write_json(_path(), {
        "version": _VERSION,
        "first_qr_login_done": True,
        "completed_at": datetime.now().astimezone().isoformat(timespec="seconds"),
    })
