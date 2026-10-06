"""Executor de comandos com políticas de segurança e captura de saída."""

from __future__ import annotations

import json
import os
import pathlib
import shlex
import shutil
import subprocess
import threading
import time
import uuid

from . import config

# Separadores de shell que NUNCA podem existir em um comando essencial puro
# (Finding 1 da re-revisão). Em cmd.exe/PowerShell (shell=True), `&`, `&&`,
# `|`, `||`, `;` encadeiam comandos e `<`, `>` fazem redirecionamento; um
# separador APÓS o prefixo essencial encadearia um comando adicional (ex.:
# `git status && del file.txt`). Mesma disciplina do `_no_safelist_risco_baixo`
# do pipeline (`harness/pipeline.py`).
_SEPARADORES_SHELL = "&|;<>"

# Interpretadores de shell que recebem uma STRING DE COMANDO via flag
# (`sh -c 'rm -rf x'`, `cmd /c rm -rf x`, `git bash -c "rm -rf x"`). Um `rm`
# destrutivo DENTRO dessa string é um `rm` que EXECUTA de verdade — o
# guardrail precisa alcançá-lo (ver `_flatten_shell`).
_SHELL_INTERPRETERS = frozenset({
    "sh", "bash", "zsh", "ksh", "dash", "ash", "csh", "tcsh",
    "fish", "pwsh", "powershell", "cmd",
})
# Flags que introduzem a string de comando do interpretador/runner. Inclui
# `-S` (env -S "cmd"), `-exec`/`-execdir` (find . -exec rm -rf ...) e as
# variantes de PowerShell.
_SHELL_CMD_FLAGS = frozenset({
    "-c", "/c", "/k", "-command", "-commandtext", "-e", "--exec",
    "-s", "-exec", "-execdir",
})
# Comandos "runner" que executam o PRÓXIMO token (ou um comando citado) como
# um comando real (ex.: `wsl rm -rf x`, `sudo rm -rf x`, `xargs rm -rf x`,
# `find . -exec rm -rf {} +`, `env -S "rm -rf x"`). Não inclui
# `echo`/`printf`/`type` — que tratam o próximo token como ARGUMENTO (texto).
_RUNNERS_DE_COMANDO = frozenset({
    "wsl", "sudo", "env", "time", "nohup", "nice", "command", "exec",
    "xargs", "pkexec", "doas", "su", "runuser", "setsid", "timeout",
    "busybox", "conda", "nix-shell", "watch", "nsenter", "screen", "find",
})
# Comandos que SÓ imprimem texto: um `rm` que vem DEPOIS deles é ARGUMENTO
# (nunca executa), mesmo sob um runner (`wsl echo rm -rf x`, `sudo echo ...`).
_COMANDOS_QUE_IMPRIMEM = frozenset({
    "echo", "printf", "type", "help", "cat", "test", "read",
})

# B1 (segurança de deploy): razão ÚNICA de bloqueio de execução no host em modo
# público. A decisão é tomada pelo MODO DE EXECUÇÃO REAL (`_sandbox_modo` +
# `_worker` — caminho único de execução), NUNCA pelo backend resolvido em
# `run()`: com HARNESS_PUBLIC=1, se o modo final seria "host" (backend
# indisponível, usa_sandbox=False, defaults, ou falha do container), o job é
# marcado como "blocked" e NADA é executado.
BLOQUEIO_PUBLICO_SEM_SANDBOX = (
    "modo público exige sandbox Docker/Podman (HARNESS_PUBLIC=1)"
)


class ExecResult:
    """Resultado de uma execução, com saída acumulada."""

    def __init__(self, cwd: str, retries: int = 0, group: str | None = None):
        self.id = uuid.uuid4().hex[:8]
        self.command = ""
        self.cwd = cwd
        self.output: list[str] = []
        self.exit_code: int | None = None
        self.status = "running"  # running | finished | blocked | stopped | error
        self.reason = ""
        self.retries = retries
        self.attempts = 1
        self.group = group
        self.sandbox = False  # True se o comando rodou em container (Item 4)
        self._usa_sandbox: bool | None = None  # override condicional (Fase 1)
        self._proc: subprocess.Popen | None = None
        self._lock = threading.Lock()
        self._output_cap = False  # True se a saída excedeu EXEC_MAX_OUTPUT_BYTES
        self._output_bytes = 0  # contador INCREMENTAL (evita O(n²) no reader)

    def append(self, chunk: str) -> None:
        with self._lock:
            self.output.append(chunk)

    def snapshot(self) -> dict:
        with self._lock:
            return {
                "id": self.id,
                "command": self.command,
                "cwd": self.cwd,
                "output": "".join(self.output),
                "exit_code": self.exit_code,
                "status": self.status,
                "reason": self.reason,
                "retries": self.retries,
                "attempts": self.attempts,
                "group": self.group,
                "sandbox": self.sandbox,
            }

    def stop(self) -> bool:
        """Solicita o encerramento do job. Marca `stopped` imediatamente e
        termina o processo se já existir (evita corrida com a thread).

        No Windows (shell=True), `terminate()` mata apenas o cmd.exe; o
        processo neto (ex.: python) continuaria segurando o pipe e a thread do
        worker ficaria presa (N3). Por isso o `taskkill /T /F` roda ANTES do
        `terminate()`: derruba a árvore inteira (cmd.exe + neto) e fecha o
        pipe; o terminate subsequente é defesa (no-op se a árvore já caiu).
        Se o taskkill falhar (sem permissão, processo já encerrado), o
        terminate cobre como fallback. Nada aqui levanta exceção. Em POSIX o
        comportamento é inalterado (terminate como antes; taskkill não existe)."""
        with self._lock:
            if self.status not in ("running",):
                return False
            self.status = "stopped"
            proc = self._proc
        if proc is not None and proc.poll() is None:
            if os.name == "nt":
                try:
                    subprocess.run(
                        ["taskkill", "/T", "/F", "/PID", str(proc.pid)],
                        capture_output=True,
                    )
                except (OSError, subprocess.SubprocessError):
                    pass  # sem permissão/processo sumiu: terminate é o fallback
            try:
                proc.terminate()
            except (OSError, subprocess.SubprocessError):
                pass  # processo já encerrado (ex.: taskkill funcionou): sem erro
        return True


class Executor:
    def __init__(self):
        self._jobs: dict[str, ExecResult] = {}
        self._lock = threading.Lock()
        self._hist_lock = threading.Lock()
        self._load_history()

    # ------------------------------------------------------------------ jobs
    def run(self, command: str, cwd: str | None = None,
            retries: int | None = None, group: str | None = None,
            usa_sandbox: bool | None = None) -> ExecResult:
        """Inicia um comando. `retries` aplica o padrão Ch12 (retry com
        backoff progressivo) a falhas de saída não-zero. `group` agrupa jobs
        para paralelização (Ch3). `cwd` é validado contra `config.ALLOWED_CWD`;
        caminhos fora do projeto são bloqueados.

        `usa_sandbox` (Update Final, Fase 1) habilita o sandbox CONDICIONAL por
        complexidade: `True` (vindo do pipeline para tarefas de complexidade
        ALTA) tenta o container quando o Docker está disponível; `False`
        (baixo/médio) força o modo HOST mesmo com Docker disponível; `None`
        (default) não força nada e respeita a config (`EXEC_SANDBOX_ENABLED` /
        `EXEC_SANDBOX_ALTO_ONLY`). A prioridade é: override explícito
        (`EXEC_SANDBOX_ENABLED=1` ou `usa_sandbox` explícito) > sandbox
        condicional por complexidade.

        B1 (segurança de deploy): em modo público (HARNESS_PUBLIC=1), a decisão
        de bloquear a execução no host acontece NO `_worker` — o único caminho
        de execução (run/run_many/retry/fallback de container passam todos por
        ele) — com base no MODO REAL resolvido por `_sandbox_modo`; ver
        `BLOQUEIO_PUBLICO_SEM_SANDBOX`. Nada é executado no host em público."""
        if retries is None:
            retries = 0
        try:
            retries = max(0, min(int(retries), config.RETRY_MAX))
        except (TypeError, ValueError):
            retries = 0
        cwd = self._resolve_cwd(cwd)
        job = ExecResult(cwd, retries=retries, group=group)
        job._usa_sandbox = usa_sandbox

        if not command.strip():
            job.status = "error"
            job.reason = "Comando vazio"
            job.exit_code = 1
            return job

        job.command = command
        if cwd is None:
            job.status = "blocked"
            job.reason = "cwd fora dos diretórios permitidos"
            self._record(command, "blocked", job.reason)
            with self._lock:
                self._jobs[job.id] = job
            return job

        policy = self.check_policy(command)
        job.status = "blocked"
        job.reason = policy
        if policy:
            self._record(command, "blocked", policy)
            with self._lock:
                self._jobs[job.id] = job
            return job

        job.status = "running"
        with self._lock:
            self._jobs[job.id] = job

        thread = threading.Thread(
            target=self._worker, args=(job,), daemon=True
        )
        thread.start()
        self._record(command, "started", "")
        return job

    def run_many(self, commands: list[str], cwd: str | None = None,
                 retries: int | None = None) -> list[ExecResult]:
        """Executa vários comandos em paralelo (Ch3). Retorna os jobs criados;
        cada um roda na própria thread. O grupo compartilha um `group` id."""
        group_id = f"g-{uuid.uuid4().hex[:8]}"
        jobs = [self.run(cmd, cwd=cwd, retries=retries, group=group_id)
                for cmd in commands]
        return jobs

    @staticmethod
    def _resolve_cwd(cwd: str | None) -> str | None:
        """Resolve e valida o cwd contra ALLOWED_CWD. Retorna None se fora
        dos diretórios permitidos (comando será bloqueado)."""
        if not cwd:
            return str(config.ROOT)
        try:
            resolved = pathlib.Path(cwd).resolve()
        except OSError:
            return None
        for allowed in config.ALLOWED_CWD:
            allowed = allowed.resolve()
            if resolved == allowed or allowed in resolved.parents:
                return str(resolved)
        return None

    def _worker(self, job: ExecResult) -> None:
        # Item 4 (Fase 1): resolve o modo de execução UMA vez. "host" = modo
        # atual (shell=True). "docker"/"podman" = container (se disponível e
        # habilitado). Nunca crasha: qualquer falha de resolução cai em host.
        # Update Final (Fase 1): o override condicional `job._usa_sandbox`
        # (usa_sandbox vindo do pipeline por complexidade) é repassado.
        # B1 (segurança de deploy): `_sandbox_modo` retorna "blocked" quando o
        # MODO REAL seria host em modo público (HARNESS_PUBLIC=1 +
        # PUBLIC_REQUIRE_SANDBOX). Nesse caso o job é marcado como bloqueado e
        # NENHUM Popen é iniciado — não há caminho de fuga para o host.
        modo = self._sandbox_modo(usa_sandbox=job._usa_sandbox)
        if modo == "blocked":
            job.status = "blocked"
            job.reason = BLOQUEIO_PUBLICO_SEM_SANDBOX
            self._record(job.command, "blocked", job.reason)
            return
        attempt = 0
        while True:
            attempt += 1
            job.attempts = attempt
            if attempt > 1:
                job.append(f"[retry {attempt - 1}/{job.retries}] em {job.cwd}...\n")
            try:
                if modo in ("docker", "podman"):
                    sand = self._run_sandbox(job.command, job.cwd,
                                             backend=modo, job=job)
                    if sand is not None:
                        job.sandbox = True
                        for chunk in sand.get("output_lines", ()):
                            job.append(chunk)
                        job.exit_code = sand.get("exit_code", 1)
                        # stop() pode ter marcado "stopped" enquanto o
                        # container rodava (job._proc registrado no sandbox);
                        # o status retornado pelo sandbox NÃO sobrescreve o
                        # pedido de parada (job deve encerrar como "stopped").
                        if job.status != "stopped":
                            job.status = sand.get("status", "finished")
                        self._record(
                            job.command, job.status,
                            f"exit={job.exit_code} sandbox={modo}",
                        )
                        # retry (Ch12) vale para falha de saída no container
                        if job.exit_code == 0 or job.status == "stopped":
                            if job.exit_code == 0:
                                job.status = "finished"
                            break
                        if attempt <= job.retries:
                            wait = config.RETRY_BACKOFF * attempt
                            job.status = "running"
                            job.reason = f"exit={job.exit_code}, retry em {wait:.1f}s"
                            self._record(
                                job.command, "retry",
                                f"exit={job.exit_code} attempt={attempt}",
                            )
                            time.sleep(wait)
                            continue
                        job.status = "error"
                        job.reason = f"exit={job.exit_code} após {attempt} tentativas"
                        self._record(job.command, "error", job.reason)
                        break
                    # sandbox não pôde ser usado. Em modo PÚBLICO
                    # (HARNESS_PUBLIC=1) o fallback para o host é BLOQUEADO
                    # (B1): o container falhou e a execução no host é proibida
                    # por deploy — marca blocked e encerra, sem executar nada.
                    # Apenas em modo NÃO público o fallback host continua.
                    if config.HARNESS_PUBLIC:
                        job.status = "blocked"
                        job.reason = BLOQUEIO_PUBLICO_SEM_SANDBOX
                        self._record(job.command, "blocked", job.reason)
                        return
                    modo = "host"
                job._proc = subprocess.Popen(
                    job.command,
                    shell=True,
                    cwd=job.cwd,
                    stdout=subprocess.PIPE,
                    stderr=subprocess.STDOUT,
                    text=True,
                    encoding="utf-8",
                    errors="replace",
                )
                # stop() pode ter ocorrido antes do Popen; encerra o processo
                if job.status == "stopped":
                    job._proc.terminate()
                    try:
                        job.exit_code = job._proc.wait()
                    except Exception:  # noqa: BLE001
                        job.exit_code = None
                    break
                # EXEC-4/EXEC-5 (safety nets do host): reader em thread para
                # STREAMAR a saída com TETO de bytes, e espera com TIMEOUT de
                # execução (EXEC_HOST_TIMEOUT) — antes, `for line in stdout`
                # bloqueava até o processo sair (um `sleep 3600` rodava 1h e o
                # output de um `dir /s` crescia sem limite).
                def _le_saida() -> None:
                    try:
                        for linha in job._proc.stdout:
                            with job._lock:
                                if job._output_bytes >= config.EXEC_MAX_OUTPUT_BYTES:
                                    job._output_cap = True
                                    break
                                job._output_bytes += len(linha)
                            job.append(linha)
                    except (OSError, ValueError):
                        pass  # pipe fechado (processo morto no timeout/cap)

                reader = threading.Thread(target=_le_saida, daemon=True)
                reader.start()
                deadline = (
                    time.monotonic() + float(config.EXEC_HOST_TIMEOUT)
                    if config.EXEC_HOST_TIMEOUT > 0 else None
                )
                while True:
                    if job.status == "stopped":
                        # pedido de parada: derruba a árvore e captura o
                        # exit_code REAL do processo morto (antes, o `wait()`
                        # pós-kill retornava o código; não zerar para None).
                        _matar_arvore_sandbox(job._proc)
                        try:
                            job.exit_code = job._proc.wait(timeout=3)
                        except subprocess.TimeoutExpired:
                            job.exit_code = None
                        except (OSError, subprocess.SubprocessError):
                            job.exit_code = None
                        break
                    if job._output_cap:
                        # saída descontrolada: encerra o processo (o reader
                        # parou de ler -> o child travaria no pipe).
                        _matar_arvore_sandbox(job._proc)
                        job.exit_code = None
                        job.status = "error"
                        job.reason = (
                            f"saída excedeu o limite "
                            f"({config.EXEC_MAX_OUTPUT_BYTES} bytes)"
                        )
                        self._record(job.command, "error", job.reason)
                        break
                    try:
                        job.exit_code = job._proc.wait(timeout=0.25)
                        break
                    except subprocess.TimeoutExpired:
                        if deadline is not None and time.monotonic() >= deadline:
                            _matar_arvore_sandbox(job._proc)
                            job.exit_code = None
                            job.status = "error"
                            job.reason = (
                                f"timeout (limite de {config.EXEC_HOST_TIMEOUT}s "
                                "no host)"
                            )
                            self._record(job.command, "error", job.reason)
                            break
                reader.join(timeout=2)
            except Exception as exc:  # noqa: BLE001
                job.status = "error"
                job.reason = str(exc)
                job.exit_code = 1
                self._record(job.command, "error", str(exc))
                break

            if job.status == "stopped":
                break

            # sucesso: encerra
            if job.exit_code == 0:
                job.status = "finished"
                self._record(job.command, "finished", f"exit={job.exit_code} attempts={attempt}")
                break

            # falha com retry restante (Ch12): espera backoff progressivo
            if attempt <= job.retries:
                wait = config.RETRY_BACKOFF * attempt
                job.status = "running"
                job.reason = f"exit={job.exit_code}, retry em {wait:.1f}s"
                self._record(job.command, "retry", f"exit={job.exit_code} attempt={attempt}")
                time.sleep(wait)
                continue

            job.status = "error"
            if not job.reason:
                job.reason = f"exit={job.exit_code} após {attempt} tentativas"
            self._record(job.command, "error", job.reason)
            break

    # ------------------------------------------------------- sandbox (Item 4)
    def _sandbox_modo(self, usa_sandbox: bool | None = None) -> str:
        """Resolve o modo de execução REAL deste job (host | docker | podman).

        B1 (segurança de deploy — re-revisão): em modo público
        (`config.HARNESS_PUBLIC` e `config.PUBLIC_REQUIRE_SANDBOX`), um modo de
        execução que seria "host" retorna o sentinela "blocked" — o `_worker`
        marca o job como bloqueado e NÃO executa nada. O critério é o MODO DE
        EXECUÇÃO REAL (não o backend resolvido isoladamente): cobre defaults
        (EXEC_SANDBOX_ENABLED/ALTO_ONLY=False, usa_sandbox=None), override
        usa_sandbox=False (força host) e backend indisponível — todos viram
        "blocked" em público. Apenas em modo não público o "host" é permitido.
        """
        modo = self._sandbox_modo_inner(usa_sandbox)
        if config.HARNESS_PUBLIC and config.PUBLIC_REQUIRE_SANDBOX \
                and modo == "host":
            return "blocked"
        return modo

    def _sandbox_modo_inner(self, usa_sandbox: bool | None = None) -> str:
        """Resolve o backend de sandbox para esta execução.

        Retorna "host" (modo atual, shell=True) quando o sandbox está
        desabilitado ou indisponível. Caso contrário, "docker"/"podman" conforme
        `EXEC_SANDBOX_BACKEND`. Sempre com try/except — nunca crasha: qualquer
        falha de detecção cai para "host". `check_policy` continua valendo em
        qualquer modo (bloqueio destrutivo não é relaxado pelo sandbox).
        O gate público (B1) é aplicado pelo wrapper `_sandbox_modo`.

        `usa_sandbox` (Update Final, Fase 1) — ativação CONDICIONAL por
        complexidade. Prioridade (documentada):
          1. Override EXPLÍCITO: `EXEC_SANDBOX_ENABLED=True` força o sandbox
             para QUALQUER comando (maior precedência).
          2. Override explícito por task: `usa_sandbox=False` força HOST
             (baixo/médio) mesmo com Docker disponível; `usa_sandbox=True`
             tenta container (grau alto) se disponível.
          3. Sandbox condicional por env: `EXEC_SANDBOX_ALTO_ONLY=True` liga o
             sandbox apenas quando `usa_sandbox=True` (complexidade alta);
             baixo/médio ficam no host.
        O default de `EXEC_SANDBOX_ENABLED` e `EXEC_SANDBOX_ALTO_ONLY` é `False`
        — NÃO ativa sandbox globalmente por padrão (ativação é por complexidade
        alta, via `usa_sandbox` vindo do pipeline).
        """
        # 1) override explícito global: EXEC_SANDBOX_ENABLED forçado
        if config.EXEC_SANDBOX_ENABLED:
            return self._resolve_backend()
        # 2) override explícito por task: usa_sandbox=False força HOST
        if usa_sandbox is False:
            return "host"
        # 3) sandbox condicional por complexidade: só para usa_sandbox=True
        #    (grau alto); se ALTO_ONLY está desligado E usa_sandbox não é True,
        #    não ativa (default host).
        if usa_sandbox is True or config.EXEC_SANDBOX_ALTO_ONLY:
            # usa_sandbox=True (complexidade alta) ou ALTO_ONLY ligado -> tenta
            # o container se disponível; senão cai para host (nunca bloqueia).
            return self._resolve_backend()
        return "host"

    def _resolve_backend(self) -> str:
        """Resolve o backend de container ("docker"/"podman") quando o sandbox
        está ativo, ou "host" se indisponível. Nunca crasha."""
        try:
            backend = (config.EXEC_SANDBOX_BACKEND or "auto").lower()
            if backend == "docker":
                if self._docker_disponivel():
                    return "docker"
                return "host"
            if backend == "podman":
                if shutil.which("podman"):
                    return "podman"
                return "host"
            # auto: docker CLI/SDK -> podman CLI -> host
            if self._docker_disponivel():
                return "docker"
            if shutil.which("podman"):
                return "podman"
            return "host"
        except Exception:  # noqa: BLE001 — nunca crashar
            return "host"

    @staticmethod
    def _docker_cli_path() -> str | None:
        """Localiza o executável do CLI docker (Docker Desktop), mesmo fora do
        PATH. Ordem:
          1. `config.EXEC_SANDBOX_DOCKER_CLI` (caminho explícito, se definido);
          2. `shutil.which("docker")` (PATH padrão);
          3. Candidatos comuns do Docker Desktop
             (`config.EXEC_SANDBOX_DOCKER_CLI_CANDIDATES`, com `%ProgramFiles%` /
             `%LOCALAPPDATA%` expandidos via `os.path.expandvars`).
        Retorna o caminho absoluto do CLI se encontrado e executável, senão
        `None`. Nunca crasha. Não conecta no daemon — apenas presença do CLI."""
        candidatos: list[str] = []
        expl = (config.EXEC_SANDBOX_DOCKER_CLI or "").strip()
        if expl:
            candidatos.append(expl)
        no_path = shutil.which("docker")
        if no_path:
            candidatos.append(no_path)
        candidatos.extend(config.EXEC_SANDBOX_DOCKER_CLI_CANDIDATES)
        for cand in candidatos:
            try:
                p = pathlib.Path(os.path.expandvars(cand))
            except (OSError, ValueError):
                continue
            if p.is_file():
                # confirma que é o CLI docker real (evita falsos positivos como
                # o "Docker Desktop.exe")
                if p.name.lower() in ("docker.exe", "docker"):
                    return str(p)
        return None

    @classmethod
    def _docker_disponivel(cls) -> bool:
        """True se o Docker está utilizável, ou seja, o DAEMON responde.

        Pendência B2 (endurecimento): antes apenas a PRESENÇA do cliente
        (CLI/SDK) era verificada — retornava True mesmo com o daemon caído
        (falso positivo). Agora, quando o CLI é localizado via
        `_docker_cli_path()`, roda `docker info` com um TIMEOUT CURTO
        (`config.EXEC_SANDBOX_DAEMON_TIMEOUT`, default 3s) e retorna True
        somente se o daemon responder (exit 0). Se o CLI não for encontrado,
        faz fallback para o SDK docker (import docker) como antes; se nem CLI
        nem SDK, False. Falha do `docker info` (daemon down, timeout, erro) ->
        False (o sandbox cai para host — comportamento seguro). Nunca crasha.
        """
        try:
            cli = cls._docker_cli_path()
            if cli is not None:
                # CLI presente: verifica a ATIVIDADE do daemon (não só a
                # presença do CLI). Timeout curto é essencial: com o daemon
                # caído, o `docker info` pode demorar para falhar; o timeout
                # evita travar a resolução do sandbox.
                try:
                    proc = subprocess.run(
                        [cli, "info"],
                        capture_output=True,
                        text=True,
                        encoding="utf-8",
                        errors="replace",
                        timeout=float(config.EXEC_SANDBOX_DAEMON_TIMEOUT),
                        shell=False,
                    )
                    return proc.returncode == 0
                except (OSError, subprocess.SubprocessError, ValueError):
                    # daemon down / timeout / CLI inexecutável -> inativo
                    return False
            # CLI ausente: fallback para o SDK docker (dependência opcional)
            import docker  # noqa: F401 — dependência opcional
            return True
        except Exception:  # noqa: BLE001
            return False

    @staticmethod
    def _traduz_cwd(cwd: str) -> str | None:
        """Traduz o cwd (host) para o caminho correspondente dentro do mount
        do container. O mount raiz (`EXEC_SANDBOX_MOUNT_ROOT`) é montado em
        `EXEC_SANDBOX_MOUNT_PATH` (ex.: /workspace); um cwd dentro da raiz vira
        o subcaminho correspondente (ex.: `c:/harness-mvp/sub` ->
        `/workspace/sub`). Retorna None se o cwd estiver fora do mount (usa o
        mount path como fallback seguro)."""
        try:
            root = pathlib.Path(config.EXEC_SANDBOX_MOUNT_ROOT).resolve()
            cwd_p = pathlib.Path(cwd).resolve()
            rel = cwd_p.relative_to(root)
        except (ValueError, OSError):
            return str(config.EXEC_SANDBOX_MOUNT_PATH)
        if str(rel) == ".":
            return str(config.EXEC_SANDBOX_MOUNT_PATH)
        return f"{config.EXEC_SANDBOX_MOUNT_PATH}/{rel.as_posix()}"

    def _run_sandbox(self, command: str, cwd: str,
                     backend: str = "docker",
                     job: ExecResult | None = None) -> dict | None:
        """Executa o comando em um container Docker/Podman e captura a saída.

        Monta `EXEC_SANDBOX_MOUNT_ROOT` em `EXEC_SANDBOX_MOUNT_PATH` (ex.:
        /workspace) e roda `sh -c "<command>"` com o `-w` apontando para o
        subcaminho traduzido do cwd host. Captura stdout/stderr e exit_code
        REAIS. Timeout via thread + `_matar_arvore_sandbox`. `check_policy` já
        foi aplicado ANTES (em `run`), valendo em qualquer modo.

        Fase 2 (hardening): o container roda com `--network none` por padrão
        (isolamento de rede — o comando no sandbox não acessa a rede).

        Se `job` for passado, o Popen do CLI (docker/podman) é registrado em
        `job._proc` para que `stop()` possa interromper o `docker run` de forma
        análoga ao modo host (taskkill/terminate da árvore do CLI). Aviso:
        matar o CLI `docker run` interrompe o comando e encerra o job, mas não
        garante a remoção do container órfão no daemon (limitação conhecida do
        docker run --rm com CLI morto).

        Retorna dict com `output`, `output_lines`, `exit_code`, `status`, ou
        `None` se o container não pôde ser usado (fallback para host). Nunca
        crasha: qualquer exceção aqui é convertida em `None`.
        """
        try:
            mount = str(pathlib.Path(config.EXEC_SANDBOX_MOUNT_ROOT))
            wdir = self._traduz_cwd(cwd)
            timeout = float(config.EXEC_SANDBOX_TIMEOUT)
            # CLI: usa o caminho absoluto do docker (Docker Desktop) quando
            # localizado; senão o backend ("docker"/"podman") como comando.
            cli = backend  # "docker" ou "podman"
            if backend == "docker":
                cli_path = self._docker_cli_path()
                if cli_path:
                    cli = cli_path
            cmd = [
                cli, "run", "--rm",
                # Fase 2: isolamento de rede por padrão (sem --network none o
                # container herdaria a rede do host).
                "--network", "none",
                "-v", f"{mount}:{config.EXEC_SANDBOX_MOUNT_PATH}",
                "-w", wdir,
            ]
            # Fase 2 (hardening): limites de recurso e privilégios. As flags
            # são aplicadas CONDICIONALMENTE à config — nenhuma quebra a
            # escrita no mount /workspace (o `--read-only` só congela o rootfs).
            if config.EXEC_SANDBOX_USER:
                cmd += ["--user", config.EXEC_SANDBOX_USER]
            if config.EXEC_SANDBOX_READ_ONLY:
                cmd.append("--read-only")
            if config.EXEC_SANDBOX_MEMORY:
                cmd += ["--memory", config.EXEC_SANDBOX_MEMORY]
            if config.EXEC_SANDBOX_CPUS:
                cmd += ["--cpus", config.EXEC_SANDBOX_CPUS]
            if config.EXEC_SANDBOX_PIDS_LIMIT > 0:
                cmd += ["--pids-limit", str(config.EXEC_SANDBOX_PIDS_LIMIT)]
            if config.EXEC_SANDBOX_CAP_DROP:
                cmd += ["--cap-drop", "ALL"]
            if config.EXEC_SANDBOX_NO_NEW_PRIVS:
                cmd += ["--security-opt", "no-new-privileges"]
            cmd += [config.EXEC_SANDBOX_IMAGE, "sh", "-c", command]
            proc = subprocess.Popen(
                cmd,
                stdout=subprocess.PIPE,
                stderr=subprocess.STDOUT,
                text=True,
                encoding="utf-8",
                errors="replace",
                shell=False,
            )
            if job is not None:
                # expõe o processo do container ao stop() (análogo ao host)
                job._proc = proc
                # stop() pode ter ocorrido antes do Popen; encerra o container
                # imediatamente (o communicate() a seguir retorna rápido)
                if job.status == "stopped":
                    _matar_arvore_sandbox(proc)
            timer = threading.Timer(timeout, _matar_arvore_sandbox, args=(proc,))
            timer.daemon = True
            timer.start()
            try:
                out, _ = proc.communicate()
            finally:
                timer.cancel()
            exit_code = proc.returncode  # real (não fabricado)
            if proc.returncode is None:
                exit_code = 1  # timeout/forçado
            # Finding A (re-revisão Item 4): quando o CLI docker existe mas o
            # DAEMON está inativo, o `docker run` NÃO levanta exceção no Popen —
            # o CLI imprime o erro de conectividade no stderr (mesclado em `out`
            # via stderr=STDOUT) e sai com exit_code != 0 (ex.: 125). Isso não é
            # um ERRO DO COMANDO que rodou no container, e sim FALHA DE
            # INFRAESTRUTURA do daemon: o isolamento não aconteceu. Nesse caso
            # retornamos None -> `_worker` cai para o modo host seguro. Só
            # tratamos como falha de infra os ERROS DE CONECTIVIDADE do daemon;
            # um exit != 0 de um comando que RODOU no container (isolamento
            # funcionou) continua sendo erro do comando (não cai para host, para
            # não re-executar no host um comando que falhou isolado).
            if exit_code != 0 and self._eh_falha_infra_daemon(out):
                return None
            lines = out.splitlines(keepends=True) if out else []
            return {
                "output": out,
                "output_lines": lines,
                "exit_code": exit_code,
                "status": "finished" if exit_code == 0 else "error",
            }
        except Exception:  # noqa: BLE001 — qualquer falha => fallback host
            return None

    @staticmethod
    def _eh_falha_infra_daemon(out: str) -> bool:
        """True se `out` (stdout/stderr do CLI docker/podman) indica FALHA DE
        INFRAESTRUTURA do daemon — ou seja, o CLI executou mas NÃO conseguiu
        conectar/acionar o daemon (o container NÃO chegou a rodar o comando).

        Detecta, case-insensitive, as assinaturas de conectividade do daemon
        emitidas pelos CLIs Docker e Podman quando o daemon está fora do ar:
          - "cannot connect to the docker daemon" (Docker, específica)
          - "cannot connect" + CONTEXTO de daemon (ex.: Podman:
            "cannot connect to ... podman.sock")
          - "podman.sock" / "docker.sock" (sockets de conectividade)
          - "dockerdesktoplinuxengine" (engine do Docker Desktop)
          - "error during connect" (Docker e Podman)
          - "connection refused" + CONTEXTO de daemon (recusa no socket)
          - "is the docker daemon running"
          - "internal server error"
          - HTTP "500" (resposta de erro do daemon via API)

        RESTRIÇÃO (A1 da re-revisão da Fase 2): as assinaturas GENÉRICAS
        "cannot connect" e "connection refused" SÓ casam com CONTEXTO de
        daemon/container ("docker", "podman", "daemon", "containerd"). Sem
        contexto, são saída LEGÍTIMA de um comando que RODOU no container
        (ex.: `curl: connection refused` numa porta fechada, cliente socket
        apontando para IP/porta) — classificar como falha de infra faria o
        comando cair para o HOST (re-execução no host + quebra pontual do
        isolamento de rede). As assinaturas específicas do Docker
        (dockerDesktopLinuxEngine, error during connect, is the docker daemon
        running, internal server error, http 500) permanecem como estão.
        """
        if not out:
            return False
        low = out.lower()
        # Assinaturas ESPECÍFICAS do daemon: o contexto de infra já está
        # embutido na frase/socket — casam sozinhas.
        if any(sig in low for sig in (
            "cannot connect to the docker daemon",
            "podman.sock",
            "docker.sock",
            "dockerdesktoplinuxengine",
            "error during connect",
            "is the docker daemon running",
            "internal server error",
            # HTTP 500 do daemon (ex.: "500 Internal Server Error") — o status
            # 500 sozinho pode aparecer na saída de um comando; exigimos o
            # contexto HTTP para reduzir falsos positivos.
            "http 500",
            "http/1.0 500",
            "http/1.1 500",
        )):
            return True
        # Assinaturas GENÉRICAS: "cannot connect" / "connection refused" só
        # valem acompanhadas de contexto de daemon/container (A1). Reduz os
        # falsos positivos com saída legítima do container ("curl: connection
        # refused", "cannot connect to 127.0.0.1:3306", etc.).
        if "cannot connect" in low or "connection refused" in low:
            return any(ctx in low for ctx in (
                "docker",     # cobre docker.sock / dockerdesktop*
                "podman",     # cobre podman.sock
                "daemon",
                "containerd",
            ))
        return False

    def get(self, job_id: str) -> dict | None:
        with self._lock:
            job = self._jobs.get(job_id)
            return job.snapshot() if job else None

    def list(self) -> list[dict]:
        with self._lock:
            return [job.snapshot() for job in self._jobs.values()]

    def stop(self, job_id: str) -> bool:
        with self._lock:
            job = self._jobs.get(job_id)
        return job.stop() if job else False

    def group(self, group_id: str) -> list[dict]:
        """Snapshots de todos os jobs que pertencem a um grupo (Ch3)."""
        with self._lock:
            return [job.snapshot() for job in self._jobs.values()
                    if job.group == group_id]

    # --------------------------------------------------------------- política
    @staticmethod
    def _tokenize(command: str) -> list[str]:
        """Tokeniza o comando com shlex. Nunca crasha: falha -> []."""
        try:
            return shlex.split(command)
        except (ValueError, OSError):
            return []

    @staticmethod
    def _base_tok(tok: str) -> str:
        """Token em caixa baixa, sem o sufixo `.exe` (Windows: os binários
        reais são `cmd.exe`, `powershell.exe`, `wsl.exe`, `bash.exe` — o
        guardrail precisa reconhecê-los como os nomes nus `cmd`/`powershell`/
        `wsl`/...). Remoção de sufixo REAL (`.endswith`), não `rstrip` por
        conjunto de caracteres — `'nice'.rstrip('.exe')` viraria `'nic'`."""
        low = tok.lower()
        if low.endswith(".exe"):
            return low[:-4]
        return low

    @staticmethod
    def _flatten_shell(tokens: list[str]) -> list[str]:
        """Expande strings de comando passadas a interpretadores/runner de
        shell em tokens individuais, para que um `rm` destrutivo DENTRO delas
        seja detectado pelas mesmas regras posicionais.

        Cobre os bypasses reais do guardrail de `rm`:
          - `sh -c 'rm -rf x'` / `git bash -c "rm -rf x"` (quoted: a string é
            UM token `rm -rf x`, que os padrões de substring e o scan por
            token `rm` não alcançavam);
          - `sh -c rm -rf x` / `cmd /c rm -rf x` (unquoted: `rm` após a flag
            `-c`/`/c`, fora de posição de comando);
          - `wsl rm -rf x` (o próximo token do runner é o comando);
          - `su -c "rm -rf x"` / `powershell.exe -Command "rm -rf x"` /
            `cmd.exe /c "rm -rf x"` (runner/interpretador com flag de comando
            + string quoted);
          - sufixo `.exe` (nomes reais no Windows).
        Nunca crasha; tokens que não são strings de comando passam intactos."""
        out: list[str] = []
        i = 0
        n = len(tokens)
        while i < n:
            base = Executor._base_tok(tokens[i])
            # interpretador/runner + flag de comando (-c, /c, -Command, ...) +
            # string a executar
            if ((base in _SHELL_INTERPRETERS or base in _RUNNERS_DE_COMANDO)
                    and i + 2 < n
                    and Executor._base_tok(tokens[i + 1]) in _SHELL_CMD_FLAGS):
                out.append(tokens[i])
                out.append(tokens[i + 1])
                out.extend(Executor._tokenize(tokens[i + 2]))
                i += 3
                continue
            # runner (`wsl`): o próximo token é o comando a executar
            if base == "wsl" and i + 1 < n:
                out.append(tokens[i])
                i += 1
                continue
            out.append(tokens[i])
            i += 1
        return out

    @staticmethod
    def _rm_em_posicao_de_comando(tokens: list[str]) -> bool:
        """True se há um token `rm` em posição de COMANDO: primeiro token do
        comando, logo após separador de shell (`&&`, `||`, `;`, `|`), logo
        após uma flag de string de comando (`-c`, `/c`, ...), ou dentro do
        "head" de um runner — inclusive com ARGUMENTOS entre o runner e o `rm`
        (`sudo -u root rm -rf x`, `env -i rm -rf x`, `timeout 5 rm -rf x`).
        False se `rm` só aparece como ARGUMENTO de outro comando (ex.:
        `echo rm -rf x` — aí `rm` é só texto impresso, não um comando rm).

        Refinamento do F1 (Follow-up): separadores encadeados já são tratados
        pelo `_no_safelist_risco_baixo`/HITL do pipeline; aqui a defesa de
        FLAGS só precisa mirar o `rm` que REALMENTE executa. Os tokens já
        passam por `_flatten_shell` para alcançar `rm` dentro de strings de
        comando de interpretadores (`sh -c`, `cmd /c`, `su -c`, `wsl`)."""
        tokens = Executor._flatten_shell(tokens)
        for i, tok in enumerate(tokens):
            if tok.lower() != "rm":
                continue
            if i == 0:
                return True
            prev = Executor._base_tok(tokens[i - 1])
            if any(ch in tokens[i - 1] for ch in "&|;"):
                return True
            if prev in _SHELL_CMD_FLAGS:
                return True
            if prev in _RUNNERS_DE_COMANDO:
                return True
            # runner com ARGUMENTOS antes do `rm` (`sudo -u root rm -rf x`):
            # varre para trás até o separador/início; se houver um runner no
            # caminho, o `rm` é o comando que o runner executa. Se aparecer um
            # comando que SÓ IMPRIME (`echo`), o `rm` é argumento dele
            # (`wsl echo rm -rf x`, `sudo echo rm -rf x` NÃO bloqueiam).
            j = i - 1
            while j >= 0:
                if any(ch in tokens[j] for ch in "&|;"):
                    break  # comando novo após separador: sem runner
                basej = Executor._base_tok(tokens[j])
                if basej in _COMANDOS_QUE_IMPRIMEM:
                    break  # rm é argumento de um comando que imprime
                if basej in _RUNNERS_DE_COMANDO:
                    return True
                j -= 1
        return False

    @staticmethod
    def _rm_destrutivo(command: str) -> str:
        """Detecta `rm` DESTRUTIVO por FLAGS (F1), coladas ou separadas, em
        qualquer ordem/caixa: `rm -r -f x`, `rm -f -R x`, `rm -rf x`,
        `rm -fr x`, `rm -rF`, `rm -Rf`, `rm -r --force x`, etc.

        Refinamento (Follow-up): `rm` só é considerado quando está em posição
        de COMANDO — primeiro token ou logo após separador de shell (`&&`,
        `;`, `|`, `||`). `rm` como ARGUMENTO de outro comando (ex.:
        `echo rm -rf x`) NÃO é um comando rm e não bloqueia (elimina o falso
        positivo do echo; o `echo` só imprime o texto).

        Critério: um `rm` em posição de comando seguido de flag recursiva
        (-r/-R/--recursive) E flag de força (-f/--force), em QUAISQUER tokens
        seguintes (cobre flags após nomes de arquivo). `rm` sem a combinação
        (ex.: `rm arquivo.txt`, `rm -f x`, `rm -r dir`) NÃO bloqueia — não é
        o apagamento recursivo forçado.

        Retorna a mensagem de bloqueio, ou "" se não é um `rm` destrutivo."""
        tokens = Executor._flatten_shell(Executor._tokenize(command))
        if not Executor._rm_em_posicao_de_comando(tokens):
            return ""
        for idx, tok in enumerate(tokens):
            if tok.lower() != "rm":
                continue
            tem_r = False  # recursivo (-r/-R/--recursive)
            tem_f = False  # força (-f/--force)
            for arg in tokens[idx + 1:]:
                low = arg.lower()
                if low.startswith("--"):
                    if low == "--recursive":
                        tem_r = True
                    elif low == "--force":
                        tem_f = True
                    continue
                if low.startswith("-") and len(low) > 1 \
                        and not low[1].isdigit():
                    if "r" in low[1:]:
                        tem_r = True
                    if "f" in low[1:]:
                        tem_f = True
            if tem_r and tem_f:
                return (
                    "bloqueado: rm destrutivo (recursivo -r/-R + força -f) "
                    "em qualquer ordem/flags"
                )
        return ""

    @staticmethod
    def check_policy(command: str) -> str:
        """Aplica as políticas de bloqueio de comandos.

        ORDEM IMPORTANTE (Follow-up do Item 2 — HITL, sem bloquear essenciais):
          0. `rm` DESTRUTIVO por FLAGS (`_rm_destrutivo`, F1): cobre `rm` em
             posição de COMANDO (primeiro token ou após separador `&&`/`;`/`|`)
             + recursivo (-r/-R) + força (-f) coladas ou separadas, em
             qualquer ordem/caixa (ex.: `rm -r -f x`, `rm -f -R x`,
             `rm -rF`). `rm` como ARGUMENTO (ex.: `echo rm -rf x`) NÃO
             bloqueia (falso positivo do echo eliminado). Os padrões de
             string abaixo cobrem as formas coladas comuns (`rm -rf`,
             `rm -fr`) só quando `rm` está em posição de comando.
          1. Padrões EXPLICITAMENTE DESTRUTIVOS (`config.BLOCKED_PATTERNS`):
             bloqueiam SEMPRE. A safelist de essenciais NUNCA prevalece aqui
             (ex.: `rm -rf`, `git reset --hard`, `shutdown`).
          2. Padrões de REFORÇO CONSERVADOR (`config.CONSERVATIVE_BLOCK_PATTERNS`,
             ex.: `del <arquivo>`, `curl ... | sh`): só bloqueiam se o comando
             NÃO for essencial. Comando essencial (ex.: `python script.py`,
             `curl url` sozinho) NÃO bloqueia; não-essencial casando o padrão
             BLOQUEIA.
        Retorna "" se permitido, ou a mensagem de bloqueio.
        """
        # 0) rm destrutivo por flags (antes do loop de padrões — F1)
        motivo_rm = Executor._rm_destrutivo(command)
        if motivo_rm:
            return motivo_rm
        lower = command.lower()
        # 1) destrutivos explícitos: bloqueia SEMPRE (essencial não prevalece).
        #    Exceção posicional (F1 refinado): os padrões de string `rm -rf` /
        #    `rm -fr` só bloqueiam quando `rm` está em posição de COMANDO —
        #    como ARGUMENTO (ex.: `echo rm -rf x`) é texto impresso, não um
        #    comando rm (falso positivo eliminado).
        tokens_rm = None
        for pattern in config.BLOCKED_PATTERNS:
            if pattern not in lower:
                continue
            if pattern in ("rm -rf", "rm -fr"):
                if tokens_rm is None:
                    tokens_rm = Executor._flatten_shell(Executor._tokenize(command))
                if not Executor._rm_em_posicao_de_comando(tokens_rm):
                    continue
            return f"bloqueado: {pattern}"
        # 2) reforço conservador: essencial prevalece
        for pattern in config.CONSERVATIVE_BLOCK_PATTERNS:
            if pattern in lower:
                if Executor._is_essential(command):
                    return ""
                return f"bloqueado: {pattern}"
        return ""

    @staticmethod
    def _is_essential(command: str) -> bool:
        """True APENAS se o comando for essencial "PURO": o prefixo essencial
        (`config.ESSENTIAL_COMMANDS`) + argumentos seguros, SEM nenhum
        separador de encadeamento (`&`, `&&`, `;`, `|`, `||`) nem
        redirecionamento (`<`, `>`), nem quebra de linha. Reusa a mesma
        disciplina do `_no_safelist_risco_baixo` do pipeline (shlex.split +
        rejeição de separadores), para que `git status && del file.txt`,
        `python script.py; del file.txt` e `dir > out.txt` NÃO sejam tratados
        como essenciais (o `&&`/`;` após o prefixo encadearia um comando
        destrutivo que o `/api/exec` executaria de fato).

        Um comando que faz PIPE para um interpretador (`| sh`, `| bash`,
        `| zsh`, `| python`) NUNCA é essencial — garante que `curl url | sh`
        bloqueie mesmo com `curl` na safelist."""
        cru = command.lstrip()
        if not cru:
            return False
        # 1) separador de shell / quebra de linha na string crua => NÃO
        #    essencial (defesa antes da tokenização: shlex trataria `\n` como
        #    espaço e o separador sumiria dos tokens).
        if any(ch in cru for ch in _SEPARADORES_SHELL):
            return False
        if "\n" in cru or "\r" in cru:
            return False
        # 2) tokeniza; aspas desbalanceadas => não essencial
        try:
            tokens = shlex.split(cru)
        except (ValueError, OSError):
            return False
        if not tokens:
            return False
        # 3) nenhum token pode conter separador de shell (cobre `git status;`
        #    com o `;` colado, `|` isolado e separador dentro de aspas —
        #    conservador).
        if any(ch in token for token in tokens for ch in _SEPARADORES_SHELL):
            return False
        # 4) pipe para interpretador => nunca essencial (defesa contra curl|sh)
        low = command.lower()
        if any(p in low for p in config.CONSERVATIVE_PIPE_TO_SHELL):
            return False
        # 5) match por TOKEN inicial do prefixo essencial (multi-token: `git
        #    commit`, `python -m`), com argumentos adicionais seguros.
        tokens_low = [t.lower() for t in tokens]
        for prefix in config.ESSENTIAL_COMMANDS:
            ptokens = [p.lower() for p in prefix.split()]
            if len(tokens_low) >= len(ptokens) \
                    and tokens_low[: len(ptokens)] == ptokens:
                return True
        return False

    # --------------------------------------------------------------- história
    def _load_history(self) -> None:
        self._history: list[dict] = []
        try:
            if config.HISTORY_FILE.exists():
                self._history = json.loads(config.HISTORY_FILE.read_text(encoding="utf-8"))
        except (OSError, json.JSONDecodeError):
            self._history = []

    def _record(self, command: str, status: str, detail: str) -> None:
        with self._hist_lock:
            self._history.append(
                {
                    "command": command,
                    "status": status,
                    "detail": detail,
                }
            )
            if len(self._history) > 500:
                self._history = self._history[-500:]
            try:
                # F5: o diretório logs/ pode não existir (primeira execução) —
                # sem o mkdir, o write_text levantava OSError e o histórico
                # NUNCA persistia (except OSError silencioso abaixo).
                config.HISTORY_FILE.parent.mkdir(parents=True, exist_ok=True)
                config.HISTORY_FILE.write_text(
                    json.dumps(self._history, ensure_ascii=False, indent=2),
                    encoding="utf-8",
                )
            except OSError:
                pass

    def history(self) -> list[dict]:
        with self._hist_lock:
            return list(self._history)

    def clear_history(self) -> None:
        """Apaga o histórico persistido (Ch19: reinício limpo de avaliação)."""
        with self._hist_lock:
            self._history = []
            try:
                config.HISTORY_FILE.unlink(missing_ok=True)
            except OSError:
                pass


def _matar_arvore_sandbox(proc: subprocess.Popen) -> None:
    """Mata o processo do container após timeout (Item 4).

    Mesmo padrão do `_matar_arvore` do motor: no Windows `taskkill /T /F`
    ANTES do `terminate` (derruba a árvore inteira e fecha o pipe); o
    terminate subsequente é defesa (no-op se a árvore já caiu). Em POSIX o
    terminate é suficiente. Nada aqui levanta exceção."""
    if proc.poll() is not None:
        return
    if os.name == "nt":
        try:
            subprocess.run(
                ["taskkill", "/T", "/F", "/PID", str(proc.pid)],
                capture_output=True,
            )
        except (OSError, subprocess.SubprocessError):
            pass  # terminate é o fallback
    try:
        proc.terminate()
    except (OSError, subprocess.SubprocessError):
        pass