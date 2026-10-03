@echo off
REM Resilient download toolchain - self test.
REM NOTE: keep this file GBK-encoded (project rule 8). No CJK in echo output.
setlocal
set "PY=C:\Users\wsz945\.workbuddy\binaries\python\envs\default\Scripts\python.exe"
set "DL=E:\AI\_scripts\dl\dl.py"

if not exist "%PY%" set "PY=C:\Users\wsz945\.workbuddy\binaries\python\versions\3.13.12\python.exe"

if not "%~1"=="" goto passthrough

echo ==================================================
echo  dl toolchain self-test  (caps / route / probe)
echo ==================================================
echo.
echo [1/4] capability manifest (machine readable)
"%PY%" -u "%DL%" caps
echo.
echo [2/4] routing decision (no bytes transferred)
"%PY%" -u "%DL%" route Qwen/Qwen2-0.5B
echo.
echo [3/4] mirror probe (real network)
"%PY%" -u "%DL%" probe
echo.
echo [4/4] end-to-end download + integrity
"%PY%" -u "%DL%" pypi six six-1.16.0.tar.gz -d "%TEMP%\dl-selftest"
echo.
echo Expected sha256: 1e61c37477a1626458e36f7b1d82aa5c9b094fa4802892072e49de9c60c4c926
echo.
echo ---- usage ----
echo   dl caps --json                  machine-readable manifest
echo   dl route ^<target^>              explain the flow, download nothing
echo   dl pypi ^<pkg^> ^<file^> -d ^<dir^>
echo   dl hf ^<repo^> -d ^<dir^> [--allow "*.safetensors"] [-p --proxy-budget 512MB]
echo   dl url ^<url^> -o ^<path^>
echo   dl health                       cached health, no re-probe
echo   add --json to any command for the raw envelope
echo.
echo Full contract: E:\AI\_scripts\dl\CONTRACT.md
pause
goto :eof

:passthrough
"%PY%" -u "%DL%" %*
endlocal
