@echo off
chcp 65001 >nul
setlocal
cd /d "%~dp0"

echo ============================================
echo   Codex 额度挂件  -  安装
echo ============================================
echo.

REM ---- 1. 找 Python ----
set "PY="
for %%P in (
  "%LocalAppData%\Programs\Python\Python314\pythonw.exe"
  "%LocalAppData%\Programs\Python\Python313\pythonw.exe"
  "%LocalAppData%\Programs\Python\Python312\pythonw.exe"
  "C:\Python314\pythonw.exe"
  "C:\Python313\pythonw.exe"
  "C:\Python312\pythonw.exe"
) do (
  if exist %%P if not defined PY set "PY=%%~P"
)
if not defined PY (
  for /f "delims=" %%P in ('where pythonw.exe 2^>nul') do (
    if not defined PY set "PY=%%P"
  )
)
if not defined PY (
  echo [X] 没找到 pythonw.exe
  echo     请先安装 Python 3.10 以上版本: https://www.python.org/downloads/
  echo     安装时记得勾选 "Add python.exe to PATH"
  pause
  exit /b 1
)
echo [1/3] Python: %PY%

REM ---- 2. 检查配置 ----
echo [2/3] 检查配置 ...
"%PY%" codex_quota_bar.py --check
if errorlevel 1 (
  echo.
  echo     ^! 上面的报错请先解决。配置在同目录的 config.json 里：
  echo         proxy          = 上游代理地址，例如 http://100.116.41.20:8317
  echo         management_key = 代理的管理密钥
  echo         auth_index     = 该代理里 Codex 账号的 auth_index
  echo.
  pause
  exit /b 1
)

REM ---- 3. 安装并启动 ----
echo [3/3] 写入开机自启并启动 ...
"%PY%" codex_quota_bar.py --install
echo.
echo 完成。挂件会出现在任务栏系统托盘左侧。
echo.
echo   自检     : python codex_quota_bar.py --check
echo   卸载     : 双击 uninstall.bat
echo.
pause