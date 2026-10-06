"""Testes de `check_policy` / `_is_essential` (Finding 1 da re-revisão) e do
sandbox opcional do Executor (Item 4, Fase 1).

Parte 1 cobre o fechamento do Finding 1: `_is_essential` agora retorna True
apenas para comandos essenciais "PUROS" — prefixo essencial + argumentos
seguros, SEM separador de shell (`&`, `&&`, `;`, `|`, `||`), redirecionamento
(`<`, `>`) nem quebra de linha. Assim, cadeias como
`git status && del file.txt`, `python script.py; del file.txt` e
`dir && del file.txt` deixam de ficar LIVRES no `check_policy` (o `&&`/`;`
após o prefixo essencial encadearia um `del` que o `/api/exec` executaria de
fato).

Parte 2 cobre o sandbox do Executor (Item 4, Fase 1): camada de container
Docker/Podman OPCIONAL com fallback para o modo host atual. Os testes usam
backend MOCKADO (não dependem de Docker real):
  (a) EXEC_SANDBOX_ENABLED=False -> _sandbox_modo() == "host" e run() no host.
  (b) EXEC_SANDBOX_ENABLED=True mas backend indisponível -> cai para host,
      sandbox: False, comando roda no host.
  (c) backend "docker" mockado -> _run_sandbox monta o mount, roda o comando,
      captura output/exit_code, retorna sandbox: True.
  (d) check_policy bloqueia ANTES do container (destrutivo em qualquer modo).
  (e) _traduz_cwd converte caminho host -> caminho no mount.
  (f) falha do container -> fallback host (não quebra).
  (g) daemon inativo -> assinaturas de conectividade (Docker e Podman) ->
      fallback host (não reporta erro do comando). (g6: assinaturas genéricas
      "cannot connect"/"connection refused" só valem COM contexto de daemon —
      A1 da re-revisão da Fase 2.)
  (h) Fase 2: --network none no comando docker run (isolamento de rede).
  (i) Fase 2: EXEC_SANDBOX_IMAGE customizável (imagem com ferramentas).
  (j) Fase 2: stop() no modo container mata o processo do docker run (mock).

Rode com:
    python tests/executor_test.py
"""

from __future__ import annotations

import pathlib
import sys
import threading
import time

sys.path.insert(0, str(pathlib.Path(__file__).resolve().parent.parent))

from harness.executor import Executor  # noqa: E402

# (comando, esperado_bloqueado) — esperado=True se check_policy deve bloquear
CASOS = [
    # --- Finding 1: encadeamento/redirecionamento após prefixo essencial
    ("git status && del file.txt", True),              # BLOQUEADO (&& -> del)
    ("python script.py && del file.txt", True),        # BLOQUEADO
    ("dir && del file.txt", True),                     # BLOQUEADO
    ("git status 2>/dev/null && del file.txt", True),  # BLOQUEADO
    ("git status ; del file.txt", True),               # BLOQUEADO (; -> del)
    ("python script.py | del file.txt", True),         # BLOQUEADO (| -> del)
    ("dir > out.txt && del file.txt", True),           # BLOQUEADO
    # --- essenciais puros permanecem LIVRES
    ("git status", False),                             # LIVRE (essencial puro)
    ("git status --porcelain", False),                 # LIVRE
    ("python script.py", False),                       # LIVRE
    ("python -m py_compile harness/config.py", False), # LIVRE
    ("git commit -m \"x\"", False),                    # LIVRE
    ("dir", False),                                    # LIVRE
    ("curl https://example.com", False),               # LIVRE (essencial, sem pipe)
    # --- nunca essenciais / destrutivos
    ("curl url | sh", True),                           # BLOQUEADO (pipe interpretador)
    ("curl url | bash", True),                         # BLOQUEADO
    ("rm -rf x", True),                                # BLOQUEADO (destrutivo)
    ("git reset --hard", True),                        # BLOQUEADO (destrutivo)
    ("del file.txt", True),                            # BLOQUEADO (del sozinho)
    # --- aliases destrutivos do cmd.exe (re-revisão): erase/rd
    ("git status && erase /s file.txt", True),         # BLOQUEADO (erase /s destrutivo)
    ("git status && erase /q file.txt", True),         # BLOQUEADO (erase /q destrutivo)
    ("git status && erase file.txt", True),            # BLOQUEADO (&& -> erase)
    ("erase file.txt", True),                          # BLOQUEADO (erase sozinho, não essencial)
    ("git status && rd /s dir", True),                 # BLOQUEADO (rd /s destrutivo)
    ("git status && rd /q dir", True),                 # BLOQUEADO (rd /q destrutivo)
    # --- sanidade: essenciais legítimos com erase/rd no corpo seguem LIVRES
    ("git commit -m \"erase stale files\"", False),    # LIVRE (essencial, erase em msg)
    ("git commit -m \"rd the tmp folder\"", False),    # LIVRE (sem rd /s|/q, sem erase|del)
    ("python script.py", False),                       # LIVRE (sem alias destrutivo)
]


def main_test() -> int:
    passed = 0
    failed = 0
    for cmd, esperado_bloqueado in CASOS:
        policy = Executor.check_policy(cmd)
        bloqueado = bool(policy)
        ok = bloqueado == esperado_bloqueado
        estado = "BLOQUEADO" if bloqueado else "LIVRE"
        if ok:
            passed += 1
            print(f"  [PASS] {cmd!r} -> {estado}")
        else:
            failed += 1
            print(f"  [FAIL] {cmd!r} -> {estado} (esperado "
                  f"{'BLOQUEADO' if esperado_bloqueado else 'LIVRE'}): {policy}")
    print(f"\nRESULTADO (policy): {passed} passaram, {failed} falharam")
    return 1 if failed else 0


# ------------------------------------------------------------- sandbox (Item 4)
class _FakeProc:
    """Subprocess fake para mockar o `docker run` do `_run_sandbox` (usa
    `communicate`) e a execução no HOST do `_worker` (usa `stdout` + `wait`)."""

    def __init__(self, out="hi\n", rc=0):
        self.returncode = rc
        self._out = out
        self._called = False
        # linha por linha para a iteração `for line in proc.stdout` do host
        self.stdout = [line if line.endswith("\n") else line + "\n"
                       for line in out.splitlines(keepends=False)]

    def communicate(self):
        self._called = True
        return self._out, ""

    def wait(self, timeout=None):
        return self.returncode

    def poll(self):
        return self.returncode


def _wait(job, tries=60):
    for _ in range(tries):
        snap = job.snapshot()
        if snap["status"] not in ("running",):
            return snap
        time.sleep(0.1)
    return job.snapshot()


def main_sandbox_test() -> int:
    import tempfile
    import unittest.mock as mock

    from harness import config
    from harness import executor as executor_mod

    # Isola o histórico de comandos num arquivo temporário: as execuções REAIS
    # no host (python --version, rm -rf -> blocked) chamam `_record` e NÃO podem
    # poluir o logs/harness_history.json real (o agents_test já isolava; aqui
    # faltava — inconsistência de disciplina entre suítes).
    with tempfile.TemporaryDirectory() as tmp:
        hist = pathlib.Path(tmp) / "history.json"
        original = config.HISTORY_FILE
        config.HISTORY_FILE = hist
        try:
            return _sandbox_inner()
        finally:
            config.HISTORY_FILE = original


def _sandbox_inner() -> int:
    import unittest.mock as mock

    from harness import config
    from harness import executor as executor_mod

    passed = 0
    failed = 0

    def check(name, cond):
        nonlocal passed, failed
        if cond:
            passed += 1
            print(f"  [PASS] {name}")
        else:
            failed += 1
            print(f"  [FAIL] {name}")

    ex = Executor()
    root = str(config.EXEC_SANDBOX_MOUNT_ROOT)

    # ============================================================ Pendência B1
    # EXEC_SANDBOX_ENABLED configurável por env: a função `_env_bool` interpreta
    # os valores de os.environ como booleano. Testamos o override direto.
    def env_bool(name, default=False):
        return config._env_bool(name, default)

    with mock.patch.dict(config.os.environ, {"EXEC_SANDBOX_ENABLED": "1"}, clear=False):
        check("(B1) '1' -> True", env_bool("EXEC_SANDBOX_ENABLED") is True)
    with mock.patch.dict(config.os.environ, {"EXEC_SANDBOX_ENABLED": "true"}, clear=False):
        check("(B1) 'true' -> True", env_bool("EXEC_SANDBOX_ENABLED") is True)
    with mock.patch.dict(config.os.environ, {"EXEC_SANDBOX_ENABLED": "YES"}, clear=False):
        check("(B1) 'YES' (upper) -> True", env_bool("EXEC_SANDBOX_ENABLED") is True)
    with mock.patch.dict(config.os.environ, {"EXEC_SANDBOX_ENABLED": "0"}, clear=False):
        check("(B1) '0' -> False", env_bool("EXEC_SANDBOX_ENABLED") is False)
    with mock.patch.dict(config.os.environ, {"EXEC_SANDBOX_ENABLED": "false"}, clear=False):
        check("(B1) 'false' -> False", env_bool("EXEC_SANDBOX_ENABLED") is False)
    with mock.patch.dict(config.os.environ, {"EXEC_SANDBOX_ENABLED": "no"}, clear=False):
        check("(B1) 'no' -> False", env_bool("EXEC_SANDBOX_ENABLED") is False)
    # ausente -> default False (comportamento seguro atual)
    with mock.patch.dict(config.os.environ, {}, clear=True):
        check("(B1) ausente -> default False", env_bool("EXEC_SANDBOX_ENABLED") is False)
    # inválido -> default False (não liga sandbox com valor desconhecido)
    with mock.patch.dict(config.os.environ, {"EXEC_SANDBOX_ENABLED": "talvez"}, clear=False):
        check("(B1) inválido -> default False", env_bool("EXEC_SANDBOX_ENABLED") is False)
    # default explícito respeitado quando ausente
    with mock.patch.dict(config.os.environ, {}, clear=True):
        check("(B1) ausente com default=True -> True",
              env_bool("EXEC_SANDBOX_ENABLED", True) is True)

    # (a) EXEC_SANDBOX_ENABLED=False -> _sandbox_modo() == "host"
    with mock.patch.object(config, "EXEC_SANDBOX_ENABLED", False):
        check("(a) disabled -> _sandbox_modo host", ex._sandbox_modo() == "host")

    # (a2) run() com default (disabled, host) -> sandbox False no snapshot
    job = ex.run("python --version")
    snap = _wait(job)
    check("(a) run default host: sandbox False + exit 0",
          snap and snap["sandbox"] is False and snap["exit_code"] == 0)

    # (b) EXEC_SANDBOX_ENABLED=True mas backend indisponível -> cai para host
    with mock.patch.object(config, "EXEC_SANDBOX_ENABLED", True), \
            mock.patch.object(Executor, "_docker_disponivel", return_value=False), \
            mock.patch("harness.executor.shutil.which", return_value=None):
        check("(b) indisponível -> _sandbox_modo host",
              ex._sandbox_modo() == "host")
        job = ex.run("python --version")
        snap = _wait(job)
        check("(b) run indisponível: sandbox False + exit 0",
              snap and snap["sandbox"] is False and snap["exit_code"] == 0)

    # (c) backend "docker" mockado -> _run_sandbox monta, roda e captura
    capturado = {}

    def fake_popen(cmd, **kwargs):
        capturado["cmd"] = list(cmd)
        capturado["kwargs"] = kwargs
        return _FakeProc(out="oi\n", rc=0)

    with mock.patch.object(config, "EXEC_SANDBOX_ENABLED", True), \
            mock.patch.object(config, "EXEC_SANDBOX_BACKEND", "docker"), \
            mock.patch.object(Executor, "_docker_disponivel", return_value=True), \
            mock.patch.object(Executor, "_docker_cli_path", return_value=None), \
            mock.patch.object(executor_mod.subprocess, "Popen",
                              side_effect=fake_popen):
        res = ex._run_sandbox("echo oi", root, backend="docker")
        check("(c) _run_sandbox retorna dict com output/exit_code",
              isinstance(res, dict)
              and res["exit_code"] == 0
              and res["output"] == "oi\n"
              and res["status"] == "finished")
        cmd = capturado.get("cmd", [])
        check("(c) sem CLI path, usa 'docker run'",
              cmd and cmd[0] == "docker" and cmd[1] == "run" and "--rm" in cmd)
        check("(c) -v monta o mount raiz em /workspace",
              any(a == "-v" and b == f"{root}:/workspace"
                  for a, b in zip(cmd, cmd[1:])))
        check("(c) -w aponta para /workspace (cwd raiz)",
              "-w" in cmd and cmd[cmd.index("-w") + 1] == "/workspace")
        check("(c) roda sh -c '<command>'",
              "sh" in cmd and "-c" in cmd and cmd[cmd.index("-c") + 1] == "echo oi")

    # (c3) com CLI path encontrado (Docker Desktop fora do PATH), _run_sandbox
    #      usa o caminho absoluto do docker.exe em vez de só "docker".
    capturado.clear()
    with mock.patch.object(config, "EXEC_SANDBOX_ENABLED", True), \
            mock.patch.object(config, "EXEC_SANDBOX_BACKEND", "docker"), \
            mock.patch.object(Executor, "_docker_disponivel", return_value=True), \
            mock.patch.object(Executor, "_docker_cli_path",
                              return_value=r"C:\docker\resources\bin\docker.exe"), \
            mock.patch.object(executor_mod.subprocess, "Popen",
                              side_effect=fake_popen):
        res = ex._run_sandbox("echo oi", root, backend="docker")
        cmd = capturado.get("cmd", [])
        check("(c3) CLI path absoluto usado no comando",
              res is not None
              and cmd and cmd[0] == r"C:\docker\resources\bin\docker.exe"
              and cmd[1] == "run")

    # (c2) _sandbox_modo com backend "docker" disponível -> "docker"
    with mock.patch.object(config, "EXEC_SANDBOX_ENABLED", True), \
            mock.patch.object(config, "EXEC_SANDBOX_BACKEND", "docker"), \
            mock.patch.object(Executor, "_docker_disponivel", return_value=True):
        check("(c2) docker disponível -> _sandbox_modo docker",
              ex._sandbox_modo() == "docker")

    # (c4) _docker_cli_path detecta via candidato comum do Docker Desktop,
    #      mesmo com shutil.which retornando None (docker fora do PATH).
    #      Pendência B2: com CLI presente, _docker_disponivel roda `docker
    #      info` para confirmar o daemon — aqui mockamos o subprocess.run para
    #      responder exit 0 (daemon OK) -> disponivel True.
    class _DaemonOkProc:
        returncode = 0

    def _subprocess_run_info_ok(cmd, **kwargs):
        return _DaemonOkProc()

    with mock.patch.object(config, "EXEC_SANDBOX_DOCKER_CLI", ""), \
            mock.patch.object(config, "EXEC_SANDBOX_DOCKER_CLI_CANDIDATES",
                              (r"C:\docker\resources\bin\docker.exe",
                               r"C:\outro\docker.exe")), \
            mock.patch("harness.executor.shutil.which", return_value=None), \
            mock.patch.object(pathlib.Path, "is_file",
                              side_effect=lambda self=None: True), \
            mock.patch.object(executor_mod.subprocess, "run",
                              side_effect=_subprocess_run_info_ok):
        cli = Executor._docker_cli_path()
        check("(c4) CLI detectado por candidato (fora do PATH)",
              cli == r"C:\docker\resources\bin\docker.exe")
        check("(c4) _docker_disponivel True com CLI por candidato + daemon OK",
              Executor._docker_disponivel() is True)

    # (c5) _docker_cli_path respeita o caminho EXPLÍCITO (EXEC_SANDBOX_DOCKER_CLI),
    #      mesmo sem PATH nem candidatos.
    with mock.patch.object(config, "EXEC_SANDBOX_DOCKER_CLI",
                          r"C:\explicito\bin\docker.exe"), \
            mock.patch.object(config, "EXEC_SANDBOX_DOCKER_CLI_CANDIDATES", ()), \
            mock.patch("harness.executor.shutil.which", return_value=None), \
            mock.patch.object(pathlib.Path, "is_file",
                              side_effect=lambda self=None: True):
        cli = Executor._docker_cli_path()
        check("(c5) CLI explicito respeitado",
              cli == r"C:\explicito\bin\docker.exe")

    # (c6) nenhum CLI nem SDK -> _docker_disponivel False. O `import docker`
    #      interno de `_docker_disponivel` (fallback para o SDK opcional,
    #      executor.py) precisa ser MOCKADO via sys.modules["docker"]=None
    #      (import -> ImportError): se o pip `docker` estiver instalado no
    #      ambiente, sem o mock o SDK seria importado com sucesso e
    #      _docker_disponivel retornaria True (quebrando o teste). O mock
    #      torna o teste INDEPENDENTE do ambiente (docker instalado ou não).
    with mock.patch.object(config, "EXEC_SANDBOX_DOCKER_CLI", ""), \
            mock.patch.object(config, "EXEC_SANDBOX_DOCKER_CLI_CANDIDATES", ()), \
            mock.patch("harness.executor.shutil.which", return_value=None), \
            mock.patch.object(pathlib.Path, "is_file", return_value=False), \
            mock.patch.dict(sys.modules, {"docker": None}):
        check("(c6) sem CLI nem SDK -> indisponivel",
              Executor._docker_disponivel() is False)

    # ============================================================ Pendência B2
    # (B2-1) CLI presente + daemon OK (docker info exit 0) -> disponivel True.
    #        `_docker_disponivel` agora verifica a ATIVIDADE do daemon, não só
    #        a presença do CLI (endurecimento do falso positivo).
    with mock.patch.object(config, "EXEC_SANDBOX_DOCKER_CLI", ""), \
            mock.patch.object(config, "EXEC_SANDBOX_DOCKER_CLI_CANDIDATES",
                              (r"C:\docker\resources\bin\docker.exe",)), \
            mock.patch("harness.executor.shutil.which", return_value=None), \
            mock.patch.object(pathlib.Path, "is_file",
                              side_effect=lambda self=None: True), \
            mock.patch.object(executor_mod.subprocess, "run",
                              side_effect=_subprocess_run_info_ok):
        check("(B2-1) CLI presente + daemon OK -> disponivel True",
              Executor._docker_disponivel() is True)

    # (B2-2) CLI presente + daemon DOWN (docker info retorna exit != 0) ->
    #        disponivel False (antes era falso positivo True). Verifica também
    #        que o comando enviado foi `docker info`.
    info_cmds = []

    def _subprocess_run_info_down(cmd, **kwargs):
        info_cmds.append(list(cmd))
        return _FakeProc(out="Cannot connect to the Docker daemon. "
                              "Is the docker daemon running?\n",
                         rc=125)

    with mock.patch.object(config, "EXEC_SANDBOX_DOCKER_CLI", ""), \
            mock.patch.object(config, "EXEC_SANDBOX_DOCKER_CLI_CANDIDATES",
                              (r"C:\docker\resources\bin\docker.exe",)), \
            mock.patch("harness.executor.shutil.which", return_value=None), \
            mock.patch.object(pathlib.Path, "is_file",
                              side_effect=lambda self=None: True), \
            mock.patch.object(executor_mod.subprocess, "run",
                              side_effect=_subprocess_run_info_down):
        check("(B2-2) CLI presente + daemon down -> disponivel False",
              Executor._docker_disponivel() is False)
        check("(B2-2) comando de verificação é 'docker info'",
              len(info_cmds) == 1
              and info_cmds[0][-1] == "info"
              and info_cmds[0][0].lower().endswith("docker.exe"))

    # (B2-3) CLI presente + timeout do docker info -> disponivel False (seguro:
    #        daemon lento/caído não trava e o sandbox cai para host).
    def _subprocess_run_info_timeout(cmd, **kwargs):
        raise executor_mod.subprocess.TimeoutExpired(cmd=cmd, timeout=3)

    with mock.patch.object(config, "EXEC_SANDBOX_DOCKER_CLI", ""), \
            mock.patch.object(config, "EXEC_SANDBOX_DOCKER_CLI_CANDIDATES",
                              (r"C:\docker\resources\bin\docker.exe",)), \
            mock.patch("harness.executor.shutil.which", return_value=None), \
            mock.patch.object(pathlib.Path, "is_file",
                              side_effect=lambda self=None: True), \
            mock.patch.object(executor_mod.subprocess, "run",
                              side_effect=_subprocess_run_info_timeout):
        check("(B2-3) CLI presente + timeout docker info -> disponivel False",
              Executor._docker_disponivel() is False)

    # (B2-4) _sandbox_modo com EXEC_SANDBOX_ENABLED=False retorna "host" ANTES
    #        de consultar o daemon (o sandbox nunca ativa com env desligado).
    with mock.patch.object(config, "EXEC_SANDBOX_ENABLED", False), \
            mock.patch.object(Executor, "_docker_disponivel",
                              side_effect=AssertionError("não deveria chamar")):
        check("(B2-4) disabled -> host sem consultar o daemon",
              ex._sandbox_modo() == "host")

    # (d) check_policy bloqueia ANTES do container (destrutivo em qualquer modo)
    check("(d) check_policy bloqueia rm -rf", bool(Executor.check_policy("rm -rf x")))
    job = ex.run("rm -rf alguma coisa")
    check("(d) run destrutivo -> blocked + sandbox False",
          job.status == "blocked" and job.snapshot()["sandbox"] is False)

    # (e) _traduz_cwd converte host -> mount
    check("(e) raiz -> /workspace", ex._traduz_cwd(root) == "/workspace")
    sub = str(pathlib.Path(root) / "harness")
    check("(e) subdir -> /workspace/harness", ex._traduz_cwd(sub) == "/workspace/harness")
    fora = str(pathlib.Path("C:/fora").resolve() if hasattr(pathlib, "WindowsPath")
               else "/fora")
    check("(e) fora do mount -> fallback /workspace",
          ex._traduz_cwd(fora) == "/workspace")

    # (f) falha do container -> _run_sandbox retorna None (fallback host)
    def raising_popen(cmd, **kwargs):
        raise OSError("docker indisponível")

    with mock.patch.object(config, "EXEC_SANDBOX_ENABLED", True), \
            mock.patch.object(config, "EXEC_SANDBOX_BACKEND", "docker"), \
            mock.patch.object(Executor, "_docker_disponivel", return_value=True), \
            mock.patch.object(executor_mod.subprocess, "Popen",
                              side_effect=raising_popen):
        check("(f) falha do container -> _run_sandbox None",
              ex._run_sandbox("echo oi", root, backend="docker") is None)

    # (f2) _sandbox_modo nunca crasha (exceção na detecção -> host)
    with mock.patch.object(config, "EXEC_SANDBOX_ENABLED", True), \
            mock.patch.object(config, "EXEC_SANDBOX_BACKEND", "auto"), \
            mock.patch.object(Executor, "_docker_disponivel",
                              side_effect=RuntimeError("boom")):
        check("(f2) detecção com exceção -> host", ex._sandbox_modo() == "host")

    # (g) Finding A/B (re-revisão Item 4): DAEMON PRESENTE MAS INATIVO.
    #      _docker_disponivel -> True (CLI presente, só valida presença) e
    #      `docker run` retorna exit_code != 0 com mensagem de conectividade do
    #      daemon. NÃO é erro do comando: é falha de INFRAESTRUTURA -> o job DEVE
    #      cair para HOST (roda no host, sandbox: False, exit_code real do host),
    #      não terminar em erro.
    daemon_out = ("Cannot connect to the Docker daemon at unix:///var/run/docker.sock. "
                  "Is the docker daemon running?\n")
    capturado.clear()
    sandbox_calls = []

    def daemon_down_popen(cmd, **kwargs):
        # `docker run` (shell=False) -> daemon inativo (rc=125)
        if kwargs.get("shell") is False:
            sandbox_calls.append(list(cmd))
            return _FakeProc(out=daemon_out, rc=125)
        # execução no HOST (shell=True) -> roda de verdade (ex.: python --version)
        return _FakeProc(out="", rc=0)

    with mock.patch.object(config, "EXEC_SANDBOX_ENABLED", True), \
            mock.patch.object(config, "EXEC_SANDBOX_BACKEND", "docker"), \
            mock.patch.object(Executor, "_docker_disponivel", return_value=True), \
            mock.patch.object(Executor, "_docker_cli_path", return_value=None), \
            mock.patch.object(executor_mod.subprocess, "Popen",
                              side_effect=daemon_down_popen):
        # (g1) _run_sandbox sozinho: com falha de infra do daemon -> None (host)
        res = ex._run_sandbox("echo oi", root, backend="docker")
        check("(g1) _run_sandbox daemon inativo -> None (fallback host)",
              res is None)
        # (g2) helper detecta as assinaturas de conectividade do daemon
        check("(g2) _eh_falha_infra_daemon True p/ daemon down",
              Executor._eh_falha_infra_daemon(daemon_out) is True)
        # (g3) JOB INTEIRO: daemon inativo -> cai para host, sem erro
        sandbox_calls.clear()  # (g1) já consumiu 1 chamada; conta só a do job
        job = ex.run("python --version")
        snap = _wait(job)
        check("(g3) daemon inativo -> job roda no host (sandbox False, exit 0)",
              snap is not None
              and snap["sandbox"] is False
              and snap["exit_code"] == 0
              and snap["status"] == "finished")
        check("(g3) docker run foi tentado 1x (falha de infra -> 1 fallback)",
              len(sandbox_calls) == 1)

    # (g4) helper NÃO confunde erro de comando legítimo do container com falha
    #      de infra (isolamento preservado: NÃO deve cair para host).
    check("(g4) erro de comando (false) não é falha de infra",
          Executor._eh_falha_infra_daemon("sh: exit 1\ncommand not found\n") is False)
    check("(g4) exit 500 de um comando (sem HTTP) não é falha de infra",
          Executor._eh_falha_infra_daemon("exit 500\n") is False)
    check("(g4) saída vazia não é falha de infra",
          Executor._eh_falha_infra_daemon("") is False)

    # (g5) PENDÊNCIA 4: assinaturas de CONECTIVIDADE DO PODMAN (backend
    #      podman pode emitir mensagens diferentes das do Docker).
    check("(g5) podman.sock -> falha de infra True",
          Executor._eh_falha_infra_daemon(
              "Error: cannot connect to the podman.sock: dial unix "
              "/run/podman/podman.sock: connect: no such file or directory\n"
          ) is True)
    check("(g5) cannot connect (podman) -> falha de infra True",
          Executor._eh_falha_infra_daemon(
              "Error: cannot connect to the podman socket\n"
          ) is True)
    check("(g5) connection refused no socket do daemon -> falha de infra True",
          Executor._eh_falha_infra_daemon(
              "Error: connection refused: dial unix /run/podman/podman.sock: "
              "connect: connection refused\n"
          ) is True)
    check("(g5) error during connect (podman) -> falha de infra True",
          Executor._eh_falha_infra_daemon(
              "Error during connect: Get \"http://d\": dial tcp: connection refused\n"
          ) is True)
    # (g5) assinaturas do DOCKER continuam detectadas
    check("(g5) docker daemon down continua detectado",
          Executor._eh_falha_infra_daemon(daemon_out) is True)

    # (g6) ACHADO A1 (re-revisão Fase 2): as assinaturas genéricas "cannot
    #      connect" e "connection refused" SÓ valem com CONTEXTO de daemon.
    #      Sem contexto, são saída LEGÍTIMA de um comando que RODOU no
    #      container (ex.: curl numa porta fechada) — classificá-las como falha
    #      de infra faria o comando ser RE-EXECUTADO no HOST (quebra pontual do
    #      isolamento de rede + execução dupla).
    check("(g6) curl connection refused (legítimo) não é falha de infra",
          Executor._eh_falha_infra_daemon(
              "curl: (7) Failed to connect to localhost port 8080: "
              "Connection refused\n"
          ) is False)
    check("(g6) cannot connect a IP/porta (legítimo) não é falha de infra",
          Executor._eh_falha_infra_daemon(
              "cannot connect to 127.0.0.1:3306\n"
          ) is False)
    check("(g6) Connection refused isolado, sem contexto daemon -> False",
          Executor._eh_falha_infra_daemon("Connection refused\n") is False)
    check("(g6) Error: connection refused: dial tcp (sem contexto) -> False",
          Executor._eh_falha_infra_daemon(
              "Error: connection refused: dial tcp 127.0.0.1:8080\n"
          ) is False)
    check("(g6) cannot connect com contexto podman.sock -> True (infra Podman)",
          Executor._eh_falha_infra_daemon(
              "Error: cannot connect to ... podman.sock\n"
          ) is True)
    check("(g6) cannot connect to the docker daemon -> True (infra Docker)",
          Executor._eh_falha_infra_daemon(
              "cannot connect to the docker daemon\n"
          ) is True)
    check("(g6) error during connect + is the docker daemon running -> True",
          Executor._eh_falha_infra_daemon(
              "error during connect: Post \"http://docker.sock/_ping\": "
              "dial unix /var/run/docker.sock: connect: connection refused. "
              "Is the docker daemon running?\n"
          ) is True)
    check("(g6) dockerDesktopLinuxEngine/_ping -> True",
          Executor._eh_falha_infra_daemon(
              "dockerDesktopLinuxEngine/_ping: connection refused\n"
          ) is True)

    # =========================================================== Fase 2 (Item 4)
    # (h) Fase 2: isolamento de rede por padrão — `--network none` no docker run
    capturado.clear()
    with mock.patch.object(config, "EXEC_SANDBOX_ENABLED", True), \
            mock.patch.object(config, "EXEC_SANDBOX_BACKEND", "docker"), \
            mock.patch.object(Executor, "_docker_disponivel", return_value=True), \
            mock.patch.object(Executor, "_docker_cli_path", return_value=None), \
            mock.patch.object(executor_mod.subprocess, "Popen",
                              side_effect=fake_popen):
        res = ex._run_sandbox("echo oi", root, backend="docker")
        cmd = capturado.get("cmd", [])
        check("(h) --network none presente no comando docker run",
              res is not None and "--network" in cmd
              and cmd[cmd.index("--network") + 1] == "none")
        check("(h) imagem default python:3-slim preservada",
              res is not None and "python:3-slim" in cmd)

    # (i) Fase 2: imagem customizável via EXEC_SANDBOX_IMAGE (imagem com
    #     ferramentas: git/compiladores embutidos — mecanismo recomendado).
    capturado.clear()
    with mock.patch.object(config, "EXEC_SANDBOX_ENABLED", True), \
            mock.patch.object(config, "EXEC_SANDBOX_BACKEND", "docker"), \
            mock.patch.object(config, "EXEC_SANDBOX_IMAGE", "custom-tools:latest"), \
            mock.patch.object(Executor, "_docker_disponivel", return_value=True), \
            mock.patch.object(Executor, "_docker_cli_path", return_value=None), \
            mock.patch.object(executor_mod.subprocess, "Popen",
                              side_effect=fake_popen):
        res = ex._run_sandbox("echo oi", root, backend="docker")
        cmd = capturado.get("cmd", [])
        check("(i) EXEC_SANDBOX_IMAGE customizada usada no run",
              res is not None and "custom-tools:latest" in cmd
              and cmd[cmd.index("sh") - 1] == "custom-tools:latest")

    # (j) Fase 2: stop() no modo container mata o processo do docker run
    #     (job._proc registrado pelo _run_sandbox; taskkill/terminate análogo
    #     ao host). Backend mockado — sem Docker real.
    class _FakeRunningProc:
        """Processo fake 'rodando' (poll() -> None) para o teste do stop() no
        modo container; communicate() BLOQUEIA até o terminate() ser chamado
        (simula um container de longa duração) — evita corrida com o worker."""

        def __init__(self):
            self.returncode = None
            self.terminated = False
            self.pid = 424242
            self._event = threading.Event()

        def poll(self):
            return self.returncode

        def terminate(self):
            self.terminated = True
            self.returncode = 1  # CLI morto -> communicate() desbloqueia
            self._event.set()

        def communicate(self):
            # bloqueia até o stop() chamar terminate() (container "rodando")
            self._event.wait(timeout=10)
            return "killed by stop\n", ""

    proc_holder = {}

    def fake_popen_slow(cmd, **kwargs):
        p = _FakeRunningProc()
        proc_holder["proc"] = p
        return p

    with mock.patch.object(config, "EXEC_SANDBOX_ENABLED", True), \
            mock.patch.object(config, "EXEC_SANDBOX_BACKEND", "docker"), \
            mock.patch.object(Executor, "_docker_disponivel", return_value=True), \
            mock.patch.object(Executor, "_docker_cli_path", return_value=None), \
            mock.patch.object(executor_mod.subprocess, "Popen",
                              side_effect=fake_popen_slow), \
            mock.patch.object(executor_mod.subprocess, "run",
                              return_value=None):  # taskkill mockado
        job = ex.run("python --version")  # inicia em sandbox (docker mockado)
        # espera o worker registrar o proc do container no job (_run_sandbox)
        deadline = time.monotonic() + 5
        while time.monotonic() < deadline and job._proc is None:
            time.sleep(0.01)
        proc = proc_holder.get("proc")
        if proc is None or job._proc is None:
            check("(j) container rodando com proc registrado no job", False)
        else:
            check("(j) container rodando com proc registrado no job",
                  job._proc is proc)
            stopped = job.stop()
            snap = _wait(job)
            check("(j) stop() True e job encerra como stopped",
                  stopped is True and snap["status"] == "stopped")
            check("(j) processo do container foi morto (terminate/taskkill)",
                  proc.terminated)

    print(f"\nRESULTADO (sandbox): {passed} passaram, {failed} falharam")
    return 1 if failed else 0


# --------------------------------------------------------- sandbox condicional
# (Update Final, Fase 1): ativação do sandbox por complexidade
# (`usa_sandbox`/`EXEC_SANDBOX_ALTO_ONLY`). Prioridade documentada:
#   override explícito (EXEC_SANDBOX_ENABLED=1 ou usa_sandbox explícito) >
#   sandbox condicional por complexidade.
def main_complexity_sandbox_test() -> int:
    import unittest.mock as mock

    from harness import config
    from harness.executor import Executor

    passed = 0
    failed = 0

    def check(name, cond):
        nonlocal passed, failed
        if cond:
            passed += 1
            print(f"  [PASS] {name}")
        else:
            failed += 1
            print(f"  [FAIL] {name}")

    ex = Executor()

    # (1) usa_sandbox=True (grau ALTO) + Docker disponível -> docker
    with mock.patch.object(config, "EXEC_SANDBOX_ENABLED", False), \
            mock.patch.object(config, "EXEC_SANDBOX_ALTO_ONLY", False), \
            mock.patch.object(config, "EXEC_SANDBOX_BACKEND", "docker"), \
            mock.patch.object(Executor, "_docker_disponivel", return_value=True):
        check("(1) usa_sandbox=True + docker disp -> docker",
              ex._sandbox_modo(usa_sandbox=True) == "docker")

    # (2) usa_sandbox=False (baixo/médio) + Docker disponível -> HOST
    with mock.patch.object(config, "EXEC_SANDBOX_ENABLED", False), \
            mock.patch.object(config, "EXEC_SANDBOX_ALTO_ONLY", False), \
            mock.patch.object(config, "EXEC_SANDBOX_BACKEND", "docker"), \
            mock.patch.object(Executor, "_docker_disponivel", return_value=True):
        check("(2) usa_sandbox=False + docker disp -> host",
              ex._sandbox_modo(usa_sandbox=False) == "host")

    # (3) EXEC_SANDBOX_ALTO_ONLY=True + usa_sandbox=True -> docker (se disp)
    with mock.patch.object(config, "EXEC_SANDBOX_ENABLED", False), \
            mock.patch.object(config, "EXEC_SANDBOX_ALTO_ONLY", True), \
            mock.patch.object(config, "EXEC_SANDBOX_BACKEND", "docker"), \
            mock.patch.object(Executor, "_docker_disponivel", return_value=True):
        check("(3) ALTO_ONLY=True + usa_sandbox=True + docker disp -> docker",
              ex._sandbox_modo(usa_sandbox=True) == "docker")

    # (4) EXEC_SANDBOX_ALTO_ONLY=True + usa_sandbox=False -> HOST (baixo/médio)
    with mock.patch.object(config, "EXEC_SANDBOX_ENABLED", False), \
            mock.patch.object(config, "EXEC_SANDBOX_ALTO_ONLY", True), \
            mock.patch.object(config, "EXEC_SANDBOX_BACKEND", "docker"), \
            mock.patch.object(Executor, "_docker_disponivel", return_value=True):
        check("(4) ALTO_ONLY=True + usa_sandbox=False -> host",
              ex._sandbox_modo(usa_sandbox=False) == "host")

    # (5) default (usa_sandbox=None) + EXEC_SANDBOX_ENABLED/ALTO_ONLY False
    #     -> host (ativação NÃO global por padrão)
    with mock.patch.object(config, "EXEC_SANDBOX_ENABLED", False), \
            mock.patch.object(config, "EXEC_SANDBOX_ALTO_ONLY", False), \
            mock.patch.object(config, "EXEC_SANDBOX_BACKEND", "docker"), \
            mock.patch.object(Executor, "_docker_disponivel", return_value=True):
        check("(5) default (usa_sandbox=None) -> host (não global)",
              ex._sandbox_modo() == "host")
        check("(5) usa_sandbox=None explícito -> host",
              ex._sandbox_modo(usa_sandbox=None) == "host")

    # (6) OVERRIDE explícito: EXEC_SANDBOX_ENABLED=True força docker mesmo com
    #     usa_sandbox=False (prioridade: override > condicional por complexidade)
    with mock.patch.object(config, "EXEC_SANDBOX_ENABLED", True), \
            mock.patch.object(config, "EXEC_SANDBOX_ALTO_ONLY", False), \
            mock.patch.object(config, "EXEC_SANDBOX_BACKEND", "docker"), \
            mock.patch.object(Executor, "_docker_disponivel", return_value=True):
        check("(6) EXEC_SANDBOX_ENABLED=True + usa_sandbox=False -> docker",
              ex._sandbox_modo(usa_sandbox=False) == "docker")

    # (7) ALTO_ONLY=True mas Docker indisponível -> host (fallback seguro)
    with mock.patch.object(config, "EXEC_SANDBOX_ENABLED", False), \
            mock.patch.object(config, "EXEC_SANDBOX_ALTO_ONLY", True), \
            mock.patch.object(config, "EXEC_SANDBOX_BACKEND", "auto"), \
            mock.patch.object(Executor, "_docker_disponivel", return_value=False), \
            mock.patch("harness.executor.shutil.which", return_value=None):
        check("(7) ALTO_ONLY=True + docker indisponível -> host",
              ex._sandbox_modo(usa_sandbox=True) == "host")

    print(f"\nRESULTADO (complexity sandbox): {passed} passaram, {failed} falharam")
    return 1 if failed else 0


def main_safety_nets_test() -> int:
    """Safety nets do host (A3): timeout de execução e teto de saída matam o
    processo com razão clara e NÃO deixam o job 'running' para sempre."""
    import tempfile
    import unittest.mock as mock

    from harness import config

    passed = 0
    failed = 0

    def check(name, cond):
        nonlocal passed, failed
        if cond:
            passed += 1
            print(f"  [PASS] {name}")
        else:
            failed += 1
            print(f"  [FAIL] {name}")

    with tempfile.TemporaryDirectory() as tmp:
        hist = pathlib.Path(tmp) / "history.json"
        with mock.patch.object(config, "HISTORY_FILE", hist):
            # (1) timeout host: processo longo é morto dentro do limite curto
            with mock.patch.object(config, "EXEC_HOST_TIMEOUT", 2):
                ex = Executor()
                job = ex.run('python -c "import time; time.sleep(30)"')
                snap = _wait(job, tries=60)
                check("host timeout: status error + exit None + razão timeout",
                      snap is not None
                      and snap["status"] == "error"
                      and snap["exit_code"] is None
                      and "timeout" in (snap.get("reason") or ""))
            # (2) cap de saída: comando verboso é encerrado por excesso
            with mock.patch.object(config, "EXEC_MAX_OUTPUT_BYTES", 200):
                ex2 = Executor()
                job2 = ex2.run('python -c "for i in range(50000): print(i)"')
                snap2 = _wait(job2, tries=120)
                check("cap saída: error + razão 'saída excedeu' + output limitado",
                      snap2 is not None
                      and snap2["status"] == "error"
                      and "saída excedeu" in (snap2.get("reason") or "")
                      and len(snap2["output"]) <= 300)
            # (3) comando verboso LEGÍTIMO dentro do cap NÃO é morto (regressão
            #     O(n²) do reader: 60k prints terminam rápido)
            with mock.patch.object(config, "EXEC_MAX_OUTPUT_BYTES", 10 * 1024 * 1024):
                ex3 = Executor()
                t0 = time.monotonic()
                job3 = ex3.run('python -c "for i in range(60000): print(i)"')
                snap3 = _wait(job3, tries=300)
                elapsed = time.monotonic() - t0
                check("comando verboso legítimo termina rápido (não O(n²))",
                      snap3 is not None
                      and snap3["status"] == "finished"
                      and snap3["exit_code"] == 0
                      and elapsed < 20)

    print(f"\nRESULTADO (safety nets): {passed} passaram, {failed} falharam")
    return 1 if failed else 0


if __name__ == "__main__":
    rc1 = main_test()
    print("\n== sandbox (Item 4, Fase 1) ==")
    rc2 = main_sandbox_test()
    print("\n== sandbox condicional por complexidade (Update Final, Fase 1) ==")
    rc3 = main_complexity_sandbox_test()
    print("\n== safety nets do host (timeout + cap de saída) ==")
    rc4 = main_safety_nets_test()
    sys.exit(1 if (rc1 or rc2 or rc3 or rc4) else 0)
