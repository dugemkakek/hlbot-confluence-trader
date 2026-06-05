@echo off
REM HLBot — Windows test runner (mirrors `make test` in the Makefile)
REM Usage: test.bat [target]
REM   test            -> run full test suite (default)
REM   test fast       -> skip slow tests
REM   test cov        -> run with coverage
REM   test lint       -> run ruff + mypy
REM   test smoke      -> run exchange adapter smoke test
REM   test backtest   -> run walk-forward backtest
REM   test calibrate  -> run strategy calibration sweep
REM   test run        -> start the live bot
REM Requires: Python 3.11+ on PATH (or py launcher)

setlocal
set PYTHON=py -3

if "%1"=="" goto :test
if "%1"=="fast" goto :fast
if "%1"=="cov" goto :cov
if "%1"=="lint" goto :lint
if "%1"=="smoke" goto :smoke
if "%1"=="backtest" goto :backtest
if "%1"=="calibrate" goto :calibrate
if "%1"=="run" goto :run
if "%1"=="help" goto :help
echo Unknown target: %1
goto :help

:test
%PYTHON% -m pytest -v
goto :eof

:fast
%PYTHON% -m pytest -v -m "not slow"
goto :eof

:cov
%PYTHON% -m pytest -v --cov=src --cov-report=term-missing
goto :eof

:lint
%PYTHON% -m ruff check src tests
%PYTHON% -m mypy src
goto :eof

:smoke
%PYTHON% scripts\test_adapter_smoke.py
goto :eof

:backtest
%PYTHON% scripts\run_backtest.py
goto :eof

:calibrate
%PYTHON% scripts\run_calibration.py
goto :eof

:run
%PYTHON% -m uvicorn src.api.main:create_app --factory --host 0.0.0.0 --port 8000
goto :eof

:help
echo HLBot Windows test runner
echo Usage: test.bat [target]
echo   test            full test suite
echo   test fast       skip slow tests
echo   test cov        with coverage
echo   test lint       ruff + mypy
echo   test smoke      exchange adapter smoke
echo   test backtest   walk-forward backtest
echo   test calibrate  strategy calibration
echo   test run        start the live bot
goto :eof
