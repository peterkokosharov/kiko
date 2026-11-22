# SPDX-License-Identifier: MIT
# Windows 10/11 (Win32). From "x64 Native Tools Command Prompt for VS 2022":
#   powershell -ExecutionPolicy Bypass -File tray\tools\build-win10.ps1
$ErrorActionPreference = "Stop"
$root = (Resolve-Path (Join-Path $PSScriptRoot "..\..")).Path
$build = Join-Path $root "build\win10"
cmake -S (Join-Path $root "tray") -B $build -DKT_GUI=win -DKT_FETCH_CJSON=OFF -DCMAKE_BUILD_TYPE=Release
if ($LASTEXITCODE -ne 0) { exit $LASTEXITCODE }
cmake --build $build --config Release
if ($LASTEXITCODE -ne 0) { exit $LASTEXITCODE }
ctest --test-dir $build -C Release --output-on-failure
if ($LASTEXITCODE -ne 0) { exit $LASTEXITCODE }
Write-Host "binary: $build\Release\kiko-tray.exe"
