@echo off
REM Launcher do web shell do harness em modo PUBLICO (HARNESS_PUBLIC=1).
REM Usado pelo tailscale-serve.bat (HTTPS via Tailscale).
REM
REM Lê a senha DENTRO deste batch (config/secrets.env ou variável de ambiente
REM HARNESS_PASSWORD) — NUNCA na linha de comando do processo (a linha de
REM comando é visível no Gerenciador de Tarefas/WMI; a senha não pode
REM aparecer nela).
REM
REM Requisito de segurança (B4 endurecido): em modo público o harness RECUSA
REM subir com a senha de fábrica opencode/opencode — sem senha personalizada
REM este launcher falha com instrução (python -m harness.auth set).

setlocal EnableDelayedExpansion
cd /d "%~dp0"
set HARNESS_USERNAME=opencode
set HARNESS_PUBLIC=1

if exist "%~dp0config\secrets.env" (
    for /f "usebackq delims=" %%p in (`python -m harness.auth password`) do set "HARNESS_PASSWORD=%%p"
    if "!HARNESS_PASSWORD!"=="" (
        echo.
        echo [run-web-public] ERRO: falha ao obter a senha personalizada de config/secrets.env
        echo [run-web-public]   corrija o arquivo ou rode: python -m harness.auth set
        echo.
        exit /b 1
    )
    echo [run-web-public] usando HARNESS_PASSWORD personalizada de config/secrets.env
) else if "%HARNESS_PASSWORD%"=="" (
    echo.
    echo [run-web-public] ERRO: modo PUBLICO exige senha personalizada.
    echo [run-web-public]   credenciais padrao opencode/opencode NAO sobem em deploy publico.
    echo [run-web-public]   defina uma senha com: python -m harness.auth set
    echo.
    exit /b 1
) else (
    echo [run-web-public] usando HARNESS_PASSWORD da variavel de ambiente
)

python app.py --host 127.0.0.1 --port 8500
endlocal