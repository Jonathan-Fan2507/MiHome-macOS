#!/bin/bash
set -euo pipefail

cd "$(dirname "$0")"
if [[ "$(uname -s)" != Darwin ]]; then
    echo "此脚本仅适用于 macOS。" >&2
    exit 1
fi

venv=".venv"
if [[ ! -x "$venv/bin/python" ]]; then
    python_path="${MIHOME_PYTHON:-python3}"
    if ! command -v "$python_path" >/dev/null || ! "$python_path" -c 'import sys; sys.exit(sys.version_info < (3, 10))'; then
        echo "请先安装 Python 3.10 或更新版本：https://www.python.org/downloads/macos/" >&2
        exit 1
    fi
    "$python_path" -m venv "$venv"
fi

# 只在首次安装、安装中断或依赖声明有变更时访问软件包仓库。
if [[ ! -f "$venv/.mihome-installed" || pyproject.toml -nt "$venv/.mihome-installed" ]]; then
    "$venv/bin/python" -m pip install -e .
    touch "$venv/.mihome-installed"
fi

exec "$venv/bin/python" run.py "$@"
