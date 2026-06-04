#!/usr/bin/env python
"""Entry point for HLBot FastAPI server.

Usage:
    python -m src.scripts.run  [--host 0.0.0.0] [--port 8000]
"""

from __future__ import annotations

import argparse
import sys
from pathlib import Path

# Ensure project root is on path
_root = Path(__file__).parent.parent.parent
if str(_root) not in sys.path:
    sys.path.insert(0, str(_root))

import uvicorn
from src.api.main import create_app
from src.utils.logging import setup_logging


def main() -> None:
    parser = argparse.ArgumentParser(description="HLBot Trading AI — FastAPI Server")
    parser.add_argument("--host", default="0.0.0.0", help="Bind host")
    parser.add_argument("--port", type=int, default=8000, help="Bind port")
    parser.add_argument("--reload", action="store_true", help="Enable auto-reload (dev only)")
    args = parser.parse_args()

    setup_logging()

    app = create_app()
    uvicorn.run(
        app,
        host=args.host,
        port=args.port,
        reload=args.reload,
        log_level="info",
    )


if __name__ == "__main__":
    main()