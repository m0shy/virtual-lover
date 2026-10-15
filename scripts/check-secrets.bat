@echo off
cd /d "%~dp0\.."
title 提交前密钥自查

echo 正在扫描 Git 已跟踪文件中的疑似敏感信息...
powershell -NoProfile -ExecutionPolicy Bypass -File "scripts\check-secrets.ps1"
if errorlevel 1 (
    echo.
    echo [X] 发现疑似敏感信息，请先处理再提交/发布！
    pause
    exit /b 1
)
pause
