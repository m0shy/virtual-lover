@echo off
cd /d "%~dp0"
title Virtual-lover 状态检查

echo ============ 容器状态（默认组 = NapCat） ============
docker compose ps
echo.
echo     （legacy 回退栈容器查看：docker compose --profile legacy ps --all）

echo.
echo ============ 服务健康 ============
curl -s -o nul -m 3 -w "NapCat WebUI (61990) : HTTP %%{http_code}\n" http://localhost:61990
curl -s -o nul -m 3 -w "管理控制台  (8787) : HTTP %%{http_code}\n" http://127.0.0.1:8787/api/status
<nul set /p="新栈进程 (virtuallover) : "
set "VLNUM="
for /f %%n in ('powershell -NoProfile -Command "@(Get-CimInstance Win32_Process | Where-Object { $_.Name -like 'python*' -and $_.CommandLine -match 'virtuallover' }).Count" 2^>nul') do set "VLNUM=%%n"
if "%VLNUM%"=="" echo 无法检测（PowerShell 不可用？）
if "%VLNUM%"=="0" echo 未在运行（先运行 一键启动.bat）
if not "%VLNUM%"=="" if not "%VLNUM%"=="0" echo 运行中（%VLNUM% 个进程）
<nul set /p="新栈记忆库 (local\vl_data): "
if exist "local\vl_data\history.db" (
    echo 已就绪
) else (
    echo 未找到 history.db（新栈未初始化或尚未执行历史迁移）
)
curl -s -o nul -m 3 -w "AstrBot 面板 (6185, legacy 回退) : HTTP %%{http_code}\n" http://localhost:6185
curl -s -o nul -m 3 -w "语音后端     (5000) : HTTP %%{http_code}\n" http://127.0.0.1:5000/character_list
echo     （HTTP 000 = 服务未启动；200/3xx = 正常；语音后端未启动属可选）
echo     （管理控制台 401 = 已在运行、未登录，属正常；000 时先运行 一键启动.bat）
echo     （AstrBot 为 legacy 回退栈，平时未运行、000 属正常）

echo.
echo ============ QQ 登录状态 ============
docker logs napcat --tail 40 2>&1 | findstr /i "Login qrcode token 二维码 扫码"
echo.
echo NapCat WebUI 登录 token（从配置文件读取）：
if exist "local\napcat_config\webui.json" (
    findstr /i "token" "local\napcat_config\webui.json"
) else (
    echo   local\napcat_config\webui.json 不存在（尚未初始化，先启动一次）
)
echo.
echo 提示：出现「二维码」说明 QQ 需要重新扫码登录（打开 http://localhost:61990）
pause
