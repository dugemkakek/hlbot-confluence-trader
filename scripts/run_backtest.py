"""Inline backtest runner — calls the harness directly via asyncio.run."""
import asyncio
import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).parent.parent))

from src.backtest.runner import main

if __name__ == "__main__":
    sys.exit(asyncio.run(main()))
