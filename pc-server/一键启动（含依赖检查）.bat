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
  echo   [X] 没找到 Python。请先安装 Python 3.9+ ： https://www.python.org/downloads/
  echo       安装时记得勾选 "Add Python to PATH"。
  echo.
  pause
  exit /b 1
)

echo.
echo   正在检查蓝牙依赖...
%PY% -c "import winrt.windows.devices.bluetooth.genericattributeprofile" >nul 2>nul
if errorlevel 1 (
  echo   缺少依赖，正在从清华镜像安装...
  %PY% -m pip install -i https://pypi.tuna.tsinghua.edu.cn/simple -r "%~dp0requirements.txt"
  if errorlevel 1 (
    echo.
    echo   [X] 依赖安装失败，请检查网络后重试。
    pause
    exit /b 1
  )
)

echo.
echo   正在自检...
%PY% "%~dp0pc_server.py" --self-test
if errorlevel 1 (
  echo.
  echo   [X] 自检未通过，请按上面的提示处理。详见 README.md
  pause
  exit /b 1
)

echo.
echo   自检通过，启动服务。关闭这个窗口 = 停止服务。
echo.
%PY% "%~dp0pc_server.py" %*

echo.
echo   服务已退出。
pause
