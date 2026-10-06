@echo off
REM Inicia o opencode web a partir da raiz do projeto, com autenticacao.
REM   - Se config/secrets.env existe, usa a senha personalizada
REM     (obtida via: python -m harness.auth password);
REM   - Senao, se OPENCODE_SERVER_PASSWORD estiver definida no ambiente, usa ela;
REM   - Senao, usa a senha PADRAO opencode/opencode (credenciais de fabrica).
REM O username padrao e "opencode" (OPENCODE_SERVER_USERNAME para trocar).
REM
REM Uso:
REM   start-web.bat              -> http://127.0.0.1:9090
REM   start-web.bat 9091         -> http://127.0.0.1:9091
REM   start-web.bat 9091 0.0.0.0 -> expoe na rede/Tailscale
REM
REM O projeto raiz e o diretorio de onde o comando roda: rode ESTE script
REM SEMPRE a partir da raiz do projeto (diretorio deste script).

setlocal EnableDelayedExpansion
cd /d "%~dp0"
set PORT=9090
if not "%~1"=="" set PORT=%~1
set HOST=127.0.0.1
if not "%~2"=="" set HOST=%~2
set OPENCODE_SERVER_USERNAME=opencode

REM Senha: personalizada (config/secrets.env) > variavel de ambiente > padrao.
if exist "%~dp0config\secrets.env" (
    for /f "usebackq delims=" %%p in (`python -m harness.auth password`) do set "OPENCODE_SERVER_PASSWORD=%%p"
    if "!OPENCODE_SERVER_PASSWORD!"=="" (
        echo.
        echo [start-web] ERRO: falha ao obter senha personalizada de config/secrets.env
        echo [start-web]   corrija o arquivo ou rode: python -m harness.auth set
        echo.
        exit /b 1
    )
    echo.
    echo [start-web] usando senha personalizada (config/secrets.env)
    echo [start-web]   usuario: opencode
    echo.
) else if "%OPENCODE_SERVER_PASSWORD%"=="" (
    REM sem secrets.env e sem env: usa a PADRAO opencode/opencode (fabricas):
    set "OPENCODE_SERVER_PASSWORD=opencode"
    echo.
    echo [start-web] OPENCODE_SERVER_PASSWORD nao definida - usando credenciais PADRAO:
    echo [start-web]   usuario: opencode
    echo [start-web]   senha : !OPENCODE_SERVER_PASSWORD!
    echo [start-web] AVISO: credenciais padrao opencode/opencode ativas.
    echo [start-web]   defina uma senha com: python -m harness.auth set
    echo.
) else (
    echo.
    echo [start-web] usando OPENCODE_SERVER_PASSWORD da variavel de ambiente
    echo [start-web]   usuario: opencode
    echo.
)

echo [start-web] opencode web em http://%HOST%:%PORT%
echo [start-web] projeto raiz: %CD%
echo [start-web] pressione Ctrl+C para parar
echo.
opencode web --hostname %HOST% --port %PORT%
endlocal