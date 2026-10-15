@echo off
cd /d "%~dp0\.."
title Virtual-lover 数据迁移 旧结构到 local

echo [STEP0] 检查迁移条件...
if exist "local\astrbot_data" (
    echo [SKIP] local\astrbot_data 已存在，看起来已迁移过，为防覆盖直接退出。
    pause
    exit /b 0
)
if not exist "astrbot_data" (
    echo [SKIP] 未发现旧结构 astrbot_data，无需迁移。
    pause
    exit /b 0
)
if not exist "local" mkdir local
echo [OK] 条件满足

echo [STEP1] 抢救 NapCat 框架配置（匿名卷 - 容器删除即丢失）...
if not exist "local\napcat_config" (
    docker cp napcat:/app/napcat/config local/napcat_config
    if errorlevel 1 (
        echo [WARN] 抢救失败（容器可能已不存在），登录后需在 6099 手动重配反向 WS。
    ) else (
        echo [OK] 已保存到 local\napcat_config
    )
) else (
    echo [SKIP] local\napcat_config 已存在
)

echo [STEP2] 停止旧容器...
docker compose down --remove-orphans
if exist "milvus\docker-compose.yml" (
    pushd milvus
    docker compose down --remove-orphans
    popd
)
rem 兜底：容器若仍在则强制移除（数据都在宿主机目录，安全）
docker rm -f astrbot napcat milvus-etcd milvus-minio milvus-standalone >nul 2>&1
echo [OK] 旧容器已停止并移除

echo [STEP3] 迁移前全量备份（约 600MB，请稍候）...
for /f %%i in ('powershell -NoProfile -Command "Get-Date -Format yyyyMMdd_HHmmss"') do set TS=%%i
set BK=backup\pre-migration_%TS%
robocopy astrbot_data "%BK%\astrbot_data" /E /NFL /NDL /NJH /NJS /NP >nul
if errorlevel 8 ( echo [FAIL] 备份 astrbot_data 失败，已中止。 & pause & exit /b 1 )
robocopy napcat_data "%BK%\napcat_data" /E /NFL /NDL /NJH /NJS /NP >nul
if errorlevel 8 ( echo [FAIL] 备份 napcat_data 失败，已中止。 & pause & exit /b 1 )
robocopy milvus\volumes "%BK%\milvus_volumes" /E /NFL /NDL /NJH /NJS /NP >nul
if errorlevel 8 ( echo [FAIL] 备份 milvus 数据失败，已中止。 & pause & exit /b 1 )
echo [OK] 备份完成：%BK%

echo [STEP4] 移动数据目录到 local\ ...
move astrbot_data local\astrbot_data >nul || ( echo [FAIL] 移动 astrbot_data 失败 & pause & exit /b 1 )
move napcat_data local\napcat_data >nul || ( echo [FAIL] 移动 napcat_data 失败 & pause & exit /b 1 )
mkdir local\milvus 2>nul
move milvus\volumes local\milvus\volumes >nul || ( echo [FAIL] 移动 milvus\volumes 失败 & pause & exit /b 1 )
echo [OK] 移动完成

echo [STEP5] 清理旧编排文件（仅删除已确认空的旧结构残留）...
if exist "milvus\docker-compose.yml" del "milvus\docker-compose.yml"
if exist "shared_plugins" rmdir "shared_plugins" 2>nul
if exist "milvus" rmdir "milvus" 2>nul
echo [OK] 清理完成

echo [STEP6] 用新的一体化编排启动...
docker compose up -d
if errorlevel 1 (
    echo [FAIL] 新编排启动失败！数据已迁移到 local\ 但服务未起。
    echo        可手动重试 docker compose up -d；仍失败则用 一键状态.bat 排查。
    echo        出问题可从 %BK% 备份恢复。
    pause
    exit /b 1
)
echo.
echo ============================================================
echo   迁移完成！容器已按新结构启动。
echo   下一步：运行 一键状态.bat 确认全绿；
echo   QQ 若掉线，打开 http://localhost:61990 重新扫码。
echo   出问题可从 %BK% 恢复。
echo ============================================================
pause
