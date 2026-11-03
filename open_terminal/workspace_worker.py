"""由工作区网关在 bubblewrap 内启动；不监听宿主 TCP 端口。"""

import json
import os
import sys


def main():
    values = json.loads(sys.stdin.buffer.readline())
    os.environ.clear()
    os.environ.update(values)
    workspace = values["OPEN_TERMINAL_WORKSPACE_DIR"]
    for name in ("uploads", "generated", "tmp", "cache"):
        os.makedirs(os.path.join(workspace, name), mode=0o700, exist_ok=True)
    os.chdir(workspace)
    import uvicorn

    uvicorn.run("open_terminal.main:app", uds="/run/open-terminal/worker.sock", proxy_headers=False, log_level="warning", access_log=False)


if __name__ == "__main__":
    main()
