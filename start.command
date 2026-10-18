#!/bin/bash
# 双击启动 HS 归类助手：第一次会自动安装运行环境，并打开 .env 让你填 API Key。
cd "$(dirname "$0")" || exit 1

if ! command -v python3 >/dev/null 2>&1; then
  echo "没有找到 python3。请先安装 Python 3.10 或更高版本：https://www.python.org/downloads/"
  read -r -p "按回车关闭"; exit 1
fi

if [ ! -d .venv ]; then
  echo "第一次运行，正在安装运行环境…"
  python3 -m venv .venv || { read -r -p "创建运行环境失败，按回车关闭"; exit 1; }
  .venv/bin/pip install -q -r requirements.txt || { read -r -p "安装依赖失败，按回车关闭"; exit 1; }
fi
# requirements.txt 更新过就补装依赖
if [ requirements.txt -nt .venv/.deps_ok ]; then
  .venv/bin/pip install -q -r requirements.txt && touch .venv/.deps_ok
fi
# 在线查询海关注释需要浏览器内核，只在第一次下载
if [ ! -f .venv/.browser_ok ]; then
  .venv/bin/python -m playwright install chromium && touch .venv/.browser_ok
fi

if [ ! -f .env ]; then
  cp .env.example .env
fi
if ! grep -qE '^LLM_API_KEY=.+' .env; then
  echo "请在打开的 .env 文件里，把 DeepSeek 的 Key 填在 LLM_API_KEY= 后面，保存并关闭。"
  open -e .env
  read -r -p "填好后按回车继续"
fi

.venv/bin/python -m hs_agent.web
