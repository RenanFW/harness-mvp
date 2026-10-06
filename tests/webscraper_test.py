"""Testes do webscraper autônomo (zero dependências, sem rede real).

Rode com:
    python tests/webscraper_test.py

Usa um servidor HTTP local fake (ThreadingHTTPServer) e monkeypatch da
função `_fetch` do módulo para que os testes sejam determinísticos: cada caso
serve respostas controladas (JSON de APIs, HTML de busca, PDFs válidos e a
deny-list de pirataria).
"""

from __future__ import annotations

import json
import pathlib
import sys
import tempfile
import threading
import urllib.error
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from unittest import mock

sys.path.insert(0, str(pathlib.Path(__file__).resolve().parent.parent))

from harness import webscraper as ws  # noqa: E402

PASS = 0
FAIL = 0


def check(name, cond, extra=""):
    global PASS, FAIL
    if cond:
        PASS += 1
        print(f"  [PASS] {name}")
    else:
        FAIL += 1
        print(f"  [FAIL] {name} {extra}")


# ---------------------------------------------------------------------------
# Servidor fake
# ---------------------------------------------------------------------------

class _FakeHandler(BaseHTTPRequestHandler):
    rotas: dict = {}

    def _responder(self):
        path = self.path.split("?")[0]
        if path in self.rotas:
            status, ct, corpo = self.rotas[path]
        else:
            status, ct, corpo = 404, "text/plain", b"not found"
        self.send_response(status)
        self.send_header("Content-Type", ct)
        self.send_header("Content-Length", str(len(corpo)))
        self.end_headers()
        self.wfile.write(corpo)

    def do_GET(self):  # noqa: N802
        self._responder()

    def log_message(self, *args):  # noqa: A003
        pass


def _serve(rotas: dict) -> tuple[ThreadingHTTPServer, str]:
    _FakeHandler.rotas = rotas
    server = ThreadingHTTPServer(("127.0.0.1", 0), _FakeHandler)
    server.daemon_threads = True
    threading.Thread(target=server.serve_forever, daemon=True).start()
    host, port = server.server_address
    return server, f"http://{host}:{port}"


# ---------------------------------------------------------------------------
# Helpers de fixtures
# ---------------------------------------------------------------------------

def _pdf_bytes() -> bytes:
    return b"%PDF-1.4\n1 0 obj\n<< /Type /Catalog >>\nendobj\ntrailer\n%%EOF\n"


def _google_books_payload():
    return {
        "items": [{
            "volumeInfo": {
                "title": "Livro Teste",
                "authors": ["Autor Teste"],
                "publishedDate": "2020-01-15",
                "publisher": "Editora Fake",
                "industryIdentifiers": [{"type": "ISBN_13", "identifier": "9781234567890"}],
                "infoLink": "http://127.0.0.1:1/books",
                "description": "descrição",
            }
        }]
    }


def _duck_html() -> str:
    return (
        '<div class="result results_links">'
        '<a class="result__a" href="/l/?uddg=https%3A%2F%2Fwww.oreilly.com%2Fbook">'
        "Livro Teste - O'Reilly</a>"
        '<a class="result__snippet">Edição oficial da editora</a>'
        "</div>"
    )


# ---------------------------------------------------------------------------
# Testes de busca web
# ---------------------------------------------------------------------------

def test_busca_web_duckduckgo():
    # Mock de _fetch_text com HTML de fixture: mantém o teste 100% offline e
    # determinístico (sem rede real), exercitando o parser de resultados.
    with mock.patch.object(ws, "_fetch_text", return_value=_duck_html()):
        res = ws._busca_duckduckgo("x")  # noqa: SLF001
        check("duckduckgo: parser extrai resultado (fixture offline)",
              isinstance(res, list) and len(res) == 1, res)
        if res:
            check("duckduckgo: decodifica uddg e título",
                  "oreilly.com" in res[0]["url"] and "Livro Teste" in res[0]["titulo"],
                  res)


def test_extrai_resultado_duckduckgo():
    res = ws._strip_tags('<a class="result__a" href="/l/?uddg=https%3A%2F%2Fex.com%2Fp">Título</a>')  # noqa: SLF001
    check("strip_tags limpa HTML", res == "Título", res)
    # O parser de PRODUÇÃO (regex real de _busca_duckduckgo) é exercitado em
    # test_busca_web_duckduckgo com a fixture — este teste cobre apenas o
    # helper de limpeza de tags (um regex local divergente não testava nada).


def test_host_pirata():
    # Política atual (decisão do usuário): SOMENTE a família z-lib/zlib é
    # recusada; libgen/dokumen.pub/annas-archive NÃO são mais bloqueados.
    check("libgen NÃO bloqueado (política atual)", not ws._url_pirata("https://libgen.is/book/1"))  # noqa: SLF001
    check("dokumen.pub NÃO bloqueado (política atual)", not ws._url_pirata("https://dokumen.pub/abc"))  # noqa: SLF001
    check("z-lib bloqueado", ws._url_pirata("https://z-lib.io/x"))  # noqa: SLF001
    check("zlib variante bloqueado", ws._url_pirata("https://zlib.gl/x"))  # noqa: SLF001
    check("annas-archive NÃO bloqueado (política atual)", not ws._url_pirata("https://annas-archive.org/x"))  # noqa: SLF001
    check("github permitido", not ws._url_pirata("https://github.com/user/repo"))  # noqa: SLF001


def test_host_editorial():
    check("oreilly é editorial", ws._host_editorial("https://www.oreilly.com/x"))  # noqa: SLF001
    check("archive.org é editorial", ws._host_editorial("https://archive.org/details/x"))  # noqa: SLF001
    check("random não é editorial", not ws._host_editorial("https://example.com/x"))  # noqa: SLF001


# ---------------------------------------------------------------------------
# Testes de APIs de livros
# ---------------------------------------------------------------------------

def test_google_books_parsing():
    server, base = _serve({"/v1/volumes": (200, "application/json", json.dumps(_google_books_payload()).encode())})
    try:
        with mock.patch.object(ws, "_fetch_json", return_value=_google_books_payload()):
            res = ws._google_books("Livro Teste", "Autor Teste")  # noqa: SLF001
            check("google books retorna item", len(res) == 1)
            check("google books isbn", res and res[0]["isbn"] == "9781234567890")
            check("google books url", res and "books" in res[0]["url"])
    finally:
        server.shutdown()


def test_google_books_vazio():
    with mock.patch.object(ws, "_fetch_json", return_value=None):
        check("google books sem resposta retorna []", ws._google_books("x") == [])  # noqa: SLF001


def test_open_library_parsing():
    payload = {"docs": [{"title": "Livro", "author_name": ["A"], "first_publish_year": 2020,
                         "isbn": ["1234"], "key": "/works/OL1"}]}
    with mock.patch.object(ws, "_fetch_json", return_value=payload):
        res = ws._open_library("Livro")  # noqa: SLF001
        check("open library retorna item", len(res) == 1)
        check("open library url", res and res[0]["url"] == "https://openlibrary.org/works/OL1")


def test_internet_archive_parsing():
    payload = {"response": {"docs": [{"identifier": "livro1", "title": "Livro", "creator": "A"}]}}
    with mock.patch.object(ws, "_fetch_json", return_value=payload):
        res = ws._internet_archive("Livro")  # noqa: SLF001
        check("internet archive retorna item", len(res) == 1)
        check("internet archive url", res and res[0]["url"] == "https://archive.org/details/livro1")


def test_search_books_consolida():
    with mock.patch.object(ws, "_google_books", return_value=[{"fonte": "google-books", "titulo": "T", "url": "u"}]):
        with mock.patch.object(ws, "_open_library", return_value=[{"fonte": "open-library", "titulo": "T2", "url": "u2"}]):
            with mock.patch.object(ws, "_internet_archive", return_value=[]):
                res = ws.search_books("T")
                check("search_books consolida 3 APIs", len(res) == 2)


# ---------------------------------------------------------------------------
# Testes de validação anti-fabricação
# ---------------------------------------------------------------------------

def test_validate_existe_duas_fontes():
    with mock.patch.object(ws, "search_books", return_value=[
        {"fonte": "google-books", "titulo": "Clean Code", "url": "https://books.google.com/x", "autores": []},
    ]):
        with mock.patch.object(ws, "search_web", return_value=[
            # Web editorial RELEVANTE: cita a obra (tokens do título no resultado).
            {"titulo": "Clean Code - O'Reilly Media", "url": "https://www.oreilly.com/library/view/clean-code/", "resumo": ""},
        ]):
            res = ws.validate_fabrication("Clean Code")
            check("duas fontes => existe", res["existe"] is True)
            check("total_fontes == 2", res["total_fontes"] == 2)


def test_validate_fabricacao_uma_fonte():
    with mock.patch.object(ws, "search_books", return_value=[
        {"fonte": "google-books", "titulo": "Livro", "url": "https://books.google.com/x", "autores": []},
    ]):
        with mock.patch.object(ws, "search_web", return_value=[]):
            res = ws.validate_fabrication("Livro Fabricado")
            check("uma fonte => não existe", res["existe"] is False)


def test_validate_ignora_dominio_nao_editorial():
    with mock.patch.object(ws, "search_books", return_value=[]):
        with mock.patch.object(ws, "search_web", return_value=[
            {"titulo": "Site aleatório", "url": "https://example.com/xyz", "resumo": ""},
            {"titulo": "Blog", "url": "https://blog.xyz.com/post", "resumo": ""},
        ]):
            res = ws.validate_fabrication("Livro X")
            check("só domínio não-editorial => não existe", res["existe"] is False)


# ---------------------------------------------------------------------------
# Robustez da validação (Update Final, Fase 3): prioridade às APIs de livros
# ---------------------------------------------------------------------------

def test_validate_duas_apis_sem_busca_web():
    """(a) Obra confirmada por 2 APIs distintas SEM busca web => existe."""
    with mock.patch.object(ws, "search_books", return_value=[
        {"fonte": "google-books", "titulo": "Pragmatic", "url": "https://books.google.com/x", "autores": ["Hunt"]},
        {"fonte": "open-library", "titulo": "Pragmatic", "url": "https://openlibrary.org/works/OL1", "autores": []},
    ]):
        with mock.patch.object(ws, "search_web", return_value=[]):  # busca degradada/vazia
            res = ws.validate_fabrication("The Pragmatic Programmer")
            check("2 APIs distintas sem web => existe", res["existe"] is True, res)
            check("n_apis == 2", res.get("n_apis") == 2, res)
            check("total_fontes == 2", res["total_fontes"] == 2, res)
            check("indeterminado == False (confirmada)", res.get("indeterminado") is False, res)


def test_validate_uma_api_busca_web_degradada_indeterminado():
    """(b) Obra só em 1 API + busca web degradada (irrelevante) => não
    confirmada, mas indeterminado (sem falso alerta de fabricação)."""
    with mock.patch.object(ws, "search_books", return_value=[
        {"fonte": "google-books", "titulo": "Livro Real", "url": "https://books.google.com/x", "autores": []},
    ]):
        with mock.patch.object(ws, "search_web", return_value=[
            {"titulo": "clean.com.br", "url": "https://clean.com.br/limpeza", "resumo": ""},
            {"titulo": "CCleaner", "url": "https://ccleaner.com/download", "resumo": ""},
        ]):  # busca degradada: resultados irrelevantes (não-editoriais)
            res = ws.validate_fabrication("Livro Real")
            check("1 API + web degradada => não existe", res["existe"] is False, res)
            check("indeterminado == True (evidência parcial, sem falso alerta)",
                  res.get("indeterminado") is True, res)
            check("detalhe documenta evidência insuficiente",
                  "insuficiente" in res["detalhe"].lower(), res["detalhe"])


def test_validate_uma_api_busca_web_vazia_indeterminado():
    """(b2) Obra só em 1 API + busca web vazia => não confirmada, indeterminado."""
    with mock.patch.object(ws, "search_books", return_value=[
        {"fonte": "internet-archive", "titulo": "Software Architecture",
         "url": "https://archive.org/details/softarch", "autores": []},
    ]):
        with mock.patch.object(ws, "search_web", return_value=[]):
            res = ws.validate_fabrication("Software Architecture in Practice")
            check("1 API + web vazia => não existe", res["existe"] is False, res)
            check("indeterminado == True", res.get("indeterminado") is True, res)
            check("n_apis == 1", res.get("n_apis") == 1, res)


def test_validate_busca_web_excecao_nao_derruba():
    """(c) Busca web lança exceção => validação não derruba (best-effort)."""
    with mock.patch.object(ws, "search_books", return_value=[
        {"fonte": "google-books", "titulo": "Livro", "url": "https://books.google.com/x", "autores": []},
    ]):
        def _boom(*args, **kwargs):
            raise RuntimeError("markup quebrado / anti-bot")
        with mock.patch.object(ws, "search_web", side_effect=_boom):
            res = ws.validate_fabrication("Livro")
            check("exceção na busca web não derruba (retorna dict)",
                  isinstance(res, dict), res)
            check("exceção na busca web => ainda avalia as APIs",
                  res["existe"] is False and res.get("indeterminado") is True, res)


def test_validate_obra_fabricada_continua_alerta():
    """(d) Obra fabricada (sem nenhuma fonte) => não existe e não indeterminada
    (provável fabricação, alerta preservado)."""
    with mock.patch.object(ws, "search_books", return_value=[]):
        with mock.patch.object(ws, "search_web", return_value=[]):
            res = ws.validate_fabrication("Livro Totalmente Inventado 2099")
            check("sem fontes => não existe", res["existe"] is False, res)
            check("sem fontes => indeterminado False (fabricação clara)",
                  res.get("indeterminado") is False, res)
            check("sem fontes => detalhe de fabricação",
                  "fabrica" in res["detalhe"].lower(), res["detalhe"])


def test_coletar_nao_confirmado_sem_falso_alerta(tmp):
    """(b/coletar) Evidência parcial (1 API) => status nao_confirmado, NÃO
    grava alerta de fabricação (sem falso alerta)."""
    with mock.patch.object(ws, "REF_DIR", tmp / "refs_nc"):
        with mock.patch.object(ws, "INDEX_FILE", tmp / "refs_nc" / "index.md"):
            with mock.patch.object(ws, "validate_fabrication", return_value={
                "existe": False,
                "indeterminado": True,
                "fontes": [{"fonte": "google-books", "url": "https://books.google.com/x", "titulo": "T"}],
                "total_fontes": 1,
                "n_apis": 1,
                "detalhe": "evidência insuficiente (1 API)",
            }):
                with mock.patch.object(ws, "fallback_indireto", return_value={
                    "status": "parcial", "slug": "x", "contexto": "",
                    "temas": [], "fontes_por_tema": {}, "status_por_tema": {},
                    "atualidade_por_tema": {}, "refs_gravadas": [],
                    "ressalvas": ["mock"],
                }):
                    res = ws.coletar("Livro Parcial", "Autor")
                    check("evidência parcial => status nao_confirmado",
                          res["status"] == "nao_confirmado", res)
                    naoconf = tmp / "refs_nc" / (res["id"] + ".md")
                    texto = naoconf.read_text(encoding="utf-8")
                    check("não grava ALERTA DE FABRICAÇÃO (sem falso alerta)",
                          "ALERTA DE FABRICAÇÃO" not in texto, texto)
                    check("grava nota de evidência insuficiente",
                          "NÃO CONFIRMADO" in texto and "insuficiente" in texto.lower(), texto)
                    # Item 2.1: `nao_confirmado` é evidência insuficiente
                    # (fraca rotulada), NÃO é não-recuperável.
                    check("não confirmado continua recuperável (recuperavel: true)",
                          "recuperavel: true" in texto, texto)


def test_coletar_fabricacao_ainda_alerta_indeterminado_ausente(tmp):
    """(d/coletar) Obra fabricada (indeterminado ausente/False) => alerta de
    fabricação preservado (anti-fabricação intacta)."""
    with mock.patch.object(ws, "REF_DIR", tmp / "refs_fab2"):
        with mock.patch.object(ws, "INDEX_FILE", tmp / "refs_fab2" / "index.md"):
            with mock.patch.object(ws, "validate_fabrication", return_value={
                "existe": False,
                "fontes": [],
                "total_fontes": 0,
                "detalhe": "obra NÃO confirmada (provável fabricação)",
            }):  # sem chave 'indeterminado' (retrocompat) => fabricação
                with mock.patch.object(ws, "fallback_indireto", return_value={
                    "status": "sem_fontes", "slug": "x", "contexto": "",
                    "temas": [], "fontes_por_tema": {}, "status_por_tema": {},
                    "atualidade_por_tema": {}, "refs_gravadas": [],
                    "ressalvas": ["mock"],
                }):
                    res = ws.coletar("Livro Fake", "Autor")
                    check("sem indeterminado => status fabricacao",
                          res["status"] == "fabricacao", res)
                    alerta = tmp / "refs_fab2" / (res["id"] + ".md")
                    texto = alerta.read_text(encoding="utf-8")
                    check("alerta de fabricação criado",
                          "ALERTA DE FABRICAÇÃO" in texto, texto)


# ---------------------------------------------------------------------------
# Testes de PDF (anti-pirataria + validação)
# ---------------------------------------------------------------------------

def test_find_pdf_bloqueia_pirata():
    with mock.patch.object(ws, "_internet_archive", return_value=[]):
        with mock.patch.object(ws, "search_web", return_value=[
            {"titulo": "Pirata", "url": "https://z-lib.org/book/1", "resumo": ""},
            {"titulo": "Oficial", "url": "https://www.oreilly.com/livro.pdf", "resumo": ""},
        ]):
            with mock.patch.object(ws, "_eh_pdf_valido", side_effect=lambda u: "z-lib" not in u):
                res = ws.find_pdf("Livro")
                check("z-lib nunca entra nos candidatos", all("z-lib" not in c["url"] for c in res))


def test_download_pdf_valida_content_type():
    server, base = _serve({
        "/ok.pdf": (200, "application/pdf", _pdf_bytes()),
        "/nao-pdf": (200, "text/html", b"<html></html>"),
        "/pirata": (200, "application/pdf", _pdf_bytes()),
    })
    try:
        tmp = pathlib.Path(tempfile.mkdtemp())
        with mock.patch.object(ws, "_fetch", wraps=ws._fetch):
            # Substitui os hosts: força o fake. Para testes unitários usamos
            # _fetch diretamente e _url_pirata no download. O servidor local
            # roda em 127.0.0.1 — liberado explicitamente (allow_loopback),
            # como o SSRF guard exige para hosts loopback.
            ok = ws.download_pdf(base + "/ok.pdf", tmp / "ok.pdf",
                                 allow_loopback=True)
            check("pdf válido é baixado", ok is not None and ok.exists())
            nao = ws.download_pdf(base + "/nao-pdf", tmp / "nao.pdf",
                                  allow_loopback=True)
            check("não-pdf é recusado", nao is None)
            pirata = ws.download_pdf("https://z-lib.org/x.pdf", tmp / "pirata.pdf")
            check("pirata é recusado no download", pirata is None)
    finally:
        server.shutdown()


def test_eh_pdf_valido():
    server, base = _serve({
        "/ok.pdf": (200, "application/pdf", _pdf_bytes()),
        "/erro": (500, "text/plain", b"x"),
    })
    try:
        with mock.patch.object(ws, "_fetch", wraps=ws._fetch):
            check("content-type pdf => True",
                  ws._eh_pdf_valido(base + "/ok.pdf", allow_loopback=True) is True)  # noqa: SLF001
            check("status 500 => False",
                  ws._eh_pdf_valido(base + "/erro", allow_loopback=True) is False)  # noqa: SLF001
            check("z-lib => False", ws._eh_pdf_valido("https://z-lib.io/x.pdf") is False)  # noqa: SLF001
    finally:
        server.shutdown()


def test_ssrf_guard():
    """Anti-SSRF: _ssrf_valido rejeita loopback/privado/esquema não-http e
    _fetch levanta URLError em URL interna (sem allow_loopback)."""
    check("ssrf: scheme file:// rejeitado", not ws._ssrf_valido("file:///etc/passwd"))  # noqa: SLF001
    check("ssrf: scheme ftp:// rejeitado", not ws._ssrf_valido("ftp://example.com/x"))  # noqa: SLF001
    check("ssrf: loopback rejeitado", not ws._ssrf_valido("http://127.0.0.1:8500/x"))  # noqa: SLF001
    check("ssrf: privado rejeitado", not ws._ssrf_valido("http://192.168.1.1/x"))  # noqa: SLF001
    check("ssrf: link-local rejeitado", not ws._ssrf_valido("http://169.254.169.254/latest/meta-data"))  # noqa: SLF001
    check("ssrf: host externo aceito", ws._ssrf_valido("https://www.google.com/"))  # noqa: SLF001
    check("ssrf: allow_loopback só libera loopback",
          ws._ssrf_valido("http://127.0.0.1:8500/x", allow_loopback=True))  # noqa: SLF001
    # _fetch com URL interna -> URLError imediato (nunca conecta)
    try:
        ws._fetch("http://127.0.0.1:1/api/health", retries=0)
        check("ssrf: _fetch loopback levanta URLError", False)
    except urllib.error.URLError:
        check("ssrf: _fetch loopback levanta URLError", True)


def test_url_externa_segura_rejeita_internos():
    """Anti-SSRF da busca web: `_url_externa_segura` rejeita IPs internos
    literais, nomes internos e esquemas inválidos SEM DNS (host literal)."""
    perigosas = [
        "http://169.254.169.254/latest/meta-data",  # metadata cloud (link-local)
        "http://127.0.0.1:8500/x",                  # loopback
        "http://10.0.0.5/",                         # privado 10/8
        "http://192.168.1.1/",                      # privado 192.168/16
        "http://172.16.0.1/",                       # privado 172.16/12
        "http://0.0.0.0/",                          # não-especificado (0/8)
        "http://100.64.0.1/",                       # CGNAT (RFC 6598)
        "http://localhost:8500/",                   # nome interno
        "http://intranet/",                         # host sem ponto
        "http://servidor.local/",                   # sufixo .local
        "http://api.internal/",                     # sufixo .internal
        "file:///etc/passwd",                       # esquema inválido
        "ftp://example.com/x",                      # esquema inválido
        "http:///x",                                # host vazio
        "http://",                                  # host vazio
        "http://[::1]/",                            # IPv6 loopback
        "http://[fe80::1]/",                        # IPv6 link-local
    ]
    for url in perigosas:
        check(f"url interna rejeitada: {url}",
              ws._url_externa_segura(url) is False, url)  # noqa: SLF001


def test_url_externa_segura_rejeita_formas_abreviadas():
    """Achado do reviewer: formas ABREVIADAS/hexa/octal de IPv4 literal NÃO
    podem escapar pelo ramo 'nome de host'. `_url_externa_segura` normaliza o
    literal (socket.inet_aton) antes de checar loopback/privado etc."""
    abreviadas = [
        "http://127.1/",            # IPv4 abreviado (127.0.0.1)
        "http://127.1:8500/",       # abreviado + porta
        "http://user@127.1/",       # userinfo não pode burlar o host
        "http://0x7f.0.0.1/",       # componentes hexa
        "http://0177.0.0.1/",       # componente octal
        "http://0x7f.1/",           # hexa + abreviado
    ]
    for url in abreviadas:
        check(f"forma abreviada rejeitada: {url}",
              ws._url_externa_segura(url) is False, url)  # noqa: SLF001


def test_url_externa_segura_aceita_externos():
    """Contraprova: URLs editoriais/externas legítimas continuam aceitas
    (host literal, sem resolução de DNS)."""
    seguras = [
        "https://www.oreilly.com/library/view/clean-code/",
        "https://archive.org/details/x",
        "https://openlibrary.org/works/OL1",
        "https://books.google.com.br/books?id=1",
        "https://github.com/user/repo",
        "https://www.amazon.com/dp/123",
        "http://example.com/artigo",
        "https://8.8.8.8/dns",  # IP público (não interno)
    ]
    for url in seguras:
        check(f"url externa aceita: {url}",
              ws._url_externa_segura(url) is True, url)  # noqa: SLF001


def test_validate_web_resultado_interno_nao_conta():
    """Resultado da busca web com host interno NÃO conta como evidência (nem
    entra em `fontes`), mesmo parecendo editorial/relevante: os mocks forçam
    `_host_editorial`/`_resultado_relevante` verdadeiros para isolar o filtro
    anti-SSRF."""
    with mock.patch.object(ws, "search_books", return_value=[]):
        with mock.patch.object(ws, "search_web", return_value=[
            {"titulo": "Clean Code", "url": "http://169.254.169.254/clean-code", "resumo": ""},
            {"titulo": "Clean Code", "url": "http://127.0.0.1:8500/clean-code", "resumo": ""},
        ]):
            with mock.patch.object(ws, "_host_editorial", return_value=True):  # noqa: SLF001
                with mock.patch.object(ws, "_resultado_relevante", return_value=True):  # noqa: SLF001
                    res = ws.validate_fabrication("Clean Code")
                    check("web interna não vira fonte web-editorial",
                          all(f["fonte"] != "web-editorial" for f in res["fontes"]),
                          res["fontes"])
                    check("web interna não confirma existência",
                          res["existe"] is False, res)
                    check("web interna não conta como relevante (detalhe sem web)",
                          "web-editorial" not in res["detalhe"], res["detalhe"])


def test_validate_web_resultado_externo_conta():
    """Contraprova: resultado com host externo seguro continua registrado como
    evidência web-editorial (o filtro anti-SSRF não é um bloqueio geral)."""
    with mock.patch.object(ws, "search_books", return_value=[]):
        with mock.patch.object(ws, "search_web", return_value=[
            {"titulo": "Clean Code", "url": "https://www.oreilly.com/library/view/clean-code/",
             "resumo": ""},
        ]):
            res = ws.validate_fabrication("Clean Code")
            check("fonte web-editorial registrada",
                  any(f["fonte"] == "web-editorial" for f in res["fontes"]), res["fontes"])
            check("host externo relevante é contado",
                  res["fontes_por_tipo"].get("web-editorial") == 1, res["fontes_por_tipo"])


# ---------------------------------------------------------------------------
# Testes de gravação na memória
# ---------------------------------------------------------------------------

def test_write_reference_e_reindex(tmp):
    from unittest.mock import patch
    with patch.object(ws, "REF_DIR", tmp / "references"):
        with patch.object(ws, "INDEX_FILE", tmp / "references" / "index.md"):
            ref = ws.write_reference({
                "id": "livro-teste",
                "tipo": "livro",
                "titulo": "Livro Teste",
                "autor": "Autor",
                "fonte": "https://x.com",
                "data": "2026-08-16",
                "tags": ["teste"],
                "corpo": "# Livro\n\nconteúdo",
            })
            check("referência criada", ref.exists())
            idx = (tmp / "references" / "index.md").read_text(encoding="utf-8")
            check("índice tem a referência", "livro-teste.md" in idx)


def test_coletar_fabricacao(tmp):
    with mock.patch.object(ws, "REF_DIR", tmp / "refs"):
        with mock.patch.object(ws, "INDEX_FILE", tmp / "refs" / "index.md"):
            with mock.patch.object(ws, "validate_fabrication", return_value={
                "existe": False,
                "fontes": [],
                "total_fontes": 0,
                "detalhe": "não confirmada",
            }):
                # Mock do fallback: mantém o teste offline e determinístico
                # (o fallback real faz busca na web).
                with mock.patch.object(ws, "fallback_indireto", return_value={
                    "status": "parcial",
                    "slug": "livro-inventado-autor-fake",
                    "contexto": "contexto sintético",
                    "temas": [],
                    "fontes_por_tema": {},
                    "status_por_tema": {},
                    "atualidade_por_tema": {},
                    "refs_gravadas": [],
                    "ressalvas": ["mock sem rede"],
                }):
                    res = ws.coletar("Livro Inventado", "Autor Fake")
                    check("fabricação => status fabricacao", res["status"] == "fabricacao")
                    check("fallback mockado entra no resultado",
                          res["fallback_indireto"]["status"] == "parcial")
                    alerta = tmp / "refs" / (res["id"] + ".md")
                    check("alerta de fabricação criado", alerta.exists())
                    texto = alerta.read_text(encoding="utf-8")
                    check("alerta contém ALERTA DE FABRICAÇÃO", "ALERTA DE FABRICAÇÃO" in texto)


def test_coletar_ok(tmp):
    with mock.patch.object(ws, "REF_DIR", tmp / "refs"):
        with mock.patch.object(ws, "INDEX_FILE", tmp / "refs" / "index.md"):
            with mock.patch.object(ws, "validate_fabrication", return_value={
                "existe": True,
                "fontes": [{"fonte": "google-books", "url": "https://books.google.com/x", "titulo": "T"}],
                "total_fontes": 1,
                "detalhe": "confirmada",
            }):
                res = ws.coletar("Livro Real", "Autor Real")
                check("obra real => status ok", res["status"] == "ok")
                ref = tmp / "refs" / (res["id"] + ".md")
                check("referência criada", pathlib.Path(res["path"]).exists())
                texto = pathlib.Path(res["path"]).read_text(encoding="utf-8")
                check("referência tem frontmatter", texto.startswith("---"))
                check("referência ok tem trust alta",
                      "trust: alta" in texto and "validado_por: motor" in texto)


def test_coletar_fabricacao_trust(tmp):
    """Alerta de fabricação grava trust fraca + origem da verificação."""
    with mock.patch.object(ws, "REF_DIR", tmp / "refs_fab"):
        with mock.patch.object(ws, "INDEX_FILE", tmp / "refs_fab" / "index.md"):
            with mock.patch.object(ws, "validate_fabrication", return_value={
                "existe": False,
                "fontes": [],
                "total_fontes": 0,
                "detalhe": "obra NÃO confirmada em nenhuma fonte editorial",
            }):
                with mock.patch.object(ws, "fallback_indireto", return_value={
                    "status": "parcial", "slug": "x", "contexto": "",
                    "temas": [], "fontes_por_tema": {}, "status_por_tema": {},
                    "atualidade_por_tema": {}, "refs_gravadas": [],
                    "ressalvas": ["mock"],
                }):
                    res = ws.coletar("Livro Fake Trust", "Autor")
                    alerta = tmp / "refs_fab" / (res["id"] + ".md")
                    texto = alerta.read_text(encoding="utf-8")
                    check("alerta de fabricação tem trust fraca", "trust: fraca" in texto)
                    check("alerta tem origem da verificação",
                          "origem: verificação anti-fabricação" in texto)
                    # Item 2.1: fabricação é NÃO-recuperável (nunca no RAG).
                    check("alerta de fabricação é não-recuperável (recuperavel: false)",
                          "recuperavel: false" in texto, texto)


def test_referencia_indireta_trust(tmp):
    """Referência indireta (validacao_indireta) grava trust media + origem."""
    with mock.patch.object(ws, "REF_DIR", tmp / "refs_ind"):
        with mock.patch.object(ws, "INDEX_FILE", tmp / "refs_ind" / "index.md"):
            caminho = ws._gravar_referencia_indireta(  # noqa: SLF001
                id_ref="obra-x-indireto",
                titulo="Obra X",
                autor="Autor Y",
                hoje="2026-08-18",
                contexto="contexto sintético",
                temas=[{"tema": "topicos", "frequencia": 3}],
                fontes_por_tema={"topicos": [
                    {"url": "https://a.com/1", "titulo": "Fonte A", "ano": 2024},
                    {"url": "https://b.com/2", "titulo": "Fonte B", "ano": None},
                ]},
                status_por_tema={"topicos": "confirmado"},
                atualidade_por_tema={"topicos": {"valido": True, "padrao_atual": None}},
            )
            texto = caminho.read_text(encoding="utf-8")
            check("indireta tem trust media", "trust: media" in texto)
            check("indireta tem origem com fontes públicas",
                  "origem: webscraping indireto" in texto and "https://a.com/1" in texto)
            check("indireta validado_por motor", "validado_por: motor" in texto)


# ---------------------------------------------------------------------------
# Trust no frontmatter (confiança/rastreabilidade)
# ---------------------------------------------------------------------------

def test_frontmatter_trust():
    """_frontmatter grava trust/origem/validado_por; defaults seguros sem eles."""
    dados = {
        "id": "r-ok", "tipo": "livro", "titulo": "T", "autor": "A",
        "fonte": "https://x.com", "data": "2026-08-18",
        "tags": ["webscraping", "coleta"],
        "trust": "alta",
        "origem": "webscraping autônomo; fontes editoriais: https://x.com",
        "validado_por": "motor",
    }
    fm = ws._frontmatter(dados)  # noqa: SLF001
    check("frontmatter tem trust alta", "trust: alta" in fm)
    check("frontmatter tem origem", "origem: webscraping autônomo" in fm)
    check("frontmatter tem validado_por", "validado_por: motor" in fm)
    # sem os campos -> defaults seguros (fraca + marcado ausente)
    sem_campos = {k: v for k, v in dados.items()
                  if k not in ("trust", "origem", "validado_por")}
    fm2 = ws._frontmatter(sem_campos)  # noqa: SLF001
    check("sem trust -> fraca", "trust: fraca" in fm2)
    check("sem origem -> marcada", "origem: _não informada_" in fm2)
    check("sem validado -> marcado", "validado_por: _não informado_" in fm2)
    # Item 2.1: campo ausente -> default recuperavel true.
    check("sem recuperavel -> default true", "recuperavel: true" in fm2)
    # Explicito False -> false (caminho do alerta de fabricação).
    fm3 = ws._frontmatter({**dados, "recuperavel": False})  # noqa: SLF001
    check("recuperavel False explícito -> false", "recuperavel: false" in fm3)
    # B1: escritor usa a MESMA coerção do leitor — strings "false"/"0"/"no"
    # (e variantes) NÃO podem virar `true`; int 0 idem.
    for valor in ("false", "0", "no", "nao", "não", 0):
        fm_x = ws._frontmatter({**dados, "recuperavel": valor})  # noqa: SLF001
        check(f'recuperavel {valor!r} -> false (B1)',
              "recuperavel: false" in fm_x, fm_x)
    fm_true = ws._frontmatter({**dados, "recuperavel": "true"})  # noqa: SLF001
    check('recuperavel "true" -> true (B1)', "recuperavel: true" in fm_true)
    fm_um = ws._frontmatter({**dados, "recuperavel": 1})  # noqa: SLF001
    check("recuperavel 1 -> true (B1)", "recuperavel: true" in fm_um)


def test_write_reference_trust(tmp):
    """write_reference grava os campos; leitura com ':' interno preservada."""
    with mock.patch.object(ws, "REF_DIR", tmp / "refs_trust"):
        with mock.patch.object(ws, "INDEX_FILE", tmp / "refs_trust" / "index.md"):
            ref = ws.write_reference({
                "id": "ref-trust",
                "tipo": "livro",
                "titulo": "T",
                "autor": "A",
                "fonte": "https://x.com",
                "data": "2026-08-18",
                "tags": ["coleta"],
                "trust": "media",
                "origem": "webscraping indireto; fontes: https://a.com:8080/x",
                "validado_por": "motor",
                "corpo": "# T\n\nconteúdo",
            })
            texto = ref.read_text(encoding="utf-8")
            campos = ws._parse_frontmatter(texto)
            check("trust media gravada e lida", campos and campos.get("trust") == "media")
            check("origem com ':' interno preservada",
                  campos and "https://a.com:8080/x" in campos.get("origem", ""))
            check("validado_por lido", campos and campos.get("validado_por") == "motor")


def test_referencia_antiga_sem_trust(tmp):
    """Referências antigas (sem trust/origem/validado_por) continuam lendo
    bem e entram no índice (retrocompatibilidade)."""
    refs = tmp / "refs_antigas"
    refs.mkdir(parents=True)
    (refs / "antiga.md").write_text(
        "---\nid: antiga\ntipo: livro\ntitulo: Livro Antigo\nfonte: https://x.com\n"
        "data: 2026-08-15\ntags: [coleta]\n---\n# corpo\n",
        encoding="utf-8",
    )
    with mock.patch.object(ws, "REF_DIR", refs):
        with mock.patch.object(ws, "INDEX_FILE", refs / "index.md"):
            campos = ws._parse_frontmatter((refs / "antiga.md").read_text(encoding="utf-8"))
            ws._reindex()  # noqa: SLF001
            idx = (refs / "index.md").read_text(encoding="utf-8")
            check("referência antiga parseia (id/tipo/titulo)",
                  campos and campos.get("id") == "antiga" and campos.get("titulo") == "Livro Antigo")
            check("referência antiga sem trust entra no índice",
                  "antiga.md" in idx and "Livro Antigo" in idx)


# ---------------------------------------------------------------------------
# Regressões da revisão 4 (A1, M1, M2, M3, B2)
# ---------------------------------------------------------------------------

def test_reindex_aceita_autores_e_campos_em_qualquer_ordem(tmp):
    """A1: `_reindex` não pode descartar arquivos com `autores:` (plural),
    sem campo autor, ou campos em ordem diferente."""
    refs = tmp / "refs_reindex"
    refs.mkdir(parents=True)
    (refs / "a.md").write_text(
        "---\nid: a\ntipo: livro\nfonte: https://x.com\ndata: 2026-08-16\n"
        "titulo: Livro A\nautores: Autor Um, Autor Dois\n---\n# corpo\n",
        encoding="utf-8",
    )
    (refs / "b.md").write_text(
        "---\ntags: [x]\nid: b\ndata: 2026-08-15\ntitulo: Livro B\ntipo: documento\n"
        "---\n# corpo\n",
        encoding="utf-8",
    )
    with mock.patch.object(ws, "REF_DIR", refs):
        with mock.patch.object(ws, "INDEX_FILE", refs / "index.md"):
            ws._reindex()  # noqa: SLF001
            idx = (refs / "index.md").read_text(encoding="utf-8")
            check("autores (plural) entra no índice",
                  "Livro A" in idx and "Autor Um, Autor Dois" in idx)
            check("sem campo autor entra no índice (fallback)", "Livro B" in idx)
            n_linhas = sum(1 for linha in idx.splitlines()
                           if linha.startswith("| ") and "---" not in linha
                           and not linha.startswith("| Título"))
            check("índice tem as 2 referências", n_linhas == 2, n_linhas)


def test_valida_contexto_tokenizacao():
    """M1: checagem contextual por token (word boundary) e termos >= 4 chars."""
    fontes = [
        {"titulo": "medidas de tempo", "resumo": "série de temperatura média",
         "url": "https://a.com/1"},
        {"titulo": "variação de temperatura", "resumo": "dados temporais",
         "url": "https://b.com/2"},
    ]
    res = ws._valida_contexto("tem", fontes, {"tem", "media"})  # noqa: SLF001
    check("tema 'tem' não casa com tempo/temperatura",
          "tem" not in res["termos_compartilhados"], res)
    check("tema curto 'tem' => nao_confirmado", res["status"] == "nao_confirmado", res)
    res2 = ws._valida_contexto("temperatura", fontes, {"temperatura", "media"})  # noqa: SLF001
    check("tema 'temperatura' casa por token", res2["status"] == "confirmado", res2)
    res3 = ws._valida_contexto("data", [  # noqa: SLF001
        {"titulo": "database", "resumo": "banco", "url": "https://c.com/3"}],
        {"data", "banco"})
    check("'data' não casa com 'database' (word boundary)",
          "data" not in res3["termos_compartilhados"], res3)


def test_buscar_fontes_tema_filtra_adulto():
    """M3: host de conteúdo adulto não entra na referência."""
    with mock.patch.object(ws, "search_web", return_value=[
        {"titulo": "Tópico", "url": "https://forum.xnxx.com/thread/1", "resumo": "x"},
        {"titulo": "Artigo oficial", "url": "https://www.oreilly.com/2024/artigo", "resumo": "conteúdo"},
        {"titulo": "Blog", "url": "https://blog.exemplo.com/post", "resumo": "mais conteúdo"},
    ]):
        res = ws._buscar_fontes_tema("topicos", "livro")  # noqa: SLF001
        check("fonte adulta (xnxx) é filtrada", all("xnxx" not in f["url"] for f in res), res)
        check("fontes legítimas permanecem", any("oreilly" in f["url"] for f in res))


def test_ano_fonte_plausivel():
    """B2: anos fora da faixa plausível (ex.: 1970 de citações) são rejeitados."""
    f1 = {"titulo": "post", "resumo": "clássico de 1970", "url": "https://blog.com/artigo"}
    check("ano 1970 (snippet) fora da faixa => None", ws._ano_fonte(f1) is None)  # noqa: SLF001
    f2 = {"titulo": "post", "resumo": "atualizado em 2024", "url": "https://blog.com/2024/01/x"}
    check("ano da URL prevalece", ws._ano_fonte(f2) == 2024)  # noqa: SLF001
    f3 = {"titulo": "post", "resumo": "referência de 1975 e edição de 2019", "url": "https://blog.com/x"}
    check("snippet misto usa ano mais frequente (2019)", ws._ano_fonte(f3) == 2019)  # noqa: SLF001


def test_fallback_indireto_consome_padrao_atual(tmp):
    """M2: padrão atual da Etapa D é consumido — segunda passada de busca com
    o padrão anexado à query e fontes do padrão registradas no tópico."""
    def fake_search(query, limite=5):
        if "llvm" in query:
            return [{"titulo": "LLVM docs", "url": "https://llvm.org/2024/docs",
                     "resumo": "llvm compiladores"}]
        return [{"titulo": "Artigo antigo", "url": "https://www.oreilly.com/2015/artigo",
                 "resumo": "conteúdo"}]

    with mock.patch.object(ws, "REF_DIR", tmp / "refs"):
        with mock.patch.object(ws, "INDEX_FILE", tmp / "refs" / "index.md"):
            with mock.patch.object(ws, "_contexto_obra",  # noqa: SLF001
                                   return_value=("compiladores " * 10 + " base base base texto", [])):
                with mock.patch.object(ws, "search_web", side_effect=fake_search):
                    with mock.patch.object(ws, "_verificar_atualidade",  # noqa: SLF001
                                           return_value={
                                               "valido": False,
                                               "padrao_atual": "llvm",
                                               "fontes_padrao_atual": [],
                                               "detalhe": "desatualizado; padrão atual llvm",
                                           }):
                        res = ws.fallback_indireto("Livro X", "Autor Y", gravar=False)
                        fontes = res["fontes_por_tema"].get("compiladores", [])
                        check("segunda passada busca com padrão anexado à query",
                              any(f.get("padrao_atual") for f in fontes), fontes)
                        check("fonte do padrão registrada no tópico",
                              any("llvm.org" in f["url"] for f in fontes))


# ---------------------------------------------------------------------------
# Regressões da re-revisão (Fase 3: achados A1/A2/M1/M2)
# ---------------------------------------------------------------------------

def test_resultado_relevante():
    """(b) `_resultado_relevante` distingue resultado que cita a obra de
    homepage/landing editorial genérica."""
    check("resultado que cita a obra (>= 2 tokens do título) => True",
          ws._resultado_relevante(  # noqa: SLF001
              {"titulo": "The Pragmatic Programmer: From Journeyman to Master",
               "url": "https://www.oreilly.com/library/view/the-pragmatic-programmer/9780135957059/"},
              "The Pragmatic Programmer", "Hunt") is True)
    check("1 token do título + 1 do autor => True (regra do autor)",
          ws._resultado_relevante(  # noqa: SLF001
              {"titulo": "Code: the hidden language (Robert Martin)",
               "url": "https://example.com/code"},
              "Clean Code", "Robert C. Martin") is True)
    check("homepage genérica Google Books => False",
          ws._resultado_relevante(  # noqa: SLF001
              {"titulo": "Google Livros", "url": "https://books.google.com.br/"},
              "The Pragmatic Programmer", "Hunt") is False)
    check("listagem genérica Amazon => False",
          ws._resultado_relevante(  # noqa: SLF001
              {"titulo": "Livros", "url": "https://www.amazon.com.br/livros/s?k=livros"},
              "Livro Que Nao Existe De Robson 2099", "Robson") is False)
    check("sem tokens do título no alvo e sem autor => False",
          ws._resultado_relevante(  # noqa: SLF001
              {"titulo": "Editora", "url": "https://www.amazon.com.br/"},
              "The Pragmatic Programmer", "Hunt") is False)


def test_validate_fabricado_homepages_editoriais_irrelevantes():
    """(a) Título fabricado + busca web com homepages editoriais genéricas
    (Google Books, listagem Amazon) => alerta de fabricação (existe False,
    indeterminado False): homepage editorial irrelevante NÃO é evidência."""
    with mock.patch.object(ws, "search_books", return_value=[]):
        with mock.patch.object(ws, "search_web", return_value=[
            {"titulo": "Google Livros", "url": "https://books.google.com.br/", "resumo": ""},
            {"titulo": "Livros", "url": "https://www.amazon.com.br/livros/s?k=livros", "resumo": ""},
        ]):
            res = ws.validate_fabrication("Livro Que Nao Existe De Robson 2099")
            check("homepages editoriais irrelevantes => não existe",
                  res["existe"] is False, res)
            check("homepages irrelevantes => indeterminado False (alerta)",
                  res.get("indeterminado") is False, res)
            check("nenhuma fonte web-editorial contada",
                  all(f["fonte"] != "web-editorial" for f in res["fontes"]), res["fontes"])
            check("detalhe de fabricação",
                  "fabrica" in res["detalhe"].lower(), res["detalhe"])


def test_validate_obra_real_duas_apis_existe():
    """(c) Obra real confirmada por 2 APIs distintas => existe (mesmo sem web)."""
    with mock.patch.object(ws, "search_books", return_value=[
        {"fonte": "google-books", "titulo": "The Pragmatic Programmer", "url": "https://books.google.com/x", "autores": ["Hunt"]},
        {"fonte": "open-library", "titulo": "The Pragmatic Programmer", "url": "https://openlibrary.org/works/OL123", "autores": []},
    ]):
        with mock.patch.object(ws, "search_web", return_value=[]):
            res = ws.validate_fabrication("The Pragmatic Programmer")
            check("2 APIs => existe", res["existe"] is True, res)
            check("n_apis == 2", res.get("n_apis") == 2, res)
            check("indeterminado False (confirmada)", res.get("indeterminado") is False, res)


def test_validate_obra_real_uma_api_web_relevante_existe():
    """(d) 1 API + 1 resultado web RELEVANTE (domínio editorial que cita a
    obra) => existe."""
    with mock.patch.object(ws, "search_books", return_value=[
        {"fonte": "internet-archive", "titulo": "The Pragmatic Programmer",
         "url": "https://archive.org/details/pprogrammer", "autores": ["Hunt"]},
    ]):
        with mock.patch.object(ws, "search_web", return_value=[
            {"titulo": "The Pragmatic Programmer - O'Reilly Media",
             "url": "https://www.oreilly.com/library/view/the-pragmatic-programmer/", "resumo": ""},
        ]):
            res = ws.validate_fabrication("The Pragmatic Programmer")
            check("1 API + web relevante => existe", res["existe"] is True, res)
            check("total_fontes == 2", res["total_fontes"] == 2, res)


def test_validate_obra_real_uma_api_web_irrelevante_indeterminado():
    """(e) 1 API real + web com homepages editoriais irrelevantes => não
    confirmada, indeterminado (evidência parcial, sem falso alerta)."""
    with mock.patch.object(ws, "search_books", return_value=[
        {"fonte": "google-books", "titulo": "The Pragmatic Programmer",
         "url": "https://books.google.com/x", "autores": ["Hunt"]},
    ]):
        with mock.patch.object(ws, "search_web", return_value=[
            {"titulo": "Google Livros", "url": "https://books.google.com.br/", "resumo": ""},
            {"titulo": "Livros", "url": "https://www.amazon.com.br/livros/s?k=livros", "resumo": ""},
        ]):
            res = ws.validate_fabrication("The Pragmatic Programmer")
            check("1 API + web irrelevante => não existe", res["existe"] is False, res)
            check("1 API real => indeterminado True", res.get("indeterminado") is True, res)


def test_validate_erros_api_degradacao_nao_silenciosa():
    """A2: falha de API (ex.: Google Books 429) é exposta em `erros_api`, não
    tratada como '0 resultados' silencioso."""
    with mock.patch.object(ws, "_google_books", return_value=[]):  # noqa: SLF001
        with mock.patch.object(ws, "_open_library", return_value=[]):  # noqa: SLF001
            with mock.patch.object(ws, "_internet_archive", return_value=[]):  # noqa: SLF001
                with mock.patch.object(ws, "_FALHAS_API",  # noqa: SLF001
                                       {"google-books": "requisição falhou (rede/429/JSON inválido)"}):
                    res = ws.validate_fabrication("Livro Qualquer")
                    check("erros_api expõe a falha do Google Books",
                          res.get("erros_api") == {"google-books": "requisição falhou (rede/429/JSON inválido)"},
                          res)


def test_google_books_falha_registra_erro():
    """A2: `_google_books` sinaliza falha (None do `_fetch_json`) via
    `_FALHAS_API` em vez de silêncio."""
    with mock.patch.object(ws, "_fetch_json", return_value=None):  # noqa: SLF001
        ws._FALHAS_API.clear()  # noqa: SLF001
        try:
            res = ws._google_books("x")  # noqa: SLF001
            check("falha => lista vazia", res == [], res)
            check("falha registrada em _FALHAS_API",
                  ws._FALHAS_API.get("google-books"), ws._FALHAS_API)
        finally:
            ws._FALHAS_API.clear()  # noqa: SLF001


# ---------------------------------------------------------------------------
# Roda tudo
# ---------------------------------------------------------------------------

def _main():
    import tempfile
    tmp = pathlib.Path(tempfile.mkdtemp(prefix="webscraper_test_"))
    test_busca_web_duckduckgo()
    test_extrai_resultado_duckduckgo()
    test_host_pirata()
    test_host_editorial()
    test_google_books_parsing()
    test_google_books_vazio()
    test_open_library_parsing()
    test_internet_archive_parsing()
    test_search_books_consolida()
    test_validate_existe_duas_fontes()
    test_validate_fabricacao_uma_fonte()
    test_validate_ignora_dominio_nao_editorial()
    test_validate_duas_apis_sem_busca_web()
    test_validate_uma_api_busca_web_degradada_indeterminado()
    test_validate_uma_api_busca_web_vazia_indeterminado()
    test_validate_busca_web_excecao_nao_derruba()
    test_validate_obra_fabricada_continua_alerta()
    test_coletar_nao_confirmado_sem_falso_alerta(tmp)
    test_coletar_fabricacao_ainda_alerta_indeterminado_ausente(tmp)
    test_find_pdf_bloqueia_pirata()
    test_download_pdf_valida_content_type()
    test_eh_pdf_valido()
    test_write_reference_e_reindex(tmp)
    test_coletar_fabricacao(tmp)
    test_coletar_ok(tmp)
    test_coletar_fabricacao_trust(tmp)
    test_referencia_indireta_trust(tmp)
    test_frontmatter_trust()
    test_write_reference_trust(tmp)
    test_referencia_antiga_sem_trust(tmp)
    test_reindex_aceita_autores_e_campos_em_qualquer_ordem(tmp)
    test_valida_contexto_tokenizacao()
    test_buscar_fontes_tema_filtra_adulto()
    test_ano_fonte_plausivel()
    test_fallback_indireto_consome_padrao_atual(tmp)
    test_resultado_relevante()
    test_validate_fabricado_homepages_editoriais_irrelevantes()
    test_validate_obra_real_duas_apis_existe()
    test_validate_obra_real_uma_api_web_relevante_existe()
    test_validate_obra_real_uma_api_web_irrelevante_indeterminado()
    test_validate_erros_api_degradacao_nao_silenciosa()
    test_google_books_falha_registra_erro()
    test_ssrf_guard()
    test_url_externa_segura_rejeita_internos()
    test_url_externa_segura_rejeita_formas_abreviadas()
    test_url_externa_segura_aceita_externos()
    test_validate_web_resultado_interno_nao_conta()
    test_validate_web_resultado_externo_conta()
    print(f"\n{'-'*40}")
    print(f"webscraper_test: {PASS} passou, {FAIL} falhou")
    return 1 if FAIL else 0


if __name__ == "__main__":
    sys.exit(_main())