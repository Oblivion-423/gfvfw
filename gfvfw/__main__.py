# GFVFW 联队管理系统
#
# 启动方式：
#   .venv\Scripts\python.exe -m gfvfw
# 或指定端口：
#   .venv\Scripts\python.exe -m gfvfw --host 127.0.0.1 --port 8000
#
# 生产环境（反向代理之后）：
#   .venv/bin/python -m gfvfw --host 127.0.0.1 --port 8000 --proxy-headers
#
# 首次使用请先创建管理员账号：
#   .venv\Scripts\python.exe -m gfvfw.cli create-admin --callsign <呼号>

from __future__ import annotations

import argparse
import logging

import uvicorn

from .config import settings


def main() -> None:
    parser = argparse.ArgumentParser(prog="gfvfw", description="启动 GFVFW 网站")
    parser.add_argument("--host", default="127.0.0.1",
                        help="监听地址（生产环境应置于反向代理之后，保持 127.0.0.1）")
    parser.add_argument("--port", type=int, default=8000)
    parser.add_argument("--reload", action="store_true", help="开发模式自动重载")
    parser.add_argument("--log-level", default="info")
    # ⚠️ 置于反向代理之后**必须**打开，否则 request.client.host 恒为 127.0.0.1、
    #    request.url.scheme 恒为 http —— 审计里的 IP 会全变成代理地址，
    #    登录限流与防刷也就失去了区分度。
    parser.add_argument("--proxy-headers", action="store_true",
                        help="采信 X-Forwarded-For/Proto（反向代理部署时开启）")
    parser.add_argument("--forwarded-allow-ips", default=None,
                        help="允许提供转发头的来源 IP（逗号分隔）。"
                             "默认取 GFVFW_TRUSTED_PROXY_IPS")
    parser.add_argument("--workers", type=int, default=1,
                        help="工作进程数。⚠️ 保持 1 —— SQLite 是单写者，"
                             "多进程会写冲突（见 deploy/DEPLOY.md）")
    args = parser.parse_args()

    logging.basicConfig(
        level=args.log_level.upper(),
        format="%(asctime)s %(levelname)-7s %(name)s: %(message)s",
    )

    allow_ips = args.forwarded_allow_ips or settings.trusted_proxy_ips

    uvicorn.run(
        "gfvfw.web.app:app",
        host=args.host,
        port=args.port,
        reload=args.reload,
        log_level=args.log_level,
        proxy_headers=args.proxy_headers,
        forwarded_allow_ips=allow_ips,
        workers=args.workers,
    )


if __name__ == "__main__":
    main()
