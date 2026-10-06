"""SandboxRunner — execução isolada de código com políticas (Etapa 1).

Modos: "docker" (SDK docker opcional) -> "podman" (CLI opcional) -> "exec"
(safe mode: subprocess com shell=False). O modo exec roda o código em um
script temporário dentro do workdir do projeto, com timeout via thread +
kill (no Windows: ``taskkill /T /F`` ANTES do ``terminate`` — ordem
crítica). Retorna exit_code REAL (não fabricado).

Zero-poisoning (regra aplicada no engine, não aqui): nenhum código com
exit != 0 é persistido.
"""

from __future__ import annotations

import logging
import os
import pathlib
import shutil
import subprocess
import sys
import tempfile
import threading
import time
import uuid

try:  # importação como pacote (preferida)
    from . import config
    from .models import ExecutionResult
except ImportError:  # execução direta (script)
    import config
    from models import ExecutionResult

logger = logging.getLogger(__name__)

# Padrões destrutivos/perigosos — o mesmo conceito de BLOCKED_PATTERNS do
# harness (harness/config.py), portado localmente para independência.
# PARIDADE TESTADA (F13): tests/security_test.py compara esta lista com
# config.BLOCKED_PATTERNS (como conjuntos) e FALHA se divergirem — se editar
# uma, edite a outra.
BLOCKED_PATTERNS = [
    "rm -rf",
    "rm -fr",
    "git reset --hard",
    "git clean -fdx",
    "git clean -fd",
    "shutdown",
    "reboot",
    "format c:",
    "mkfs",
    "dd if=",
    "> /dev/sda",
    "rmdir /s",
    "rd /s",
    "rd /q",
    "del /f /s /q",
    "erase /s",
    "erase /q",
    "rd /s /q",
]


class SandboxRunner:
    """Runner de código com política de bloqueio e timeout real.

    ``force_mode`` permite forçar um modo ("docker"|"podman"|"exec") — útil
    em testes e operação; sem ele, a resolução automática é:
    docker SDK -> podman CLI -> exec.
    """

    def __init__(
        self,
        workdir: str | pathlib.Path | None = None,
        timeout: float | None = None,
        force_mode: str | None = None,
    ):
        self.workdir = pathlib.Path(workdir) if workdir else config.DATA_DIR / "sandbox"
        self.workdir.mkdir(parents=True, exist_ok=True)
        self.timeout = float(timeout) if timeout is not None else config.TIMEOUT_SANDBOX
        self._mode = self._resolver_modo(force_mode)

    # ------------------------------------------------------------- modo
    def _resolver_modo(self, force_mode: str | None) -> str:
        if force_mode in ("docker", "podman", "exec"):
            return force_mode
        # docker: só escolhe se o DAEMON responde (não basta o SDK instalado —
        # SDK presente + daemon caído fazia TODA execução falhar sem failover;
        # mesma disciplina do `executor._docker_disponivel` do harness).
        if self._docker_daemon_ativo():
            return "docker"
        if shutil.which("podman"):
            return "podman"
        return "exec"

    @staticmethod
    def _docker_daemon_ativo() -> bool:
        """True se o daemon docker responde ao `ping` (com timeout curto).
        Nunca crasha: qualquer falha (SDK ausente, daemon caído, timeout) ->
        False."""
        try:
            import docker  # noqa: F401 — SDK opcional
            client = docker.from_env(timeout=2)
            client.ping()
            return True
        except Exception:  # noqa: BLE001 — daemon caído/timeout => inativo
            return False

    @property
    def mode(self) -> str:
        return self._mode

    # ------------------------------------------------------------- API
    def run(self, code: str, timeout: float | None = None) -> ExecutionResult:
        """Executa o código com timeout e retorna ExecutionResult com o
        exit_code real. Código com padrões bloqueados não é executado
        (exit_code 1 + error). Nunca crasha."""
        inicio = time.perf_counter()
        limite = float(timeout) if timeout is not None else self.timeout

        if code is None:
            code = ""
        if not isinstance(code, str):
            code = str(code)

        motivo = self._check_policy(code)
        if motivo:
            return ExecutionResult(
                success=False,
                executed_locally=True,
                execution_time_ms=self._ms(inicio),
                error=f"bloqueado pela política: {motivo}",
                exit_code=1,
            )

        try:
            if self._mode == "docker":
                return self._run_docker(code, limite, inicio)
            if self._mode == "podman":
                return self._run_podman(code, limite, inicio)
            return self._run_exec(code, limite, inicio)
        except Exception as exc:  # noqa: BLE001 — nunca crashar
            return ExecutionResult(
                success=False,
                executed_locally=True,
                execution_time_ms=self._ms(inicio),
                error=f"falha na execução: {exc}",
                exit_code=1,
            )

    # ------------------------------------------------------------- políticas
    @staticmethod
    def _check_policy(code: str) -> str:
        lower = code.lower()
        for padrao in BLOCKED_PATTERNS:
            if padrao in lower:
                return padrao
        return ""

    # ------------------------------------------------------------- exec
    def _run_exec(self, code: str, limite: float, inicio: float) -> ExecutionResult:
        """Safe mode "exec": subprocess com shell=False, script temporário no
        workdir do projeto (data/sandbox/), Python em modo isolado.

        Reforço defensivo (achado A2 da revisão): o subprocess roda com
        ``-I`` (Python isolated mode — ignora PYTHONPATH, user site-packages
        e variáveis de ambiente herdadas) e com ``env`` mínimo (apenas PATH),
        mantendo taskkill ANTES do terminate (árvore inteira), cwd no
        data/sandbox/ do projeto e decode utf-8 com errors=replace.

        IMPORTANTE (risco residual declarado): o modo "exec" é o safe mode do
        harness, mas NÃO oferece isolamento de host — não atende o isolamento
        da spec. Para conformidade operacional, docker/podman são o
        pré-requisito (ver README).
        """
        script = self.workdir / f"ahs_{uuid.uuid4().hex}.py"
        try:
            script.write_text(code, encoding="utf-8")
            proc = subprocess.Popen(
                [sys.executable, "-I", str(script)],
                cwd=str(self.workdir),
                env={"PATH": os.environ.get("PATH", "")},
                stdout=subprocess.PIPE,
                stderr=subprocess.PIPE,
                shell=False,
            )
            timer = threading.Timer(limite, _matar_arvore, args=(proc,))
            timer.daemon = True
            timer.start()
            try:
                stdout_b, stderr_b = proc.communicate()
            finally:
                timer.cancel()
            stdout = stdout_b.decode("utf-8", errors="replace")
            stderr = stderr_b.decode("utf-8", errors="replace")
            exit_code = proc.returncode  # real (não fabricado)
            return ExecutionResult(
                success=exit_code == 0,
                output=(stdout + stderr).strip(),
                executed_locally=True,
                execution_time_ms=self._ms(inicio),
                error=None if exit_code == 0 else f"exit {exit_code}",
                exit_code=exit_code,
            )
        finally:
            try:
                script.unlink(missing_ok=True)
            except OSError:
                pass

    # ------------------------------------------------------------- docker
    def _run_docker(self, code: str, limite: float, inicio: float) -> ExecutionResult:
        """Modo docker via SDK (opcional). Falhas viram ExecutionResult."""
        import docker  # import local: dependência opcional

        client = docker.from_env(timeout=2)
        container = client.containers.run(
            image="python:3-slim",
            command=["python", "-c", code],
            detach=True,
            remove=False,  # remove=True + logs() causava race 404 (NotFound)
            stdout=True,
            stderr=True,
            # F2: isolamento de rede — o container do motor não acessa a rede
            # do host (mesma disciplina do executor com `--network none`).
            network_mode="none",
        )
        try:
            resultado = container.wait(timeout=limite)
            try:
                logs = container.logs(stdout=True, stderr=True).decode(
                    "utf-8", errors="replace"
                )
            except Exception:  # noqa: BLE001
                # container pode já ter sido removido (corrida) — logs vazios
                logs = ""
            exit_code = int(resultado.get("StatusCode", 1))
            return ExecutionResult(
                success=exit_code == 0,
                output=logs.strip(),
                executed_locally=True,
                execution_time_ms=self._ms(inicio),
                error=None if exit_code == 0 else f"exit {exit_code}",
                exit_code=exit_code,
            )
        finally:
            try:
                container.remove(force=True)
            except Exception:  # noqa: BLE001
                pass

    # ------------------------------------------------------------- podman
    def _run_podman(self, code: str, limite: float, inicio: float) -> ExecutionResult:
        """Modo podman via CLI (opcional). Container nomeado para limpeza de
        órfão no timeout (matar o CLI não remove o container no daemon)."""
        nome = f"ahs_{uuid.uuid4().hex[:12]}"
        proc = subprocess.Popen(
            # F2: `--network none` — isolamento de rede (o container do motor
            # não acessa a rede do host).
            ["podman", "run", "--rm", "--name", nome, "--network", "none",
             "python:3-slim", "python", "-c", code],
            stdout=subprocess.PIPE,
            stderr=subprocess.PIPE,
            shell=False,
        )
        timer = threading.Timer(limite, _matar_arvore, args=(proc,))
        timer.daemon = True
        timer.start()
        try:
            stdout_b, stderr_b = proc.communicate()
        finally:
            timer.cancel()
        # best-effort: se o CLI foi morto no timeout, remove o órfão pelo nome
        try:
            subprocess.run(
                ["podman", "rm", "-f", nome],
                capture_output=True,
                timeout=5,
            )
        except Exception:  # noqa: BLE001 — limpeza é best-effort
            pass
        stdout = stdout_b.decode("utf-8", errors="replace")
        stderr = stderr_b.decode("utf-8", errors="replace")
        exit_code = proc.returncode
        return ExecutionResult(
            success=exit_code == 0,
            output=(stdout + stderr).strip(),
            executed_locally=True,
            execution_time_ms=self._ms(inicio),
            error=None if exit_code == 0 else f"exit {exit_code}",
            exit_code=exit_code,
        )

    # ------------------------------------------------------------- helpers
    @staticmethod
    def _ms(inicio: float) -> float:
        return round((time.perf_counter() - inicio) * 1000, 3)


def _matar_arvore(proc: subprocess.Popen) -> None:
    """Mata o processo após timeout.

    No Windows: ``taskkill /T /F`` ANTES do ``terminate`` (ordem crítica) —
    derruba a árvore inteira e fecha o pipe; o terminate subsequente é
    defesa (no-op se a árvore já caiu). Em POSIX o terminate é suficiente.
    """
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