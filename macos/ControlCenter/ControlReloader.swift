import WidgetKit

/// 供 Python 主程序经 ctypes 调用：请系统重新向扩展要所有 MiHome 控件的当前状态。
/// 必须在主程序进程里调用，系统按调用方所属的应用找到它的控件扩展。
@_cdecl("mihome_reload_controls")
public func mihomeReloadControls() {
    ControlCenter.shared.reloadAllControls()
}
