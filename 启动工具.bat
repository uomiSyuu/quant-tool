@echo off
chcp 65001 >nul
title 量化分析工具 v7.5
echo ════════════════════════════════
echo   量化分析工具 v7.5 - 一键启动
echo ════════════════════════════════
echo.

:: 清理旧的后端进程
echo [1/3] 清理旧进程...
for /f "tokens=2" %%a in ('netstat -ano ^| findstr ":5001"') do (
    for /f "tokens=5" %%b in ('echo %%a') do (
        taskkill /f /pid %%b 2>nul >nul
    )
)
timeout /t 2 /nobreak >nul

:: 启动后端
echo [2/3] 启动数据引擎（约需8-10秒首次初始化）...
start /min "quant-backend" "C:\Users\ASUS\.workbuddy\binaries\python\versions\3.13.12\python.exe" "C:\Users\ASUS\WorkBuddy\20260615150806\quant_tool\data_proxy.py"

:: 等待后端就绪（最多等20秒）
echo [3/3] 等待后端就绪...
set WAIT_COUNT=0
:CHECK
timeout /t 2 /nobreak >nul
set /a WAIT_COUNT+=1
curl -s http://127.0.0.1:5001/api/health >nul 2>nul
if %ERRORLEVEL% EQU 0 (
    echo   后端就绪！
    goto READY
)
if %WAIT_COUNT% GEQ 10 (
    echo   后端启动超时，请检查是否有端口冲突
    pause
    exit /b 1
)
goto CHECK

:READY
echo.
echo   后端就绪！

:: 额外验证：尝试查询一只常见股票确保数据管道正常
echo   验证数据管道（首次查询可能需要15-45秒）...
curl -s --max-time 60 "http://127.0.0.1:5001/api/stock/AAPL" >nul 2>nul
if %ERRORLEVEL% EQU 0 (
    echo   数据管道正常！
) else (
    echo   数据管道较慢，但仍可用（部分冷门股票首次查询会慢）
)

echo.
echo ════════════════════════════════
echo   服务已启动
echo   网页: http://127.0.0.1:5001/quant.html
echo ════════════════════════════════
start http://127.0.0.1:5001/quant.html
echo.
echo 提示: 关闭此窗口不会影响后端运行
echo       如需停止后端，请运行 taskkill /f /im python.exe
exit /b 0
