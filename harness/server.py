"""Servidor HTTP local (zero dependências) — web shell + API do harness.

Endpoints:
  GET  /                        terminal web
  GET  /app.js                  cliente do terminal
  GET  /api/health              status do servidor
  GET  /api/status              visão geral (jobs, memória, versão)
  GET  /api/history             histórico de comandos
  DELETE /api/history           limpa o histórico
  GET  /api/jobs                execuções ativas/realizadas
  POST /api/exec                {command, cwd?, retries?} -> inicia execução
  POST /api/exec/parallel       {commands: [...], retries?} -> grupo em paralelo
  GET  /api/job?id=<id>         saída acumulada de uma execução
  POST /api/job/stop            {id} -> encerra execução
  GET  /api/group?id=<g-id>     resultados consolidados de um grupo
  GET  /api/memory              registros da memória do Brain
  GET  /api/memory?id=<id>      registro específico
  GET  /api/memory/search?q=    busca RAG por relevância (Ch14)
  GET  /api/memory/stats        relatório de saúde da memória (Ch19); ?save=1 grava Markdown
  POST /api/memory              grava registro episódico
  GET  /api/refs/query          RAG consultivo sobre referências validadas (Item 7): ?q=&limit=&incluir_fraca=
  POST /api/eval                avalia texto contra critérios (Ch19)
  GET  /api/agents              playbook do sistema interno de agentes
  POST /api/agents/learn        recompila o playbook (compile + save)
  POST /api/pipeline            executa o orquestrador determinístico
  POST /api/webscrape           {titulo, autor?, pdf?} -> coleta referência externa
  GET  /api/sideprjs            subpastas de sidePrjs/ (nome + data)
  GET  /api/observability       panorama da evolução do aprendizado do hub
                                (episódicos + histórico + playbook; Item 6)
  GET  /api/skills              registro das skills de domínio (total, válidas,
                                trust, erros de validação)
"""

from __future__ import annotations

import base64
import hmac
import json
import threading
import time
from datetime import datetime
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from urllib.parse import urlparse, parse_qs

from . import auth, config
from .agents import compile_playbook, load_playbook
from .eval import evaluate
from .eval_memory import gerar_relatorio, salvar_relatorio
from .executor import Executor
from .memory import Memory
from .observability import gerar_panorama
from .pipeline import AgentPipeline
from .rag_refs import consultar
from .skills import resumo_sintetico as skills_resumo
from .webscraper import coletar


class _BoundedThreadingHTTPServer(ThreadingHTTPServer):
    """ThreadingHTTPServer com TETO de requisições simultâneas.

    O ThreadingMixIn padrão abre uma thread POR conexão, sem limite —
    um peer podia abrir milhares de conexões e exaurir threads (DoS). Aqui,
    quando o teto é atingido, a conexão recebe 503 e é fechada imediatamente
    (sem thread nova). O semáforo é liberado quando a requisição termina."""

    def __init__(self, *args, **kwargs):
        super().__init__(*args, **kwargs)
        self._semaforo = threading.BoundedSemaphore(
            config.MAX_CONCURRENT_REQUESTS)

    def process_request(self, request, client_address):
        if not self._semaforo.acquire(blocking=False):
            self._rejeita_sobrecarga(request)
            return
        try:
            threading.Thread(
                target=self._processa_um,
                args=(request, client_address),
                daemon=True,
            ).start()
        except Exception:  # noqa: BLE001
            self._semaforo.release()
            self.close_request(request)

    def _processa_um(self, request, client_address):
        try:
            self.finish_request(request, client_address)
            self.shutdown_request(request)
        finally:
            self._semaforo.release()

    @staticmethod
    def _rejeita_sobrecarga(request):
        corpo = b"servidor ocupado\n"
        try:
            request.sendall(
                b"HTTP/1.1 503 Service Unavailable\r\n"
                b"Content-Type: text/plain\r\n"
                b"Content-Length: " + str(len(corpo)).encode("ascii") + b"\r\n"
                b"Connection: close\r\n\r\n" + corpo
            )
        except OSError:
            pass
        finally:
            try:
                request.close()
            except OSError:
                pass

MIME = {
    ".html": "text/html; charset=utf-8",
    ".js": "application/javascript; charset=utf-8",
    ".css": "text/css; charset=utf-8",
    ".json": "application/json; charset=utf-8",
    ".png": "image/png",
    ".ico": "image/x-icon",
}


def _sideprjs_items() -> list[dict]:
    """Lista as subpastas de sidePrjs/ com nome e data (somente leitura).

    Endpoint de leitura (GET) sem mutação: apenas enumera diretórios diretos
    de config.SIDE_PRJS_DIR, em ordem alfabética.
    """
    items: list[dict] = []
    if not config.SIDE_PRJS_DIR.is_dir():
        return items
    for pasta in sorted(config.SIDE_PRJS_DIR.iterdir()):
        if not pasta.is_dir():
            continue
        st = pasta.stat()
        items.append({
            "nome": pasta.name,
            "data": datetime.fromtimestamp(st.st_mtime).isoformat(timespec="seconds"),
        })
    return items


def _log_auth(chave: str, rota: str, status: int) -> None:
    """Log LEVE de eventos de autenticação/erros de rota (F10) — sem
    framework: append de uma linha em logs/server_access.log (timestamp,
    chave, rota, status). Usado em 401 (auth negada), 429 (rate-limit) e 500
    (exceção não tratada no do_POST) para rastreabilidade. A escrita é um
    append atômico de linha única (sem locks complexos); falha de escrita
    nunca levanta (log é best-effort)."""
    try:
        logs_dir = config.ROOT / "logs"
        logs_dir.mkdir(parents=True, exist_ok=True)
        linha = (
            f"{datetime.now().isoformat(timespec='seconds')} "
            f"chave={chave} rota={rota} status={status}\n"
        )
        with open(logs_dir / "server_access.log", "a", encoding="utf-8") as f:
            f.write(linha)
    except OSError:
        pass


class HarnessServer:
    def __init__(self, host: str = "127.0.0.1", port: int = config.DEFAULT_PORT):
        # B4 (segurança de deploy): modo público exige HTTPS (Tailscale) —
        # bind 0.0.0.0 bloqueado; credenciais padrão/fábrica são RECUSADAS
        # (não sobem) — expor o web shell com senha de fábrica em deploy
        # público é inaceitável. Cobre os dois fluxos com senha de fábrica:
        # AUTH_MODE=="padrao" (nenhuma credencial definida) e AUTH_MODE=="env"
        # com HARNESS_PASSWORD igual ao valor padrão de fábrica "opencode"
        # (ex.: tailscale-serve.bat injetava via ambiente).
        if config.HARNESS_PUBLIC:
            if host == "0.0.0.0":
                raise SystemExit(
                    "[harness] modo público exige HTTPS (Tailscale) — use "
                    "127.0.0.1 + tailscale-serve.bat, ou HARNESS_PUBLIC=0"
                )
            if config.AUTH_MODE == "padrao":
                raise SystemExit(
                    "[harness] modo público exige senha personalizada — "
                    "credenciais padrão opencode/opencode NÃO sobem em "
                    "deploy público; defina com: python -m harness.auth set"
                )
            if config.AUTH_MODE == "env" \
                    and config.AUTH_PASSWORD == config.AUTH_DEFAULT_PASSWORD:
                raise SystemExit(
                    "[harness] modo público com HARNESS_PASSWORD igual à senha "
                    "PADRÃO opencode/opencode NÃO sobe — defina uma senha "
                    "personalizada com: python -m harness.auth set"
                )
        self.host = host
        self.port = port
        self.executor = Executor()
        self.memory = Memory()
        self._httpd: ThreadingHTTPServer | None = None
        self._httpd_thread: threading.Thread | None = None

    # ------------------------------------------------------------------ HTTP
    def status(self) -> dict:
        jobs = self.executor.list()
        running = sum(1 for j in jobs if j["status"] == "running")
        return {
            "name": "harness",
            "ok": True,
            # F11: não revela o caminho absoluto — apenas o NOME da pasta raiz
            # (ex.: "harness-mvp").
            "root": config.ROOT.name,
            "jobs_running": running,
            "jobs_total": len(jobs),
            "history": len(self.executor.history()),
            "memory_records": len(self.memory.list_records()),
        }

    def serve_forever(self) -> None:
        handler = self._make_handler()
        self._httpd = _BoundedThreadingHTTPServer((self.host, self.port), handler)
        self._httpd.daemon_threads = True
        print(f"[harness] web shell em http://{self.host}:{self.port}")
        print(f"[harness] pare com Ctrl+C")
        self._httpd_thread = threading.Thread(target=self._httpd.serve_forever, daemon=True)
        self._httpd_thread.start()

    def shutdown(self) -> None:
        if self._httpd:
            self._httpd.shutdown()
            self._httpd.server_close()

    def _make_handler(self):
        server = self

        class Handler(BaseHTTPRequestHandler):
            server_version = "HarnessLocal"  # A3: sem versão "/1.0"
            # F7: remove o "Python/x.y.z" do header Server (BaseHTTPRequestHandler
            # usa `sys_version` quando não vazio). Sem versão de runtime exposta.
            sys_version = ""

            def handle(self):
                """B3: TimeoutError de leitura (anti-slowloris, ex.: GET com a
                linha de request lenta) NÃO pode derrubar a thread com
                traceback — fecha a conexão limpa. O do_POST responde 408; o
                GET encerrado por timeout fecha sem corpo (o socket morreu)."""
                try:
                    super().handle()
                except TimeoutError:
                    try:
                        self.connection.close()
                    except OSError:
                        pass

            def version_string(self):
                # F7 (cosmético): com sys_version vazio, o base monta
                # "HarnessLocal " (espaço final). Evita o trailing space.
                if self.sys_version:
                    return f"{self.server_version} {self.sys_version}"
                return self.server_version

            # B2: rate-limit por IP em falhas de autenticação. Compartilhado
            # entre threads (lock de classe) — o ThreadingHTTPServer usa
            # threads por conexão.
            _falhas: dict[str, list[float]] = {}  # ip -> timestamps de falhas
            _bloqueios: dict[str, float] = {}  # ip -> início do bloqueio (BLOQUEIO_FALHAS)
            _lock_rate: threading.Lock = threading.Lock()
            MAX_FALHAS = 5
            JANELA_FALHAS = 60.0  # segundos
            BLOQUEIO_FALHAS = 300.0  # segundos de bloqueio após exceder
            # TTL (memory leak, B2): IPs sem evento recente são removidos dos
            # dicts — limpeza oportunista ao acessar (sem thread extra). Deve
            # ser >= BLOQUEIO_FALHAS + JANELA_FALHAS para nunca expirar um IP
            # ainda dentro do ciclo falha->bloqueio (360s < 600s).
            _TTL_FALHAS = 600.0  # segundos

            # Rate-limit de EXECUÇÃO por identidade (anti thread/process
            # exhaustion): posts em /api/exec e /api/exec/parallel. Chave =
            # identidade Tailscale (público) ou IP (local), mesmo critério do
            # `_rate_key`. Janela deslizante.
            _exec_reqs: dict[str, list[float]] = {}

            # Anti-slowloris: timeout de conexão (socket). Sem ele, um peer
            # podia segurar a conexão indefinidamente lendo o corpo (thread
            # presa). `self.rfile.read` levanta TimeoutError após 60s ociosos.
            timeout = 60.0

            # --------------------------------------------------- helpers
            def _send(self, code: int, body: bytes, content_type: str = "application/json"):
                self.send_response(code)
                self.send_header("Content-Type", content_type)
                self.send_header("Content-Length", str(len(body)))
                self.send_header("Cache-Control", "no-store")
                # A2: headers de hardening HTTP (todas as respostas)
                self.send_header("X-Content-Type-Options", "nosniff")
                self.send_header("Referrer-Policy", "no-referrer")
                self.send_header("X-Frame-Options", "DENY")
                self.end_headers()
                if not getattr(self, "_head_only", False):
                    self.wfile.write(body)

            def _json(self, code: int, payload: dict):
                body = json.dumps(payload, ensure_ascii=False, indent=2).encode("utf-8")
                self._send(code, body)

            def _read_body(self) -> dict | None:
                """Lê e faz parse do corpo JSON. Retorna:
                  - dict  -> corpo JSON válido (ou {} para corpo vazio);
                  - None  -> F16: corpo NÃO vazio com JSON inválido (o handler
                             responde 400 em vez de tratar como comando vazio);
                  - levanta ValueError (413) se exceder MAX_BODY_BYTES."""
                try:
                    length = int(self.headers.get("Content-Length", 0))
                except (TypeError, ValueError):
                    length = 0
                if length <= 0:
                    return {}
                if length > config.MAX_BODY_BYTES:
                    raise ValueError("corpo excede o limite permitido")
                # socket timeout (anti-slowloris): peer ocioso -> TimeoutError
                raw = self.rfile.read(length)
                if not raw.strip():
                    return {}  # POST legítimo com corpo vazio (Content-Length > 0)
                try:
                    return json.loads(raw.decode("utf-8"))
                except (json.JSONDecodeError, UnicodeDecodeError):
                    return None

            def _auth_ok(self) -> bool:
                """Valida HTTP Basic auth (mesmo modelo do painel web do
                opencode): header `Authorization: Basic base64(user:pass)`.
                Usuário e senha comparados em tempo constante (hmac). Em modo
                "hash" (F0/F1), valida contra config/auth.json via
                auth.validate (scrypt); nos modos "env"/"padrao", contra as
                constantes AUTH_USERNAME/AUTH_PASSWORD."""
                header = self.headers.get("Authorization", "")
                if not header.startswith("Basic "):
                    return False
                try:
                    decoded = base64.b64decode(header[6:]).decode("utf-8", errors="replace")
                except (ValueError, base64.binascii.Error):
                    return False
                user, _, pwd = decoded.partition(":")
                if config.AUTH_MODE == "hash":
                    return auth.validate(user, pwd)
                if not hmac.compare_digest(user, config.AUTH_USERNAME):
                    return False
                return hmac.compare_digest(pwd, config.AUTH_PASSWORD)

            def _limpa_ttl(self, now: float) -> None:
                """Remove IPs inativos dos dicts de rate-limit (memory leak).

                Um IP é expirado quando o ÚLTIMO evento (falha mais recente ou
                início de bloqueio) é mais antigo que `_TTL_FALHAS` (600s).
                Limpeza oportunista chamada no início de `_rate_limit_ok` — sem
                thread extra. Adquire `_lock_rate` (chamada ANTES do lock no
                `_rate_limit_ok`)."""
                with self._lock_rate:
                    for ip in [ip for ip, ts in self._falhas.items()
                               if ts and now - ts[-1] > self._TTL_FALHAS]:
                        self._falhas.pop(ip, None)
                        self._bloqueios.pop(ip, None)
                    for ip in [ip for ip, t0 in self._bloqueios.items()
                               if now - t0 > self._TTL_FALHAS]:
                        self._bloqueios.pop(ip, None)
                    for ip in [ip for ip, ts in self._exec_reqs.items()
                               if ts and now - ts[-1] > self._TTL_FALHAS]:
                        self._exec_reqs.pop(ip, None)

            def _exec_rate_limit_ok(self, chave: str) -> bool:
                """True se a identidade ainda pode postar /api/exec nesta
                janela (MAX_EXEC_PER_WINDOW posts por EXEC_RATE_WINDOW)."""
                now = time.time()
                with self._lock_rate:
                    lista = [t for t in self._exec_reqs.get(chave, [])
                             if now - t < config.EXEC_RATE_WINDOW]
                    if len(lista) >= config.MAX_EXEC_PER_WINDOW:
                        self._exec_reqs[chave] = lista
                        return False
                    lista.append(now)
                    self._exec_reqs[chave] = lista
                    return True

            def _rate_limit_ok(self, ip: str) -> bool:
                """True se o IP ainda pode tentar autenticar (B2).

                Remove falhas fora da janela; se restarem >= MAX_FALHAS,
                mantém o IP bloqueado por BLOQUEIO_FALHAS segundos (desde a
                última vez que excedeu). Após o bloqueio expirar, zera e
                libera. IPs inativos são expurgados via `_limpa_ttl` (TTL)."""
                now = time.time()
                self._limpa_ttl(now)
                with self._lock_rate:
                    falhas = [t for t in self._falhas.get(ip, [])
                              if now - t < self.JANELA_FALHAS]
                    if falhas:
                        self._falhas[ip] = falhas
                    elif ip in self._falhas:
                        # todas as falhas expiraram: não mantém chave vazia
                        # (evita recriar logo após o TTL remover o IP)
                        self._falhas.pop(ip, None)
                    if len(falhas) >= self.MAX_FALHAS:
                        inicio = self._bloqueios.get(ip, now)
                        self._bloqueios[ip] = inicio
                        if now - inicio < self.BLOQUEIO_FALHAS:
                            return False
                        # bloqueio expirado: libera o IP
                        self._falhas.pop(ip, None)
                        self._bloqueios.pop(ip, None)
                    return True

            def _registra_falha(self, ip: str) -> None:
                with self._lock_rate:
                    self._falhas.setdefault(ip, []).append(time.time())

            def _limpa_falhas(self, ip: str) -> None:
                with self._lock_rate:
                    self._falhas.pop(ip, None)
                    self._bloqueios.pop(ip, None)

            def _rate_key(self) -> str:
                """Chave do rate-limit por IDENTIDADE (F3 — estratégia B).

                Precedência:
                  1. Em modo PÚBLICO (`config.HARNESS_PUBLIC`, deploy via
                     Tailscale Serve), se o header `Tailscale-User-Login`
                     estiver presente, usa o LOGIN do usuário Tailscale (ex.:
                     alice@example.com) — é a identidade estável que o Serve
                     injeta (fonte: docs oficiais do Tailscale Serve, "Identity
                     headers"). Fallback: `Tailscale-User-Name` (nome de
                     exibição). Atrás do proxy, TODOS os clientes aparecem como
                     o mesmo IP — rate-limit por IP causaria lockout global.
                  2. Em modo local (`HARNESS_PUBLIC=0`) ou sem header Tailscale:
                     `client_address[0]` (IP).

                SEGURANÇA: NUNCA confia em header Tailscale em modo local — um
                cliente direto poderia forjá-lo. Só considera os headers em modo
                público, onde o harness escuta em 127.0.0.1 atrás do Tailscale
                Serve (que REMOVE esses headers de requisições forjadas,
                anti-spoofing do próprio Serve) — sem caminho direto ao backend
                sem passar pelo proxy."""
                if config.HARNESS_PUBLIC:
                    for nome in ("Tailscale-User-Login", "Tailscale-User-Name"):
                        valor = self.headers.get(nome)
                        if valor and valor.strip():
                            return f"ts:{valor.strip()}"
                return self.client_address[0]

            def _autentica(self) -> bool:
                """Autentica com rate-limit por identidade (B2 + F3). Retorna
                True se autorizado; em falha responde 429 (bloqueado) ou 401
                (credenciais inválidas) e retorna False."""
                chave = self._rate_key()
                if not self._rate_limit_ok(chave):
                    _log_auth(chave, self.path, 429)
                    body = json.dumps(
                        {"error": "muitas tentativas de autenticação — chave bloqueada"},
                        ensure_ascii=False,
                    ).encode("utf-8")
                    self.send_response(429)
                    self.send_header("Content-Type", "application/json")
                    self.send_header("Retry-After", str(int(self.BLOQUEIO_FALHAS)))
                    self.send_header("Content-Length", str(len(body)))
                    self.send_header("Cache-Control", "no-store")
                    self.send_header("X-Content-Type-Options", "nosniff")
                    self.send_header("Referrer-Policy", "no-referrer")
                    self.send_header("X-Frame-Options", "DENY")
                    self.end_headers()
                    if not getattr(self, "_head_only", False):
                        self.wfile.write(body)
                    return False
                if not self._auth_ok():
                    self._registra_falha(chave)
                    self._auth_denied()
                    return False
                self._limpa_falhas(chave)
                return True

            def _auth_denied(self):
                """401 com WWW-Authenticate para o navegador exibir o prompt."""
                _log_auth(self._rate_key(), self.path, 401)
                body = json.dumps(
                    {"error": "credenciais de acesso ausentes ou inválidas"},
                    ensure_ascii=False,
                ).encode("utf-8")
                self.send_response(401)
                self.send_header("Content-Type", "application/json")
                self.send_header(
                    "WWW-Authenticate",
                    f'Basic realm="{config.AUTH_REALM}", charset="UTF-8"',
                )
                self.send_header("Content-Length", str(len(body)))
                self.send_header("Cache-Control", "no-store")
                self.send_header("X-Content-Type-Options", "nosniff")
                self.send_header("Referrer-Policy", "no-referrer")
                self.send_header("X-Frame-Options", "DENY")
                self.end_headers()
                if not getattr(self, "_head_only", False):
                    self.wfile.write(body)

            def _serve_static(self, rel: str):
                path = (config.WEB_DIR / rel).resolve()
                if config.WEB_DIR not in path.parents and path != config.WEB_DIR:
                    self._json(403, {"error": "fora do diretório web"})
                    return
                if not path.is_file():
                    self._send(404, b"not found", "text/plain; charset=utf-8")
                    return
                mime = MIME.get(path.suffix.lower(), "application/octet-stream")
                self._send(200, path.read_bytes(), mime)

            # --------------------------------------------------- routing
            def do_GET(self):  # noqa: N802
                parsed = urlparse(self.path)
                path = parsed.path

                # Autenticação: toda rota /api/* (exceto /api/health e os
                # estáticos /, /app.js que não começam com /api/) exige HTTP
                # Basic auth. B2: rate-limit por IP em falhas de autenticação.
                if path.startswith("/api/") and path != "/api/health":
                    if not self._autentica():
                        return

                if path == "/" or path == "/index.html":
                    self._serve_static("index.html")
                elif path == "/app.js":
                    self._serve_static("app.js")
                elif path == "/api/health":
                    # A1: endpoint público sem dados internos (sem ROOT).
                    self._json(200, {
                        "ok": True,
                        "name": "harness",
                    })
                elif path == "/api/status":
                    self._json(200, server.status())
                elif path == "/api/history":
                    self._json(200, {"items": server.executor.history()})
                elif path == "/api/jobs":
                    self._json(200, {"items": server.executor.list()})
                elif path == "/api/job":
                    qs = parse_qs(parsed.query)
                    job = server.executor.get(qs.get("id", [""])[0])
                    if job is None:
                        self._json(404, {"error": "job nao encontrado"})
                    else:
                        self._json(200, job)
                elif path == "/api/group":
                    qs = parse_qs(parsed.query)
                    items = server.executor.group(qs.get("id", [""])[0])
                    if not items:
                        self._json(404, {"error": "grupo nao encontrado"})
                    else:
                        done = all(i["status"] in ("finished", "error", "blocked", "stopped")
                                   for i in items)
                        failed = any(i["status"] in ("error", "blocked") for i in items)
                        self._json(200, {
                            "group": qs.get("id", [""])[0],
                            "done": done,
                            "failed": failed,
                            "items": items,
                        })
                elif path == "/api/memory/search":
                    qs = parse_qs(parsed.query)
                    query = qs.get("q", [""])[0]
                    try:
                        limit = int(qs.get("limit", ["5"])[0])
                    except (TypeError, ValueError):
                        limit = 5
                    limit = max(1, min(limit, config.MAX_SEARCH_LIMIT))
                    self._json(200, {"items": server.memory.search(query, limit=limit)})
                elif path == "/api/memory/stats":
                    qs = parse_qs(parsed.query)
                    relatorio = gerar_relatorio(server.memory)
                    if qs.get("save", ["0"])[0] in ("1", "true", "yes"):
                        # A6: passa o relatório já gerado — salvar_relatorio
                        # não gera de novo (evita trabalho duplicado).
                        caminho = salvar_relatorio(
                            server.memory, config.EVAL_DIR, relatorio=relatorio)
                        relatorio["arquivo"] = str(caminho)
                    self._json(200, relatorio)
                elif path == "/api/refs/query":
                    qs = parse_qs(parsed.query)
                    query = qs.get("q", [""])[0]
                    try:
                        limit = int(qs.get("limit", ["5"])[0])
                    except (TypeError, ValueError):
                        limit = 5
                    limit = max(1, min(limit, config.MAX_REF_QUERY_LIMIT))
                    incluir_fraca = qs.get("incluir_fraca", ["0"])[0] in ("1", "true", "yes")
                    self._json(200, consultar(
                        query, limit=limit, incluir_fraca=incluir_fraca))
                elif path == "/api/memory":
                    qs = parse_qs(parsed.query)
                    record_id = qs.get("id", [""])[0]
                    if record_id:
                        rec = server.memory.get(record_id)
                        if rec is None:
                            self._json(404, {"error": "registro nao encontrado"})
                        else:
                            self._json(200, rec)
                    else:
                        self._json(200, {
                            "core": server.memory.core(),
                            "records": server.memory.list_records(),
                        })
                elif path == "/api/agents":
                    playbook = load_playbook()
                    if playbook is None:
                        self._json(200, {
                            "error": "playbook não compilado",
                            "hint": "python -m harness.agents compile",
                        })
                    else:
                        self._json(200, {"playbook": playbook})
                elif path == "/api/observability":
                    # Item 6: panorama da evolução do aprendizado do hub
                    # (episódicos + histórico + playbook; somente leitura).
                    self._json(200, gerar_panorama(server.memory, load_playbook()))
                elif path == "/api/skills":
                    # Registro das skills de domínio (.opencode/skills/*/SKILL.md):
                    # total, válidas/inválidas, trust e erros de validação.
                    self._json(200, skills_resumo())
                elif path == "/api/sideprjs":
                    # F11: sem caminho absoluto no retorno — apenas o NOME da
                    # pasta base (os items já trazem nome + data).
                    self._json(200, {
                        "base": config.SIDE_PRJS_DIR.name,
                        "items": _sideprjs_items(),
                    })
                else:
                    self._json(404, {"error": "rota nao encontrada"})

            def do_HEAD(self):  # noqa: N802
                """HEAD: mesmos headers do GET correspondente, sem corpo.

                Segurança (achado baixo do reviewer): exige autenticação nas
                rotas /api/* como os demais métodos — sem credenciais responde
                401 (nunca 200/headers de dados). Delega para do_GET com
                `_head_only=True`; `_send`/`_autentica`/`_auth_denied` respeitam
                a flag (enviam headers, não escrevem o corpo)."""
                self._head_only = True
                self.do_GET()

            def do_OPTIONS(self):  # noqa: N802
                """OPTIONS: exige autenticação nas rotas /api/* (consistente
                com os demais métodos); autenticado responde 204 sem corpo.
                O BaseHTTPRequestHandler padrão responderia 501 sem auth — um
                scanner externo usaria OPTIONS/HEAD/PUT/PATCH como sonda não
                autenticada; aqui o comportamento é igual ao dos demais."""
                parsed = urlparse(self.path)
                if parsed.path.startswith("/api/") and parsed.path != "/api/health":
                    if not self._autentica():
                        return
                self.send_response(204)
                self.send_header("Content-Length", "0")
                self.send_header("Cache-Control", "no-store")
                self.send_header("X-Content-Type-Options", "nosniff")
                self.send_header("Referrer-Policy", "no-referrer")
                self.send_header("X-Frame-Options", "DENY")
                self.end_headers()

            def _handle_nao_permitido(self):
                """PUT/PATCH (F8): autentica como os demais métodos e responde
                405 (Method Not Allowed) com headers de hardening — o
                BaseHTTPRequestHandler padrão responderia 501 sem auth (sonda
                não autenticada). Sem credenciais, responde 401."""
                if not self._autentica():
                    return
                self._json(405, {
                    "error": "método não permitido (PUT/PATCH) — use POST",
                })

            def do_PUT(self):  # noqa: N802
                self._handle_nao_permitido()

            def do_PATCH(self):  # noqa: N802
                self._handle_nao_permitido()

            def do_DELETE(self):  # noqa: N802
                parsed = urlparse(self.path)
                path = parsed.path

                if not self._autentica():
                    return

                if path == "/api/history":
                    server.executor.clear_history()
                    self._json(200, {"ok": True, "cleared": True})
                else:
                    self._json(404, {"error": "rota nao encontrada"})

            def do_POST(self):  # noqa: N802
                parsed = urlparse(self.path)
                path = parsed.path

                if not self._autentica():
                    return

                try:
                    data = self._read_body()
                except ValueError as exc:
                    self._json(413, {"error": str(exc)})
                    return
                except TimeoutError:
                    self._json(408, {"error": "timeout de leitura da requisição"})
                    return
                if data is None:
                    # F16: corpo NÃO vazio com JSON inválido — não trata como
                    # comando vazio; responde 400 explícito.
                    self._json(400, {"error": "corpo JSON inválido"})
                    return

                try:
                    self._do_post_rota(path, data)
                except Exception as exc:  # noqa: BLE001 — F10: exceção não
                    # tratada no do_POST não pode derrubar a thread silenciosa-
                    # mente: loga e responde 500.
                    _log_auth(self._rate_key(), path, 500)
                    self._json(500, {"error": f"erro interno: {exc}"})

            def _do_post_rota(self, path: str, data: dict) -> None:
                """Roteamento POST (extraído para o try/except genérico do
                do_POST — F10)."""
                if path == "/api/exec":
                    if not self._exec_rate_limit_ok(self._rate_key()):
                        self._json(429, {
                            "error": "limite de execução atingido — aguarde alguns segundos",
                        })
                        return
                    command = data.get("command", "")
                    cwd = data.get("cwd") or str(config.ROOT)
                    retries = data.get("retries")
                    job = server.executor.run(command, cwd=cwd, retries=retries)
                    self._json(200, {"id": job.id, "status": job.status, "reason": job.reason})
                elif path == "/api/exec/parallel":
                    if not self._exec_rate_limit_ok(self._rate_key()):
                        self._json(429, {
                            "error": "limite de execução atingido — aguarde alguns segundos",
                        })
                        return
                    commands = data.get("commands", [])
                    cwd = data.get("cwd") or str(config.ROOT)
                    retries = data.get("retries")
                    if not isinstance(commands, list) or not all(isinstance(c, str) for c in commands):
                        self._json(400, {"error": "campo 'commands' é obrigatório (lista de strings)"})
                    elif len(commands) > config.MAX_PARALLEL:
                        self._json(400, {"error": f"máximo de {config.MAX_PARALLEL} comandos por vez"})
                    else:
                        jobs = server.executor.run_many(commands, cwd=cwd, retries=retries)
                        group_id = jobs[0].group if jobs else None
                        self._json(200, {
                            "group": group_id,
                            "items": [{"id": j.id, "status": j.status, "reason": j.reason}
                                      for j in jobs],
                        })
                elif path == "/api/job/stop":
                    stopped = server.executor.stop(data.get("id", ""))
                    self._json(200, {"stopped": stopped})
                elif path == "/api/eval":
                    text = data.get("text", "")
                    criteria = data.get("criteria")
                    if criteria is None:
                        from .eval import default_rules
                        criteria = default_rules()
                    if not isinstance(criteria, list):
                        self._json(400, {"error": "campo 'criteria' deve ser uma lista"})
                    else:
                        result = evaluate(text, criteria, exit_code=data.get("exit_code"))
                        self._json(200, result)
                elif path == "/api/memory":
                    try:
                        # Pendência 4: registros via web shell são inseridos por
                        # um humano — default validado_por=human quando o corpo
                        # não traz o campo. Se o cliente enviar validado_por
                        # (ex.: "motor"), o valor enviado é respeitado.
                        # Pendência 3 (HITL/trust): quando validado_por é
                        # "human" (humano validou explicitamente) e o corpo NÃO
                        # fornece trust, o trust sobe para "media" (validação
                        # humana explícita não é memória sem validação = fraca).
                        # Trust fornecido explicitamente no corpo é SEMPRE
                        # respeitado (inclusive com validado_por=human).
                        # origem opcional; ausente -> "_não informada_" (default).
                        validado_por = data.get("validado_por", "human")
                        trust = data.get("trust")
                        if trust is None and validado_por == "human":
                            trust = "media"
                        rec = server.memory.record(
                            keywords=data.get("keywords", []),
                            agente=data.get("agente", "web"),
                            tema=data.get("tema", "registro"),
                            entrada=data.get("entrada", ""),
                            fluxo=data.get("fluxo", ""),
                            resultado=data.get("resultado", ""),
                            contexto=data.get("contexto", ""),
                            status=data.get("status", "completed"),
                            origem=data.get("origem", ""),
                            validado_por=validado_por,
                            trust=trust,
                        )
                        self._json(200, rec)
                    except Exception as exc:  # noqa: BLE001
                        self._json(400, {"error": str(exc)})
                elif path == "/api/agents/learn":
                    try:
                        playbook = compile_playbook()
                        playbook.save(config.AGENTS_DIR)
                        self._json(200, {
                            "ok": True,
                            "path": str(config.PLAYBOOK_FILE),
                            "agents": len(playbook.agents),
                        })
                    except Exception as exc:  # noqa: BLE001
                        self._json(400, {"error": str(exc)})
                elif path == "/api/pipeline":
                    task = data.get("task")
                    if not isinstance(task, dict):
                        self._json(400, {
                            "error": "campo 'task' é obrigatório (dict com objetivo/escopo/criterios_de_aceite)",
                        })
                    else:
                        # F4: dry-run explícito — approve=None (nada executado,
                        # contrato BLOQUEADA) E gravar_registro=False (NUNCA grava
                        # memória episódica a cada chamada; o pipeline também não
                        # grava BLOQUEADA por padrão). "dry_run": True deixa claro
                        # ao consumidor que nada foi executado/gravado.
                        pipeline = AgentPipeline(approve=None, gravar_registro=False)
                        try:
                            resultado = pipeline.run_task(task)
                        except Exception as exc:  # noqa: BLE001 — F10: pipeline
                            # não pode derrubar a thread silenciosamente.
                            _log_auth(self._rate_key(), path, 500)
                            self._json(500, {"error": f"falha no pipeline: {exc}"})
                            return
                        resultado["dry_run"] = True
                        self._json(200, resultado)
                elif path == "/api/webscrape":
                    titulo = data.get("titulo", "")
                    autor = data.get("autor", "")
                    baixar_pdf = bool(data.get("pdf", False))
                    if not titulo:
                        self._json(400, {"error": "campo 'titulo' é obrigatório"})
                    else:
                        try:
                            resultado = coletar(
                                titulo,
                                autor=autor,
                                baixar_pdf=baixar_pdf,
                            )
                            self._json(200, resultado)
                        except Exception as exc:  # noqa: BLE001
                            self._json(400, {"error": str(exc)})
                else:
                    self._json(404, {"error": "rota nao encontrada"})

            def log_message(self, fmt, *args):  # noqa: A003
                # silenciar log padrão; mantém o console limpo
                pass

        return Handler