@echo off
cd /d "%~dp0\.."
title Virtual-lover 数据备份

echo ============================================
echo   Virtual-lover 数据备份
echo   （聊天记录/人格 + 新栈数据 vl_data + QQ 登录数据）
echo ============================================
echo.

set FAILED=0
choice /c YN /n /m "备份前先停止服务以保证数据一致？[Y=推荐 N=不停机备份]"
if errorlevel 2 goto livemode
echo 正在停止容器（NapCat；legacy 回退栈未运行时自动忽略）...
docker compose --profile legacy stop
goto backup
:livemode
echo [!] 不停机备份：数据库正在写入，恢复时可能丢失最后几条消息

:backup
for /f %%i in ('powershell -NoProfile -Command "Get-Date -Format yyyyMMdd_HHmmss"') do set TS=%%i
set BK=backup\%TS%
echo 备份到 %BK% （请稍候）...
mkdir "%BK%" 2>nul

rem 【O8 备份链路】复制前把 WAL 日志合并进主库并快照行数（WAL 化后必须，
rem 否则复制出的 .db 缺最新页）；脚本输出一律 ASCII，规避控制台 GBK 编码问题
set "VLPY=python"
if exist ".venv\Scripts\python.exe" set "VLPY=.venv\Scripts\python.exe"
%VLPY% scripts\backup_guard.py pre --data local\vl_data --snapshot "%BK%\vl_data\_row_counts.json"
if errorlevel 3 echo [!] 部分库 checkpoint 忙碌（服务写入中），备份可能不含最新几条消息
if errorlevel 1 if not errorlevel 3 ( set "FAILED=1" & goto finish )

rem 备份清单与 virtuallover\console\control.py 的 BACKUP_SOURCE_DIRS 保持一致（两处同步维护）
robocopy local\astrbot_data "%BK%\astrbot_data" /E /NFL /NDL /NJH /NJS /NP >nul
if errorlevel 8 ( set "FAILED=1" & goto finish )
robocopy local\napcat_data "%BK%\napcat_data" /E /NFL /NDL /NJH /NJS /NP >nul
if errorlevel 8 ( set "FAILED=1" & goto finish )
robocopy local\napcat_config "%BK%\napcat_config" /E /NFL /NDL /NJH /NJS /NP >nul
if errorlevel 8 ( set "FAILED=1" & goto finish )
robocopy local\shared_plugin_data "%BK%\shared_plugin_data" /E /NFL /NDL /NJH /NJS /NP >nul
if errorlevel 8 ( set "FAILED=1" & goto finish )
rem 新栈数据（历史库/向量库/token）：运行中跳过 SQLite 临时文件，与控制台备份口径一致；
rem voice_cache 语音缓存为可再生数据，不进备份（控制台 BACKUP_EXCLUDE_RELPATHS 同步排除）
robocopy local\vl_data "%BK%\vl_data" /E /NFL /NDL /NJH /NJS /NP /XF "*-wal" "*-shm" "*-journal" /XD voice_cache >nul
if errorlevel 8 ( set "FAILED=1" & goto finish )

rem 【O8 备份链路】备份后行数校验（复制前快照 vs 备份副本逐库比对；WAL 化后必须）
%VLPY% scripts\backup_guard.py verify --backup "%BK%\vl_data" --snapshot "%BK%\vl_data\_row_counts.json"
if errorlevel 1 ( set "FAILED=1" & goto finish )

:finish
if "%FAILED%"=="1" (
    echo [FAIL] 备份中断：备份不完整，但原数据未被修改。
) else (
    echo [OK] 备份完成：%BK%
)
echo.
choice /c YN /n /m "是否重新启动服务？[Y=是 N=否]"
if errorlevel 2 goto done
rem 只拉起默认组（NapCat）。若你正处于 legacy 回退模式，请手动运行：
rem   docker compose --profile legacy start
docker compose start
:done
echo.
pause
