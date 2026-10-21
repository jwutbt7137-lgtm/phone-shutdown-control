@echo off
chcp 65001 >nul
setlocal

cd /d "%~dp0"

set PY=
where python >nul 2>nul && set PY=python
if "%PY%"=="" (
  if exist "C:\Python314\python.exe" set PY=C:\Python314\python.exe
)

if "%PY%"=="" (
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

"%PY%" "%~dp0pc_server.py" %*

echo.
echo   服务已退出。
pause
