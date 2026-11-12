#!/usr/bin/env bash
# macOS / Linux launcher. Windows users: double-click start.bat instead.
cd "$(dirname "$0")" || exit 1

if ! command -v uv >/dev/null 2>&1; then
  echo "找不到 uv（這個工具用它來安裝與執行 Python 環境）。"
  echo "請先安裝 uv，再重新執行本程式："
  echo "  curl -LsSf https://astral.sh/uv/install.sh | sh"
  echo "  （macOS 也可以用： brew install uv）"
  echo "安裝說明： https://docs.astral.sh/uv/getting-started/installation/"
  exit 1
fi

exec uv run python run.py
