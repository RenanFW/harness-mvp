"""Testes da auditoria de segurança web v2 (harness/security.py).

Suíte auto-contida no padrão do harness (helper `check`, lista de casos, exit
1 se algum falhar). NENHUM teste depende de rede/DNS real:

  - Transporte HTTP local usa `ThreadingHTTPServer` em 127.0.0.1 (loopback
    liberado explicitamente com `allow_loopback=True`).
  - Funções de rede (DoH/RDAP/crt.sh) são mockadas via
    `unittest.mock.patch`, exceto o smoke offline do `--self-test`.
  - SSRF: patcheia-se `harness.webscraper._host_ips` (usado por `_ssrf_valido`
    importado pelo security).

Rode com:
    python tests/security_audit_test.py
"""

from __future__ import annotations

import contextlib
import io
import json
import pathlib
import ssl
import sys
import tempfile
import traceback
import urllib.error
import urllib.request
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from unittest import mock

sys.path.insert(0, str(pathlib.Path(__file__).resolve().parent.parent))

# Console Windows (cp1252) não imprime todos os caracteres UTF-8; usa UTF-8
# com substituição para nunca quebrar a saída (padrão do harness).
try:
    sys.stdout.reconfigure(encoding="utf-8", errors="replace")
except (AttributeError, ValueError):
    pass

from harness import config  # noqa: E402
from harness import security  # noqa: E402
from harness.security import (  # noqa: E402
    Budget,
    Confianca,
    Finding,
    Relatorio,
    Resposta,
    Severidade,
    _alvo_valido,
    _AuditRedirectHandler,
    _avaliar_cadeia_redirects,
    _cipher_fraco,
    _dias_para_expirar,
    _dominio_base,
    _handshake_versao,
    _hash_findings,
    _porta_tls_padrao,
    _slug_seguro,
    _urls_mistas,
    agrega_severidade,
    audit_ativo,
    audit_cabecalhos,
    audit_componentes_osv,
    audit_cookies,
    audit_cors_ativo,
    audit_cors_passivo,
    audit_ct_logs,
    audit_email,
    audit_erros_expostos,
    audit_graphql,
    audit_metodos,
    audit_mixed_content,
    audit_open_redirect,
    audit_openapi,
    audit_paths_sensiveis,
    audit_portas,
    audit_rdap,
    audit_redirects,
    audit_reflexao,
    audit_segredos_js,
    audit_superficie,
    audit_tls,
    audit_well_known,
    fases_do_perfil,
    mascarar,
    mascarar_sempre,
    perfil_autorizado,
    perfil_valido,
    redigir_conteudo,
    redigir_header,
    requisicao,
    requisicao_json,
)


# ---------------------------------------------------------------------------
# Helpers
# ---------------------------------------------------------------------------

def _finding(**over) -> Finding:
    """Finding com defaults válidos; sobrescreva o que o caso precisar."""
    dados = {
        "id": "teste-1",
        "categoria": "teste",
        "titulo": "Titulo",
        "severidade": Severidade.MEDIA,
        "confianca": Confianca.OBSERVADO,
        "descricao": "descricao",
        "evidencia": "evidencia-bruta",
        "origem": "https://exemplo.com/",
        "passos_repro": ["passo 1"],
        "recomendacao": "recomendacao",
    }
    dados.update(over)
    return Finding(**dados)


class _HandlerTeste(BaseHTTPRequestHandler):
    """Servidor local mínimo para testar transporte/redirect/cookies."""

    def log_message(self, *args):  # noqa: N802
        return

    def _responder(self, status, body=b"", extras=None):
        self.send_response(status)
        for nome, valor in (extras or []):
            self.send_header(nome, valor)
        if not any(n.lower() == "content-type" for n, _ in (extras or [])):
            self.send_header("Content-Type", "text/plain; charset=utf-8")
        self.end_headers()
        if body:
            self.wfile.write(body)

    def do_GET(self):
        if self.path == "/redirect":
            self.send_response(302)
            self.send_header("Location", "/")
            self.end_headers()
            return
        if self.path == "/loop":
            self.send_response(302)
            self.send_header("Location", "/loop")
            self.end_headers()
            return
        if self.path == "/cookie":
            self.send_response(200)
            self.send_header("Set-Cookie", "sid=abcdef123456; Path=/")
            self.send_header("Content-Type", "text/plain")
            self.end_headers()
            self.wfile.write(b"cookie")
            return
        if self.path == "/cookie-host":
            self.send_response(200)
            self.send_header(
                "Set-Cookie", "__Host-a=b; Path=/; Domain=exemplo.com",
            )
            self.end_headers()
            self.wfile.write(b"cookie-host")
            return
        if self.path == "/cookie-samesite-none":
            self.send_response(200)
            self.send_header(
                "Set-Cookie",
                "sess=x; Path=/; SameSite=None",
            )
            self.end_headers()
            self.wfile.write(b"cookie-none")
            return
        if self.path == "/headers":
            self._responder(200, b"sem headers", [("Server", "Teste/1.0")])
            return
        if self.path == "/hsts-fraco":
            self._responder(
                200, b"hsts",
                [("Strict-Transport-Security", "max-age=100")],
            )
            return
        if self.path == "/csp-fraco":
            self._responder(
                200, b"csp",
                [("Content-Security-Policy",
                  "default-src 'self' 'unsafe-inline' *")],
            )
            return
        if self.path == "/csp-sem-aspas":
            self._responder(
                200, b"csp",
                [("Content-Security-Policy", "script-src unsafe-inline")],
            )
            return
        if self.path == "/csp-frame-ancestors":
            self._responder(
                200, b"csp",
                [("Content-Security-Policy",
                  "default-src 'self'; frame-ancestors *")],
            )
            return
        if self.path == "/xfo-invalido":
            self._responder(
                200, b"xfo", [("X-Frame-Options", "ALLOWALL")],
            )
            return
        if self.path == "/erro":
            self._responder(
                200,
                b"Traceback (most recent call last):\n"
                b'  File "C:\\\\app.py", line 1, in <module>\n'
                b"ValueError: boom",
            )
            return
        if self.path == "/mixed":
            self._responder(
                200,
                b'<html><script src="http://cdn.example/x.js"></script>'
                b'</html>',
                [("Content-Type", "text/html; charset=utf-8")],
            )
            return
        if self.path == "/cors":
            origem = self.headers.get("Origin", "*")
            self._responder(
                200, b"cors", [("Access-Control-Allow-Origin", origem)],
            )
            return
        if self.path == "/cors-wildcard-creds":
            self._responder(200, b"cors", [
                ("Access-Control-Allow-Origin", "*"),
                ("Access-Control-Allow-Credentials", "true"),
            ])
            return
        if self.path == "/robots.txt":
            self._responder(
                200, b"User-agent: *\nDisallow: /admin\nDisallow: /.env\n",
            )
            return
        if self.path == "/listing/":
            self._responder(200, b"<title>Index of /</title>")
            return
        if self.path in ("/sitemap.xml", "/.well-known/security.txt"):
            self._responder(404, b"nao encontrado")
            return
        if self.path.startswith("/adv-form"):
            qs = urllib.parse.parse_qs(
                urllib.parse.urlparse(self.path).query
            )
            valor = qs.get("q", [""])[0]
            self._responder(
                200,
                (
                    '<html><body>q=' + valor
                    + '<form action="/adv-echo" method="post">'
                    '<input name="q" type="text"></form></body></html>'
                ).encode("utf-8"),
                [("Content-Type", "text/html; charset=utf-8")],
            )
            return
        if self.path == "/com-js":
            self._responder(
                200,
                b'<html><script src="/app.js"></script></html>',
                [("Content-Type", "text/html; charset=utf-8")],
            )
            return
        if self.path == "/app.js":
            self._responder(
                200,
                b'var k="AKIAABCDEFGHIJKLMNOP";\n'
                b"//# sourceMappingURL=app.js.map\n",
                [("Content-Type", "application/javascript")],
            )
            return
        if self.path == "/com-js-2":
            self._responder(
                200,
                b'<html><script src="/lib-a.js"></script>'
                b'<script src="/lib-b.js"></script></html>',
                [("Content-Type", "text/html; charset=utf-8")],
            )
            return
        if self.path == "/lib-a.js":
            # M5: 1o script SEM segredo, so source map (BAIXA).
            self._responder(
                200,
                b"var x=1;\n//# sourceMappingURL=lib-a.js.map\n",
                [("Content-Type", "application/javascript")],
            )
            return
        if self.path == "/lib-b.js":
            # M5: 2o script COM segredo (ALTA) que nao pode ser escondido.
            self._responder(
                200,
                b'var k="AKIAABCDEFGHIJKLMNOP";\n',
                [("Content-Type", "application/javascript")],
            )
            return
        if self.path == "/com-js-3":
            # N3: 2 arquivos JS, cada um com uma AWS key DISTINTA.
            self._responder(
                200,
                b'<html><script src="/seg-a.js"></script>'
                b'<script src="/seg-b.js"></script></html>',
                [("Content-Type", "text/html; charset=utf-8")],
            )
            return
        if self.path == "/seg-a.js":
            self._responder(
                200,
                b'var k="AKIAABCDEFGHIJKLMNOP";\n',
                [("Content-Type", "application/javascript")],
            )
            return
        if self.path == "/seg-b.js":
            self._responder(
                200,
                b'var k="AKIAQRSTUVWXYZABCDEF";\n',
                [("Content-Type", "application/javascript")],
            )
            return
        if self.path == "/.env":
            self._responder(200, b"DB_PASSWORD=supersecret123\n")
            return
        if self.path == "/openapi.json":
            self._responder(
                200,
                b'{"openapi":"3.0.0","paths":{"/x":{}}}',
                [("Content-Type", "application/json")],
            )
            return
        if self.path.startswith("/adv-redirect"):
            qs = urllib.parse.parse_qs(
                urllib.parse.urlparse(self.path).query
            )
            self.send_response(302)
            self.send_header("Location", qs.get("next", ["/"])[0])
            self.end_headers()
            return
        self.send_response(200)
        self.send_header("X-Teste", "ok")
        self.send_header("Content-Type", "text/plain; charset=utf-8")
        self.end_headers()
        self.wfile.write("ola-servidor".encode("utf-8"))

    def do_OPTIONS(self):  # noqa: N802
        self._responder(
            200, b"", [("Allow", "GET, POST, PUT, DELETE, OPTIONS")],
        )

    def do_TRACE(self):  # noqa: N802
        self._responder(200, b"TRACE " + self.path.encode("utf-8"))

    def do_POST(self):  # noqa: N802
        rota = urllib.parse.urlparse(self.path).path
        tamanho = int(self.headers.get("Content-Length") or 0)
        corpo = self.rfile.read(tamanho) if tamanho else b""
        if rota == "/graphql":
            self._responder(
                200,
                b'{"data":{"__schema":{"queryType":{"name":"Query"}}}}',
                [("Content-Type", "application/json")],
            )
            return
        if rota == "/adv-echo":
            self._responder(200, corpo)
            return
        self._responder(404, b"nao encontrado")


@contextlib.contextmanager
def _servidor_local():
    """Sobe um ThreadingHTTPServer em 127.0.0.1:0 e devolve a base URL."""
    srv = ThreadingHTTPServer(("127.0.0.1", 0), _HandlerTeste)
    import threading
    t = threading.Thread(target=srv.serve_forever, daemon=True)
    t.start()
    try:
        yield f"http://127.0.0.1:{srv.server_address[1]}"
    finally:
        srv.shutdown()
        srv.server_close()


# ---------------------------------------------------------------------------
# Casos
# ---------------------------------------------------------------------------

def _caso_1_modelo() -> bool:
    """Finding OBSERVADO sem evidência -> ValueError; com evidência -> ok;
    INFERIDO sem evidência -> ok."""
    try:
        _finding(evidencia="")
        return False  # deveria ter levantado ValueError
    except ValueError:
        pass
    f_obs = _finding(evidencia="raw-123")
    f_inf = _finding(id="teste-2", confianca=Confianca.INFERIDO, evidencia="")
    d = f_obs.to_dict()
    return (
        f_obs.confianca == Confianca.OBSERVADO
        and f_inf.confianca == Confianca.INFERIDO
        and d["evidencia"] == "raw-123"
        and d["severidade"] == "media"
        and d["confianca"] == "observado"
        and d["passos_repro"] == ["passo 1"]
    )


def _caso_2_agrega() -> bool:
    """agrega_severidade: a pior vence; lista vazia -> INFO."""
    return (
        agrega_severidade([
            Severidade.BAIXA, Severidade.ALTA, Severidade.INFO,
        ]) == Severidade.ALTA
        and agrega_severidade([
            Severidade.CRITICA, Severidade.ALTA,
        ]) == Severidade.CRITICA
        and agrega_severidade([]) == Severidade.INFO
        and agrega_severidade(["media", "baixa"]) == Severidade.MEDIA
    )


def _caso_3_relatorio() -> bool:
    """Relatorio.to_dict + resumo_por_severidade."""
    rel = Relatorio(
        alvo="https://exemplo.com",
        perfil="osint",
        data_inicio="2026-01-01T00:00:00+00:00",
        data_fim="2026-01-01T00:00:05+00:00",
        escopo={"hosts": ["exemplo.com"]},
        findings=[
            _finding(id="a", severidade=Severidade.ALTA),
            _finding(id="b", severidade=Severidade.BAIXA),
        ],
        fases_executadas=["passivo"],
    )
    resumo = rel.resumo_por_severidade()
    d = rel.to_dict()
    return (
        resumo["alta"] == 1
        and resumo["baixa"] == 1
        and resumo["critica"] == 0
        and set(resumo) == {"critica", "alta", "media", "baixa", "info"}
        and d["alvo"] == "https://exemplo.com"
        and d["perfil"] == "osint"
        and len(d["findings"]) == 2
        and d["findings"][0]["id"] == "a"
        and d["resumo_severidade"]["alta"] == 1
        and d["fases_executadas"] == ["passivo"]
    )


def _caso_4_perfis() -> bool:
    """perfil_valido / fases_do_perfil / perfil_autorizado (gate do completo)."""
    ok_valido = (
        perfil_valido("osint")
        and perfil_valido("superficial")
        and perfil_valido("completo")
        and not perfil_valido("inexistente")
    )
    ok_fases = (
        fases_do_perfil("osint") == ["passivo"]
        and fases_do_perfil("superficial") == ["passivo", "superficie"]
        and fases_do_perfil("completo") == ["passivo", "superficie", "ativo"]
        and fases_do_perfil("inexistente") == []
    )
    negado, motivo = perfil_autorizado("completo", {})
    liberado_osint, _ = perfil_autorizado("osint", {})
    liberado_sup, _ = perfil_autorizado("superficial", {})
    so_fase, _ = perfil_autorizado("completo", {
        "fases": ["passivo", "superficie", "ativo"], "autorizado": False,
    })
    ok_gate, _ = perfil_autorizado("completo", {
        "fases": ["passivo", "superficie", "ativo"], "autorizado": True,
    })
    return (
        ok_valido and ok_fases
        and negado is False and bool(motivo)
        and so_fase is False
        and ok_gate is True
        and liberado_osint is True and liberado_sup is True
    )


def _caso_5_alvo_valido() -> bool:
    """_alvo_valido: interno bloqueia sem allowlist; libera com allowlist;
    externo libera; allow_loopback libera loopback."""
    with mock.patch("harness.webscraper._host_ips", return_value=["127.0.0.1"]):
        bloqueado_loop = _alvo_valido("http://localhost:8000/x")
        liberado_loop = _alvo_valido("http://localhost:8000/x", allow_loopback=True)
    with mock.patch("harness.webscraper._host_ips", return_value=["192.168.1.10"]):
        bloqueado_priv = _alvo_valido("http://192.168.1.10/x")
    with mock.patch("harness.webscraper._host_ips", return_value=["8.8.8.8"]):
        liberado_externo = _alvo_valido("https://exemplo.com/x")
    with mock.patch("harness.webscraper._host_ips", return_value=["192.168.1.10"]), \
            mock.patch.object(config, "AUDIT_ALLOW_INTERNAL", True), \
            mock.patch.object(config, "ALLOWED_AUDIT_TARGETS", {"alvo.interno"}):
        liberado_allowlist = _alvo_valido("http://alvo.interno/x")
        bloqueado_fora = _alvo_valido("http://outro.interno/x")
    return (
        bloqueado_loop is False
        and liberado_loop is True
        and bloqueado_priv is False
        and liberado_externo is True
        and liberado_allowlist is True
        and bloqueado_fora is False
    )


def _caso_6_redacao() -> bool:
    """mascarar / redigir_header mascaram segredos; valor curto é preservado."""
    m = mascarar("abcdef1234567890")
    return (
        m == "abcd***7890"
        and "***" in m
        and "abcdef1234567890" not in m
        and mascarar("curto") == "curto"
        and "***" in redigir_header("Authorization", "abcdef1234567890")
        and "abcdef1234567890" not in redigir_header(
            "Authorization", "abcdef1234567890")
        and redigir_header("Content-Type", "text/html") == "text/html"
    )


def _caso_7_budget() -> bool:
    """Budget: consome até o teto e nega além; contador/restantes corretos."""
    b = Budget(3)
    primeiros = [b.consumir(), b.consumir(), b.consumir()]
    excedente = b.consumir()
    return (
        primeiros == [True, True, True]
        and excedente is False
        and b.contador == 3
        and b.restantes == 0
        and Budget(0).consumir() is False
    )


def _caso_8_requisicao_local() -> bool:
    """requisicao contra servidor local (allow_loopback): status/headers/body/
    set_cookie; redirect seguido; alvo interno sem allow_loopback -> bloqueado."""
    with _servidor_local() as base:
        r = requisicao(base + "/", allow_loopback=True)
        r_redir = requisicao(base + "/redirect", allow_loopback=True)
        r_cookie = requisicao(base + "/cookie", allow_loopback=True)
        r_bloqueado = requisicao(base + "/", allow_loopback=False)
    return (
        r.status == 200
        and r.headers.get("x-teste") == "ok"
        and "ola-servidor" in r.body
        and r.final_url.rstrip("/") == base.rstrip("/")
        and not r.erro and not r.bloqueado
        and r_redir.status == 200
        and r_redir.final_url.rstrip("/") == base.rstrip("/")
        and any("sid=" in c for c in r_cookie.set_cookie)
        and bool(r_bloqueado.bloqueado)
        and r_bloqueado.status is None
    )


def _caso_9_requisicao_json() -> bool:
    """requisicao_json: JSON válido -> objeto; JSON inválido/erro/5xx -> None."""
    with mock.patch(
        "harness.security.requisicao",
        return_value=Resposta(status=200, body='{"a": 1}', final_url="u"),
    ):
        ok = requisicao_json("https://exemplo.com/j") == {"a": 1}
    with mock.patch(
        "harness.security.requisicao",
        return_value=Resposta(status=200, body="{invalido", final_url="u"),
    ):
        ruim = requisicao_json("https://exemplo.com/j") is None
    with mock.patch(
        "harness.security.requisicao",
        return_value=Resposta(status=500, body="{}", final_url="u"),
    ):
        servidor_erro = requisicao_json("https://exemplo.com/j") is None
    with mock.patch(
        "harness.security.requisicao",
        return_value=Resposta(erro="timeout", final_url="u"),
    ):
        erro = requisicao_json("https://exemplo.com/j") is None
    return ok and ruim and servidor_erro and erro


def _caso_10_audit_email() -> bool:
    """D2: (a) lookup com erro -> sem ausencia e erro registrado; (b) lookup OK
    sem SPF/DMARC -> Findings observados; (c) lookup OK com SPF/DMARC -> nada."""
    # (a) DoH falhou em todas as consultas -> nenhum falso positivo.
    with mock.patch("harness.security.requisicao_json", return_value=None):
        dns_erro = security.audit_dns("exemplo.com")
        sem_erro = audit_email("exemplo.com", dns_erro)
    ok_erro = (
        sem_erro == []
        and bool(dns_erro["erros"])
        and dns_erro["lookup_ok"]["TXT"] is False
    )

    # (b) DoH OK e sem SPF/DMARC -> dois findings de ausencia.
    with mock.patch(
        "harness.security.requisicao_json",
        side_effect=lambda *a, **k: {"Status": 0, "Answer": []},
    ):
        dns_ok = security.audit_dns("exemplo.com")
        sem = audit_email("exemplo.com", dns_ok)
    ids = {f.id for f in sem}
    ok_sem = (
        len(sem) == 2
        and ids == {"email-spf-ausente", "email-dmarc-ausente"}
        and all(f.categoria == "email" for f in sem)
        and all(f.severidade == Severidade.MEDIA for f in sem)
        and all(f.confianca == Confianca.OBSERVADO for f in sem)
        and all(f.cwe == "CWE-290" for f in sem)
        and all(f.stride == "Spoofing" for f in sem)
        and all(f.evidencia.strip() for f in sem)
    )

    # (c) DoH OK com SPF/DMARC -> nenhum finding.
    com = audit_email("exemplo.com", {
        "TXT": ["v=spf1 -all"],
        "lookup_ok": {"TXT": True},
        "DMARC": ["v=DMARC1; p=reject"],
    })
    return ok_erro and ok_sem and com == []


def _caso_11_audit_ct_logs() -> bool:
    """audit_ct_logs (mock): extrai subdomínios únicos, remove wildcard."""
    dados = [
        {"name_value": "a.exemplo.com\nb.exemplo.com"},
        {"name_value": "*.exemplo.com"},
        {"name_value": "a.exemplo.com"},
        {"name_value": "outro.org"},
    ]
    with mock.patch("harness.security.requisicao_json", return_value=dados):
        r = audit_ct_logs("exemplo.com")
    return (
        r["dominio"] == "exemplo.com"
        and r["subdominios"] == ["a.exemplo.com", "b.exemplo.com"]
        and r["erros"] == []
    )


def _caso_12_audit_rdap() -> bool:
    """audit_rdap (mock requisicao_json): bootstrap IANA + consulta ao TLD."""
    boot = {"services": [[["com"], ["https://rdap.verisign.com/com/"]]]}
    dados = {
        "status": ["client transfer prohibited"],
        "entities": [{
            "roles": ["registrar"],
            "vcardArray": ["vcard", [["fn", {}, "text", "Exemplo Registrar"]]],
        }],
        "events": [
            {"eventAction": "registration", "eventDate": "2020-01-01T00:00:00Z"},
            {"eventAction": "expiration", "eventDate": "2025-01-01T00:00:00Z"},
        ],
        "nameservers": [
            {"ldhName": "ns1.exemplo.com"}, {"ldhName": "ns2.exemplo.com"},
        ],
    }
    with mock.patch(
        "harness.security.requisicao_json", side_effect=[boot, dados]
    ):
        r = audit_rdap("exemplo.com")
    return (
        r["tld"] == "com"
        and r["rdap_url"] == "https://rdap.verisign.com/com/domain/exemplo.com"
        and r["registrar"] == "Exemplo Registrar"
        and r["criado"] == "2020-01-01T00:00:00Z"
        and r["expira"] == "2025-01-01T00:00:00Z"
        and r["nameservers"] == ["ns1.exemplo.com", "ns2.exemplo.com"]
        and "client transfer prohibited" in r["status"]
        and "erro" not in r
    )


def _caso_13_redirect_handler() -> bool:
    """_AuditRedirectHandler: interno sem allowlist -> URLError; com allowlist
    -> segue com o novo Request."""
    handler = _AuditRedirectHandler()
    req = urllib.request.Request("https://externo.com/a")
    with mock.patch("harness.security._alvo_valido", return_value=False):
        try:
            handler.redirect_request(
                req, None, 301, "Moved", {}, "http://localhost:8000/x"
            )
            return False
        except urllib.error.URLError:
            bloqueado = True
    with mock.patch("harness.security._alvo_valido", return_value=True):
        novo = handler.redirect_request(
            req, None, 301, "Moved", {}, "https://externo.com/b"
        )
    return (
        bloqueado
        and novo is not None
        and novo.full_url == "https://externo.com/b"
        and handler.max_redirections == security.MAX_REDIRECTS
    )


def _caso_14_wp4_gate_ativo() -> bool:
    """WP5 e GATED: `audit_ativo` nega perfil não-completo e completo sem
    autorização, sem tocar a rede; WP4 implementado não depende de rede com a
    `Resposta` fornecida."""
    url = "https://exemplo.com"
    # Gate: perfil != completo -> []; completo sem autorização -> [].
    negado_superficial = audit_ativo(url, "superficial", {})
    negado_sem_aut = audit_ativo(
        url, "completo", {"fases": ["ativo"], "autorizado": False},
    )
    negado_sem_fase = audit_ativo(
        url, "completo", {"autorizado": True},
    )
    resp_erro = Resposta(
        status=200, headers={},
        body="Traceback (most recent call last): x", final_url=url,
    )
    f_erros = audit_erros_expostos(url, resposta=resp_erro)
    f_mixed_http = audit_mixed_content(
        "http://exemplo.com", resposta=Resposta(status=200, body="x"),
    )
    return (
        negado_superficial == []
        and negado_sem_aut == []
        and negado_sem_fase == []
        and isinstance(f_erros, list)
        and any(f.id == "erro-traceback-python" for f in f_erros)
        and f_mixed_http == []
    )


def _caso_15_cli_sem_perfil() -> bool:
    """CLI sem --perfil -> erro claro e exit 1 (o módulo NÃO assume o perfil)."""
    buf = io.StringIO()
    with contextlib.redirect_stdout(buf):
        codigo = security.main(["https://exemplo.com"])
    saida = buf.getvalue()
    return (
        codigo == 1
        and "--perfil" in saida
        and "osint" in saida and "superficial" in saida and "completo" in saida
    )


def _caso_16_self_test() -> bool:
    """--self-test retorna 0 e imprime SELF-TEST OK."""
    buf = io.StringIO()
    with contextlib.redirect_stdout(buf):
        codigo = security.main(["--self-test"])
    return codigo == 0 and "SELF-TEST OK" in buf.getvalue()


def _caso_17_audit_cabecalhos() -> bool:
    """CHECK A: headers ausentes/fraco, CSP permissiva e banner (local).

    B11: HSTS ausente só é reportado em alvo https; o alvo local é http, então
    o finding NÃO aparece. A ausência em https é confirmada com uma `Resposta`
    fornecida (sem rede)."""
    with _servidor_local() as base:
        f = audit_cabecalhos(base + "/headers", allow_loopback=True)
        f_hsts = audit_cabecalhos(base + "/hsts-fraco", allow_loopback=True)
        f_csp = audit_cabecalhos(base + "/csp-fraco", allow_loopback=True)
    ids = {x.id for x in f}
    ids_csp = {x.id for x in f_csp}
    esperados = {
        "header-csp-ausente", "header-xfo-ausente",
        "header-xcto-ausente", "header-referrer-policy-ausente",
        "header-permissions-policy-ausente", "header-banner-server",
    }
    f_https = audit_cabecalhos(
        "https://exemplo.com",
        resposta=Resposta(status=200, headers={}, final_url="https://exemplo.com"),
    )
    return (
        esperados <= ids
        and "header-hsts-ausente" not in ids
        and any(x.id == "header-hsts-ausente" for x in f_https)
        and all(x.confianca == Confianca.OBSERVADO and x.evidencia.strip()
                for x in f)
        and any(x.id == "header-hsts-fraco"
                and x.severidade == Severidade.BAIXA for x in f_hsts)
        and "header-csp-unsafe-inline" in ids_csp
        and "header-csp-wildcard" in ids_csp
    )


def _caso_18_audit_cookies() -> bool:
    """CHECK B: flags ausentes, SameSite=None sem Secure e prefixo __Host-."""
    with _servidor_local() as base:
        f = audit_cookies(base + "/cookie", allow_loopback=True)
        f_host = audit_cookies(base + "/cookie-host", allow_loopback=True)
        f_none = audit_cookies(
            base + "/cookie-samesite-none", allow_loopback=True,
        )
    ids = {x.id for x in f}
    return (
        {"cookie-sem-secure", "cookie-sem-httponly", "cookie-sem-samesite"}
        <= ids
        and all(x.confianca == Confianca.OBSERVADO and x.evidencia.strip()
                for x in f)
        and any(x.id == "cookie-prefixo-host-invalido" for x in f_host)
        and any(x.id == "cookie-samesite-none-sem-secure"
                and x.severidade == Severidade.ALTA for x in f_none)
    )


def _caso_19_audit_erros_expostos() -> bool:
    """CHECK E: traceback + caminho Windows expostos (local) -> ALTA/MEDIA."""
    with _servidor_local() as base:
        f = audit_erros_expostos(base + "/erro", allow_loopback=True)
    ids = {x.id for x in f}
    return (
        "erro-traceback-python" in ids
        and "erro-caminho-windows" in ids
        and all(x.severidade in (Severidade.ALTA, Severidade.MEDIA) for x in f)
        and all(x.evidencia.strip() for x in f)
    )


def _caso_20_audit_well_known() -> bool:
    """CHECK F: robots sensível, security.txt ausente e dict de arquivos."""
    with _servidor_local() as base:
        f, dados = audit_well_known(base, allow_loopback=True)
    ids = {x.id for x in f}
    return (
        "well-known-security-txt-ausente" in ids
        and "well-known-robots-sensivel" in ids
        and dados["/robots.txt"]["encontrado"] is True
        and dados["/sitemap.xml"]["encontrado"] is False
        and dados["/.well-known/security.txt"]["encontrado"] is False
        and all(x.evidencia.strip() for x in f)
    )


def _caso_21_audit_cors_passivo() -> bool:
    """CHECK G: reflexão de Origin e wildcard+credenciais -> ALTA."""
    with _servidor_local() as base:
        f_ref = audit_cors_passivo(base + "/cors", allow_loopback=True)
        f_cred = audit_cors_passivo(
            base + "/cors-wildcard-creds", allow_loopback=True,
        )
        f_sem = audit_cors_passivo(base + "/headers", allow_loopback=True)
    return (
        any(x.id == "cors-origem-refletida"
            and x.severidade == Severidade.ALTA for x in f_ref)
        and any(x.id == "cors-wildcard-credenciais"
                and x.severidade == Severidade.ALTA for x in f_cred)
        and f_sem == []
    )


def _caso_22_audit_mixed_content() -> bool:
    """CHECK H: recursos http:// em página https e caso limpo."""
    with _servidor_local() as base:
        r = requisicao(base + "/mixed", allow_loopback=True)
        f = audit_mixed_content(
            base.replace("http://", "https://") + "/mixed", resposta=r,
        )
        f_limpo = audit_mixed_content(
            "https://exemplo.com",
            resposta=Resposta(
                status=200, body='<img src="https://ok/x.png">',
            ),
        )
    return (
        any(x.id == "mixed-content-http"
            and x.severidade == Severidade.MEDIA for x in f)
        and f_limpo == []
        and _urls_mistas(r.body) == ["http://cdn.example/x.js"]
    )


def _caso_23_audit_redirects() -> bool:
    """CHECK D: cadeia limpa, loop (local) e downgrade/cross (helper)."""
    with _servidor_local() as base:
        f_limpo = audit_redirects(base + "/redirect", allow_loopback=True)
        f_loop = audit_redirects(base + "/loop", allow_loopback=True)
    f_down = _avaliar_cadeia_redirects(
        "https://a.com/", [(301, "http://a.com/")], "http://a.com/", 200,
    )
    f_cross = _avaliar_cadeia_redirects(
        "https://a.com/", [(302, "https://b.com/")], "https://b.com/", 200,
    )
    # B11: www <-> apex do MESMO domínio NÃO é cross-domain (sem ruído).
    f_www = _avaliar_cadeia_redirects(
        "https://a.com/", [(302, "https://www.a.com/")],
        "https://www.a.com/", 200,
    )
    f_apex = _avaliar_cadeia_redirects(
        "https://www.a.com/", [(302, "https://a.com/")],
        "https://a.com/", 200,
    )
    return (
        f_limpo == []
        and any(x.id == "redirect-loop" for x in f_loop)
        and any(x.id == "redirect-downgrade"
                and x.severidade == Severidade.ALTA for x in f_down)
        and any(x.id == "redirect-cross-domain"
                and x.severidade == Severidade.BAIXA for x in f_cross)
        and not any(x.id == "redirect-cross-domain" for x in (f_www + f_apex))
        and _dominio_base("www.a.com") == _dominio_base("a.com")
        and all(x.evidencia.strip() for x in (f_loop + f_down + f_cross))
    )


def _caso_24_audit_tls_helpers() -> bool:
    """CHECK C: helpers puros (expiração/cipher/versão) com datas/mocks."""
    from datetime import datetime, timezone
    agora = datetime(2026, 1, 1, tzinfo=timezone.utc)
    dias_ok = _dias_para_expirar("Jan 10 00:00:00 2026 GMT", agora)
    expirado = _dias_para_expirar("Dec 01 00:00:00 2025 GMT", agora)
    invalido = _dias_para_expirar("data-invalida", agora)
    with _servidor_local() as base:
        porta = int(base.rsplit(":", 1)[1])
        versao_falsa = _handshake_versao(
            "127.0.0.1", porta, ssl.TLSVersion.TLSv1,
        )
    return (
        dias_ok is not None and abs(dias_ok - 9.0) < 0.01
        and expirado is not None and expirado < 0
        and invalido is None
        and _cipher_fraco("RC4-SHA") is True
        and _cipher_fraco("DES-CBC3-SHA") is True
        and _cipher_fraco("NULL-SHA") is True
        and _cipher_fraco("ECDHE-RSA-AES256-GCM-SHA384") is False
        and versao_falsa is False
    )


def _caso_25_audit_superficie() -> bool:
    """AGREGADOR: roda A-H sem derrubar e consolida findings (local). A1: em
    alvo http sem --porta, o TLS é pulado (sem falso positivo de handshake)."""
    with _servidor_local() as base:
        f = audit_superficie(
            base + "/headers", "superficial", allow_loopback=True,
        )
    ids = {x.id for x in f}
    return (
        isinstance(f, list) and len(f) >= 5
        and all(isinstance(x, Finding) for x in f)
        and {"header-xfo-ausente", "header-csp-ausente"} <= ids
        and not any(x.categoria == "tls" for x in f)
    )


def _caso_26_audit_metodos() -> bool:
    """ATIVO: OPTIONS com metodos perigosos e TRACE ecoando -> MEDIA."""
    with _servidor_local() as base:
        f = audit_metodos(base + "/adv", allow_loopback=True)
    ids = {x.id for x in f}
    return (
        "ativo-metodos-perigosos" in ids
        and "ativo-trace-habilitado" in ids
        and all(x.severidade == Severidade.MEDIA for x in f)
        and all(x.confianca == Confianca.OBSERVADO and x.evidencia.strip()
                for x in f)
        and any("Allow" in x.evidencia for x in f)
    )


def _caso_27_audit_cors_ativo() -> bool:
    """ATIVO: CORS reflete a Origin canario e wildcard+creds -> ALTA."""
    with _servidor_local() as base:
        f_ref = audit_cors_ativo(base + "/cors", allow_loopback=True)
        f_cred = audit_cors_ativo(
            base + "/cors-wildcard-creds", allow_loopback=True,
        )
        f_sem = audit_cors_ativo(base + "/headers", allow_loopback=True)
    return (
        any(x.id == "cors-origem-refletida"
            and x.severidade == Severidade.ALTA for x in f_ref)
        and any(x.id == "cors-wildcard-credenciais"
                and x.severidade == Severidade.ALTA for x in f_cred)
        and f_sem == []
        and all(x.evidencia.strip() for x in (f_ref + f_cred))
    )


def _caso_28_audit_reflexao() -> bool:
    """ATIVO: canario refletido por query -> MEDIA. M6: POST de formulario é
    opt-in — por padrão NÃO é sondado; com `permitir_post=True` reflete."""
    with _servidor_local() as base:
        f_q = audit_reflexao(base + "/adv-form?q=x", allow_loopback=True)
        f_form_padrao = audit_reflexao(base + "/adv-form", allow_loopback=True)
        f_form_post = audit_reflexao(
            base + "/adv-form", allow_loopback=True, permitir_post=True,
        )
        f_limpo = audit_reflexao(base + "/headers", allow_loopback=True)
    return (
        any(x.id == "ativo-reflexao" for x in f_q)
        and all(x.severidade == Severidade.MEDIA and x.cwe == "CWE-79"
                for x in f_q)
        and f_form_padrao == []
        and any(x.id == "ativo-reflexao" for x in f_form_post)
        and f_limpo == []
    )


def _caso_29_audit_open_redirect() -> bool:
    """ATIVO: parametro controla Location para o canario -> ALTA (CWE-601)."""
    with _servidor_local() as base:
        f = audit_open_redirect(
            base + "/adv-redirect?next=/x", allow_loopback=True,
        )
        f_sem = audit_open_redirect(base + "/headers", allow_loopback=True)
    return (
        any(x.id == "ativo-open-redirect"
            and x.severidade == Severidade.ALTA for x in f)
        and any("canario-audit.example" in x.evidencia for x in f)
        and all(x.cwe == "CWE-601" for x in f)
        and f_sem == []
    )


def _caso_30_audit_graphql() -> bool:
    """ATIVO: introspeccao GraphQL respondida -> MEDIA (CWE-200)."""
    with _servidor_local() as base:
        f = audit_graphql(base, allow_loopback=True)
    return (
        any(x.id == "ativo-graphql-introspeccao"
            and x.severidade == Severidade.MEDIA for x in f)
        and all(x.categoria == "graphql" for x in f)
        and all(x.evidencia.strip() for x in f)
    )


def _caso_31_audit_openapi() -> bool:
    """ATIVO: spec OpenAPI exposta -> BAIXA/MEDIA com URL na evidencia."""
    with _servidor_local() as base:
        f = audit_openapi(base, allow_loopback=True)
    return (
        any(x.id == "ativo-openapi-exposto"
            and x.categoria == "openapi" for x in f)
        and any("openapi.json" in x.evidencia for x in f)
        and all(x.severidade in (Severidade.BAIXA, Severidade.MEDIA)
                for x in f)
    )


def _caso_32_audit_paths_sensiveis() -> bool:
    """ATIVO: .env exposto -> CRITICA, com valor REDIGIDO (nunca o segredo)."""
    with _servidor_local() as base:
        f = audit_paths_sensiveis(base, allow_loopback=True)
    env = [x for x in f if x.id == "ativo-path-env"]
    return (
        bool(env)
        and any(x.severidade == Severidade.CRITICA for x in env)
        and all("supersecret123" not in x.evidencia for x in f)
        and all("REDIGIDO" in x.evidencia for x in f)
        and all(x.cwe == "CWE-538" for x in f)
    )


def _caso_33_audit_portas() -> bool:
    """ATIVO: porta aberta -> INFO; porta fechada -> nenhum Finding."""
    with _servidor_local() as base:
        porta = int(base.rsplit(":", 1)[1])
        abertas = audit_portas(
            "127.0.0.1", portas=(porta,), allow_loopback=True,
        )
        fechadas = audit_portas(
            "127.0.0.1", portas=(1,), allow_loopback=True,
        )
    return (
        any(x.severidade == Severidade.INFO for x in abertas)
        and all(x.categoria == "portas" for x in abertas)
        and all(str(porta) in x.evidencia for x in abertas)
        and fechadas == []
    )


def _caso_34_audit_segredos_js() -> bool:
    """ATIVO: segredo em JS -> ALTA REDIGIDO; source map -> BAIXA."""
    with _servidor_local() as base:
        f = audit_segredos_js(base + "/com-js", allow_loopback=True)
    seg = [
        x for x in f
        if x.id.startswith("ativo-segredo-js-aws-access-key-")
    ]
    return (
        bool(seg)
        and any(x.severidade == Severidade.ALTA for x in seg)
        and all("AKIAABCDEFGHIJKLMNOP" not in x.evidencia for x in f)
        and all("valor=REDIGIDO" in x.evidencia for x in seg)
        and any("posicao~=" in x.evidencia for x in seg)
        and any(x.id == "ativo-source-map-exposto"
                and x.severidade == Severidade.BAIXA for x in f)
    )


def _caso_35_audit_ativo_gate() -> bool:
    """ATIVO GATED: nega sem perfil completo/autorizacao e roda autorizado."""
    with _servidor_local() as base:
        negado_sup = audit_ativo(base, "superficial", {})
        negado_sem_aut = audit_ativo(base, "completo", {"fases": ["ativo"]})
        liberado = audit_ativo(
            base + "/adv-form?q=x", "completo",
            {"fases": ["passivo", "superficie", "ativo"], "autorizado": True},
            allow_loopback=True,
        )
    return (
        negado_sup == []
        and negado_sem_aut == []
        and isinstance(liberado, list) and len(liberado) >= 4
        and all(isinstance(x, Finding) for x in liberado)
        and any(x.id == "ativo-reflexao" for x in liberado)
        and any(x.id == "ativo-metodos-perigosos" for x in liberado)
    )


def _caso_36_audit_componentes_osv() -> bool:
    """ATIVO: consulta OSV (mock) de lib versionada -> ALTA/MEDIA."""
    html = '<script src="https://cdn.exemplo/jquery-3.4.1.min.js"></script>'
    resposta = Resposta(
        status=200,
        body=json.dumps({
            "vulns": [{
                "id": "GHSA-teste-0001",
                "database_specific": {"severity": "HIGH"},
            }],
        }),
        final_url="https://api.osv.dev/v1/query",
    )
    with mock.patch(
        "harness.security.requisicao", return_value=resposta,
    ) as chamada:
        f = audit_componentes_osv(
            "https://exemplo.com", html, allow_loopback=True,
        )
    return (
        any(x.id == "ativo-componente-vulneravel-jquery" for x in f)
        and any(x.severidade == Severidade.ALTA for x in f)
        and any("jquery@3.4.1" in x.evidencia for x in f)
        and chamada.called
    )


def _caso_37_relatorio_artefatos() -> bool:
    """gerar_relatorio cria README.md, resultados.md, achados.json e raw/."""
    with tempfile.TemporaryDirectory() as tmp:
        with mock.patch.object(config, "AUDIT_REPORT_DIR", pathlib.Path(tmp)):
            d = security.gerar_relatorio(
                "https://exemplo.com", "superficial",
                [
                    _finding(id="obs-1", categoria="headers"),
                    _finding(id="inf-1", confianca=Confianca.INFERIDO,
                             evidencia=""),
                ],
            )
            ok = (
                (d / "README.md").is_file()
                and (d / "resultados.md").is_file()
                and (d / "achados.json").is_file()
                and (d / "raw").is_dir()
            )
    return ok


def _caso_38_achados_json_hash() -> bool:
    """achados.json tem schema/hash estavel; 2 chamadas -> mesmo hash."""
    with tempfile.TemporaryDirectory() as tmp:
        with mock.patch.object(config, "AUDIT_REPORT_DIR", pathlib.Path(tmp)):
            fs = [
                _finding(id="b", severidade=Severidade.ALTA),
                _finding(id="a", severidade=Severidade.BAIXA),
            ]
            d1 = security.gerar_relatorio("https://exemplo.com", "superficial", fs)
            j1 = json.loads((d1 / "achados.json").read_text(encoding="utf-8"))
            d2 = security.gerar_relatorio("https://exemplo.com", "superficial", fs)
            j2 = json.loads((d2 / "achados.json").read_text(encoding="utf-8"))
    return (
        j1["schema"] == "harness.security.relatorio/2.0"
        and isinstance(j1["hash_findings"], str)
        and len(j1["hash_findings"]) == 64
        and len(j1["findings"]) == 2
        and j1["hash_findings"] == j2["hash_findings"]
        and j1["resumo_severidade"]["alta"] == 1
        and j1["resumo_severidade"]["baixa"] == 1
        and j1["veredito_por_categoria"]["teste"] == "reprovou"
        and j1["versao_ferramenta"] == security.VERSAO
    )


def _caso_39_readme_contexto_nao_verificado() -> bool:
    """README marca contexto_informado como NAO verificado (nunca fato)."""
    with tempfile.TemporaryDirectory() as tmp:
        with mock.patch.object(config, "AUDIT_REPORT_DIR", pathlib.Path(tmp)):
            d = security.gerar_relatorio(
                "https://exemplo.com", "superficial", [_finding(id="obs-1")],
                contexto_informado=["A empresa usa Cloudflare"],
            )
            readme = (d / "README.md").read_text(encoding="utf-8")
            j = json.loads((d / "achados.json").read_text(encoding="utf-8"))
    pos_sec = readme.find("Contexto informado")
    pos_txt = readme.find("A empresa usa Cloudflare")
    return (
        "NÃO verificado" in readme
        and "informado pelo solicitante" in readme
        and pos_sec != -1 and pos_txt != -1 and pos_sec < pos_txt
        and j["contexto_informado"] == [
            {"texto": "A empresa usa Cloudflare", "verificado": False}
        ]
    )


def _caso_40_readme_inferencias_separadas() -> bool:
    """README separa achados observados de inferencias nao verificadas."""
    with tempfile.TemporaryDirectory() as tmp:
        with mock.patch.object(config, "AUDIT_REPORT_DIR", pathlib.Path(tmp)):
            d = security.gerar_relatorio(
                "https://exemplo.com", "superficial",
                [
                    _finding(id="obs-1"),
                    _finding(id="inf-1", confianca=Confianca.INFERIDO,
                             evidencia=""),
                ],
            )
            readme = (d / "README.md").read_text(encoding="utf-8")
    pos_obs = readme.find("## Achados (observados)")
    pos_inf = readme.find("## Inferências")
    pos_obs_id = readme.find("[obs-1]")
    pos_inf_id = readme.find("[inf-1]")
    return (
        pos_obs != -1 and pos_inf != -1 and pos_obs < pos_inf
        and pos_obs_id != -1 and pos_inf_id != -1
        and pos_obs_id < pos_inf
        and pos_inf_id > pos_inf
    )


def _caso_41_raw_redacao() -> bool:
    """Segredo plantado em capturas NAO aparece em nenhum arquivo de raw/."""
    segredo = "SECRET12345"
    jwt = "eyJhbGciOiJIUzI1NiJ9.eyJzdWIiOiIxIn0.abcdef"
    capturas = {
        "cabecalhos.txt": (
            "HTTP/1.1 200 OK\nAuthorization: Bearer " + segredo + "\n"
            "Set-Cookie: sid=abc123456; HttpOnly\nCookie: sess=" + segredo + "\n"
        ),
        "tls.txt": "JWT " + jwt,
        "body-x.html": "api_key=" + segredo,
    }
    with tempfile.TemporaryDirectory() as tmp:
        with mock.patch.object(config, "AUDIT_REPORT_DIR", pathlib.Path(tmp)):
            d = security.gerar_relatorio(
                "https://exemplo.com", "superficial", [_finding(id="obs-1")],
                capturas=capturas,
            )
            arquivos = list((d / "raw").iterdir())
            conteudos = "".join(p.read_text(encoding="utf-8") for p in arquivos)
            nomes = {p.name for p in arquivos}
    return (
        segredo not in conteudos
        and jwt not in conteudos
        and "[REDIGIDO]" in conteudos
        and {"cabecalhos.txt", "tls.txt", "body-x.html"} <= nomes
    )


def _caso_42_diff_runs() -> bool:
    """1a execucao diz 'primeira execução'; 2a detecta novos/resolvidos."""
    with tempfile.TemporaryDirectory() as tmp:
        with mock.patch.object(config, "AUDIT_REPORT_DIR", pathlib.Path(tmp)):
            d = security.gerar_relatorio(
                "https://exemplo.com", "superficial",
                [
                    _finding(id="obs-a", severidade=Severidade.ALTA),
                    _finding(id="obs-b", severidade=Severidade.MEDIA),
                ],
            )
            runs1 = sorted(
                [p for p in (d / "runs").iterdir() if p.is_dir()],
                key=lambda p: p.name,
            )
            diff1 = (runs1[-1] / "diff.md").read_text(encoding="utf-8")
            d2 = security.gerar_relatorio(
                "https://exemplo.com", "superficial",
                [
                    _finding(id="obs-b", severidade=Severidade.MEDIA),
                    _finding(id="obs-c", severidade=Severidade.BAIXA),
                ],
            )
            runs2 = sorted(
                [p for p in (d2 / "runs").iterdir() if p.is_dir()],
                key=lambda p: p.name,
            )
            diff2 = (runs2[-1] / "diff.md").read_text(encoding="utf-8")
    return (
        "primeira execução" in diff1
        and "obs-c (baixa)" in diff2
        and "obs-a (alta)" in diff2
        and "obs-b (media)" in diff2
        and "## Novos" in diff2
        and "## Resolvidos" in diff2
        and "## Persistentes" in diff2
    )


def _caso_43_resultados_golden() -> bool:
    """resultados.md: linha da categoria com o veredito correto (golden leve)."""
    with tempfile.TemporaryDirectory() as tmp:
        with mock.patch.object(config, "AUDIT_REPORT_DIR", pathlib.Path(tmp)):
            d = security.gerar_relatorio(
                "https://exemplo.com", "superficial",
                [
                    _finding(id="header-hsts-ausente", categoria="headers",
                             severidade=Severidade.MEDIA),
                    _finding(id="info-x", categoria="well-known",
                             severidade=Severidade.INFO),
                ],
            )
            res = (d / "resultados.md").read_text(encoding="utf-8")
    return (
        "| headers | reprovou |" in res
        and "| well-known | passou |" in res
        and "## Reprovados (severidade >= media)" in res
        and "header-hsts-ausente" in res
    )


def _caso_44_porta_tls_padrao() -> bool:
    """A1: _porta_tls_padrao mapeia esquema (https->443, http->80); o agregador
    chama audit_tls com 443 em https, PULA TLS em http sem --porta e roda TLS
    em http quando a porta é explícita."""
    chamadas: list[tuple[str, int]] = []

    def _fake_tls(host, porta=443, allow_loopback=False):
        chamadas.append((str(host), int(porta)))
        return []

    vazia = Resposta(status=200, headers={}, final_url="https://exemplo.com")
    with mock.patch("harness.security.audit_tls", side_effect=_fake_tls), \
         mock.patch("harness.security._buscar", return_value=vazia), \
         mock.patch("harness.security.audit_cabecalhos", return_value=[]), \
         mock.patch("harness.security.audit_cookies", return_value=[]), \
         mock.patch("harness.security.audit_erros_expostos", return_value=[]), \
         mock.patch("harness.security.audit_mixed_content", return_value=[]), \
         mock.patch("harness.security.audit_cors_passivo", return_value=[]), \
         mock.patch("harness.security.audit_redirects", return_value=[]), \
         mock.patch("harness.security.audit_well_known", return_value=([], {})):
        security.audit_superficie("https://exemplo.com", "superficial")
        n_https = len(chamadas)
        security.audit_superficie("http://exemplo.com", "superficial")
        n_http = len(chamadas)
        security.audit_superficie(
            "http://exemplo.com:8443", "superficial", porta=8443,
        )
    return (
        _porta_tls_padrao("https://exemplo.com") == 443
        and _porta_tls_padrao("http://exemplo.com") == 80
        and _porta_tls_padrao("https://exemplo.com:8443") == 8443
        and n_https == 1 and ("exemplo.com", 443) in chamadas
        and n_http == 1  # http sem --porta: TLS pulado
        and ("exemplo.com", 8443) in chamadas  # porta explícita força TLS
    )


def _caso_45_redacao_json() -> bool:
    """A2: redigir_conteudo mascara chave/valor entre aspas (JSON)."""
    j1 = redigir_conteudo('{"api_key": "SECRET12345"}')
    j2 = redigir_conteudo('"Set-Cookie": "sid=SECRET12345; HttpOnly"')
    j3 = redigir_conteudo('{"password": "SECRET12345"}')
    j4 = redigir_conteudo('{"token": "SECRET12345"}')
    return (
        "SECRET12345" not in j1
        and "SECRET12345" not in j2
        and "SECRET12345" not in j3
        and "SECRET12345" not in j4
        and "[REDIGIDO]" in j1
        and "[REDIGIDO]" in j2
    )


def _caso_46_findings_redigidos_saida() -> bool:
    """M3: segredo em evidencia/descricao/recomendacao NAO vaza para os
    artefatos versionados (achados.json, README.md, resultados.md)."""
    segredo = "supersecret123"
    f = _finding(
        id="vaza",
        evidencia=f"Traceback ... DB_PASSWORD={segredo}",
        descricao=f"corpo continha DB_PASSWORD={segredo}",
        recomendacao=f"remover DB_PASSWORD={segredo}",
    )
    with tempfile.TemporaryDirectory() as tmp:
        with mock.patch.object(config, "AUDIT_REPORT_DIR", pathlib.Path(tmp)):
            d = security.gerar_relatorio(
                "https://exemplo.com", "superficial", [f],
            )
            textos = {
                nome: (d / nome).read_text(encoding="utf-8")
                for nome in ("achados.json", "README.md", "resultados.md")
            }
    return (
        all(segredo not in t for t in textos.values())
        and all("[REDIGIDO]" in t for t in textos.values())
    )


def _caso_47_hash_estavel_canario() -> bool:
    """M4: hash_findings depende SÓ da identidade (id/categoria/severidade/
    confianca/titulo normalizado); canário em evidencia/passos/origem não muda.
    2 execuções com canários diferentes -> mesmo hash_findings."""
    f1 = _finding(
        id="ativo-reflexao", categoria="reflexao", titulo="Reflexao",
        evidencia="... canario A ...", passos_repro=["enviar A"],
    )
    f2 = _finding(
        id="ativo-reflexao", categoria="reflexao", titulo="  Reflexao  ",
        evidencia="... canario B ...", passos_repro=["enviar B"],
        origem="https://outro.example/",
    )
    direto = (
        _hash_findings([f1]) == _hash_findings([f2])
        and len(_hash_findings([f1])) == 64
    )
    with tempfile.TemporaryDirectory() as tmp:
        with mock.patch.object(config, "AUDIT_REPORT_DIR", pathlib.Path(tmp)):
            d1 = security.gerar_relatorio(
                "https://exemplo.com", "superficial", [f1],
            )
            h1 = json.loads(
                (d1 / "achados.json").read_text(encoding="utf-8")
            )["hash_findings"]
            d2 = security.gerar_relatorio(
                "https://exemplo.com", "superficial", [f2],
            )
            h2 = json.loads(
                (d2 / "achados.json").read_text(encoding="utf-8")
            )["hash_findings"]
    return direto and h1 == h2


def _caso_48_segredos_js_acumula() -> bool:
    """M5: não para no 1o script com finding; source map (BAIXA) no 1o JS não
    esconde segredo (ALTA) no 2o."""
    with _servidor_local() as base:
        f = audit_segredos_js(base + "/com-js-2", allow_loopback=True)
    ids = {x.id for x in f}
    return (
        "ativo-source-map-exposto" in ids
        and any(i.startswith("ativo-segredo-js-aws-access-key-") for i in ids)
        and all("AKIAABCDEFGHIJKLMNOP" not in x.evidencia for x in f)
    )


def _caso_49_reflexao_post_optin() -> bool:
    """M6: POST é opt-in (default OFF); sem a flag nenhum POST é enviado."""
    with _servidor_local() as base:
        padrao = audit_reflexao(base + "/adv-form", allow_loopback=True)
        com_post = audit_reflexao(
            base + "/adv-form", allow_loopback=True, permitir_post=True,
        )
    return padrao == [] and any(x.id == "ativo-reflexao" for x in com_post)


def _caso_50_cabecalhos_xfo_csp() -> bool:
    """B8: X-Frame-Options inválido (ALLOWALL) e CSP frame-ancestors * ->
    findings de clickjacking."""
    with _servidor_local() as base:
        f_xfo = audit_cabecalhos(base + "/xfo-invalido", allow_loopback=True)
        f_csp = audit_cabecalhos(
            base + "/csp-frame-ancestors", allow_loopback=True,
        )
    return (
        any(x.id == "header-xfo-invalido"
            and x.severidade == Severidade.MEDIA for x in f_xfo)
        and any(x.id == "header-csp-frame-ancestors-wildcard" for x in f_csp)
    )


def _caso_51_csp_unsafe_sem_aspas() -> bool:
    """B11: CSP com unsafe-inline SEM aspas também é detectada."""
    with _servidor_local() as base:
        f = audit_cabecalhos(base + "/csp-sem-aspas", allow_loopback=True)
    return any(x.id == "header-csp-unsafe-inline" for x in f)


def _caso_52_cookie_mascara_sempre() -> bool:
    """B11: mascarar_sempre mascara o valor de cookie mesmo curto (<= 8), e
    `redigir_header` de Set-Cookie nunca exibe o valor."""
    return (
        mascarar_sempre("b") == "*"
        and mascarar_sempre("") == ""
        and "abcdef123456" not in mascarar_sempre("abcdef123456")
        and "*" in mascarar_sempre("abcdef123456")
        and "abc" not in redigir_header("Set-Cookie", "sid=abc")
        and "abc" not in redigir_header("Cookie", "s=abc")
    )


def _caso_53_slug_seguro_reservado() -> bool:
    """B10: _slug_seguro nunca devolve nome reservado do Windows."""
    return (
        _slug_seguro("CON").upper() != "CON"
        and _slug_seguro("com1").upper() != "COM1"
        and _slug_seguro("CON").startswith("con")
        and _slug_seguro("...") == "alvo"
    )


def _caso_54_contexto_redigido_artefatos() -> bool:
    """N1: segredo em contexto_informado NAO vaza em README/achados.json/runs."""
    segredo = "CTXSECRET999"
    with tempfile.TemporaryDirectory() as tmp:
        with mock.patch.object(config, "AUDIT_REPORT_DIR", pathlib.Path(tmp)):
            d = security.gerar_relatorio(
                "https://exemplo.com", "superficial", [_finding(id="obs-1")],
                contexto_informado=["DB_PASSWORD=" + segredo],
            )
            textos = {
                nome: (d / nome).read_text(encoding="utf-8")
                for nome in ("README.md", "achados.json", "resultados.md")
            }
            runs_txt = "".join(
                p.read_text(encoding="utf-8")
                for r in (d / "runs").iterdir() for p in r.iterdir()
            )
    return (
        all(segredo not in t for t in textos.values())
        and segredo not in runs_txt
        and "[REDIGIDO]" in textos["README.md"]
        and "[REDIGIDO]" in textos["achados.json"]
    )


def _caso_55_stdout_findings_redigidos() -> bool:
    """N2: o JSON impresso no stdout nao contem segredo presente no finding."""
    segredo = "STDOUTSECRET123"
    f = _finding(
        id="vaza-stdout",
        evidencia="DB_PASSWORD=" + segredo,
        descricao="campo DB_PASSWORD=" + segredo,
    )
    buf = io.StringIO()
    with mock.patch(
        "harness.security.executar_fase_passiva", return_value=([f], {}),
    ), contextlib.redirect_stdout(buf):
        codigo = security.main(["https://exemplo.com", "--perfil", "osint"])
    saida = buf.getvalue()
    return (
        codigo == 0
        and segredo not in saida
        and "[REDIGIDO]" in saida
        and "vaza-stdout" in saida
    )


def _caso_56_segredos_js_dois_arquivos() -> bool:
    """N3: 2 arquivos JS com AWS keys distintas -> 2 findings (sem dedup)."""
    with _servidor_local() as base:
        f = audit_segredos_js(base + "/com-js-3", allow_loopback=True)
    segs = [
        x for x in f if x.id.startswith("ativo-segredo-js-aws-access-key-")
    ]
    return (
        len(segs) == 2
        and len({x.id for x in segs}) == 2
        and all("AKIA" not in x.evidencia for x in segs)
    )


def _caso_57_csp_frame_ancestors_sem_wildcard() -> bool:
    """N4: CSP so com 'frame-ancestors *' -> apenas o finding de
    frame-ancestors (MEDIA); NAO gera header-csp-wildcard (ALTA)."""
    with _servidor_local() as base:
        f = audit_cabecalhos(base + "/csp-frame-ancestors", allow_loopback=True)
    ids = {x.id for x in f}
    return (
        "header-csp-frame-ancestors-wildcard" in ids
        and "header-csp-wildcard" not in ids
    )


def _caso_58_rsc_sem_falso_positivo_caminho() -> bool:
    """D1: payload RSC do Next.js com 'd:\\"' NAO casa caminho Windows."""
    corpo = (
        '1d:\\"$Sreact.suspense\\"\n'
        '3:D:"$\n'
        '4:d:\\"OutletBoundary\\"\n'
    )
    f = audit_erros_expostos(
        "https://exemplo.com",
        resposta=Resposta(
            status=200, body=corpo, final_url="https://exemplo.com",
        ),
    )
    return not any(x.id == "erro-caminho-windows" for x in f)


def _caso_59_caminho_windows_real_detectado() -> bool:
    """D1: caminho Windows real (C:\\Users\\app\\server.py) -> finding MEDIA."""
    f = audit_erros_expostos(
        "https://exemplo.com",
        resposta=Resposta(
            status=200,
            body='File "C:\\Users\\app\\server.py", line 3',
            final_url="https://exemplo.com",
        ),
    )
    return (
        any(x.id == "erro-caminho-windows"
            and x.severidade == Severidade.MEDIA for x in f)
        and all(x.evidencia.strip() for x in f)
    )


CASES = [
    ("modelo: Finding OBSERVADO sem evidencia -> erro; com/INFERIDO -> ok", _caso_1_modelo),
    ("agrega_severidade: pior vence; vazio -> INFO", _caso_2_agrega),
    ("Relatorio.to_dict + resumo_por_severidade", _caso_3_relatorio),
    ("perfis: validade, fases e gate do completo", _caso_4_perfis),
    ("_alvo_valido: interno bloqueia; allowlist e allow_loopback liberam", _caso_5_alvo_valido),
    ("mascarar / redigir_header redigem segredo", _caso_6_redacao),
    ("Budget: consome ate o teto e nega alem", _caso_7_budget),
    ("requisicao local: status/headers/body/set-cookie/redirect/bloqueio", _caso_8_requisicao_local),
    ("requisicao_json: JSON valido, invalido, 5xx e erro", _caso_9_requisicao_json),
    ("audit_email: lookup erro sem ausencia; OK sem/com SPF-DMARC", _caso_10_audit_email),
    ("audit_ct_logs (mock): extrai subdominios unicos sem wildcard", _caso_11_audit_ct_logs),
    ("audit_rdap (mock requisicao_json): bootstrap + domain", _caso_12_audit_rdap),
    ("_AuditRedirectHandler allowlist-aware bloqueia/segue", _caso_13_redirect_handler),
    ("WP4 implementado; gate do ativo nega sem autorizacao", _caso_14_wp4_gate_ativo),
    ("CLI sem --perfil -> exit 1 com opcoes (nao assume perfil)", _caso_15_cli_sem_perfil),
    ("--self-test retorna 0 (SELF-TEST OK)", _caso_16_self_test),
    ("check A: headers ausentes/fraco/CSP/banner (local)", _caso_17_audit_cabecalhos),
    ("check B: cookies Secure/HttpOnly/SameSite/prefixo (local)", _caso_18_audit_cookies),
    ("check E: traceback e caminho expostos (local)", _caso_19_audit_erros_expostos),
    ("check F: well-known robots/security.txt (local)", _caso_20_audit_well_known),
    ("check G: CORS refletido/wildcard+creds (local)", _caso_21_audit_cors_passivo),
    ("check H: mixed content http em https (local)", _caso_22_audit_mixed_content),
    ("check D: redirect limpo/loop/downgrade/cross", _caso_23_audit_redirects),
    ("check C: helpers TLS expiracao/cipher/versao (mock)", _caso_24_audit_tls_helpers),
    ("agregador audit_superficie tolerante (local)", _caso_25_audit_superficie),
    ("ativo metodos: OPTIONS perigoso + TRACE (local)", _caso_26_audit_metodos),
    ("ativo CORS refletido/wildcard+creds (local)", _caso_27_audit_cors_ativo),
    ("ativo reflexao: query e POST de form (local)", _caso_28_audit_reflexao),
    ("ativo open redirect aponta para canario (local)", _caso_29_audit_open_redirect),
    ("ativo GraphQL introspeccao (local)", _caso_30_audit_graphql),
    ("ativo OpenAPI exposto (local)", _caso_31_audit_openapi),
    ("ativo paths sensiveis: .env CRITICA redigido (local)", _caso_32_audit_paths_sensiveis),
    ("ativo portas: aberta INFO / fechada nada (local)", _caso_33_audit_portas),
    ("ativo segredos JS redigidos + source map (local)", _caso_34_audit_segredos_js),
    ("ativo gate: nega sem autorizacao; roda autorizado (local)", _caso_35_audit_ativo_gate),
    ("ativo componentes OSV (mock) vulneravel (local)", _caso_36_audit_componentes_osv),
    ("relatorio v2: cria README/resultados/achados.json/raw/", _caso_37_relatorio_artefatos),
    ("relatorio v2: schema/hash_findings estavel", _caso_38_achados_json_hash),
    ("relatorio v2: contexto informado NAO verificado", _caso_39_readme_contexto_nao_verificado),
    ("relatorio v2: inferencias separadas das observacoes", _caso_40_readme_inferencias_separadas),
    ("relatorio v2: redacao de segredos no raw/", _caso_41_raw_redacao),
    ("relatorio v2: diff entre runs (novos/resolvidos/persistentes)", _caso_42_diff_runs),
    ("relatorio v2: resultados.md veredito por categoria (golden)", _caso_43_resultados_golden),
    ("A1: porta TLS por esquema (https 443, http pulado sem --porta)", _caso_44_porta_tls_padrao),
    ("A2: redigir_conteudo mascara chave/valor entre aspas (JSON)", _caso_45_redacao_json),
    ("M3: findings redigidos nos artefatos versionados", _caso_46_findings_redigidos_saida),
    ("M4: hash_findings estavel com canarios diferentes", _caso_47_hash_estavel_canario),
    ("M5: segredos JS acumulam (nao para no 1o finding)", _caso_48_segredos_js_acumula),
    ("M6: reflexao POST opt-in (default OFF)", _caso_49_reflexao_post_optin),
    ("B8: XFO invalido e CSP frame-ancestors wildcard", _caso_50_cabecalhos_xfo_csp),
    ("B11: CSP unsafe-inline sem aspas", _caso_51_csp_unsafe_sem_aspas),
    ("B11: mascarar_sempre em valor de cookie curto", _caso_52_cookie_mascara_sempre),
    ("B10: _slug_seguro evita nome reservado do Windows", _caso_53_slug_seguro_reservado),
    ("N1: contexto informado redigido nos artefatos versionados", _caso_54_contexto_redigido_artefatos),
    ("N2: findings redigidos no JSON do stdout", _caso_55_stdout_findings_redigidos),
    ("N3: segredos JS em 2 arquivos -> 2 findings (sem dedup)", _caso_56_segredos_js_dois_arquivos),
    ("N4: CSP frame-ancestors * nao gera header-csp-wildcard", _caso_57_csp_frame_ancestors_sem_wildcard),
    ("D1: payload RSC Next.js nao gera falso caminho Windows", _caso_58_rsc_sem_falso_positivo_caminho),
    ("D1: caminho Windows real continua detectado", _caso_59_caminho_windows_real_detectado),
]


def main_test() -> int:
    # Estado global limpo: nenhum budget ativo na entrada da suíte.
    security._BUDGET = None
    passed = 0
    failed = 0
    for nome, teste in CASES:
        try:
            ok = teste()
        except Exception:
            traceback.print_exc()
            ok = False
        if ok:
            passed += 1
            print(f"  [PASS] {nome}")
        else:
            failed += 1
            print(f"  [FAIL] {nome}")
    print(f"\nRESULTADO: {passed} passaram, {failed} falharam")
    return 1 if failed else 0


if __name__ == "__main__":
    sys.exit(main_test())
