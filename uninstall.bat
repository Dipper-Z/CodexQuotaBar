@echo off
chcp 65001 >nul
setlocal
cd /d "%~dp0"

echo ============================================
echo   Codex 额度挂件  -  卸载
echo ============================================
echo.
echo 将取消开机自启并停止当前进程。
echo 目录内的文件会保留，如需彻底删除请手动删掉整个文件夹。
echo.

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
  echo [X] 没找到 pythonw.exe，请手动删除注册表项：
  echo     HKCU\Software\Microsoft\Windows\CurrentVersion\Run  ^>  CodexQuotaBar
  pause
  exit /b 1
)

"%PY%" codex_quota_bar.py --uninstall
echo.
pause