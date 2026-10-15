@echo off
chcp 65001 >nul
title Virtual-lover 一键启动
echo ============================================================
echo   她的一键启动（napcat + 大脑 + vl 进程）
echo   大脑按 vl.toml 当前配置自动联动（本地自动带起 8002）
echo   语音引擎首次开口自动拉起；vl 崩溃自动重启
echo ============================================================
pause
cd /d "%~dp0"
rem 解释器统一 .venv 优先（与 scripts/backup.bat 同款三行；依赖只装在 .venv）
set "VLPY=python"
if exist ".venv\Scripts\python.exe" set "VLPY=.venv\Scripts\python.exe"
%VLPY% scripts/service.py start
pause
