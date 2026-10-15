@echo off
setlocal enabledelayedexpansion
cd /d "%~dp0"
title Virtual-lover 安装向导（v2）

echo ================================================================
echo        欢迎使用 Virtual-lover 安装向导（v2：1 进程 + 1 容器）
echo      她 = 宿主机 virtuallover 单进程（大脑+控制台）+ 协议端容器
echo ================================================================
echo.

rem ---- [1/8] 分流识别（PERF_SPEC §4-O12）：已有 vl.toml = 老用户，直接用；
rem      没有 = 全新安装，交互收集 API Key + 机器人 QQ 号（§7.1-7 ①不等控制台）----
set "FRESH=1"
if exist "local\config\vl.toml" set "FRESH=0"
if "%FRESH%"=="0" (
    echo [1/8] 检测到已有配置 local\config\vl.toml —— 老用户升级路径，跳过配置收集。
    echo       改 Key / 人格 / 语音都在控制台 http://127.0.0.1:8787 完成。
) else (
    echo [1/8] 全新安装：需要你提供以下信息（只写进 local\，绝不入 git）：
    echo        1^) 大模型 API Key（服务商下一步选择，自动写端点与模型预设）
    echo        2^) 机器人小号的 QQ 号（务必小号，勿用大号，有风控风险）
    echo        3^) 你常用的 QQ 号（可选：她只回白名单里的主人）
    echo.
    set "APIKEY="
    set /p APIKEY=  粘贴 API Key 后回车：
    set "QQNUM="
    set /p QQNUM=  输入机器人 QQ 号后回车：
    if not defined APIKEY (
        echo [X] API Key 不能为空（记忆与回复都靠它）。重跑本向导。
        pause
        exit /b 1
    )
    if not defined QQNUM (
        echo [X] QQ 号不能为空（她的会话/门禁/扫码都靠它）。重跑本向导。
        pause
        exit /b 1
    )
    echo !QQNUM!| findstr /r "^[0-9][0-9]*$" >nul 2>&1
    if errorlevel 1 (
        echo [X] QQ 号必须是纯数字（收到非法格式）。重跑本向导。
        pause
        exit /b 1
    )
    set "OWNERQQ="
    set /p OWNERQQ=  输入你常用 QQ 号后回车（可选，回车跳过=用小号兼当主人）：
    if defined OWNERQQ (
        echo !OWNERQQ!| findstr /r "^[0-9][0-9]*$" >nul 2>&1
        if errorlevel 1 (
            echo [X] 主人 QQ 号必须是纯数字（收到非法格式）。重跑本向导。
            pause
            exit /b 1
        )
    )
    echo      [OK] 已记录（Key 不回显；小号尾号 !QQNUM:~-4!）。
    if defined OWNERQQ echo      [OK] 主人 QQ 尾号 !OWNERQQ:~-4!（她只回白名单里的号）。
    echo.
    echo      选择 chat 服务商（自动写 api_base 与模型预设，之后可随时改 vl.toml）：
    echo        [1] 火山方舟豆包（Key 一般以 ark- 开头）
    echo        [2] DeepSeek（Key 一般以 sk- 开头）
    echo        [3] 其他 OpenAI 兼容（需手填 api_base 与模型名）
    set "VLP="
    set /p VLP=  输入 1/2/3 后回车：
    if "!VLP!"=="1" set "VL_SETUP_PROVIDER=ark"
    if "!VLP!"=="2" set "VL_SETUP_PROVIDER=deepseek"
    if not defined VL_SETUP_PROVIDER set "VL_SETUP_PROVIDER=custom"
    if "!VL_SETUP_PROVIDER!"=="custom" (
        set /p VL_SETUP_API_BASE=  粘贴 api_base 后回车：
        set /p VL_SETUP_MODEL=  输入模型名后回车：
    )
    echo.
    echo      记忆功能需要单独的 Embedding 模型（说过的话自动总结成终身记忆）。
    echo        没有它聊天正常，只是她记不住事；之后随时可在 vl.toml 补开。
    set "EMB="
    set /p EMB=  配置 Embedding？[1=火山方舟 2=其他OpenAI兼容 3=跳过，记忆暂闭]：
    if "!EMB!"=="1" (
        set "VL_SETUP_EMBEDDING=ark"
        set /p VL_SETUP_EMBED_MODEL=  输入 Embedding 模型名（如 ep-2024xxxxxxxx）后回车：
    )
    if "!EMB!"=="1" if not "!VL_SETUP_PROVIDER!"=="ark" set /p VL_SETUP_EMBED_KEY=  输入火山方舟 Key（Embedding 与 chat 不同账号时）后回车：
    if "!EMB!"=="2" (
        set "VL_SETUP_EMBEDDING=custom"
        set /p VL_SETUP_EMBED_BASE=  粘贴 Embedding 的 api_base 后回车：
        set /p VL_SETUP_EMBED_MODEL=  输入 Embedding 模型名后回车：
        set /p VL_SETUP_EMBED_DIM=  输入向量维度后回车（默认 2048）：
    )
    if "!EMB!"=="3" set "VL_SETUP_EMBEDDING=skip"
    if not defined VL_SETUP_EMBEDDING set "VL_SETUP_EMBEDDING=skip"
    if "!VL_SETUP_EMBEDDING!"=="ark" if not defined VL_SETUP_EMBED_MODEL echo       [!] Embedding 模型名没输入，本次跳过记忆配置（之后可在 vl.toml 补开）
    if "!VL_SETUP_EMBEDDING!"=="ark" if not defined VL_SETUP_EMBED_MODEL set "VL_SETUP_EMBEDDING=skip"
    if "!VL_SETUP_EMBEDDING!"=="custom" if not defined VL_SETUP_EMBED_BASE set "VL_SETUP_EMBEDDING=skip"
)
echo.

rem ---- [2/8] Docker 检查（协议端仍以容器运行）----
echo [2/8] 检查 Docker...
docker info >nul 2>&1
if not errorlevel 1 goto dockerok
if exist "C:\Program Files\Docker\Docker\Docker Desktop.exe" (
    echo       Docker 未运行，正在启动 Docker Desktop...
    start "" "C:\Program Files\Docker\Docker\Docker Desktop.exe"
) else (
    echo [X] 未安装 Docker Desktop！请先安装（Win10 64 位以上，装后重启电脑）：
    echo     https://www.docker.com/products/docker-desktop/
    pause
    exit /b 1
)
set /a tries=0
:waitdocker
docker info >nul 2>&1
if not errorlevel 1 goto dockerok
set /a tries+=1
if %tries% gtr 40 (
    echo [X] 等待 Docker 启动超时。请手动启动 Docker Desktop 后重跑本向导。
    pause
    exit /b 1
)
timeout /t 3 /nobreak >nul
goto waitdocker
:dockerok
echo       [OK] Docker 正常
echo.

rem ---- [3/8] 协议端容器 heart（默认组；legacy 回退组不触碰）----
echo [3/8] 启动协议端容器（QQ 协议端）...
docker compose up -d --remove-orphans
if errorlevel 1 (
    echo.
    echo [X] 容器启动失败。自查：端口占用 netstat -ano ^| findstr 61990；
    echo     或 docker compose ps 看状态；国内拉不动镜像在 .env 启用镜像源行。
    pause
    exit /b 1
)
echo       [OK] 协议端容器已启动
echo.

rem ---- [4/8] .venv 与依赖（运行依赖 + 测试依赖一并装，门禁/自检可用）----
echo [4/8] 检查 Python 虚拟环境（.venv，要求 Python 3.11+）...
if exist ".venv\Scripts\python.exe" goto venvok
where python >nul 2>&1
if errorlevel 1 (
    echo [X] 未找到 python。请安装 Python 3.11+ 后重跑本向导。
    pause
    exit /b 1
)
python -c "import sys; sys.exit(0 if sys.version_info >= (3, 11) else 1)" >nul 2>&1
if errorlevel 1 (
    echo [X] 当前 Python 低于 3.11：
    python --version
    pause
    exit /b 1
)
python -m venv .venv
if errorlevel 1 (
    echo [X] .venv 创建失败，请把上面的报错截图保存后寻求帮助。
    pause
    exit /b 1
)
:venvok
set "VLPY=.venv\Scripts\python.exe"
echo       [OK] .venv 就绪
"%VLPY%" -c "import websockets, numpy" >nul 2>&1
if errorlevel 1 (
    echo       安装依赖（websockets、numpy、pytest）...
    "%VLPY%" -m pip install -r requirements.txt -r requirements-dev.txt
    if errorlevel 1 (
        echo [X] 依赖安装失败（多为网络问题，可配 pip 镜像源后重跑本向导）。
        pause
        exit /b 1
    )
) else (
    echo       依赖已就绪
)
echo.

rem ---- [5/8] vl.toml：新装 --init-config 生成骨架，再写回收集值（§7.1-7 ①）----
echo [5/8] 生成 / 核对配置 local\config\vl.toml...
if "%FRESH%"=="1" (
    "%VLPY%" -m virtuallover --init-config
    if errorlevel 1 (
        echo [X] 配置初始化失败，见上方报错。
        pause
        exit /b 1
    )
    rem 收集值经环境变量写回（不走命令行参数：Key 含特殊字符免转义）
    set "VL_SETUP_API_KEY=!APIKEY!"
    set "VL_SETUP_QQ=!QQNUM!"
    if defined OWNERQQ set "VL_SETUP_OWNER_QQ=!OWNERQQ!"
    set "VL_SETUP_ROOT=%CD%"
    "%VLPY%" scripts\setup_writeback.py
    if errorlevel 1 (
        echo [X] 配置写回失败，见上方报错。
        pause
        exit /b 1
    )
    rem 反向 WS 已随写回代写/改指向：协议端只在启动时读 onebot11 配置，定点重启加载
    docker compose restart heart
    if errorlevel 1 echo [!] 协议端重启失败（不致命）：扫码完成后手动 docker compose restart heart
    set "VL_SETUP_API_KEY="
    set "VL_SETUP_QQ="
    set "VL_SETUP_OWNER_QQ="
    set "VL_SETUP_PROVIDER="
    set "VL_SETUP_API_BASE="
    set "VL_SETUP_MODEL="
    set "VL_SETUP_EMBEDDING="
    set "VL_SETUP_EMBED_BASE="
    set "VL_SETUP_EMBED_MODEL="
    set "VL_SETUP_EMBED_KEY="
    set "VL_SETUP_EMBED_DIM="
) else (
    echo       已有配置，直接使用（不覆盖；改 Key/人格进控制台）。
)
echo.

rem ---- [6/8] 起 vl 主进程（scripts/service.py start：协议端+大脑+vl 编排 +
rem      独立看护守护；不再跳板 一键启动.bat——其第 9 行无条件 pause 且无 guard
rem      分支，start /min call 会在 pause 上挂死，全新装机 vl 永不起）----
echo [6/8] 启动 virtuallover 主进程（编排+看护守护，约 10-30 秒）...
if not exist "scripts\service.py" (
    echo [X] 找不到 scripts\service.py（启动编排与看护在其中），请确认仓库完整。
    pause
    exit /b 1
)
"%VLPY%" scripts\service.py start
if errorlevel 1 (
    echo [X] 启动失败，见上方报错（常见为 6199/8787 端口被占用，
    echo     netstat -ano ^| findstr 6199 或 8787；或 vl.toml 校验失败）。
    pause
    exit /b 1
)
echo       停止请用 一键停止.bat。
echo.

rem ---- [7/8] /api/status 探测（200=就绪；401=服务在线未登录控制台，也算就绪）----
echo [7/8] 等待她上线（探测控制台 /api/status）...
set "VLCODE="
set /a tries=0
:waitapi
set "VLCODE="
for /f "delims=" %%c in ('curl -s -o nul -m 3 -w "%%{http_code}" http://127.0.0.1:8787/api/status 2^>nul') do set "VLCODE=%%c"
if "%VLCODE%"=="200" goto apiok
if "%VLCODE%"=="401" goto apiok
set /a tries+=1
if %tries% gtr 60 (
    echo [!] 3 分钟未就绪——排查：上方第 6 步启动输出（常见为 6199/8787 端口被占用，
    echo     netstat -ano ^| findstr 6199 或 8787）；或 local\config\vl.toml 校验失败。
    goto wrapup
)
timeout /t 3 /nobreak >nul
goto waitapi
:apiok
echo       [OK] 控制台在线（/api/status HTTP %VLCODE%）
echo.

rem ---- [8/8] 收尾只留三件事（§4-O12 八条任务书 + §7.1-7 ②③）----
:wrapup
echo.
echo [8/8] 收尾判定与三件事：
set "LINKED=0"
if "%VLCODE%"=="200" goto checklink
if not "%VLCODE%"=="401" goto linkhint
:checklink
rem ② 收尾判定 = 服务在线 且 日志见协议端反连（vl.log 是 UTF-8，用 python 判定
rem 而非 findstr——GBK 控制台的 findstr 对 UTF-8 中文必然失配）
"%VLPY%" -c "import sys,pathlib; root=pathlib.Path('local/logs'); sys.exit(0 if any('协议端已反连上线' in p.read_text(encoding='utf-8',errors='replace') for p in root.glob('vl*.log*')) else 1)" >nul 2>&1
if not errorlevel 1 set "LINKED=1"
:linkhint
if "%LINKED%"=="1" (
    echo   [OK] 判定通过：控制台在线 且 协议端已反连上线 —— 全链路就绪！
) else (
    echo   [..] 协议端反连尚未建立（QQ 还没扫码登录）——扫码后自动建立，属正常；
    echo        可稍后重跑本向导或 一键启动.bat 复核判定。
)
echo.
echo   剩下三件事：
echo   1. 控制台 http://127.0.0.1:8787 （密码见 local\console.json）——
if "%FRESH%"=="1" (
    echo      API Key 与 QQ 号已在第 5 步写入配置，进去核对一遍人格即可
) else (
    echo      在这里填 API Key、改人格、看状态
)
echo   2. QQ 扫码登录：控制台「QQ 登录」页（http://127.0.0.1:8787）用机器人小号扫码
echo      （反向 WS 已在第 5 步自动配好，无需碰协议端 WebUI；61990 仅作备查）
echo   3. 可选语音：下载 GSVI 语音包（docs\语音.md 第 0 节），控制台「语音」页开启
echo.
rem 协议端 WebUI token 备查链接（勿截图外传）
set TK=
if exist "local\napcat_config\webui.json" (
    for /f "tokens=2 delims=:," %%a in ('findstr /i "token" "local\napcat_config\webui.json"') do if not defined TK set TK=%%a
)
if defined TK set TK=%TK:"=%
if defined TK set TK=%TK: =%
if defined TK (
    echo   协议端 WebUI（已带 token，备查）: http://localhost:61990/webui?token=%TK%
) else (
    echo   协议端 WebUI: http://localhost:61990 （token 见 local\napcat_config\webui.json）
)
echo ============================================================
choice /c YN /n /m "现在打开控制台 QQ 登录页扫码？[Y=是 N=稍后]"
if errorlevel 2 goto done
start "" "http://127.0.0.1:8787"
:done
echo.
echo   完成后用常用 QQ 加机器人为好友，说一句话——她就活了。
echo   日常启停用 一键启动.bat / 一键停止.bat；备份 scripts\backup.bat。
pause
exit /b 0
