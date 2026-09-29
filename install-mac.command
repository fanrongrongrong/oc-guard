#!/bin/bash
cd "$(dirname "$0")"
command -v python3 >/dev/null || { echo "没装 Python，先去 https://www.python.org/downloads/ 装一个（3.10 以上）"; read -n1; exit 1; }
python3 -m venv .venv && .venv/bin/pip install -q -r requirements.txt && .venv/bin/python guard.py init
echo; echo "装好了。以后在这个文件夹里用 ./oc.sh embed 图.png --to \"发给谁\""; read -n1 -p "按任意键关闭"
