@echo off
chcp 65001 >nul
title Virtual-lover 一键停止
echo ============================================================
echo   她的一键停止：vl 进程 + 本地大脑 + GSVI 语音 + napcat 全停
echo ============================================================
pause
cd /d "%~dp0"
rem 解释器统一 .venv 优先（与 scripts/backup.bat 同款三行；依赖只装在 .venv）
set "VLPY=python"
if exist ".venv\Scripts\python.exe" set "VLPY=.venv\Scripts\python.exe"
%VLPY% scripts/service.py stop
pause
