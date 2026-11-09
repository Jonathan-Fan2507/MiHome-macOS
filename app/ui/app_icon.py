# SPDX-License-Identifier: GPL-3.0-or-later
# MiHome-Windows: 米家设备的 Windows 桌面控制端
# Copyright (C) 2026 MiHome-Windows contributors
"""应用窗口和 macOS 菜单栏图标。"""

import qtawesome as qta
from PySide6.QtCore import QSize
from PySide6.QtGui import QIcon, QPixmap

from app import IS_MACOS, resource_path


def app_icon() -> QIcon:
    if IS_MACOS:
        return qta.icon("mdi.home-outline", color="#007f71")
    return QIcon(str(resource_path("app/ui/icon.png")))


def app_icon_pixmap(size: int) -> QPixmap:
    return app_icon().pixmap(QSize(size, size))


def menubar_icon(color: str | None = None) -> QIcon:
    icon = qta.icon("mdi.home-outline", color=color or "#000000")
    icon.setIsMask(color is None)
    return icon
