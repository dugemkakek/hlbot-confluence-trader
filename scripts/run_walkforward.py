"""Run walk-forward backtest from CLI."""
import asyncio
import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).parent.parent))
from src.backtest.walkforward import main

if __name__ == "__main__":
    sys.exit(asyncio.run(main()))
