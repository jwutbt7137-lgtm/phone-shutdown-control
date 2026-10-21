@echo off
chcp 65001 >nul
setlocal

cd /d "%~dp0"

rem 优先用 PATH 里的 python，其次用 Windows 的 py 启动器
set "PY=python"
where python >nul 2>nul || set "PY=py -3"
%PY% -c "import sys" >nul 2>nul
if errorlevel 1 (
  echo.
  echo   [X] 没有找到 Python。
  echo       请先安装 Python 3.9 或更高版本： https://www.python.org/downloads/
  echo       安装时记得勾选 "Add Python to PATH"。
  echo.
  pause
  exit /b 1
)

echo.
echo   正在启动电脑端蓝牙关机服务...
echo   关闭这个窗口 = 停止服务
echo.

%PY% "%~dp0pc_server.py" %*

echo.
echo   服务已退出。
pause
