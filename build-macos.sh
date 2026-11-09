#!/bin/bash
set -euo pipefail

cd "$(dirname "$0")"
python_path="${MIHOME_PYTHON:-.venv/bin/python}"
export PYINSTALLER_CONFIG_DIR="${PYINSTALLER_CONFIG_DIR:-$PWD/build/pyinstaller-cache}"
# Preserve the TLS policy file when building with the bundled Codex Python.
python_stdlib="$("$python_path" -c 'import sysconfig; print(sysconfig.get_path("stdlib"))')"
extra_data=()
if [[ -f "$python_stdlib/codex-rejected-cas.pem" ]]; then
    extra_data+=(--add-data "$python_stdlib/codex-rejected-cas.pem:.")
fi

"$python_path" -m PyInstaller ${extra_data[@]+"${extra_data[@]}"} --noconfirm --windowed \
    --name MiHome --osx-bundle-identifier io.github.huanyuejue.mihome.macos \
    run.py

# PyInstaller 的命令行没有版本参数，生成的 Info.plist 版本号固定是 0.0.0（Finder「显示简介」里看到的就是它）。
# 这里写入 app/__init__.py 的 __version__。改 Info.plist 会让原有签名失效，所以随后重新做一次 ad-hoc 签名，
# 只重签外层 .app；里面已由 PyInstaller 签好的动态库和框架保持不变。
bundle="dist/MiHome.app"
plist="$bundle/Contents/Info.plist"
app_version="$(sed -n 's/^__version__ = "\(.*\)"$/\1/p' app/__init__.py)"
# 每次构建都换一个构建号（CFBundleVersion）。系统的 chronod 按「版本号」缓存控制中心控件的描述，
# 版本号不变就一直沿用旧描述：改了控件配置（例如 promptsForUserConfiguration）重新安装也不生效。
build_number="${MIHOME_BUILD_NUMBER:-$(date +%y%m%d.%H%M%S)}"
if [[ -n "$app_version" ]]; then
    /usr/libexec/PlistBuddy -c "Set :CFBundleShortVersionString $app_version" "$plist"
else
    echo "warning: 没能从 app/__init__.py 读到 __version__，Info.plist 保持 PyInstaller 默认版本号" >&2
    app_version="$(/usr/libexec/PlistBuddy -c "Print :CFBundleShortVersionString" "$plist")"
fi
/usr/libexec/PlistBuddy -c "Delete :CFBundleVersion" "$plist" 2>/dev/null || true
/usr/libexec/PlistBuddy -c "Add :CFBundleVersion string $build_number" "$plist"

# 控制中心控件（可选）：设置 MIHOME_CONTROL_GROUP（以 Team ID 开头的 App Group，如 TEAMID.io.github.huanyuejue.mihome）
# 和 MIHOME_SIGN_IDENTITY（该团队的签名身份）后，构建并嵌入 MiHomeControls.appex。
# 已有构建好的扩展可用 MIHOME_CONTROL_EXTENSION 指定，脚本仍会写入本次构建号并重新签名。
if [[ -n "${MIHOME_CONTROL_GROUP:-}" ]]; then
    : "${MIHOME_SIGN_IDENTITY:?Set the signing identity of the team that owns MIHOME_CONTROL_GROUP}"
    controls_dir="$PWD/build/controls"
    mkdir -p "$controls_dir"
    sed "s/\$(MIHOME_CONTROL_GROUP)/$MIHOME_CONTROL_GROUP/g" \
        macos/ControlCenter/Controls.entitlements > "$controls_dir/MiHomeControls.entitlements"
    host_entitlements="${MIHOME_HOST_ENTITLEMENTS:-$controls_dir/MiHome.entitlements}"
    if [[ -z "${MIHOME_HOST_ENTITLEMENTS:-}" ]]; then
        cat > "$host_entitlements" <<EOF
<?xml version="1.0" encoding="UTF-8"?>
<!DOCTYPE plist PUBLIC "-//Apple//DTD PLIST 1.0//EN" "http://www.apple.com/DTDs/PropertyList-1.0.dtd">
<plist version="1.0"><dict>
<key>com.apple.security.application-groups</key><array><string>$MIHOME_CONTROL_GROUP</string></array>
</dict></plist>
EOF
    fi

    extension="${MIHOME_CONTROL_EXTENSION:-$controls_dir/products/Release/MiHomeControls.appex}"
    if [[ -z "${MIHOME_CONTROL_EXTENSION:-}" ]]; then
        xcodebuild -quiet -project macos/ControlCenter/ControlCenter.xcodeproj \
            -target MiHomeControls -configuration Release CODE_SIGNING_ALLOWED=NO \
            MIHOME_CONTROL_GROUP="$MIHOME_CONTROL_GROUP" \
            MARKETING_VERSION="$app_version" CURRENT_PROJECT_VERSION="$build_number" \
            SYMROOT="$controls_dir/products" OBJROOT="$controls_dir/objects" \
            CLANG_MODULE_CACHE_PATH="$controls_dir/clang-cache" \
            SWIFT_MODULE_CACHE_PATH="$controls_dir/swift-cache"
    fi
    embedded="$bundle/Contents/PlugIns/MiHomeControls.appex"
    rm -rf "$embedded"
    mkdir -p "$bundle/Contents/PlugIns"
    ditto "$extension" "$embedded"
    extension_plist="$embedded/Contents/Info.plist"
    if [[ "$(/usr/libexec/PlistBuddy -c 'Print :MiHomeControlGroup' "$extension_plist")" != "$MIHOME_CONTROL_GROUP" ]]; then
        echo "error: widget and host App Groups differ" >&2
        exit 1
    fi
    /usr/libexec/PlistBuddy -c "Set :CFBundleShortVersionString $app_version" "$extension_plist"
    /usr/libexec/PlistBuddy -c "Set :CFBundleVersion $build_number" "$extension_plist"
    codesign --force --sign "$MIHOME_SIGN_IDENTITY" \
        --entitlements "$controls_dir/MiHomeControls.entitlements" "$embedded"

    # 主程序（Python）经 ctypes 调用这个小库，请系统重载控件，让控件跟上设备的实际开关状态
    reloader="$bundle/Contents/Frameworks/libMiHomeControlBridge.dylib"
    slices=()
    for arch in $(lipo -archs "$bundle/Contents/MacOS/MiHome"); do
        xcrun swiftc -O -emit-library -module-name MiHomeControlBridge \
            -sdk "$(xcrun --sdk macosx --show-sdk-path)" -target "$arch-apple-macos26.0" \
            -module-cache-path "$controls_dir/swift-cache" \
            -Xlinker -install_name -Xlinker @rpath/libMiHomeControlBridge.dylib \
            -o "$controls_dir/libMiHomeControlBridge-$arch.dylib" macos/ControlCenter/ControlReloader.swift
        slices+=("$controls_dir/libMiHomeControlBridge-$arch.dylib")
    done
    lipo -create "${slices[@]}" -output "$reloader"
    codesign --force --sign "$MIHOME_SIGN_IDENTITY" "$reloader"

    /usr/libexec/PlistBuddy -c "Add :MiHomeControlGroup string $MIHOME_CONTROL_GROUP" "$plist"
    if [[ -n "${MIHOME_HOST_PROFILE:-}" ]]; then
        cp "$MIHOME_HOST_PROFILE" "$bundle/Contents/embedded.provisionprofile"
    fi
    codesign --force --sign "$MIHOME_SIGN_IDENTITY" --entitlements "$host_entitlements" "$bundle"
else
    codesign --force --sign - "$bundle"
fi
codesign --verify --deep --strict "$bundle"

# Finder / Dock 按包的修改时间刷新图标缓存；同一路径重建后不 touch 可能还显示旧图标
touch "$bundle"
echo "built $bundle (version ${app_version:-unknown} build $build_number)"
