"""Load the Control secret and the account's own env file (CARME_ENV_FILE pins it to
/config/.env inside the container; a host repository .env can never be reached)."""
import os
from pathlib import Path

os.environ["CARME_TOKEN"] = Path("/run/secrets/control-token").read_text().strip()
os.environ["CARME_CONTAINER_CONTROL"] = "1"
os.environ.pop("CARME_DEV_NO_AUTH", None)
import uvicorn

# uvicorn 默认 keep-alive 只有 5 秒；网关的 httpx 连接池和 cloudflared 的源站连接池都会复用到
# 已被服务端关掉的连接，拿到 RST 后表现为 Cloudflare 502。服务端必须比客户端更晚关连接。
uvicorn.run("carme.app:app", host="0.0.0.0", port=8899, access_log=False, timeout_keep_alive=120)
