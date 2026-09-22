"""视觉验证专用本地服务：参数化端口与 token 文件，配合 tools/visual_verify.mjs。"""
import secrets
import sys
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT))

port = int(sys.argv[1])
token_file = Path(sys.argv[2])

from web import app as app_module  # noqa: E402

bootstrap = secrets.token_urlsafe(32)
token_file.write_text(bootstrap, encoding="utf-8")
app_module.configure_local_access(
    session_token=secrets.token_urlsafe(32),
    bootstrap_token=bootstrap,
    csrf_token=secrets.token_urlsafe(32),
    launch_challenge=secrets.token_urlsafe(32),
)

import uvicorn  # noqa: E402

uvicorn.run(app_module.app, host="127.0.0.1", port=port, access_log=False)
