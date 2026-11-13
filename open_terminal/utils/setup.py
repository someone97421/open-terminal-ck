"""非隔离会话的默认起始目录；隔离工作区沿用其持久化 cwd。"""

import os

from open_terminal.env import START_DIR


def start_directory(home: str) -> str:
    value = START_DIR
    if not value or value == "~":
        return home
    if value.startswith("~/"):
        value = value[2:]
    root = os.path.realpath(home)
    path = os.path.realpath(os.path.join(home, value))
    if path != root and not path.startswith(root + os.sep):
        return home
    return path if os.path.isdir(path) else home
