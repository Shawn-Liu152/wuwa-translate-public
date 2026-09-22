# -*- coding: utf-8 -*-
"""Mini-launcher：复用工作台官方配置流程，但把 bootstrap URL 打印出来"""
import argparse
import secrets
import socket
import sys
import threading
import time
import urllib.parse
from pathlib import Path

HOST = "127.0.0.1"

parser = argparse.ArgumentParser(add_help=False)
parser.add_argument("--project-root", default="")
args, _ = parser.parse_known_args()

project_root = Path(args.project_root or Path(__file__).resolve().parents[1]).resolve()
sys.path.insert(0, str(project_root))
sys.path.insert(0, str(project_root / "web"))

from web import app as app_module  # noqa: E402
from web.launcher import _bind_loopback_socket  # noqa: E402

listener = _bind_loopback_socket()
port = int(listener.getsockname()[1])

session_token = secrets.token_urlsafe(32)
bootstrap_token = secrets.token_urlsafe(32)
csrf_token = secrets.token_urlsafe(32)
launch_challenge = secrets.token_urlsafe(32)
app_module.configure_local_access(
    session_token=session_token,
    bootstrap_token=bootstrap_token,
    csrf_token=csrf_token,
    launch_challenge=launch_challenge,
)

import uvicorn  # noqa: E402

config = uvicorn.Config(
    app_module.app,
    host=HOST,
    port=port,
    access_log=False,
    proxy_headers=False,
    server_header=False,
    limit_concurrency=64,
    timeout_keep_alive=5,
)
server = uvicorn.Server(config)
fragment = urllib.parse.quote(bootstrap_token, safe="")
print(f"WORKBENCH_URL=http://{HOST}:{port}/app#bootstrap={fragment}", flush=True)
print(f"WORKBENCH_PORT={port}", flush=True)
try:
    server.run(sockets=[listener])
finally:
    listener.close()