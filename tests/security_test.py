"""Testes da superfície de segurança do harness (B1/B4/B2/A1/A2 + HEAD/OPTIONS).

Cobre:
  - B1: HARNESS_PUBLIC=1 + modo de execução real host -> job BLOCKED (sem
    Popen no host). Cenários do reviewer: (1) Docker disponível + defaults
    (EXEC_SANDBOX_ENABLED/ALTO_ONLY=False, usa_sandbox=None); (2)
    EXEC_SANDBOX_ENABLED=1 + container falha (_run_sandbox -> None). E modo
    NÃO público mantém o fallback host (comportamento antigo). run_many passa
    pelo _worker (sem bypass).
  - B4: HARNESS_PUBLIC=1 + host 0.0.0.0 -> SystemExit (HTTPS obrigatório via
    Tailscale — bind de HTTP puro bloqueado). Público + senha de
    fábrica/padrão (AUTH_MODE "padrao" opencode/opencode, ou "env" com
    HARNESS_PASSWORD igual à de fábrica) RECUSA subir (SystemExit) — expor o
    web shell com senha de fábrica em deploy público é inaceitável; senha
    personalizada (AUTH_MODE "hash", python -m harness.auth set) exigida. NÃO
    público + 0.0.0.0 continua permitido (dev local).
  - B2: rate-limit por IP (5 falhas -> bloqueado; janela de expiração; TTL
    remove IPs inativos — memory leak).
  - A1: /api/health público sem dados internos (sem root).
  - A2: headers de hardening HTTP presentes em todas as respostas.
  - HEAD/OPTIONS exigem autenticação nas rotas /api/* (sem credenciais -> 401).
  - F1: `rm` destrutivo por FLAGS (coladas/separadas, qualquer ordem/caixa)
    bloqueado; `rm` sem a combinação recursivo+força permitido. Refinado:
    `rm` como ARGUMENTO de outro comando (ex.: `echo rm -rf x`) NÃO bloqueia
    (não é um comando rm — falso positivo do echo eliminado).
  - F3: rate-limit por IDENTIDADE em modo público (Tailscale-User-Login);
    em modo local a chave é o IP (headers Tailscale ignorados).
  - F7: sys_version vazia (header Server sem "Python/x.y.z").
  - F11: /api/status e /api/sideprjs sem caminho absoluto no retorno.
  - F13: paridade BLOCKED_PATTERNS entre harness/config.py e motor/sandbox.py.
  - F16: corpo JSON inválido -> 400 (não vira comando vazio); corpo vazio
    legítimo continua {} (POSTs legítimos não quebram).

Padrão do executor_test: unittest.mock + asserts manuais com contador
[PASS]/[FAIL].

Rode com:
    python tests/security_test.py
"""

from __future__ import annotations

import io
import json
import pathlib
import sys
import time
import unittest.mock as mock

sys.path.insert(0, str(pathlib.Path(__file__).resolve().parent.parent))

from harness import config  # noqa: E402
from harness import executor as executor_mod  # noqa: E402
from harness.executor import Executor  # noqa: E402
from harness.server import HarnessServer  # noqa: E402


class _FakeProc:
    """Processo fake para a execução no HOST (usa `stdout` + `wait`)."""

    def __init__(self, out="", rc=0):
        self.returncode = rc
        self._out = out
        self.stdout = [line if line.endswith("\n") else line + "\n"
                       for line in out.splitlines(keepends=False)]

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


class _Wfile:
    """Captura o corpo escrito pelo handler (como wfile de socket)."""

    def __init__(self):
        self.data = b""

    def write(self, b):
        self.data += b


def _handler_fake(Handler, path, headers=None, ip="127.0.0.1"):
    """Instância da classe Handler (criada UMA vez em main_test — os dicts de
    classe `_falhas`/`_bloqueios` são compartilhados pela MESMA classe)."""
    h = Handler.__new__(Handler)
    h.path = path
    h.headers = headers if headers is not None else {}
    h.client_address = (ip, 4321)
    h.send_response = lambda code: setattr(h, "_resp_code", code)
    h.send_header = lambda k, v: None
    h.end_headers = lambda: None
    h.wfile = _Wfile()
    return h


def main_test() -> int:
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
    # UMA única classe Handler para toda a suíte (os dicts de rate-limit são
    # atributos de CLASSE — criar _make_handler() por fake os duplicaria).
    Handler = HarnessServer()._make_handler()

    # =============================================================== B1
    print("== B1: modo publico bloqueia execucao no host (modo REAL) ==")
    # Cenário 1 do reviewer: Docker DISPONÍVEL + HARNESS_PUBLIC=1 + defaults
    # (EXEC_SANDBOX_ENABLED=False, ALTO_ONLY=False, usa_sandbox=None) -> o modo
    # de execução real é host -> BLOCKED (antes rodava no host: bypass).
    no_popen = mock.patch.object(
        executor_mod.subprocess, "Popen",
        side_effect=AssertionError("Popen não deveria ser chamado (blocked)"))
    with mock.patch.object(config, "HARNESS_PUBLIC", True), \
            mock.patch.object(config, "PUBLIC_REQUIRE_SANDBOX", True), \
            mock.patch.object(config, "EXEC_SANDBOX_ENABLED", False), \
            mock.patch.object(config, "EXEC_SANDBOX_ALTO_ONLY", False), \
            mock.patch.object(Executor, "_docker_disponivel", return_value=True), \
            no_popen:
        check("B1 público+defaults -> _sandbox_modo 'blocked'",
              ex._sandbox_modo() == "blocked")
        job = ex.run("python --version")
        snap = _wait(job)
        check("B1 público+defaults -> job blocked (não roda no host)",
              snap is not None
              and snap["status"] == "blocked"
              and "sandbox Docker/Podman" in snap["reason"]
              and snap["sandbox"] is False
              and snap["exit_code"] is None)

    # usa_sandbox=False (baixo/médio) força host -> também blocked em público
    with mock.patch.object(config, "HARNESS_PUBLIC", True), \
            mock.patch.object(config, "PUBLIC_REQUIRE_SANDBOX", True), \
            mock.patch.object(config, "EXEC_SANDBOX_ENABLED", False), \
            mock.patch.object(Executor, "_docker_disponivel", return_value=True):
        check("B1 público+usa_sandbox=False -> blocked (host forçado)",
              ex._sandbox_modo(usa_sandbox=False) == "blocked")

    # Cenário 2 do reviewer: EXEC_SANDBOX_ENABLED=1 + container falha
    # (_run_sandbox -> None) + público -> BLOCKED (não cai para host)
    with mock.patch.object(config, "HARNESS_PUBLIC", True), \
            mock.patch.object(config, "PUBLIC_REQUIRE_SANDBOX", True), \
            mock.patch.object(config, "EXEC_SANDBOX_ENABLED", True), \
            mock.patch.object(config, "EXEC_SANDBOX_BACKEND", "docker"), \
            mock.patch.object(Executor, "_docker_disponivel", return_value=True), \
            mock.patch.object(Executor, "_run_sandbox", return_value=None), \
            no_popen:
        check("B1 público+container falha -> _sandbox_modo 'docker' (tentou)",
              ex._sandbox_modo() == "docker")
        job = ex.run("python --version")
        snap = _wait(job)
        check("B1 público+container falha -> job blocked (sem fallback host)",
              snap is not None
              and snap["status"] == "blocked"
              and "sandbox Docker/Podman" in snap["reason"])

    # run_many passa pelo _worker (sem bypass)
    with mock.patch.object(config, "HARNESS_PUBLIC", True), \
            mock.patch.object(config, "PUBLIC_REQUIRE_SANDBOX", True), \
            mock.patch.object(config, "EXEC_SANDBOX_ENABLED", False), \
            mock.patch.object(Executor, "_docker_disponivel", return_value=True), \
            no_popen:
        jobs = ex.run_many(["echo a", "echo b"])
        snaps = [_wait(j) for j in jobs]
        check("B1 público+run_many -> todos blocked (sem bypass)",
              all(s is not None and s["status"] == "blocked" for s in snaps))

    # Cenário 3: modo NÃO público mantém o comportamento antigo — container
    # falha -> fallback host roda o comando (finished, sandbox False)
    def _popen_host_fallback(cmd, **kwargs):
        if kwargs.get("shell") is not True:
            raise AssertionError("container Popen não deveria ser chamado "
                                 "no fallback host (não público)")
        return _FakeProc(out="", rc=0)

    with mock.patch.object(config, "HARNESS_PUBLIC", False), \
            mock.patch.object(config, "EXEC_SANDBOX_ENABLED", True), \
            mock.patch.object(config, "EXEC_SANDBOX_BACKEND", "docker"), \
            mock.patch.object(Executor, "_docker_disponivel", return_value=True), \
            mock.patch.object(Executor, "_run_sandbox", return_value=None), \
            mock.patch.object(executor_mod.subprocess, "Popen",
                              side_effect=_popen_host_fallback):
        job = ex.run("python --version")
        snap = _wait(job)
        check("B1 NÃO público+container falha -> fallback host (antigo)",
              snap is not None
              and snap["status"] == "finished"
              and snap["sandbox"] is False
              and snap["exit_code"] == 0)

    # =============================================================== B4
    print("== B4: modo publico exige HTTPS (127.0.0.1) e senha personalizada ==")
    with mock.patch.object(config, "HARNESS_PUBLIC", True), \
            mock.patch.object(config, "AUTH_MODE", "hash"):
        try:
            HarnessServer(host="0.0.0.0")
            check("B4 público+0.0.0.0 -> SystemExit", False)
        except SystemExit:
            check("B4 público+0.0.0.0 -> SystemExit", True)
    # B4 ENDURECIDO: público + senha padrão NÃO pode subir — recusa com
    # SystemExit (expor o web shell com senha de fábrica em deploy público
    # era inaceitável; antes apenas avisava).
    with mock.patch.object(config, "HARNESS_PUBLIC", True), \
            mock.patch.object(config, "AUTH_MODE", "padrao"):
        try:
            HarnessServer(host="127.0.0.1")
            check("B4 público+senha padrão -> SystemExit (recusa subir)", False)
        except SystemExit:
            check("B4 público+senha padrão -> SystemExit (recusa subir)", True)
    # env com HARNESS_PASSWORD == senha de fábrica também recusa
    with mock.patch.object(config, "HARNESS_PUBLIC", True), \
            mock.patch.object(config, "AUTH_MODE", "env"), \
            mock.patch.object(config, "AUTH_PASSWORD", "opencode"):
        try:
            HarnessServer(host="127.0.0.1")
            check("B4 público+env senha=fábrica -> SystemExit", False)
        except SystemExit:
            check("B4 público+env senha=fábrica -> SystemExit", True)
    # público + AUTH_MODE="hash" (senha personalizada) sobe normalmente
    with mock.patch.object(config, "HARNESS_PUBLIC", True), \
            mock.patch.object(config, "AUTH_MODE", "hash"):
        try:
            HarnessServer(host="127.0.0.1")
            check("B4 público+senha personalizada (hash) -> sobe", True)
        except SystemExit:
            check("B4 público+senha personalizada (hash) -> sobe", False)
    # modo NÃO público com 0.0.0.0 continua permitido (dev local)
    with mock.patch.object(config, "HARNESS_PUBLIC", False):
        try:
            HarnessServer(host="0.0.0.0")
            check("B4 não público+0.0.0.0 -> permitido (dev)", True)
        except SystemExit:
            check("B4 não público+0.0.0.0 -> permitido (dev)", False)

    # =============================================================== B2
    print("== B2: rate-limit por IP (falhas, janela, TTL) ==")
    # zera os dicts de classe para o teste ficar determinístico
    Handler._falhas.clear()
    Handler._bloqueios.clear()
    h = _handler_fake(Handler, "/api/status")
    # 5 falhas dentro da janela -> bloqueado
    for _ in range(5):
        h._registra_falha("203.0.113.9")
    check("B2 5 falhas -> rate-limit nega", h._rate_limit_ok("203.0.113.9") is False)
    check("B2 5 falhas -> IP marcado em _bloqueios",
          "203.0.113.9" in Handler._bloqueios)
    # com 4 falhas ainda libera
    Handler._falhas.clear()
    Handler._bloqueios.clear()
    h2 = _handler_fake(Handler, "/api/status")
    for _ in range(4):
        h2._registra_falha("198.51.100.7")
    check("B2 4 falhas -> ainda libera", h2._rate_limit_ok("198.51.100.7") is True)
    # janela: falhas antigas (fora de JANELA_FALHAS) expiram e liberam
    Handler._falhas["198.51.100.7"] = [time.time() - 120.0] * 5
    Handler._bloqueios.clear()
    check("B2 falhas fora da janela -> libera",
          h2._rate_limit_ok("198.51.100.7") is True
          and "198.51.100.7" not in Handler._bloqueios)
    # TTL (memory leak): IP com último evento há > _TTL_FALHAS é expurgado
    Handler._falhas["192.0.2.55"] = [time.time() - 700.0]
    Handler._bloqueios["192.0.2.55"] = time.time() - 700.0
    h3 = _handler_fake(Handler, "/api/status")
    h3._rate_limit_ok("192.0.2.55")
    check("B2 TTL remove IPs inativos dos dicts",
          "192.0.2.55" not in Handler._falhas
          and "192.0.2.55" not in Handler._bloqueios)
    # IP recente NÃO é expurgado (dentro do TTL)
    Handler._falhas.clear()
    Handler._bloqueios.clear()
    Handler._falhas["203.0.113.77"] = [time.time() - 10.0]
    h4 = _handler_fake(Handler, "/api/status")
    h4._rate_limit_ok("203.0.113.77")
    check("B2 TTL preserva IP recente", "203.0.113.77" in Handler._falhas)
    Handler._falhas.clear()
    Handler._bloqueios.clear()
    Handler._exec_reqs.clear()

    # =============================================================== EXEC-RL
    print("== exec rate-limit: /api/exec por identidade ==")
    hx = _handler_fake(Handler, "/api/exec")
    identidade = "203.0.113.50"
    # dentro do limite: posts são aceitos
    aceitos = 0
    for _ in range(config.MAX_EXEC_PER_WINDOW):
        if hx._exec_rate_limit_ok(identidade):
            aceitos += 1
    check("exec-rl: aceita exatamente o teto da janela",
          aceitos == config.MAX_EXEC_PER_WINDOW)
    # acima do teto: nega (429)
    check("exec-rl: acima do teto nega", hx._exec_rate_limit_ok(identidade) is False)
    # identidade DIFERENTE não é afetada (chave por identidade)
    check("exec-rl: outra identidade liberada",
          hx._exec_rate_limit_ok("198.51.100.9") is True)
    # TTL: entradas antigas expiram e liberam a chave
    Handler._exec_reqs[identidade] = [time.time() - 500.0]
    check("exec-rl: TTL expira e libera", hx._exec_rate_limit_ok(identidade) is True)
    Handler._exec_reqs.clear()

    # =============================================================== A1
    print("== A1: /api/health público sem dados internos ==")
    h5 = _handler_fake(Handler, "/api/health")
    h5.do_GET()
    body = json.loads(h5.wfile.data.decode("utf-8"))
    check("A1 health 200 + sem root/dados internos",
          h5._resp_code == 200
          and body.get("ok") is True
          and body.get("name") == "harness"
          and "root" not in body
          and "ROOT" not in body
          and "history" not in body)
    check("A1 health NÃO exige auth (público)",
          h5._resp_code == 200)

    # =============================================================== A2
    print("== A2: headers de hardening HTTP em todas as respostas ==")
    h6 = _handler_fake(Handler, "/api/status")
    hdrs = []
    h6.send_response = lambda code: hdrs.append(("RESP", code))
    h6.send_header = lambda k, v: hdrs.append((k, v))
    h6._send(200, b"{}")
    nomes = {k for k, _ in hdrs}
    check("A2 headers de hardening presentes",
          {"X-Content-Type-Options", "Referrer-Policy",
           "X-Frame-Options", "Cache-Control"} <= nomes)
    check("A2 nosniff/deny/no-referrer com valores corretos",
          ("X-Content-Type-Options", "nosniff") in hdrs
          and ("X-Frame-Options", "DENY") in hdrs
          and ("Referrer-Policy", "no-referrer") in hdrs)
    check("A2 server_version sem versão (HarnessLocal)",
          Handler.server_version == "HarnessLocal")
    check("A2/F7 sys_version vazia (header Server sem Python/x.y.z)",
          Handler.sys_version == "")

    # ====================================================== HEAD/OPTIONS
    print("== HEAD/OPTIONS exigem auth nas rotas /api/* (achado baixo) ==")
    Handler._falhas.clear()
    Handler._bloqueios.clear()
    h7 = _handler_fake(Handler, "/api/status")
    h7.do_HEAD()
    check("HEAD /api/status sem auth -> 401", h7._resp_code == 401)
    check("HEAD sem auth não escreve corpo (401 sem body)",
          h7.wfile.data == b"")
    h8 = _handler_fake(Handler, "/api/status")
    h8.do_OPTIONS()
    check("OPTIONS /api/status sem auth -> 401", h8._resp_code == 401)
    h9 = _handler_fake(Handler, "/api/health")
    h9.do_OPTIONS()
    check("OPTIONS /api/health sem auth -> 204 (rota pública)",
          h9._resp_code == 204)
    h10 = _handler_fake(Handler, "/api/health")
    h10.do_HEAD()
    check("HEAD /api/health -> 200 sem body",
          h10._resp_code == 200 and h10.wfile.data == b"")

    # ============================================================== F1
    print("== F1: rm destrutivo por FLAGS (coladas/separadas, qualquer ordem) ==")
    for cmd in (
        "rm -r -f /tmp/x",   # espaçadas
        "rm -R -f x",        # -R maiúsculo
        "rm -f -r x",        # ordem invertida
        "rm -f -R x",        # ordem invertida + -R
        "rm -rf x",          # coladas (já coberto pelos padrões)
        "rm -fr x",          # coladas ordem invertida
        "rm -rF x",          # cluster com caixa mista
        "rm -Rf x",          # cluster com -R
        "rm -r --force x",   # long flag --force
        "rm --recursive -f x",  # long flag --recursive
    ):
        check(f"F1 {cmd!r} -> BLOCKED", bool(Executor.check_policy(cmd)))
    # rm NÃO-destrutivo (sem recursivo+força) continua permitido
    check("F1 'rm arquivo.txt' -> permitido (rm não-flag)",
          Executor.check_policy("rm arquivo.txt") == "")
    check("F1 'rm -f arquivo.txt' -> permitido (sem recursivo)",
          Executor.check_policy("rm -f arquivo.txt") == "")
    check("F1 'rm -r dir' -> permitido (sem força)",
          Executor.check_policy("rm -r dir") == "")
    # F1 refinado (Follow-up): `rm` como ARGUMENTO de outro comando (echo)
    # NÃO é um comando rm — não bloqueia (falso positivo eliminado). Só `rm`
    # em posição de comando (primeiro token) bloqueia.
    check("F1 'echo rm -rf x' -> permitido (rm como argumento)",
          Executor.check_policy("echo rm -rf x") == "")
    check("F1 'echo rm -r -f x' -> permitido (rm como argumento)",
          Executor.check_policy("echo rm -r -f x") == "")
    check("F1 'echo \"rm -rf\" x' -> permitido (rm entre aspas)",
          Executor.check_policy('echo "rm -rf" x') == "")
    # encadeado COM separador continua bloqueado (rm executa de verdade)
    check("F1 'git status && rm -rf x' -> BLOCKED (encadeado)",
          bool(Executor.check_policy("git status && rm -rf x")))
    check("F1 'pytest; rm -r -f x' -> BLOCKED (encadeado)",
          bool(Executor.check_policy("pytest; rm -r -f x")))
    # F1 hardening: `rm` destrutivo DENTRO de strings de comando de
    # interpretadores/runner também bloqueia (antes era bypass real — o `rm`
    # ficava fora de "posição de comando").
    for cmd in (
        "sh -c 'rm -rf x'",      # quoted: string é UM token
        "sh -c rm -rf x",        # unquoted: rm após flag -c
        "cmd /c rm -rf x",       # cmd.exe
        'cmd /c "rm -fr /x"',    # cmd.exe quoted
        "wsl rm -rf x",          # runner wsl
        "git bash -c 'rm -rf x'",# git bash
        "bash -c rm -r -f /x",   # flags separadas
        "sudo rm -rf x",         # runner sudo
        "xargs rm -rf x",        # runner xargs
        "sudo -u root rm -rf x", # runner com argumento
        "env -i rm -rf x",       # runner env com flag
        'env -S "rm -rf x"',     # env -S (string de comando)
        "timeout 5 rm -rf x",    # timeout com argumento
        'su -c "rm -rf x"',      # su -c
        "nice -n 10 rm -rf x",   # nice com argumento
        "powershell.exe -Command 'rm -rf x'",  # .exe + flag PowerShell
        "cmd.exe /c rm -rf x",   # .exe cmd
        "wsl.exe rm -rf x",      # .exe wsl
        "find . -exec rm -rf {} +",    # find -exec
        "find . -execdir rm -rf {} ;", # find -execdir
    ):
        check(f"F1 {cmd!r} -> BLOCKED (interpretador/runner)",
              bool(Executor.check_policy(cmd)))
    # `rm` destrutivo dentro de um `echo`/string de comando que SÓ imprime
    # continua permitido (falso positivo preservado).
    check("F1 'sh -c \"echo rm -rf x\"' -> permitido (só imprime)",
          Executor.check_policy("sh -c 'echo rm -rf x'") == "")

    # ============================================================== F3
    print("== F3: rate-limit por IDENTIDADE Tailscale (só em modo público) ==")
    h3a = _handler_fake(Handler, "/api/status", headers={
        "Tailscale-User-Login": "alice@example.com",
        "Tailscale-User-Name": "Alice Architect",
    })
    with mock.patch.object(config, "HARNESS_PUBLIC", True):
        check("F3 público + Tailscale-User-Login -> chave ts:login",
              h3a._rate_key() == "ts:alice@example.com")
    with mock.patch.object(config, "HARNESS_PUBLIC", False):
        check("F3 local IGNORA header Tailscale (usa IP)",
              h3a._rate_key() == "127.0.0.1")
    h3b = _handler_fake(Handler, "/api/status", headers={
        "Tailscale-User-Name": "Alice Architect",
    })
    with mock.patch.object(config, "HARNESS_PUBLIC", True):
        check("F3 público sem Login -> fallback Tailscale-User-Name",
              h3b._rate_key() == "ts:Alice Architect")
    h3c = _handler_fake(Handler, "/api/status")  # sem headers Tailscale
    with mock.patch.object(config, "HARNESS_PUBLIC", True):
        check("F3 público sem headers Tailscale -> IP",
              h3c._rate_key() == "127.0.0.1")

    # ============================================================== F11
    print("== F11: /api/status e /api/sideprjs sem caminho absoluto ==")
    Handler._falhas.clear()
    Handler._bloqueios.clear()
    with mock.patch.object(Handler, "_auth_ok", return_value=True), \
            mock.patch.object(Handler, "_rate_limit_ok", return_value=True), \
            mock.patch.object(Handler, "_limpa_falhas",
                              lambda self, ip: None), \
            mock.patch.object(Handler, "_limpa_ttl",
                              lambda self, now: None):
        h11 = _handler_fake(Handler, "/api/status")
        h11.do_GET()
        body11 = json.loads(h11.wfile.data.decode("utf-8"))
        h11b = _handler_fake(Handler, "/api/sideprjs")
        h11b.do_GET()
        body11b = json.loads(h11b.wfile.data.decode("utf-8"))
    check("F11 status.root = nome da pasta (sem caminho absoluto)",
          body11.get("root") == config.ROOT.name
          and str(config.ROOT) not in str(body11.get("root", "")))
    check("F11 sideprjs.base = nome da pasta (sem caminho absoluto)",
          body11b.get("base") == config.SIDE_PRJS_DIR.name
          and ":" not in str(body11b.get("base", "")))

    # ============================================================== F13
    print("== F13: paridade BLOCKED_PATTERNS (harness config vs motor sandbox) ==")
    from harness.motor import sandbox as motor_sandbox  # noqa: E402
    cfg_set = set(config.BLOCKED_PATTERNS)
    motor_set = set(motor_sandbox.BLOCKED_PATTERNS)
    check("F13 conjuntos BLOCKED_PATTERNS idênticos",
          cfg_set == motor_set)
    check("F13 sem padrões faltando no motor",
          not (cfg_set - motor_set))
    check("F13 sem padrões extras no motor",
          not (motor_set - cfg_set))

    # ============================================================== F16
    print("== F16: corpo JSON inválido -> 400 (não vira comando vazio) ==")
    invalid = b'{"command": "echo x"'  # JSON truncado (inválido)
    h16 = _handler_fake(Handler, "/api/exec", headers={
        "Content-Length": str(len(invalid)),
    })
    h16.rfile = io.BytesIO(invalid)
    check("F16 _read_body JSON inválido -> None", h16._read_body() is None)
    h16b = _handler_fake(Handler, "/api/exec", headers={
        "Content-Length": "0",
    })
    h16b.rfile = io.BytesIO(b"")
    check("F16 corpo vazio legítimo -> {} (POSTs legítimos não quebram)",
          h16b._read_body() == {})
    h16c = _handler_fake(Handler, "/api/exec", headers={
        "Content-Length": str(len(invalid)),
    })
    h16c.rfile = io.BytesIO(invalid)
    with mock.patch.object(Handler, "_auth_ok", return_value=True), \
            mock.patch.object(Handler, "_rate_limit_ok", return_value=True), \
            mock.patch.object(Handler, "_limpa_falhas",
                              lambda self, ip: None):
        h16c.do_POST()
    check("F16 do_POST JSON inválido -> 400", h16c._resp_code == 400)

    print(f"\nRESULTADO (security): {passed} passaram, {failed} falharam")
    return 1 if failed else 0


if __name__ == "__main__":
    sys.exit(main_test())