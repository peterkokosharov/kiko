@echo off
rem SPDX-License-Identifier: MIT
rem Copyright (c) 2026 Peter Kokosharov <peter.kokosharov@gmail.com>
rem LLM: Anthropic Sonnet
setlocal
set KIKO_DIR=%~dp0
set KIKO_PY=%KIKO_DIR%python\python.exe

if not exist "%KIKO_PY%" (
    echo [kiko] ERROR: python\python.exe not found inside the kiko folder.
    echo        The build is incomplete; download a fresh copy.
    pause
    exit /b 1
)

rem One config folder for everything (kiko.toml, database, .env, logs): the data folder of this portable copy.
rem Ports and the database path come from kiko's own config (kikolib.config), nothing is written down here.
set KIKO_CONFIG_DIR=%KIKO_DIR%data
set PYTHONPATH=%KIKO_DIR%lib
set KIKO_YAML=%KIKO_DIR%kiko-config.yaml
if not exist "%KIKO_CONFIG_DIR%\" mkdir "%KIKO_CONFIG_DIR%"

set KIKO_PORT=
for /f "usebackq delims=" %%P in (`"%KIKO_PY%" -m kiko_lite config --get proxy_port`) do set KIKO_PORT=%%P
for /f "usebackq delims=" %%P in (`"%KIKO_PY%" -m kiko_lite config --get database_path`) do set KIKO_DB_PATH=%%P
if "%KIKO_PORT%"=="" (
    echo [kiko] ERROR: cannot read the port from the kiko configuration.
    pause
    exit /b 1
)

echo [kiko] Starting proxy on port %KIKO_PORT% ...
"%KIKO_PY%" "%KIKO_DIR%kiko_start.py" --config "%KIKO_YAML%" --port %KIKO_PORT%
