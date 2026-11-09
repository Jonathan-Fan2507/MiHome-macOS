# SPDX-License-Identifier: GPL-3.0-or-later
"""按系统要求取得 macOS App Group 容器路径。

macOS 15 起 ~/Library/Group Containers 受系统完整性保护（见 macOS 15 发行说明）：
不经 Mac App Store 分发的应用，只有**通过**
`-[NSFileManager containerURLForSecurityApplicationGroupIdentifier:]` 取得容器路径、
且组标识以本应用的 Team ID 开头，才算这个 App Group 的成员。

自己拼 `~/Library/Group Containers/<组>` 的路径会被当成「访问其他应用的数据」：
系统弹窗「MiHome 想要访问其他 App 的数据」，弹窗期间发起访问的线程一直卡住
（实测主线程卡了 30 秒），同意也只对本次运行有效，下次启动再弹；点了「不允许」
控制中心通信就静默失效。所以这里经 Objective-C 运行时调用 Foundation，不手拼路径。
"""

import ctypes
import logging
from pathlib import Path

logger = logging.getLogger(__name__)

_OBJC = "/usr/lib/libobjc.A.dylib"
_FOUNDATION = "/System/Library/Frameworks/Foundation.framework/Foundation"


def container_path(identifier: str) -> Path | None:
    """返回组容器目录；系统拒绝或不在 macOS 上时返回 None。"""
    try:
        objc = ctypes.CDLL(_OBJC)
        ctypes.CDLL(_FOUNDATION)
    except OSError:
        return None
    objc.objc_getClass.restype = ctypes.c_void_p
    objc.objc_getClass.argtypes = [ctypes.c_char_p]
    objc.sel_registerName.restype = ctypes.c_void_p
    objc.sel_registerName.argtypes = [ctypes.c_char_p]
    objc.objc_autoreleasePoolPush.restype = ctypes.c_void_p
    objc.objc_autoreleasePoolPush.argtypes = []
    objc.objc_autoreleasePoolPop.restype = None
    objc.objc_autoreleasePoolPop.argtypes = [ctypes.c_void_p]
    msg_send = ctypes.cast(objc.objc_msgSend, ctypes.c_void_p).value

    def send(restype, *argtypes):
        # arm64 上 objc_msgSend 必须按确切原型调用（不能当可变参数函数用）
        return ctypes.CFUNCTYPE(restype, ctypes.c_void_p, ctypes.c_void_p,
                                *argtypes)(msg_send)

    def sel(name: bytes) -> int:
        return objc.sel_registerName(name)

    pool = objc.objc_autoreleasePoolPush()
    try:
        name = send(ctypes.c_void_p, ctypes.c_char_p)(
            objc.objc_getClass(b"NSString"), sel(b"stringWithUTF8String:"),
            identifier.encode("utf-8"))
        manager = send(ctypes.c_void_p)(
            objc.objc_getClass(b"NSFileManager"), sel(b"defaultManager"))
        if not name or not manager:
            return None
        url = send(ctypes.c_void_p, ctypes.c_void_p)(
            manager, sel(b"containerURLForSecurityApplicationGroupIdentifier:"), name)
        if not url:
            logger.warning("系统没有返回 App Group 容器：%s", identifier)
            return None
        raw = send(ctypes.c_char_p)(url, sel(b"fileSystemRepresentation"))
        return Path(raw.decode("utf-8")) if raw else None
    finally:
        objc.objc_autoreleasePoolPop(pool)
