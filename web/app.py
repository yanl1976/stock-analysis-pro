#!/usr/bin/env python
# -*- coding: utf-8 -*-
"""股票分析系统 Web 仪表盘 — Flask 应用入口.

启动: python web/app.py  或  python core/cli.py dashboard
访问: http://localhost:8500
"""
import os
import sys

BASE_DIR = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
sys.path.insert(0, BASE_DIR)

from flask import Flask
from web.api import dashboard_bp


def create_app():
    app = Flask(__name__, template_folder=os.path.join(BASE_DIR, "templates"))
    app.register_blueprint(dashboard_bp)
    return app


def main():
    host = os.environ.get("DASHBOARD_HOST", "0.0.0.0")
    port = int(os.environ.get("DASHBOARD_PORT", "8500"))
    debug = os.environ.get("DASHBOARD_DEBUG", "").lower() in ("1", "true", "yes")

    app = create_app()
    print(f"\n  Dashboard: http://{host if host != '0.0.0.0' else 'localhost'}:{port}\n")
    app.run(host=host, port=port, debug=debug)


if __name__ == "__main__":
    main()
