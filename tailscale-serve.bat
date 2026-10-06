@echo off
REM Configura, inicia e valida o acesso HTTPS via Tailscale Serve.
REM
REM Expe os dois servicos locais no tailnet com HTTPS real (Let's Encrypt):
REM   https://<maquina>.<tailnet>.ts.net/          -> web shell harness (8500)
REM   https://<maquina>.<tailnet>.ts.net:8443/     -> painel web do opencode (9090)
REM
REM Se os servicos locais nao estiverem rodando, ESTE script os inicia em
REM janelas separadas com o mesmo sistema de login dos scripts normais:
REM   - web shell : via run-web-public.bat (senha de config/secrets.env ou
REM                 HARNESS_PASSWORD do ambiente; sem senha personalizada o
REM                 modo publico RECUSA subir - B4 endurecido)
REM   - opencode  : via start-web.bat (OPENCODE_SERVER_PASSWORD de
REM                 config/secrets.env, ambiente ou padrao)
REM As senhas NAO sao impressas no console nem passadas na linha de comando
REM dos processos (visivel no Gerenciador de Tarefas/WMI). O TLS e terminado
REM pelo Tailscale (certificado Let's Encrypt).
REM
REM Uso:
REM   tailscale-serve.bat            -> inicia servicos se preciso, aplica serve, imprime URLs+credenciais
REM   tailscale-serve.bat status     -> mostra a config atual do serve
REM   tailscale-serve.bat off        -> remove toda a config do serve
REM
REM Requisitos:
REM   - Tailscale instalado, conectado e com HTTPS habilitado no tailnet.
REM   - Rodar a partir do diretorio do projeto (usa %~dp0).
REM
REM ATENCAO: este arquivo e ASCII puro (sem acentos) - cmd.exe le .bat como
REM ANSI/OEM e bytes UTF-8 corrompem o parsing (ex.: %1 vira vazio). Nao
REM adicione acentos neste arquivo.

setlocal EnableDelayedExpansion
cd /d "%~dp0"
goto :main

REM ---------------------------------------------------------------- helper
REM Verifica se uma porta local esta em LISTEN (retorna True/False em PORTUP)
:check_port
set "PORTUP=False"
for /f "tokens=*" %%p in ('powershell -NoProfile -Command "Test-NetConnection -ComputerName 127.0.0.1 -Port %1 -InformationLevel Quiet"') do set "PORTUP=%%p"
exit /b 0

:main
set "TS=%ProgramFiles%\Tailscale\tailscale.exe"
if not exist "%TS%" (
    set "TS=%LOCALAPPDATA%\Programs\Tailscale\tailscale.exe"
)
if not exist "%TS%" (
    echo [tailscale-serve] ERRO: tailscale.exe nao encontrado (procurei %%ProgramFiles%% e %%LOCALAPPDATA%%)
    exit /b 1
)

set "CMD=%~1"
if "%CMD%"=="" set CMD=apply

if "%CMD%"=="status" (
    "%TS%" serve status
    exit /b %ERRORLEVEL%
)

if "%CMD%"=="off" (
    "%TS%" serve --https=443 off
    "%TS%" serve --https=8443 off
    echo [tailscale-serve] serve config removida
    exit /b 0
)

if not "%CMD%"=="apply" (
    echo [tailscale-serve] uso: tailscale-serve.bat [apply^|status^|off]
    exit /b 1
)

echo [tailscale-serve] verificando conectividade do Tailscale...
"%TS%" status >nul 2>&1
if errorlevel 1 (
    echo [tailscale-serve] ERRO: Tailscale nao esta conectado.
    exit /b 1
)
echo [tailscale-serve] AVISO: o HTTPS e terminado pelo Tailscale (Let's Encrypt).
echo   Se o Tailscale nao estiver instalado/conectado, os URLs https://... nao funcionarao.
echo   Instale em https://tailscale.com e conecte-se antes.

REM ----------------------------------------------------- web shell (8500)
call :check_port 8500
if not "!PORTUP!"=="True" (
    if not exist "%~dp0config\secrets.env" (
        if "%HARNESS_PASSWORD%"=="" (
            echo [tailscale-serve] ERRO: modo PUBLICO exige senha personalizada.
            echo   credenciais padrao opencode/opencode NAO sobem em deploy publico.
            echo   defina uma senha com: python -m harness.auth set
            echo   (ou defina HARNESS_PASSWORD no ambiente).
            exit /b 1
        )
        echo [tailscale-serve] usando HARNESS_PASSWORD da variavel de ambiente
    ) else (
        echo [tailscale-serve] usando HARNESS_PASSWORD personalizada (config/secrets.env)
    )
    echo [tailscale-serve] web shell 8500 nao rodando - iniciando em nova janela (modo PUBLICO)...
    echo [tailscale-serve] HARNESS_PUBLIC=1: execucoes exigem sandbox Docker/Podman e HTTPS via Tailscale
    REM o launcher resolve a senha internamente - a senha NAO aparece na
    REM linha de comando do processo (run-web-public.bat).
    start "harness web shell (8500)" cmd /k "call run-web-public.bat"
) else (
    echo [tailscale-serve] web shell 8500 ja esta rodando.
)

REM --------------------------------------------------- painel opencode (9090)
call :check_port 9090
if not "!PORTUP!"=="True" (
    if not exist "%~dp0config\secrets.env" (
        if "%OPENCODE_SERVER_PASSWORD%"=="" (
            echo [tailscale-serve] ERRO: modo PUBLICO exige senha personalizada para o painel opencode.
            echo   defina uma senha com: python -m harness.auth set
            echo   (ou defina OPENCODE_SERVER_PASSWORD no ambiente).
            exit /b 1
        )
    )
    echo [tailscale-serve] opencode web 9090 nao rodando - iniciando em nova janela...
    REM o start-web.bat resolve a senha internamente (nao passa na linha de comando).
    start "opencode web (9090)" cmd /k "call start-web.bat"
) else (
    echo [tailscale-serve] opencode web 9090 ja esta rodando.
)

REM --------------------------------------------- aguarda servicos subirem
echo [tailscale-serve] aguardando servicos locais responderem...
set /a WAIT=0
:wait_loop
call :check_port 8500
set "H8500=!PORTUP!"
call :check_port 9090
set "H9090=!PORTUP!"
if not "!H8500!"=="True" if not "!H9090!"=="True" (
    set /a WAIT+=1
    if !WAIT! GEQ 15 (
        echo [tailscale-serve] ATENCAO: servicos locais demoraram a subir.
        echo   web shell: !H8500! ^| opencode web: !H9090!
    ) else (
        timeout /t 1 /nobreak >nul
        goto wait_loop
    )
)

REM ------------------------------------------------------ aplica serve config
echo [tailscale-serve] aplicando serve config...
"%TS%" serve --bg --https 443 http://127.0.0.1:8500
"%TS%" serve --bg --https 8443 http://127.0.0.1:9090

REM --------------------------------------------------------------- impressao
"%TS%" status --json > "%TEMP%\harness_ts_status.json" 2>nul
for /f "tokens=*" %%n in ('powershell -NoProfile -Command "$d=Get-Content '%TEMP%\harness_ts_status.json' -Raw|ConvertFrom-Json; Write-Host $d.Self.DNSName.TrimEnd('.')"') do set "DNS=%%n"
del "%TEMP%\harness_ts_status.json" 2>nul

echo.
echo [tailscale-serve] HTTPS pronto no tailnet:
echo   web shell harness : https://!DNS!
echo   painel opencode    : https://!DNS!:8443/
echo.
echo [tailscale-serve] credenciais de acesso:
echo   web shell : usuario=opencode, senha personalizada (config/secrets.env ou HARNESS_PASSWORD)
echo   painel    : usuario=opencode, senha personalizada (config/secrets.env ou OPENCODE_SERVER_PASSWORD)
echo   defina/troque com: python -m harness.auth set
echo   (as senhas NAO sao exibidas aqui nem na linha de comando dos processos)
echo.
echo [tailscale-serve] AVISO: o HTTPS e terminado pelo Tailscale (Let's Encrypt).
echo   Se o Tailscale nao estiver instalado/conectado, os URLs https://... nao funcionarao.
echo   Instale em https://tailscale.com e conecte-se antes.
echo.
echo [tailscale-serve] web shell em modo PUBLICO (HARNESS_PUBLIC=1):
echo   - exige HTTPS via Tailscale (este script) e SENHA PERSONALIZADA
echo     (credenciais padrao opencode/opencode NAO sobem - B4 endurecido);
echo   - execucoes no HOST sao bloqueadas: exigem sandbox Docker/Podman (B1).
echo.
echo [tailscale-serve] dica: se os servicos foram iniciados por este script,
echo   as janelas "harness web shell (8500)" e "opencode web (9090)" continuam
echo   abertas - feche-as para parar os servicos. As senhas seguem o fluxo do
echo   start-web.bat (config/secrets.env, variavel de ambiente ou padrao opencode).
echo.
"%TS%" serve status

endlocal
