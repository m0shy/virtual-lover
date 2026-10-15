@echo off
setlocal enabledelayedexpansion
cd /d "%~dp0"
title Virtual-lover 更新（vl 进程）

echo ============================================
echo   Virtual-lover 更新（备份 -^> 拉代码 -^> 依赖 -^> 重启 -^> 验证）
echo   （v2：她 = 宿主机 vl 进程 + napcat 容器；更新以 git 代码为准）
echo ============================================
echo.

rem ---- [1/5] 升级前必须备份（保命步骤，不可跳过）----
echo [1/5] 升级前备份（保命步骤，不可跳过）...
choice /c YN /n /m "     现在运行 scripts\backup.bat 完整备份？[Y=是 N=取消更新]"
if errorlevel 2 (
    echo [X] 未备份，已取消更新。请先备份再来。
    pause
    exit /b 1
)
call scripts\backup.bat
echo.

rem ---- [2/5] 拉取更新（vl 代码更新 = git 拉取；napcat 镜像 latest 由 compose 顺带检查）----
echo [2/5] 拉取最新代码（git pull --ff-only）...
git pull --ff-only
if errorlevel 1 (
    echo [!] git pull 失败：多为本地有未提交改动或网络问题。
    echo     处理：git stash 或 git status 查看后手工处理，再重跑本脚本。
    pause
    exit /b 1
)
echo.

rem ---- [3/5] 刷新依赖（运行依赖 + 测试依赖；幂等，装过即快）----
echo [3/5] 刷新 Python 依赖（.venv）...
if not exist ".venv\Scripts\python.exe" (
    echo [X] 未找到 .venv——请先运行 setup.bat 完成安装。
    pause
    exit /b 1
)
set "VLPY=.venv\Scripts\python.exe"
"%VLPY%" -m pip install -r requirements.txt -r requirements-dev.txt
if errorlevel 1 (
    echo [!] 依赖安装失败（多为网络问题）。可先继续（老依赖通常兼容），或修网重试。
)
echo.

rem ---- [4/5] 重启 vl 进程（先停后起；napcat 容器不用动，继续在线）----
echo [4/5] 重启 vl 主进程（napcat 容器保持在线，QQ 消息在重启期间会丢，属预期）...
call 一键停止.bat
call 一键启动.bat
if errorlevel 1 (
    echo [X] 重启失败——回滚见下方指引。
    goto rollbackhint
)
echo.

rem ---- [5/5] 验证：/api/status 就绪（200/401 均为服务在线）----
echo [5/5] 验证（/api/status；NapCat 反连扫码后自动恢复）...
set /a tries=0
:waitapi
set "VLCODE="
for /f "delims=" %%c in ('curl -s -o nul -m 3 -w "%%{http_code}" http://127.0.0.1:8787/api/status 2^>nul') do set "VLCODE=%%c"
if "%VLCODE%"=="200" goto apiok
if "%VLCODE%"=="401" goto apiok
set /a tries+=1
if %tries% gtr 40 (
    echo [!] 2 分钟未就绪——大概率更新翻车，走回滚：
    goto rollbackhint
)
timeout /t 3 /nobreak >nul
goto waitapi
:apiok
echo      [OK] 更新完成，她已重新上线（/api/status HTTP %VLCODE%）。
echo      QQ 未自动反连的话：QQ 掉线重扫（NapCat WebUI http://127.0.0.1:61990）。
echo.
echo ====== 如果更新后出问题（回滚三步） ======
echo   1. 回滚代码：git log --oneline -5 找更新前提交号，
echo      git checkout <提交号> -- . （或 git reset --hard <提交号>，注意先备份）
echo   2. 重跑本脚本（会再次重启 vl 生效）
echo   3. 仍不行：用 backup\ 里最近一次备份覆盖 local\ 对应目录后重启
echo ============================================
pause
exit /b 0

:rollbackhint
echo   1. 回滚代码：git log --oneline -5 找更新前提交号，git checkout <提交号> -- .
echo   2. 重跑本脚本重启生效
echo   3. 仍不行：用 backup\ 最近备份覆盖 local\ 对应目录
pause
exit /b 1
