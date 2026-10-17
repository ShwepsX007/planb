#!/usr/bin/env python3
"""Entry point: ``python run.py`` (or ``uvicorn app.main:app``)."""
from __future__ import annotations

import os

import uvicorn

if __name__ == "__main__":
    host = os.environ.get("PLANB_HOST", "0.0.0.0")
    port = int(os.environ.get("PLANB_PORT", "8000"))
    uvicorn.run("app.main:app", host=host, port=port, log_level="info", access_log=False,
                proxy_headers=True, forwarded_allow_ips="*")
