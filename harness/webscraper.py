"""Webscraper autônomo do harness (stdlib only, com pypdf opcional).

Módulo que se vira sozinho para encontrar, validar e registrar conteúdo da web
(livros, PDFs, documentação) que fundamente projetos do harness. Ele decide o
melhor caminho por conta própria — busca, valida existência, baixa e grava na
memória — sem depender do agente.

Pipeline (Chaining):
    search_books -> validate_fabrication -> find_pdf -> download_pdf ->
    extract_pdf -> write_reference
    (obra fabricada) -> alerta `<slug>-fabricacao.md` + fallback por
    conhecimento indireto (`fallback_indireto`, Etapas A-F) ->
    referência `<slug>-indireto.md` (tag `validacao_indireta`)

Regras não negociáveis:
    - NUNCA inventar conteúdo (anti-fabricação: obra existe em >= 2 fontes
      editoriais independentes, senão grava alerta em `<slug>-fabricacao.md`).
    - NUNCA piratear: somente a família z-lib/zlib é recusada na deny-list
      (`DENY_PIRATARIA`), aplicada em `find_pdf`, `download_pdf` e no fallback
      indireto.
    - Não coletar fontes de conteúdo adulto (filtro `DENY_ADULTO` na Etapa C).
    - Só baixa PDF quando o servidor confirma `Content-Type: application/pdf`.
    - Trust (confiança): toda referência gravada carrega `trust`/`origem`/
      `validado_por` no frontmatter — `ok` (>= 2 fontes editoriais) -> alta;
      `validacao_indireta` -> media; `fabricacao` (alerta) -> fraca.

Uso:
    python -m harness.webscraper "<título>" --autor "<autor>"
    python -m harness.webscraper "<título>" --autor "<autor>" --pdf
    python -m harness.webscraper "<título>" --busca-apenas

Import programático:
    from harness.webscraper import (coletar, search_books, validate_fabrication,
                                    fallback_indireto)

--------------------------------------------------------------------------
Detalhes operacionais do protocolo (movidos do SKILL.md de webscraping —
Lote 2). O documenter lê seletivamente quando precisa; o SKILL.md mantém o
resumo.

Método de busca (passo a passo)
-------------------------------
1. `websearch` com o padrão: `<título> <autor> summary/table of contents`
   (ou "official site", "free pdf", "github").
2. `webfetch` nas páginas candidatas (site do autor, página da editora,
   Google Books, repositório).
3. Extraia: sumário/índice, conceitos-chave, padrões e exemplos aplicáveis.
4. Confirme a edição (ano, ISBN quando disponível) e o autor correto — não
   confunda obras homônimas.
5. Verifique a fabricação em 2 fontes ANTES de extrair.

Downloads legítimos
-------------------
- Baixe **apenas** PDFs gratuitos oficiais (autor/editora/universidade) para
  `sidePrjs/<projeto>/docs/livros/<slug>.pdf`, **com a fonte registrada**
  (URL) na referência em `memory/references/`.
- **Recusar pirataria**: somente a família **z-lib/zlib** é recusada
  (deny-list atual). Não há exceção.
- Preferir capítulos-amostra oficiais quando a obra completa não for livre.

Fallback por conhecimento indireto (obra fabricada ou sem fontes)
------------------------------------------------------------------
Quando a validação anti-fabricação falha, o módulo **não para no alerta**:
além de gravar `<slug>-fabricacao.md`, executa `fallback_indireto` — coleta o
conhecimento temático da obra **indiretamente, por tópicos** — e grava
`memory/references/<slug>-indireto.md` com a tag `validacao_indireta`.

**Gatilho**: `validate_fabrication` retorna `existe: false` (fabricação) ou o
fallback não encontra nada (`sem_fontes`). O resultado entra no dict de
resposta em `fallback_indireto` (com `status: ok | parcial | sem_fontes`); o
status do alerta de fabricação continua `fabricacao`.

Etapas (A–F):

- **A — Consultas rápidas de contexto**: `search_web` + `search_books` com
  título+autor para obter um resumo dos contextos exatos (descrições, resenhas,
  sumário, páginas editoriais). Obra fabricada com pouca informação → usa a
  descrição do título/autor e obras homônimas como referência, com **ressalva
  explícita**.
- **B — Ranking de tópicos por frequência de termos**: **sem filtro de
  importância** — conta palavras/caracteres por tema no conteúdo coletado e
  ordena do mais frequente para o menos. **Limite: até 50 tópicos** por livro
  (constante `MAX_TOPICOS`); os 50 primeiros são os escolhidos.
- **C — Pesquisa indireta em fontes públicas**: para cada tópico (até 50),
  busca fontes públicas confiáveis (documentação oficial, artigos, cursos,
  papers, sites de autor/editora). **PDFs do GitHub são aceitos como fonte de
  validação** (podem ser antigos — ver Etapa D). A deny-list atual (somente
  z-lib/zlib) e o filtro de hosts de conteúdo adulto (`DENY_ADULTO`:
  xnxx, xvideos, pornhub, etc.) são aplicados na filtragem.
- **D — Validação de atualidade (janela de 5 anos)**: qualquer fonte com mais
  de 5 anos (ano atual − 5, não só PDFs do GitHub) dispara uma **consulta
  única** usando o ano atual para verificar se o tema ainda vale. Se o tema
  não reaparece, a consulta é ampliada com variações de query; se **≥ 2 hosts
  independentes** apontam o mesmo padrão substituto, o padrão atual é
  **identificado e consumido**: o módulo refaz a busca de fontes do tópico com
  o padrão anexado à query (segunda passada limitada, até 5 resultados) e
  registra essas fontes no tópico marcadas como `[padrão atual]`. Sem loop de
  re-verificação; o padrão identificado entra nas fontes e na coluna de
  atualidade do tópico, mas **não substitui automaticamente o tema** — a
  curadoria final é do documenter.
- **E — Validação contextual**: cada tópico é validado contra o contexto do
  livro — **≥ 2 termos-chave compartilhados** (>= 4 caracteres, comparação por
  **token exato** — não substring — para não casar "tem" com "tempo") entre o
  sumário do contexto e as fontes do tema **E ≥ 2 fontes independentes** →
  `confirmado`; senão `nao_confirmado` (tema fora do contexto ou sem fontes
  suficientes). O filtro semântico fino (LLM) é feito depois pelo agente
  **documenter** — o módulo deixa os dados estruturados prontos (`temas`,
  `fontes_por_tema`, `status_por_tema`).
- **F — Gravação na memória**: `memory/references/<slug>-indireto.md` com
  frontmatter padrão, tags incluindo `validacao_indireta`, sumário temático,
  lista de tópicos rankeados (até 50), fontes públicas por tópico com data e
  status por tópico (confirmado/nao_confirmado), e a **ressalva de que o livro
  não foi validado diretamente** — o conhecimento foi adquirido indiretamente
  por tópicos. `memory/references/index.md` é atualizado via `_reindex()`.

Inspeção sem gravar:

    python -m harness.webscraper "<título>" --autor "<autor>" --busca-apenas

Armadilhas (Windows e rede)
---------------------------
- **Console cp1252 quebra acentos** (Windows): rode o extractor com
  `PYTHONIOENCODING=utf-8` e use apenas ASCII em prints/identificadores.
- **Editoras bloqueiam bots (403)**: O'Reilly/Manning direto → usar site do
  autor, Google Books, GitHub, versões gratuitas oficiais.
- **Formulários com dados pessoais**: não preencher; abortar a coleta na
  página.
- **Concorrência entre sessões**: vários lotes gravando referências em
  paralelo podem dessincronizar `memory/references/index.md` — ao final,
  re-consolide o índice (uma única passada por todos os `<slug>.md`).

Saída esperada
--------------
Relatório com uma **tabela por item**:

| Título | Autor | Sucesso (SIM/PARCIAL/NÃO/INDIRETO) | URLs | Alertas de fabricação |

- `SIM` — referência completa criada e indexada;
- `PARCIAL` — só sumário/metadados ou capítulo-amostra (registrar o que
  faltou);
- `NÃO` — nenhuma fonte legítima (registrar motivo);
- `INDIRETO` — obra fabricada/sem fontes, mas conhecimento por tópicos
  coletado via `fallback_indireto`: referência `<slug>-indireto.md` criada com
  a tag `validacao_indireta` (status `ok`/`parcial` no campo
  `fallback_indireto`); se nem isso foi encontrado, `fallback_indireto.status`
  é `sem_fontes`.

Fabricação detectada → linha com `NÃO`/`PARCIAL`/`INDIRETO` + alerta explícito
e o arquivo `-fabricacao.md` criado. Nenhuma execução de scraping termina sem a
tabela de saída e sem o índice de referências atualizado.
"""

from __future__ import annotations

import argparse
import base64
import html as html_mod
import ipaddress
import json
import pathlib
import re
import socket
import ssl
import sys
import time
import urllib.error
import urllib.parse
import urllib.request
from datetime import date, datetime

from . import config
from . import frontmatter
from .memory import recuperavel_de
from .namer import slugify

# ---------------------------------------------------------------------------
# Constantes
# ---------------------------------------------------------------------------

USER_AGENT = (
    "Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36 "
    "(KHTML, like Gecko) Chrome/126.0 Safari/537.36"
)
TIMEOUT = 15  # segundos por requisição
RETRY_MAX = 2  # tentativas extras por requisição (Exception Handling, Ch12)
RETRY_BACKOFF = 1.0  # segundos entre tentativas

# Limite de PDF baixado (80 MB) — proteção contra arquivo gigante/stream corrompido
MAX_PDF_BYTES = 80 * 1024 * 1024

# Deny-list anti-pirataria (independe de letra maiúscula/subdomínio).
# Política atual: somente a família z-lib/zlib é recusada (decisão do usuário).
DENY_PIRATARIA = (
    "z-lib",
    "zlib",
)

# Hosts de conteúdo adulto recusados na coleta de fontes públicas (Etapa C).
# Mecanismo de substring no host, semelhante à deny-list de pirataria.
DENY_ADULTO = (
    "xnxx",
    "xvideos",
    "xhamster",
    "pornhub",
    "youporn",
    "redtube",
    "tube8",
    "spankbang",
    "porntrex",
    "pornhd",
    "onlyfans",
    "hentai",
    "porno",
)

# Domínios editoriais confiáveis usados na validação de fabricação.
# Fontes independentes: site do autor, Google Books, Open Library, Internet
# Archive, Goodreads, Amazon e páginas oficiais de editoras.
# TODAS são domínios PLENOS (sem entrada "prefixo"): o match é por SUFIXO
# (host == domínio OU subdomínio). `books.google` virou os domínios concretos
# do Google Books — um host `books.google.com.evil.example` NUNCA casa.
EDITORIAL_HOSTS = (
    "books.google.com",
    "books.google.com.br",
    "books.google.co.uk",
    "openlibrary.org",
    "archive.org",
    "goodreads.com",
    "amazon.com",
    "oreilly.com",
    "manning.com",
    "packtpub.com",
    "springer.com",
    "wiley.com",
    "mitpress.mit.edu",
    "cambridge.org",
    "github.com",
)

# Fontes de livros por API (estáveis — JSON documentado). Na validação
# anti-fabricação, cada API distinta conta como 1 fonte independente FORTE,
# com prioridade sobre a busca web (que é best-effort / markup frágil).
FONTE_APIS = frozenset({"google-books", "open-library", "internet-archive"})

REF_DIR = config.MEMORY_DIR / "references"
INDEX_FILE = REF_DIR / "index.md"

ssl_ctx = ssl.create_default_context()


# ---------------------------------------------------------------------------
# HTTP (com retry/backoff e User-Agent)
# ---------------------------------------------------------------------------

# Anti-SSRF: teto de redirects seguidos (o default do urllib é 10; aqui é
# reduzido e cada hop é revalidado contra hosts internos).
MAX_REDIRECTS = 5


def _host_ips(host: str) -> list[str]:
    """Resolve `host` para IPs. Nunca crasha (lista vazia em falha)."""
    try:
        return sorted({item[4][0] for item in socket.getaddrinfo(host, None)})
    except (socket.gaierror, OSError, ValueError):
        return []


def _ip_perigoso(ip: str) -> bool:
    """True se o IP é alvo de SSRF interno: loopback, privado, link-local,
    multicast, não-especificado, reservado ou CGNAT (RFC 6598). IP inválido
    também conta (conservador — não arrisca)."""
    try:
        addr = ipaddress.ip_address(ip.split("%")[0])
    except ValueError:
        return True
    if (addr.is_loopback or addr.is_private or addr.is_link_local
            or addr.is_multicast or addr.is_unspecified or addr.is_reserved):
        return True
    if isinstance(addr, ipaddress.IPv4Address) \
            and addr in ipaddress.ip_network("100.64.0.0/10"):
        return True
    return False


def _ssrf_valido(url: str, allow_loopback: bool = False) -> bool:
    """Valida URL para fetch externo (anti-SSRF): scheme http/https e host
    resolvido NÃO pode ser loopback/privado/link-local/etc.

    `allow_loopback=True` permite explicitamente hosts loopback (ex.: testes
    unitários contra servidor local) — NUNCA habilita outros IPs internos e
    NÃO afeta os callers de produção (default False). DNS falhou/inválido ->
    False (conservador: não arrisca). Nota residual: a validação é
    resolve-depois-valida; um atacante com controle de DNS pode rebinding
    (TOCTOU) — mitigação padrão, não fronteira absoluta."""
    try:
        partes = urllib.parse.urlparse(url)
    except ValueError:
        return False
    if partes.scheme not in ("http", "https"):
        return False
    host = partes.hostname
    if not host:
        return False
    ips = _host_ips(host)
    if not ips:
        return False
    if allow_loopback:
        # exclusivo para testes locais: todos os IPs resolvidos DEVEM ser
        # loopback (nunca libera IP público/privado junto).
        return bool(ips) and all(_eh_loopback(ip) for ip in ips)
    return not any(_ip_perigoso(ip) for ip in ips)


def _eh_loopback(ip: str) -> bool:
    """True se o IP é loopback (127.0.0.0/8, ::1)."""
    try:
        return ipaddress.ip_address(ip.split("%")[0]).is_loopback
    except ValueError:
        return False


def _ip_literal_normalizado(host: str) -> str | None:
    """Normaliza um host literal de IP (IPv4/IPv6) para a forma canônica.

    Retorna `None` se `host` não for um literal de IP. Cobre o IPv4 canônico e
    o IPv6 via `ipaddress` e, como fallback, as formas ABREVIADAS/hexa/octal
    do IPv4 legado (`127.1`, `0x7f.1`, `0177.0.0.1`, inteiro de 32 bits)
    aceitas por `socket.inet_aton`. Assim `_ip_perigoso` (que exige IP válido)
    pode ser reutilizado com segurança. Sem DNS: apenas parsing literal.
    """
    try:
        return str(ipaddress.ip_address(host))
    except ValueError:
        pass
    try:
        empacotado = socket.inet_aton(host)
    except (OSError, ValueError):
        return None
    return socket.inet_ntoa(empacotado)


def _url_externa_segura(url: str) -> bool:
    """True se a URL é http/https e aponta para um host EXTERNO/público.

    Variante SEM DNS (adequada a resultados de busca web, best-effort): só
    inspeciona o host LITERAL, então nunca resolve nomes. Rejeita:
      - esquemas diferentes de http/https;
      - host vazio/inválido;
      - IP literal loopback/privado/link-local/multicast/não-especificado/
        reservado/CGNAT (10/8, 172.16/12, 192.168/16, 127/8, 169.254/16,
        0.0.0.0/8, 100.64/10, ::1, fe80::/10, ...), incluindo as formas
        ABREVIADAS/hexa/octal do IPv4 legado (`127.1`, `user@127.1`,
        `0x7f.1`, `0x7f.0.0.1`, `0177.0.0.1`, inteiro de 32 bits), que
        `socket.inet_aton` normaliza antes da checagem;
      - nomes internos: `localhost`, host sem ponto (nome de máquina) e
        sufixos `.local`/`.internal`.

    É um filtro de ENTRADA para descartar resultados de busca que apontariam
    para a rede interna — NÃO substitui a fronteira anti-SSRF do transporte
    (`_ssrf_valido`, que resolve DNS e revalida redirects).
    """
    try:
        partes = urllib.parse.urlparse(url)
    except ValueError:
        return False
    if partes.scheme not in ("http", "https"):
        return False
    host = partes.hostname
    if not host:
        return False
    host = host.strip().rstrip(".")
    if not host:
        return False
    ip_norm = _ip_literal_normalizado(host)
    if ip_norm is not None:
        if _ip_perigoso(ip_norm):
            return False
        # 0.0.0.0/8 (inclui 0.0.0.1..): "este host"/rota inválida.
        addr = ipaddress.ip_address(ip_norm)
        if addr.version == 4 and addr in ipaddress.ip_network("0.0.0.0/8"):
            return False
        return True
    # Nome de host (sem DNS): exige domínio com ponto e sem TLD interno.
    if "." not in host:
        return False
    host_lower = host.lower()
    if host_lower.endswith((".local", ".internal")):
        return False
    return True


class _SsrfRedirectHandler(urllib.request.HTTPRedirectHandler):
    """Segue redirects com revalidação SSRF em cada hop e teto de saltos.

    Se o alvo do redirect for inválido (scheme não-http(s), host interno),
    levanta URLError antes de conectar — impede que um site externo redirecione
    o scraper para serviços internos (127.0.0.1, RFC-1918)."""

    max_redirections = MAX_REDIRECTS

    def __init__(self, allow_loopback: bool = False):
        super().__init__()
        self._allow_loopback = allow_loopback

    def redirect_request(self, req, fp, code, msg, headers, newurl):
        if not _ssrf_valido(newurl, allow_loopback=self._allow_loopback):
            raise urllib.error.URLError(
                f"redirect bloqueado (SSRF): {newurl!r}"
            )
        return super().redirect_request(req, fp, code, msg, headers, newurl)


def _fetch(url: str, timeout: int = TIMEOUT, retries: int = RETRY_MAX,
           allow_loopback: bool = False) -> tuple[int, bytes, str]:
    """Baixa `url` com User-Agent, retry/backoff, timeout e proteção anti-SSRF.

    Valida o alvo inicial (scheme http/https + host externo) e REVALIDA cada
    redirect (teto de `MAX_REDIRECTS` hops). `allow_loopback` (exclusivo para
    testes locais) permite explicitamente hosts 127.0.0.1/::1. Retorna (status,
    corpo, content_type). Levanta `urllib.error.URLError` (ou TimeoutError)
    após esgotar as tentativas; URL inválida/SSRF -> URLError imediato.
    """
    if not _ssrf_valido(url, allow_loopback=allow_loopback):
        raise urllib.error.URLError(f"URL bloqueada (SSRF): {url!r}")
    req = urllib.request.Request(
        url,
        headers={
            "User-Agent": USER_AGENT,
            "Accept": "text/html,application/xhtml+xml,application/json;q=0.9,*/*;q=0.8",
        },
    )
    # Opener próprio: HTTPSHandler com o contexto de TLS (verificação de
    # certificado) + redirect handler com revalidação SSRF.
    abridor = urllib.request.build_opener(
        urllib.request.HTTPSHandler(context=ssl_ctx),
        _SsrfRedirectHandler(allow_loopback=allow_loopback),
    )
    ultimo_erro: Exception | None = None
    for tentativa in range(retries + 1):
        try:
            with abridor.open(req, timeout=timeout) as resp:
                body = resp.read(MAX_PDF_BYTES + 1024)
                ct = resp.headers.get("Content-Type", "") or ""
                return resp.status, body, ct
        except (urllib.error.URLError, TimeoutError, ConnectionError, ssl.SSLError) as exc:
            ultimo_erro = exc
            if tentativa < retries:
                time.sleep(RETRY_BACKOFF * (tentativa + 1))
    raise (ultimo_erro or RuntimeError("falha de rede"))


def _fetch_json(url: str) -> dict | list | None:
    """Busca URL que deve retornar JSON; retorna None em falha/JSON inválido."""
    try:
        status, body, ct = _fetch(url)
    except Exception:  # noqa: BLE001
        return None
    if status != 200:
        return None
    try:
        return json.loads(body.decode("utf-8"))
    except (UnicodeDecodeError, json.JSONDecodeError):
        return None


def _fetch_text(url: str) -> str | None:
    """Busca URL que deve retornar texto/HTML; retorna None em falha."""
    try:
        status, body, ct = _fetch(url)
    except Exception:  # noqa: BLE001
        return None
    if status != 200:
        return None
    return body.decode("utf-8", errors="replace")


# ---------------------------------------------------------------------------
# Busca web (failover: DuckDuckGo HTML -> Bing HTML)
# ---------------------------------------------------------------------------

def _strip_tags(texto: str) -> str:
    """Remove tags HTML e entidades de um fragmento."""
    sem_tags = re.sub(r"<[^>]+>", " ", texto)
    return html_mod.unescape(re.sub(r"\s+", " ", sem_tags)).strip()


def _busca_duckduckgo(query: str, limite: int = 5) -> list[dict]:
    """Busca no DuckDuckGo (HTML simples, sem API/chave)."""
    url = "https://html.duckduckgo.com/html/?q=" + urllib.parse.quote(query)
    texto = _fetch_text(url)
    if not texto:
        return []
    resultados: list[dict] = []
    # Cada resultado vem em <a class="result__a" href="...">Título</a> seguido
    # de um bloco com <a class="result__snippet" ...>Descrição</a>.
    blocos = re.split(r'<div class="result results_links', texto)
    for bloco in blocos[1:]:
        a = re.search(r'<a[^>]*class="[^"]*result__a[^"]*"[^>]*href="([^"]+)"[^>]*>(.*?)</a>', bloco, re.S)
        sn = re.search(r'<a[^>]*class="[^"]*result__snippet[^"]*"[^>]*>(.*?)</a>', bloco, re.S)
        if not a:
            continue
        href = a.group(1)
        # DuckDuckGo redireciona via /l/?uddg=<url>&...
        m = re.search(r"[?&]uddg=([^&]+)", href)
        url_real = urllib.parse.unquote(m.group(1)) if m else href
        titulo = _strip_tags(a.group(2))
        resumo = _strip_tags(sn.group(1)) if sn else ""
        resultados.append({"titulo": titulo, "url": url_real, "resumo": resumo})
        if len(resultados) >= limite:
            break
    return resultados


def _bing_destino(href: str) -> str:
    """Decodifica o destino real do redirect `https://www.bing.com/ck/a?...u=<b64>`.

    O Bing atual embute o alvo em base64url no parâmetro `u` (com prefixo
    `a1`). Retorna o href original se não conseguir decodificar.
    """
    href = href.replace("&amp;", "&")
    try:
        u = urllib.parse.parse_qs(urllib.parse.urlparse(href).query).get("u", [""])[0]
    except Exception:  # noqa: BLE001
        return href
    if not u:
        return href
    if u.startswith("a1"):
        u = u[2:]
    pad = "=" * (-len(u) % 4)
    try:
        alvo = base64.urlsafe_b64decode(u + pad).decode("utf-8", errors="replace")
    except Exception:  # noqa: BLE001
        return href
    return alvo if alvo.startswith(("http://", "https://")) else href


def _busca_bing(query: str, limite: int = 5) -> list[dict]:
    """Busca no Bing (fallback do DuckDuckGo).

    O markup atual usa `<li class="b_algo">` com links de redirect
    `bing.com/ck/a` (alvo real em base64 no parâmetro `u`) e resumo em `<p>`.
    """
    url = "https://www.bing.com/search?q=" + urllib.parse.quote(query)
    texto = _fetch_text(url)
    if not texto:
        return []
    resultados: list[dict] = []
    for bloco in re.split(r'<li class="b_algo"', texto)[1:]:
        alvo = ""
        titulo = ""
        for m in re.finditer(
            r'<a[^>]+href="(https?://[^"]+)"[^>]*>(.*?)</a>', bloco, re.S
        ):
            href = m.group(1)
            if "u=" in href:
                alvo = _bing_destino(href)
                titulo = _strip_tags(m.group(2))
                break
        if not alvo:
            continue
        resumo = ""
        m = re.search(r"<p[^>]*>(.*?)</p>", bloco, re.S)
        if m:
            resumo = _strip_tags(m.group(1))
        resultados.append({"titulo": titulo or alvo, "url": alvo, "resumo": resumo})
        if len(resultados) >= limite:
            break
    return resultados


def search_web(query: str, limite: int = 5) -> list[dict]:
    """Busca na web com failover automático (DuckDuckGo -> Bing)."""
    resultados = _busca_duckduckgo(query, limite)
    if resultados:
        return resultados
    return _busca_bing(query, limite)


# ---------------------------------------------------------------------------
# APIs de livros (sem chave)
# ---------------------------------------------------------------------------

# Falhas de API na última chamada (degradação NÃO silenciosa): {api: mensagem}.
# Preenchida pelas funções de API quando a requisição falha (rede/429/JSON
# inválido) e consumida por `validate_fabrication` no campo `erros_api` — uma
# falha não pode ser confundida com "0 resultados".
_FALHAS_API: dict[str, str] = {}


def _sem_artigo_inicial(titulo: str) -> str:
    """Remove artigos/stopwords iniciais do título (ex.: "The X" -> "X").

    Usado nas queries de API: buscar com o artigo inicial ("The ...") tende a
    retornar 0 resultados nas APIs de livros.
    """
    partes = titulo.split()
    while partes and partes[0].strip(".,;:!?()\"'").lower() in STOPWORDS:
        partes.pop(0)
    return " ".join(partes)


def _google_books(titulo: str, autor: str = "") -> list[dict]:
    """Busca Google Books API (sem chave)."""
    q = f'intitle:{titulo}'
    if autor:
        q += f" inauthor:{autor}"
    url = "https://www.googleapis.com/books/v1/volumes?q=" + urllib.parse.quote(q)
    dados = _fetch_json(url)
    if not isinstance(dados, dict):
        _FALHAS_API["google-books"] = "requisição falhou (rede/429/JSON inválido)"
        return []
    _FALHAS_API.pop("google-books", None)
    out: list[dict] = []
    for item in dados.get("items", [])[:3]:
        vi = item.get("volumeInfo", {})
        out.append({
            "fonte": "google-books",
            "titulo": vi.get("title", ""),
            "autores": vi.get("authors", []),
            "ano": (vi.get("publishedDate") or "")[:4],
            "editora": vi.get("publisher", ""),
            "isbn": next((i.get("identifier", "") for i in vi.get("industryIdentifiers", [])
                          if i.get("type") == "ISBN_13"), ""),
            "url": vi.get("infoLink", ""),
            "descricao": (vi.get("description") or "")[:300],
        })
    return out


def _open_library(titulo: str, autor: str = "") -> list[dict]:
    """Busca Open Library Search API (sem chave)."""
    q = f'title:"{titulo}"'
    if autor:
        q += f" author:{autor}"
    url = "https://openlibrary.org/search.json?q=" + urllib.parse.quote(q)
    dados = _fetch_json(url)
    if not isinstance(dados, dict):
        _FALHAS_API["open-library"] = "requisição falhou (rede/429/JSON inválido)"
        return []
    _FALHAS_API.pop("open-library", None)
    docs = dados.get("docs", [])
    if not docs:
        # Variação sem o artigo inicial ("The X" -> "X"): casamento mais robusto.
        sem_artigo = _sem_artigo_inicial(titulo)
        if sem_artigo and sem_artigo != titulo:
            q2 = f'title:"{sem_artigo}"'
            if autor:
                q2 += f" author:{autor}"
            dados2 = _fetch_json(
                "https://openlibrary.org/search.json?q=" + urllib.parse.quote(q2)
            )
            if isinstance(dados2, dict):
                docs = dados2.get("docs", [])
    out: list[dict] = []
    for doc in docs[:3]:
        out.append({
            "fonte": "open-library",
            "titulo": doc.get("title", ""),
            "autores": doc.get("author_name", []),
            "ano": str(doc.get("first_publish_year") or ""),
            "editora": "",
            "isbn": (doc.get("isbn", []) or [""])[0],
            "url": f"https://openlibrary.org{doc.get('key', '')}",
            "descricao": "",
        })
    return out


def _internet_archive(titulo: str, autor: str = "") -> list[dict]:
    """Busca Internet Archive Advanced Search API (sem chave)."""

    def _url(q: str) -> str:
        return (
            "https://archive.org/advancedsearch.php?"
            "fl=identifier,title,creator,year,downloads"
            f"&q={urllib.parse.quote(q)}&rows=3&page=1&output=json"
        )

    # `title:` sozinho (título simplificado, sem artigo inicial) é mais robusto
    # que combinado com `creator:` ("The X" + creator tende a 0 resultados).
    simplificado = _sem_artigo_inicial(titulo) or titulo
    dados = _fetch_json(_url(f"title:({simplificado})"))
    if not isinstance(dados, dict):
        _FALHAS_API["internet-archive"] = "requisição falhou (rede/429/JSON inválido)"
        return []
    _FALHAS_API.pop("internet-archive", None)
    docs = dados.get("response", {}).get("docs", [])
    if not docs and autor:
        dados2 = _fetch_json(_url(f"title:({simplificado}) AND creator:({autor})"))
        if isinstance(dados2, dict):
            docs = dados2.get("response", {}).get("docs", [])
    out: list[dict] = []
    for doc in docs:
        ident = doc.get("identifier", "")
        out.append({
            "fonte": "internet-archive",
            "titulo": doc.get("title", ""),
            "autores": [doc["creator"]] if isinstance(doc.get("creator"), str) else doc.get("creator", []),
            "ano": str(doc.get("year") or ""),
            "editora": "",
            "isbn": "",
            "url": f"https://archive.org/details/{ident}",
            "descricao": "",
        })
    return out


def search_books(titulo: str, autor: str = "") -> list[dict]:
    """Consulta 3 APIs de livros (Google Books, Open Library, Internet Archive).

    Retorna a lista consolidada de resultados (com `fonte` em cada item).
    """
    return (
        _google_books(titulo, autor)
        + _open_library(titulo, autor)
        + _internet_archive(titulo, autor)
    )


# ---------------------------------------------------------------------------
# Validação anti-fabricação
# ---------------------------------------------------------------------------

def _host(url: str) -> str:
    try:
        return urllib.parse.urlparse(url).netloc.lower()
    except ValueError:
        return ""


def _host_editorial(url: str) -> bool:
    """True se o host pertence a um domínio editorial confiável.

    Match por SUFIXO de domínio com fronteira de rótulo (NUNCA substring nem
    prefixo): o host DEVE ser o domínio exato OU um subdomínio (`amazon.com`,
    `www.amazon.com`). `amazon.com.evil.example` e
    `books.google.com.evil.example` NÃO casam — antes, substring/prefixo
    permitiam falsos positivos exploráveis para passar a checagem de
    fabricação."""
    host = _host(url)
    if not host:
        return False
    host_limpo = host.split(":", 1)[0].rstrip(".")
    return any(
        host_limpo == dom or host_limpo.endswith("." + dom)
        for dom in EDITORIAL_HOSTS
    )


def _resultado_relevante(resultado: dict, titulo: str, autor: str = "") -> bool:
    """True se um resultado de busca web REFERENCIA a obra consultada.

    Evita que homepages/landings editoriais genéricas (ex.: homepage do Google
    Books, listagem da Amazon) contem como "evidência" de existência — elas não
    citam a obra, só o domínio. O resultado é relevante se o texto alvo
    (título do resultado + URL) contém:
      - >= 2 tokens de conteúdo do TÍTULO da obra, OU
      - >= 1 token do título E >= 1 token do autor.
    Tokens: letras/dígitos >= 3 chars, minúsculas/sem acentos, sem stopwords.
    """
    titulo_tokens = {t for t in _tokenizar(titulo)
                     if t not in STOPWORDS and len(t) >= 3}
    if not titulo_tokens:
        return False
    autor_tokens = {t for t in _tokenizar(autor)
                    if t not in STOPWORDS and len(t) >= 3}
    alvo = f"{resultado.get('titulo', '')} {resultado.get('url', '')}"
    alvo_tokens = set(_tokenizar(alvo))
    if len(titulo_tokens & alvo_tokens) >= 2:
        return True
    return bool(titulo_tokens & alvo_tokens and autor_tokens & alvo_tokens)


def validate_fabrication(titulo: str, autor: str = "") -> dict:
    """Confirma a existência da obra em >= 2 fontes editoriais independentes.

    Estratégia autônoma (falha para a próxima fonte), com **prioridade às APIs
    de livros** (Google Books, Open Library, Internet Archive) — estáveis
    (JSON documentado) — e a **busca web como reforço (best-effort)**:

      1. APIs de livros — cada API distinta com resultado relevante conta como
         1 fonte independente FORTE.
      2. Busca web (DuckDuckGo -> Bing) filtrando por domínio editorial E
         relevância (`_resultado_relevante`): homepages/landings editoriais
         genéricas (ex.: homepage do Google Books, listagem da Amazon) NÃO
         contam como evidência — não citam a obra. Resultados cujo host NÃO
         seja externo/seguro (`_url_externa_segura`: anti-SSRF sem DNS —
         loopback/privado/link-local/nome interno) são descartados antes de
         contar. A busca é **reforço (best-effort)**: se degradar
         (markup/anti-bot/consentimento) ou falhar (exceção), ela NUNCA
         derruba a validação — é envolvida em try/except e tratada como
         vazia.

      Critério de existência:
      - 2+ APIs distintas confirmam => existe (independência forte entre as
        APIs), mesmo sem busca web.
      - 1 API + 1 resultado de busca web RELEVANTE (domínio editorial que cita
        a obra) => existe (2 tipos distintos).

      Evidência parcial (>= 1 API REAL confirma, mas abaixo do limiar):
      `existe: False` + `indeterminado: True` — a obra NÃO é confirmada, mas
      também não é fabricação clara (evita falso alerta quando uma API real
      aponta que a obra existe).

      Zero evidência real de API => `existe: False` + `indeterminado: False`
      (fabricação provável; o chamador grava o alerta). Ruído de busca web
      (homepages editoriais sem citação da obra) NÃO conta como evidência.

      Degradação de API (rede/429/JSON inválido) é exposta em `erros_api`
      ({api: mensagem}) — falha NÃO é tratada como "0 resultados" silencioso.

    Retorna dict com `existe`, `indeterminado`, `fontes` (lista de dicts),
    `fontes_por_tipo` (contagem por tipo de fonte), `total_fontes`, `n_apis`
    (APIs distintas), `erros_api` e `detalhe` (human-readable).
    """
    fontes: list[dict] = []
    # 1) APIs de livros (fontes estáveis — prioridade máxima)
    apis = search_books(titulo, autor)
    for item in apis:
        if item.get("titulo") and item.get("url"):
            fonte = {
                "fonte": item["fonte"],
                "url": item["url"],
                "titulo": item["titulo"],
                "autores": item.get("autores", []),
            }
            if fonte not in fontes:
                fontes.append(fonte)
    # Falhas de API (rede/429/JSON inválido) são expostas, não silenciosas.
    erros_api = dict(_FALHAS_API)
    _FALHAS_API.clear()

    # 2) Busca web por domínios editoriais E relevância (bônus/best-effort).
    # Falha da busca nunca derruba a validação (markup frágil, anti-bot, rede).
    query = f"{titulo} {autor}" if autor else titulo
    try:
        web_resultados = search_web(query, limite=6)
    except Exception:  # noqa: BLE001 -- busca web é best-effort; nunca derruba
        web_resultados = []
    web_relevante = 0
    for r in web_resultados:
        # Anti-SSRF (sem DNS): descarta resultado que apontaria para a rede
        # interna (loopback/privado/link-local/nome interno) ANTES de contar
        # como evidência — nunca entra em `fontes`. Best-effort: não derruba.
        if not _url_externa_segura(r.get("url", "")):
            continue
        if _host_editorial(r["url"]) and _resultado_relevante(r, titulo, autor):
            web_relevante += 1
            fonte = {"fonte": "web-editorial", "url": r["url"], "titulo": r["titulo"]}
            if fonte not in fontes:
                fontes.append(fonte)

    # Contagem por tipo de fonte e por API distinta
    fontes_por_tipo: dict[str, int] = {}
    for f in fontes:
        fontes_por_tipo[f["fonte"]] = fontes_por_tipo.get(f["fonte"], 0) + 1
    apis_distintas = sum(1 for t in FONTE_APIS if fontes_por_tipo.get(t))
    chaves = sorted(fontes_por_tipo)  # tipos distintos
    n_tipos = len(chaves)

    # Existe: >= 2 fontes independentes. Isso cobre 2+ APIs distintas (mesmo
    # sem busca web) e 1 API + 1 resultado web RELEVANTE (domínio editorial
    # que cita a obra — homepage genérica não conta).
    existe = apis_distintas >= 2 or (apis_distintas >= 1 and web_relevante >= 1)

    # Evidência parcial (>= 1 API REAL, mas abaixo do limiar): indeterminado,
    # não fabricação clara — evita falso alerta quando uma API confirma.
    # Ruído de busca web (homepage editorial) NÃO conta como evidência.
    indeterminado = (not existe) and apis_distintas >= 1

    if existe:
        detalhe = (
            f"obra confirmada em {n_tipos} tipo(s) de fonte "
            f"({', '.join(chaves)}): {apis_distintas} API(s) de livro + "
            f"{fontes_por_tipo.get('web-editorial', 0)} domínio(s) editorial(is)"
        )
    elif indeterminado:
        detalhe = (
            f"obra NÃO confirmada (evidência insuficiente): encontrada em "
            f"{n_tipos} tipo(s) de fonte ({', '.join(chaves)}), abaixo do limiar "
            f"de 2 fontes independentes. Evidência parcial presente — não é "
            f"fabricação clara"
        )
    else:
        detalhe = (
            "obra NÃO confirmada em nenhuma fonte editorial (provável fabricação)"
        )

    return {
        "existe": existe,
        "indeterminado": indeterminado,
        "fontes": fontes,
        "fontes_por_tipo": fontes_por_tipo,
        "total_fontes": n_tipos,
        "n_apis": apis_distintas,
        "erros_api": erros_api,
        "detalhe": detalhe,
    }


# ---------------------------------------------------------------------------
# Busca de PDF legítimo
# ---------------------------------------------------------------------------

def _url_pirata(url: str) -> bool:
    host = _host(url)
    return any(p in host for p in DENY_PIRATARIA)


def _host_adulto(url: str) -> bool:
    """True se o host está na lista de conteúdo adulto (substring no host)."""
    host = _host(url)
    return any(p in host for p in DENY_ADULTO)


def _eh_pdf_valido(url: str, allow_loopback: bool = False) -> bool:
    """Verifica se a URL aponta para um PDF real (Content-Type)."""
    if _url_pirata(url):
        return False
    try:
        status, _body, ct = _fetch(url, timeout=10, allow_loopback=allow_loopback)
    except Exception:  # noqa: BLE001
        return False
    if status != 200:
        return False
    return "application/pdf" in ct.lower() or url.lower().endswith(".pdf")


def find_pdf(titulo: str, autor: str = "", limite: int = 8) -> list[dict]:
    """Encontra URLs de PDFs legítimos da obra.

    Fontes tentadas em ordem (falha -> próxima):
      1. Internet Archive (obras de acesso aberto mantidas pela própria IA).
      2. Busca web filtrando por: site do autor, GitHub oficial, universidades
         (.edu), domínios editoriais — e recusando a deny-list de pirataria.
      3. Open Library (que aponta para archive.org em obras abertas).

    Retorna lista de dicts {url, titulo, fonte, tipo} já validados
    (Content-Type application/pdf ou extensão .pdf).
    """
    candidatos: list[dict] = []
    vistos: set[str] = set()

    def _adiciona(url: str, titulo_item: str, fonte: str) -> None:
        if url in vistos:
            return
        vistos.add(url)
        candidatos.append({"url": url, "titulo": titulo_item, "fonte": fonte})

    # 1) Internet Archive: arquivos `*_djvu.txt`/texto indicam obra com PDF
    for item in _internet_archive(titulo, autor):
        ident = item["url"].rstrip("/").rsplit("/", 1)[-1]
        pdf_url = f"https://archive.org/download/{ident}/{ident}.pdf"
        _adiciona(pdf_url, item["titulo"], "internet-archive")

    # 2) Busca web focada em PDFs oficiais
    query = f'{titulo} {autor} free pdf official' if autor else f'{titulo} free pdf official'
    for r in search_web(query, limite=limite):
        url = r["url"]
        if _url_pirata(url):
            continue
        host = _host(url)
        if host.endswith(".pdf"):
            _adiciona(url, r["titulo"], "web-pdf")
        elif _host_editorial(url) or host.endswith(".edu") or "github" in host:
            if "github" in host and "/blob/" in url:
                url = url.replace("/blob/", "/raw/")
            _adiciona(url, r["titulo"], "web-oficial")

    # 3) Open Library apontando para archive.org
    for item in _open_library(titulo, autor):
        if "archive.org" in item.get("url", ""):
            ident = item["url"].rstrip("/").rsplit("/", 1)[-1]
            _adiciona(f"https://archive.org/download/{ident}/{ident}.pdf",
                      item["titulo"], "open-library")

    validos = [c for c in candidatos if _eh_pdf_valido(c["url"])]
    return validos[:limite]


# ---------------------------------------------------------------------------
# Download + extração
# ---------------------------------------------------------------------------

def download_pdf(url: str, destino: pathlib.Path,
                 allow_loopback: bool = False) -> pathlib.Path | None:
    """Baixa PDF de `url` para `destino` validando Content-Type.

    Retorna o Path salvo ou None em falha (não-PDF, rede, pirataria).
    `allow_loopback` é exclusivo de testes locais (servidor 127.0.0.1)."""
    if _url_pirata(url):
        return None
    try:
        status, body, ct = _fetch(url, timeout=60, allow_loopback=allow_loopback)
    except Exception:  # noqa: BLE001
        return None
    if status != 200:
        return None
    if "application/pdf" not in ct.lower() and not body[:5].startswith(b"%PDF-"):
        return None
    destino.parent.mkdir(parents=True, exist_ok=True)
    destino.write_bytes(body[:MAX_PDF_BYTES])
    return destino


def extract_pdf(pdf_path: pathlib.Path) -> str | None:
    """Extrai o texto de um PDF usando pypdf (se instalado); senão None."""
    try:
        from pypdf import PdfReader  # type: ignore
    except ImportError:
        return None
    try:
        reader = PdfReader(str(pdf_path))
        partes = [p.extract_text() or "" for p in reader.pages[:30]]
        return "\n".join(partes)
    except Exception:  # noqa: BLE001
        return None


# ---------------------------------------------------------------------------
# Gravação em memória
# ---------------------------------------------------------------------------

def _slug_titulo(titulo: str, autor: str = "") -> str:
    base = slugify(titulo)
    if autor:
        base = f"{slugify(autor)}-{base}"
    return base


def _frontmatter(dados: dict) -> str:
    """Monta o frontmatter de uma referência com Trust (confiança).

    Campos novos: `trust` (alta|media|fraca; default fraca), `origem`
    (rastreável; default marcado como não informada), `validado_por` (quem
    validou; default não informado) e `recuperavel` (booleano; default `true`).
    Valores com `:` interno (ex.: URLs) são seguros — o parser usa a primeira
    ocorrência de `:` como separador. Mapeamento dos status do webscraper: `ok`
    (>= 2 fontes editoriais) → alta; `validacao_indireta` → media; `fabricacao`
    (alerta) → fraca + `recuperavel: false` (fabricação é tóxica e NUNCA deve
    ser recuperada por RAG). Referências antigas sem os campos continuam lendo
    bem (`_parse_frontmatter` só exige id/tipo/titulo); sem `recuperavel`
    assume-se `true` (conservador, não perde memória antiga).
    """
    tags = ", ".join(dados["tags"])
    trust = str(dados.get("trust") or config.TRUST_DEFAULT).strip().lower()
    if trust not in config.TRUST:
        trust = config.TRUST_DEFAULT
    origem = str(dados.get("origem") or "_não informada_").strip()
    validado_por = str(dados.get("validado_por") or "_não informado_").strip()
    # B1: MESMA coerção do leitor (`memory.recuperavel_de`) — antes
    # `bool(dados.get(...))` fazia strings "false"/"0"/"no" virarem `true`
    # (assimetria leitor/escritor). `recuperavel_de` trata bool/int do Python
    # e strings, com default `True` quando a chave está ausente.
    recuperavel = recuperavel_de(dados)
    rec_txt = "true" if recuperavel else "false"
    return (
        "---\n"
        f"id: {dados['id']}\n"
        f"tipo: {dados['tipo']}\n"
        f'titulo: "{dados["titulo"]}"\n'
        f"autor: {dados['autor']}\n"
        f"fonte: {dados['fonte']}\n"
        f"data: {dados['data']}\n"
        f"tags: [{tags}]\n"
        f"trust: {trust}\n"
        f"origem: {origem}\n"
        f"validado_por: {validado_por}\n"
        f"recuperavel: {rec_txt}\n"
        "---\n"
    )


def write_reference(dados: dict) -> pathlib.Path:
    """Grava `memory/references/<id>.md` e regenere o índice.

    `dados` deve conter: id, tipo, titulo, autor, fonte, data, tags e o corpo
    (texto livre). Atualiza `index.md` com uma linha por referência
    (consolidação única ao final, evitando dessincronização concorrente).
    """
    ref_path = REF_DIR / f"{dados['id']}.md"
    corpo = dados.get("corpo", "")
    ref_path.parent.mkdir(parents=True, exist_ok=True)
    ref_path.write_text(
        _frontmatter(dados) + "\n" + corpo,
        encoding="utf-8",
    )
    _reindex()
    return ref_path


def _parse_frontmatter(texto: str) -> dict[str, str] | None:
    """Extrai os campos do bloco `--- ... ---` de uma referência via parser
    COMPARTILHADO (harness/frontmatter.py).

    Aceita campos em **qualquer ordem** e chaves variantes (`autor`/`autores`,
    ou ausência do campo). Valores com `:` interno (ex.: títulos com aspas)
    são preservados. Retorna None se o bloco não existir ou faltarem campos
    obrigatórios (`id`, `tipo`, `titulo`).
    """
    campos, _ = frontmatter.parse(texto)
    if not campos:
        return None
    for campo in ("id", "tipo", "titulo"):
        if campo not in campos:
            return None
    return campos


def _reindex() -> None:
    """Reconstrói `memory/references/index.md` a partir dos `<id>.md` reais.

    Uma única passada por todos os arquivos (evita dessincronização quando
    várias sessões gravam em paralelo). Ordena por data desc e depois título.
    """
    linhas: list[tuple[str, str, str, str, str]] = []
    if REF_DIR.is_dir():
        for arquivo in sorted(REF_DIR.glob("*.md")):
            if arquivo.name == "index.md":
                continue
            texto = arquivo.read_text(encoding="utf-8", errors="replace")
            campos = _parse_frontmatter(texto)
            if not campos:
                continue
            titulo = (campos.get("titulo") or "").strip().strip('"').strip()
            # `autor` (singular) ou `autores` (plural); ausência tem fallback
            # seguro ("—").
            autor = (campos.get("autor") or campos.get("autores") or "—").strip()
            tipo = (campos.get("tipo") or "").strip()
            data = (campos.get("data") or "").strip()
            linhas.append((data, titulo, tipo, autor, arquivo.name))

    linhas.sort(key=lambda linha: (linha[0], linha[1]), reverse=True)
    cabecalho = [
        "# Índice de Referências",
        "",
        "Referências extraídas pelo agente **documenter** a partir de livros,",
        "documentos e arquivos. Cada referência vive em `memory/references/<id>.md`.",
        f"Índice consolidado em {date.today().isoformat()} (regeneração automática).",
        "",
        "| Título | Autor | Tipo | Data | Arquivo |",
        "| --- | --- | --- | --- | --- |",
    ]
    for data, titulo, tipo, autor, nome in linhas:
        cabecalho.append(
            f"| {titulo} | {autor} | {tipo} | {data or '—'} | {nome} |"
        )
    INDEX_FILE.write_text("\n".join(cabecalho) + "\n", encoding="utf-8")


# ---------------------------------------------------------------------------
# Fallback por conhecimento indireto (obra fabricada / sem fontes)
# ---------------------------------------------------------------------------

# Janela de atualidade (Etapa D): fonte com mais de 5 anos é revalidada usando
# o ano atual como referência (ano atual menos este corte).
ANOS_JANELA_ATUALIDADE = 5

# Limite de tópicos rankeados por livro (Etapa B) — os 50 primeiros por
# frequência de termos.
MAX_TOPICOS = 50

# Termos de "andaime" de busca que não viram tópicos (pt/en, sem acentos).
STOPWORDS = frozenset("""
a o e de do da dos das em no na nos nas para por com sem sobre que se
um uma uns umas ao aos as mais menos mas ou nem os as e sao foi ser
seu sua seus suas este esta estes estas esse essa esses essas aquele
aquela aqueles aquelas isto isso aquilo meu minha meus minhas teu tua
teus tuas nosso nossa nossos nossas vosso vossa vossos vossas lhe lhes
me te nos vos eu tu ele ela eles elas nos vos voce voces quem cujo cuja
cujos cujas onde quando quanto quanta quantos quantas como qual quais
tal tais mesmo mesma mesmos mesmas proprio propria proprios proprias
todo toda todos todas outro outra outros outras algum alguma alguns
algumas nenhum nenhuma nenhuns nenhumas cada qualquer quaisquer apenas
somente tambem ainda ja muito muita muitos muitas pouco pouca poucos
poucas bem mal assim depois antes durante enquanto entre contra ate
desde apos sob perante the a an and or but if then else for with
without from by to of in on at is are was were be been being have has
had do does did will would can could should may might must not no yes
this that these those it its which who whom whose what when where why
how all any some each every both few more most other others such only
own same than too very just about into over under again further once
here there livro livros autor autores obra obras book books author
authors edicao editora capitulo ano anos resumo sumario review pdf
gratis free official oficial pagina paginas site sites https http
www com org br net wiki wikipedia artigo artigos article articles
html padrao new novo nova jan fev mar abr mai jun jul ago set out
nov dez janeiro fevereiro marco abril maio junho julho agosto setembro
outubro novembro dezembro january february march april may june july
august september october november december several primarily serve
serves cover covers also still even much many such could would should
will shall may might must can cannot used use using often usually
generally typically available according however therefore thus hence
dois duas tres quatro cinco seis sete oito nove dez two three four
five six seven eight nine ten
tem ter era eram vai vao estao estava estavam havia haviam sido sendo
serao pode podem deve devem large small big high low long short wide
great good best main key top full real true fast slow easy hard
""".split())

# Palavras de transição que não contam como "padrão atual" na Etapa D.
_RUIDO_PADRAO = frozenset({
    "alternative", "alternatives", "standard", "new", "novo", "nova",
    "atual", "atualizado", "padrao", "sucessor", "successor", "antigo",
    "obsoleto", "deprecated", "moderno", "modern",
})

_ACENTOS = str.maketrans(
    "áàâãäéèêëíìîïóòôõöúùûüç",
    "aaaaaeeeeiiiiooooouuuuc",
)

_URL_RE = re.compile(
    r"https?://\S+|www\.\S+|\b[\w-]+\.(?:com|org|net|io|dev|co|br|gov|edu)\b",
    re.IGNORECASE,
)


def _limpar_texto(texto: str) -> str:
    """Remove URLs de um texto (snippets de busca incluem URLs visíveis)."""
    return _URL_RE.sub(" ", texto)


def _tokenizar(texto: str) -> list[str]:
    """Normaliza (lowercase, sem acentos) e quebra texto em termos."""
    texto = texto.lower().translate(_ACENTOS)
    return re.findall(r"[a-z0-9]+(?:[-_][a-z0-9]+)*", texto)


def _extrair_topicos(
    texto: str,
    limite: int = MAX_TOPICOS,
    ignorar: set[str] | None = None,
) -> list[dict]:
    """Etapa B: ranking de temas por frequência de termos (sem filtro de
    importância).

    Remove stopwords, normaliza e conta unigramas e bigramas de termos de
    conteúdo; ordena do mais frequente para o menos e retorna até `limite`
    tópicos (os 50 primeiros são os escolhidos). `ignorar` permite excluir
    termos (ex.: sobrenome do autor).
    """
    termos = [t for t in _tokenizar(texto)
              if len(t) >= 3 and not t.isdigit() and t not in STOPWORDS
              and not (ignorar and t in ignorar)]
    if not termos:
        return []
    freq_unigramas: dict[str, int] = {}
    for t in termos:
        freq_unigramas[t] = freq_unigramas.get(t, 0) + 1
    freq_bigramas: dict[str, int] = {}
    for i in range(len(termos) - 1):
        p1, p2 = termos[i], termos[i + 1]
        if p1 == p2:
            continue
        par = f"{p1} {p2}"
        freq_bigramas[par] = freq_bigramas.get(par, 0) + 1
    candidatos: list[tuple[str, int, int]] = []  # (termo, frequência, nº palavras)
    for t, f in freq_unigramas.items():
        if f >= 2:
            candidatos.append((t, f, 1))
    for b, f in freq_bigramas.items():
        if f >= 2:
            candidatos.append((b, f, 2))
    candidatos.sort(key=lambda c: (-c[1], c[2], c[0]))
    escolhidos: list[dict] = []
    subsumidos: set[str] = set()
    for termo, freq, n_palavras in candidatos:
        # Subsumição: tópico cujo token já foi coberto por um escolhido é
        # quase-duplicata (ex.: "language" escolhido subsumi "language models"
        # e "large language").
        if n_palavras == 2 and any(p in subsumidos for p in termo.split()):
            continue
        if termo in subsumidos:
            continue
        escolhidos.append({"tema": termo, "frequencia": freq})
        subsumidos.update(termo.split())
        if len(escolhidos) >= limite:
            break
    return escolhidos


def _contexto_obra(titulo: str, autor: str = "") -> tuple[str, list[str]]:
    """Etapa A: consultas rápidas para obter o contexto exato do livro.

    Consolida descrições das APIs de livros + resumos de busca web. Quando a
    obra é fabricada e há pouca informação, usa a descrição do título/autor e
    obras homônimas como referência de contexto, com ressalva explícita.
    """
    partes: list[str] = []
    for item in search_books(titulo, autor):
        if item.get("descricao"):
            partes.append(item["descricao"])
        titulo_item = item.get("titulo", "")
        autores = " ".join(item.get("autores", []))
        partes.append(f"{titulo_item} {autores} {item.get('ano', '')}".strip())
    consultas = [f"{titulo} {autor} resumo sumario"]
    consultas.append(f"{titulo} {autor} review")
    for q in consultas:
        for r in search_web(q, limite=5):
            partes.append(f"{_limpar_texto(r['titulo'])} {_limpar_texto(r['resumo'])}")
    texto = " ".join(p for p in partes if p).strip()
    ressalvas: list[str] = []
    if len(texto) < 200:
        ressalvas.append(
            "pouca informação direta encontrada; contexto baseado na descrição "
            "do título/autor e em obras homônimas (não na obra em si)"
        )
    return texto, ressalvas


def _ano_fonte(fonte: dict) -> int | None:
    """Estima o ano de uma fonte a partir da URL (blogs/docs) e do snippet.

    Só aceita anos plausíveis (1981..ano atual + 1): filtra "1970"/"1975" de
    citações e anos futuros inventados em snippets. Anos da URL têm precedência
    sobre o snippet; entre anos do snippet vale o mais frequente (desempate
    pelo mais recente) — sem `max()` cego sobre valores fora de faixa.
    """
    from collections import Counter

    hoje = date.today().year
    url = fonte.get("url", "")
    texto = f"{fonte.get('titulo', '')} {fonte.get('resumo', '')}"
    anos_url = [int(a.strip("/")) for a in re.findall(r"/(?:19|20)\d{2}/", url)]
    anos_texto = [int(a) for a in re.findall(r"\b(?:19|20)\d{2}\b", texto)]

    def plausivel(a: int) -> bool:
        return 1980 < a <= hoje + 1

    anos_url = [a for a in anos_url if plausivel(a)]
    anos_texto = [a for a in anos_texto if plausivel(a)]
    if anos_url:
        return max(anos_url)
    if not anos_texto:
        return None
    contagem = Counter(anos_texto)
    return max(contagem, key=lambda a: (contagem[a], a))


def _hosts_independentes(fontes: list[dict]) -> set[str]:
    return {_host(f["url"]) for f in fontes if f.get("url")}


def _prioridade_fonte(fonte: dict) -> int:
    """Preferência por fontes oficiais (github, .edu, domínios editoriais)."""
    host = _host(fonte["url"])
    if "github" in host or host.endswith(".edu") or _host_editorial(fonte["url"]):
        return 0
    if fonte.get("github_pdf"):
        return 0
    return 1


def _buscar_fontes_tema(
    tema: str, titulo: str, autor: str = "", limite: int = 4
) -> list[dict]:
    """Etapa C: pesquisa indireta de fontes públicas para um tópico.

    Aplica a deny-list atual (somente z-lib/zlib) e o filtro de hosts de
    conteúdo adulto (`DENY_ADULTO`). PDFs do GitHub são aceitos como fonte de
    validação (a atualidade é verificada na Etapa D).
    """
    consultas = [f"{tema} {titulo}", tema]
    if autor:
        consultas.insert(1, f"{tema} {autor}")
    fontes: list[dict] = []
    vistos: set[str] = set()
    for q in consultas:
        for r in search_web(q, limite=5):
            url = r["url"]
            if _url_pirata(url) or _host_adulto(url) or url in vistos:
                continue
            vistos.add(url)
            eh_pdf_github = "github" in _host(url) and (
                url.lower().endswith(".pdf") or "/raw/" in url
            )
            fontes.append({
                "url": url,
                "titulo": r["titulo"],
                "resumo": r["resumo"],
                "ano": _ano_fonte(r),
                "github_pdf": eh_pdf_github,
            })
        if len(fontes) >= limite:
            break
    fontes.sort(key=_prioridade_fonte)
    return fontes[:limite]


def _tema_em_resultados(tema: str, resultados: list[dict]) -> bool:
    """True se todos os termos do tema aparecem em algum resultado.

    Comparação por token (word boundary), não substring: "tem" não casa com
    "tempo"/"temperatura" nos snippets.
    """
    termos = [t for t in _tokenizar(tema) if t not in STOPWORDS]
    if not termos:
        return False
    for r in resultados:
        bloco = f"{r.get('titulo', '')} {r.get('resumo', '')}".lower()
        tokens = set(_tokenizar(bloco))
        if all(t in tokens for t in termos):
            return True
    return False


def _candidatos_padrao_atual(
    tema: str, resultados: list[dict]
) -> tuple[str | None, list[dict]]:
    """Extrai candidato a 'padrão atual' exigindo >= 2 fontes independentes."""
    termos_tema = {t for t in _tokenizar(tema) if t not in STOPWORDS and len(t) >= 3}
    contagem: dict[str, int] = {}
    fontes_por_termo: dict[str, list[dict]] = {}
    hosts_por_termo: dict[str, set[str]] = {}
    for r in resultados:
        host = _host(r["url"])
        for t in _tokenizar(f"{r.get('titulo', '')} {r.get('resumo', '')}"):
            if (t in STOPWORDS or t in _RUIDO_PADRAO or t in termos_tema
                    or len(t) < 3 or t.isdigit()):
                continue
            contagem[t] = contagem.get(t, 0) + 1
            fontes_por_termo.setdefault(t, []).append(r)
            hosts_por_termo.setdefault(t, set()).add(host)
    melhores = sorted(
        ((t, contagem[t]) for t, hosts in hosts_por_termo.items() if len(hosts) >= 2),
        key=lambda x: (-x[1], x[0]),
    )
    if not melhores:
        return None, []
    t = melhores[0][0]
    return t, fontes_por_termo[t][:4]


def _verificar_atualidade(tema: str, hoje: date) -> dict:
    """Etapa D: validação de atualidade (janela de 5 anos) — consulta única.

    1. Busca com o ano atual: se o tema ainda aparece, é válido.
    2. Se não: amplia com variações de query e exige >= 2 fontes independentes
       confirmando o padrão atual antes de prosseguir (sem loop de
       re-verificação).
    """
    resultados = [r for r in search_web(f"{tema} {hoje.year}", limite=5)
                  if not _url_pirata(r["url"]) and not _host_adulto(r["url"])]
    if _tema_em_resultados(tema, resultados):
        return {
            "valido": True,
            "padrao_atual": None,
            "fontes_padrao_atual": [],
            "detalhe": f"tema ainda presente em resultados de {hoje.year}",
        }
    ampliado: list[dict] = []
    for q in (f"{tema} alternative {hoje.year}", f"{tema} new standard {hoje.year}"):
        ampliado += [r for r in search_web(q, limite=5)
                     if not _url_pirata(r["url"]) and not _host_adulto(r["url"])]
    padrao, fontes = _candidatos_padrao_atual(tema, ampliado)
    if padrao:
        return {
            "valido": False,
            "padrao_atual": padrao,
            "fontes_padrao_atual": list(dict.fromkeys(f["url"] for f in fontes)),
            "detalhe": (
                f"tema provavelmente desatualizado; padrão atual identificado: "
                f"{padrao!r} (>= 2 fontes independentes)"
            ),
        }
    return {
        "valido": False,
        "padrao_atual": None,
        "fontes_padrao_atual": [],
        "detalhe": "tema não reencontrado com o ano atual; padrão atual não identificado",
    }


def _valida_contexto(
    tema: str, fontes: list[dict], termos_contexto: set[str]
) -> dict:
    """Etapa E: validação contextual determinística.

    Critério: >= 2 termos-chave do contexto do livro (>= 4 caracteres)
    presentes nas fontes do tema por **token exato** (word boundary, não
    substring — "tem" não casa com "tempo"/"temperatura") E >= 2 fontes
    independentes (hosts distintos). Tema fora do contexto ou sem fontes
    suficientes -> `nao_confirmado`. O filtro semântico fino (LLM) fica para
    o agente documenter.
    """
    textos = " ".join(f"{f.get('titulo', '')} {f.get('resumo', '')}" for f in fontes)
    textos = _limpar_texto(textos).lower().translate(_ACENTOS)
    tokens = set(_tokenizar(textos))
    # Termos curtos (< 4 caracteres) não contam na checagem contextual.
    candidatos = {t for t in termos_contexto if len(t) >= 4}
    compartilhados = sorted(t for t in candidatos if t in tokens)
    hosts = _hosts_independentes(fontes)
    confirmado = len(compartilhados) >= 2 and len(hosts) >= 2
    return {
        "status": "confirmado" if confirmado else "nao_confirmado",
        "termos_compartilhados": compartilhados[:10],
        "fontes_independentes": len(hosts),
    }


def _gravar_referencia_indireta(
    id_ref: str,
    titulo: str,
    autor: str,
    hoje: str,
    contexto: str,
    temas: list[dict],
    fontes_por_tema: dict[str, list[dict]],
    status_por_tema: dict[str, str],
    atualidade_por_tema: dict[str, dict],
) -> pathlib.Path:
    """Etapa F: grava a referência indireta com a tag `validacao_indireta`."""
    linhas_topico: list[str] = []
    for i, item in enumerate(temas, start=1):
        tema = item["tema"]
        st = status_por_tema.get(tema, "nao_confirmado")
        at = atualidade_por_tema.get(tema)
        at_txt = f"padrão: {at['padrao_atual']}" if at and at["padrao_atual"] else (
            "desatualizado" if at and not at["valido"] else "atual")
        linhas_topico.append(f"| {i} | {tema} | {item['frequencia']} | {st} | {at_txt} |")

    blocos_fontes: list[str] = []
    for tema, fontes in fontes_por_tema.items():
        st = status_por_tema.get(tema, "nao_confirmado")
        if not fontes:
            blocos_fontes.append(f"### {tema}\n\n- nenhuma fonte pública encontrada ({st})\n")
            continue
        linhas = [f"### {tema} ({st})", ""]
        for f in fontes:
            ano = f"ano {f['ano']}" if f.get("ano") else "ano não identificado"
            extra = " [github pdf]" if f.get("github_pdf") else ""
            padrao = " [padrão atual]" if f.get("padrao_atual") else ""
            linhas.append(f"- {f['url']} — {f['titulo']} ({ano}){extra}{padrao}")
        blocos_fontes.append("\n".join(linhas))

    # Troca de sufixo segura: substitui APENAS o sufixo final `-indireto`
    # (replace global geraria id errado se o slug contiver "-indireto").
    if id_ref.endswith("-indireto"):
        id_alerta = id_ref[: -len("-indireto")] + "-fabricacao"
    else:
        id_alerta = id_ref + "-fabricacao"
    # Origem rastreável: URLs das fontes públicas coletadas por tópico
    # (limitadas — a lista completa fica no corpo da referência).
    urls_fontes = sorted({
        f["url"] for fontes in fontes_por_tema.values() for f in fontes
    })
    origem = (
        "webscraping indireto; obra não confirmada em fonte editorial; "
        f"fontes públicas por tópico: {'; '.join(urls_fontes[:10])}"
    ) if urls_fontes else (
        "webscraping indireto; obra não confirmada em fonte editorial; "
        "sem fontes públicas por tópico"
    )
    corpo = (
        f"# {titulo}{f' — {autor}' if autor else ''} — conhecimento indireto\n\n"
        f"## Ressalva\n\n"
        f"Esta obra **não foi validada diretamente** (ver alerta "
        f"`{id_alerta}.md`): nenhuma fonte editorial confirma sua existência. "
        f"O conhecimento abaixo foi **adquirido indiretamente por tópicos** — "
        f"contexto temático coletado na web e fontes públicas sobre cada tema, "
        f"não sobre o livro em si. O filtro semântico fino (LLM) deve ser "
        f"aplicado pelo agente documenter antes de usar este conteúdo.\n\n"
        f"## Sumário temático (contexto coletado)\n\n"
        f"{contexto[:2000] or '—'}\n\n"
        f"## Tópicos rankeados por frequência de termos (até {MAX_TOPICOS})\n\n"
        f"| # | Tema | Frequência | Status | Atualidade |\n"
        f"| --- | --- | --- | --- | --- |\n"
        f"{chr(10).join(linhas_topico)}\n\n"
        f"## Fontes públicas por tópico\n\n"
        f"{chr(10).join(blocos_fontes)}\n"
    )
    return write_reference({
        "id": id_ref,
        "tipo": "livro",
        "titulo": titulo,
        "autor": autor or "—",
        "fonte": "conhecimento indireto (web pública por tópicos)",
        "data": hoje,
        "tags": ["webscraping", "coleta", "validacao_indireta", "fabricacao"],
        # validacao_indireta -> trust media (conhecimento parcial por tópicos).
        "trust": config.TRUST["media"],
        "origem": origem,
        "validado_por": "motor",
        "corpo": corpo,
    })


def fallback_indireto(
    titulo: str,
    autor: str = "",
    gravar: bool = True,
    hoje: str | None = None,
) -> dict:
    """Fallback por conhecimento indireto para obra não confirmada (Etapas A-F).

    Executado quando `validate_fabrication` falha: mesmo assim coleta o
    contexto temático da obra, rankeia tópicos por frequência de termos,
    pesquisa fontes públicas por tópico, valida atualidade (janela de 5 anos)
    e contexto, e grava uma referência indireta com tag `validacao_indireta`.

    Etapas:
      A. Consultas rápidas de contexto (descrições, resenhas, sumário).
      B. Ranking de tópicos por frequência de termos (até `MAX_TOPICOS`).
      C. Pesquisa indireta em fontes públicas por tópico (deny-list aplicada).
      D. Validação de atualidade (janela de 5 anos; consulta única ampliada).
      E. Validação contextual determinística (termos-chave + >= 2 fontes).
      F. Gravação da referência indireta em
         `memory/references/<slug>-indireto.md`.

    Retorna dict com `status` (`ok` | `parcial` | `sem_fontes`), `temas`,
    `fontes_por_tema`, `status_por_tema`, `atualidade_por_tema`,
    `refs_gravadas` e `ressalvas`.
    """
    hoje = hoje or date.today().isoformat()
    hoje_dt = datetime.strptime(hoje, "%Y-%m-%d").date()
    slug = _slug_titulo(titulo, autor)
    # Termos do autor não viram tópicos (ex.: sobrenome).
    ignorar = set(t for t in _tokenizar(autor) if len(t) >= 3) if autor else set()

    contexto, ressalvas = _contexto_obra(titulo, autor)
    temas = _extrair_topicos(contexto, limite=MAX_TOPICOS, ignorar=ignorar)
    if not temas:
        ressalvas.append("nenhum tópico extraído do contexto coletado")
        return {
            "status": "sem_fontes",
            "slug": slug,
            "contexto": contexto[:1000],
            "temas": [],
            "fontes_por_tema": {},
            "status_por_tema": {},
            "atualidade_por_tema": {},
            "refs_gravadas": [],
            "ressalvas": ressalvas,
        }

    termos_contexto: set[str] = set()
    for item in temas:
        termos_contexto.update(t for t in _tokenizar(item["tema"]) if t not in STOPWORDS)

    fontes_por_tema: dict[str, list[dict]] = {}
    status_por_tema: dict[str, str] = {}
    atualidade_por_tema: dict[str, dict] = {}
    corte = hoje_dt.year - ANOS_JANELA_ATUALIDADE

    for item in temas:
        tema = item["tema"]
        fontes = _buscar_fontes_tema(tema, titulo, autor)
        if fontes:
            anos = [f["ano"] for f in fontes if f.get("ano")]
            # Etapa D: qualquer fonte com mais de 5 anos dispara a revalidação
            # de atualidade do tema (consulta única, sem loop).
            if any(a is not None and a < corte for a in anos):
                at = _verificar_atualidade(tema, hoje_dt)
                atualidade_por_tema[tema] = at
                # Consumo do padrão atual: quando o tema está desatualizado e a
                # Etapa D identifica um padrão com >= 2 hosts independentes,
                # refaz a busca de fontes do tema com o padrão anexado à query
                # (segunda passada limitada) e registra as fontes do padrão no
                # tópico, marcadas com `padrao_atual`.
                padrao = at.get("padrao_atual")
                if padrao:
                    fontes_padrao = _buscar_fontes_tema(
                        f"{tema} {padrao}", titulo, autor, limite=5
                    )
                    if fontes_padrao:
                        for f in fontes_padrao:
                            f["padrao_atual"] = True
                        vistos_url = {f["url"] for f in fontes}
                        for f in fontes_padrao:
                            if f["url"] not in vistos_url:
                                vistos_url.add(f["url"])
                                fontes.append(f)
        fontes_por_tema[tema] = fontes
        val = _valida_contexto(tema, fontes, termos_contexto)
        status_por_tema[tema] = val["status"]
        item["termos_compartilhados"] = val["termos_compartilhados"]
        item["fontes_independentes"] = val["fontes_independentes"]

    n_confirmados = sum(1 for s in status_por_tema.values() if s == "confirmado")
    n_temas_com_fontes = sum(1 for f in fontes_por_tema.values() if f)
    if n_confirmados >= 2:
        status_fb = "ok"
    elif n_confirmados >= 1 or n_temas_com_fontes >= 1:
        status_fb = "parcial"
    else:
        status_fb = "sem_fontes"

    refs_gravadas: list[str] = []
    if gravar:
        id_ref = f"{slug}-indireto"
        caminho = _gravar_referencia_indireta(
            id_ref, titulo, autor, hoje, contexto, temas,
            fontes_por_tema, status_por_tema, atualidade_por_tema,
        )
        refs_gravadas.append(str(caminho))

    return {
        "status": status_fb,
        "slug": slug,
        "contexto": contexto[:1000],
        "temas": temas,
        "fontes_por_tema": fontes_por_tema,
        "status_por_tema": status_por_tema,
        "atualidade_por_tema": atualidade_por_tema,
        "refs_gravadas": refs_gravadas,
        "ressalvas": ressalvas,
    }


# ---------------------------------------------------------------------------
# Orquestração principal (autônoma)
# ---------------------------------------------------------------------------

def coletar(
    titulo: str,
    autor: str = "",
    baixar_pdf: bool = False,
    pasta_pdf: pathlib.Path | None = None,
    tipo: str = "livro",
    tags: list[str] | None = None,
) -> dict:
    """Pipeline completo e autônomo para coletar uma obra e registrá-la.

    1. `validate_fabrication`: obra existe? (>= 2 fontes editoriais)
    2. Se evidência insuficiente (indeterminado, ex.: 1 API confirma) -> grava
       `<slug>-nao-confirmado.md` (nota de evidência parcial, sem falso alerta)
       e executa o fallback indireto.
    3. Se não existe (sem nenhuma evidência) -> grava `<slug>-fabricacao.md`
       (alerta) e executa o fallback por conhecimento indireto
       (`fallback_indireto`, Etapas A-F), gravando a referência
       `<slug>-indireto.md` com tag `validacao_indireta` quando há
       tópicos/fontes coletados.
    4. Se existe -> grava referência em `memory/references/<slug>.md` com
       metadados das fontes e, se pedido, baixa o PDF oficial.

    Retorna dict de resultado com `status` (`ok` | `nao_confirmado` |
    `fabricacao`), `id`, `path`, `validacao` e `pdf` (opcional). Quando a obra
    não é confirmada (fabricação ou evidência insuficiente), inclui
    `fallback_indireto` (com `status`: `ok` | `parcial` | `sem_fontes`).
    """
    tags = tags or ["webscraping", "coleta"]
    validacao = validate_fabrication(titulo, autor)
    slug = _slug_titulo(titulo, autor)
    hoje = date.today().isoformat()

    if not validacao["existe"]:
        # Evidência parcial (ex.: 1 API real confirma a existência, mas abaixo
        # do limiar de 2 fontes independentes): NÃO é fabricação clara. Grava
        # uma nota de evidência insuficiente (sem falso alerta de fabricação)
        # e ainda tenta o fallback indireto para contexto.
        if validacao.get("indeterminado"):
            id_naoconf = f"{slug}-nao-confirmado"
            nota = (
                f"# NÃO CONFIRMADO — evidência insuficiente\n\n"
                f"- **Título consultado**: {titulo}\n"
                f"- **Autor**: {autor or '—'}\n"
                f"- **Data da verificação**: {hoje}\n"
                f"- **Verificação**: {validacao['detalhe']}\n\n"
                f"A obra foi encontrada em alguma fonte editorial (API de livro "
                f"ou domínio editorial), mas em MENOS de 2 fontes independentes "
                f"— o limiar para confirmar existência. Isso NÃO é fabricação "
                f"confirmada: há evidência parcial de que a obra existe. NUNCA "
                f"inventar sumário, conceitos ou citações além do que as fontes "
                f"apontam.\n"
            )
            ref = write_reference({
                "id": id_naoconf,
                "tipo": "documento",
                "titulo": f'NÃO CONFIRMADO — "{titulo}" (evidência insuficiente)',
                "autor": autor or "n/a",
                "fonte": "verificação em fontes editoriais (evidência parcial)",
                "data": hoje,
                "tags": ["validacao_insuficiente", "alerta"],
                # Evidência não confirmada -> trust fraca (não tratada como
                # conhecimento validado).
                "trust": config.TRUST_DEFAULT,
                "origem": (
                    f"verificação anti-fabricação em {hoje}: {validacao['detalhe']}"
                ),
                "validado_por": "motor",
                "corpo": nota,
            })
            resultado = {
                "status": "nao_confirmado",
                "id": id_naoconf,
                "path": str(ref),
                "validacao": validacao,
            }
            try:
                fallback = fallback_indireto(titulo, autor, gravar=True, hoje=hoje)
            except Exception:  # noqa: BLE001
                fallback = {
                    "status": "sem_fontes",
                    "slug": slug,
                    "contexto": "",
                    "temas": [],
                    "fontes_por_tema": {},
                    "status_por_tema": {},
                    "atualidade_por_tema": {},
                    "refs_gravadas": [],
                    "ressalvas": ["falha inesperada no fallback indireto"],
                }
            resultado["fallback_indireto"] = fallback
            return resultado

        id_fab = f"{slug}-fabricacao"
        alerta = (
            f"# ALERTA DE FABRICAÇÃO\n\n"
            f"- **Título consultado**: {titulo}\n"
            f"- **Autor**: {autor or '—'}\n"
            f"- **Data da verificação**: {hoje}\n"
            f"- **Verificação**: {validacao['detalhe']}\n\n"
            f"Nenhuma fonte editorial confiável (site do autor, editora, Google "
            f"Books, Open Library, Internet Archive) confirma a existência desta "
            f"obra. NUNCA inventar sumário, conceitos ou citações para ela. "
            f"Procure a referência real mais próxima e registre o substituto "
            f"com ressalva.\n"
        )
        ref = write_reference({
            "id": id_fab,
            "tipo": "documento",
            "titulo": f'ALERTA DE FABRICAÇÃO — "{titulo}" não existe',
            "autor": autor or "n/a (título fabricado)",
            "fonte": "verificação em múltiplas fontes editoriais",
            "data": hoje,
            "tags": ["fabricacao", "alerta"],
            # Alerta de fabricação -> trust fraca (origem não validada; o
            # harness nunca trata isso como conhecimento confirmado) E
            # `recuperavel: false` (Item 2.1): fabricação explícita é tóxica e
            # NUNCA deve ser recuperada por search/RAG.
            "trust": config.TRUST_DEFAULT,
            "recuperavel": False,
            "origem": f"verificação anti-fabricação em {hoje}: {validacao['detalhe']}",
            "validado_por": "motor",
            "corpo": alerta,
        })
        resultado = {
            "status": "fabricacao",
            "id": id_fab,
            "path": str(ref),
            "validacao": validacao,
        }
        # Fallback por conhecimento indireto (Etapas A-F): mesmo fabricada, a
        # obra pode ter contexto temático coletável indiretamente por tópicos.
        # O fallback nunca derruba o fluxo principal (Exception Handling).
        try:
            fallback = fallback_indireto(titulo, autor, gravar=True, hoje=hoje)
        except Exception:  # noqa: BLE001
            fallback = {
                "status": "sem_fontes",
                "slug": slug,
                "contexto": "",
                "temas": [],
                "fontes_por_tema": {},
                "status_por_tema": {},
                "atualidade_por_tema": {},
                "refs_gravadas": [],
                "ressalvas": ["falha inesperada no fallback indireto"],
            }
        resultado["fallback_indireto"] = fallback
        return resultado

    # Existe -> monta a referência
    linhas_fontes = "\n".join(
        f"- {f['fonte']}: {f['url']} — {f['titulo']}" for f in validacao["fontes"]
    )
    corpo = (
        f"# {titulo}{f' — {autor}' if autor else ''}\n\n"
        f"## Validação de existência\n\n"
        f"{validacao['detalhe']}\n\n"
        f"## Fontes editoriais confirmadas\n\n"
        f"{linhas_fontes or '—'}\n\n"
        f"## Notas do webscraper\n\n"
        f"Referência coletada e validada automaticamente por "
        f"`harness/webscraper.py` em {hoje}. Completar sumário, conceitos-chave, "
        f"padrões acionáveis e aplicação no motor após leitura do conteúdo.\n"
    )
    ref = write_reference({
        "id": slug,
        "tipo": tipo,
        "titulo": titulo,
        "autor": autor or "—",
        "fonte": "; ".join(f["url"] for f in validacao["fontes"][:3]),
        "data": hoje,
        "tags": tags,
        # Obra confirmada em >= 2 fontes editoriais -> trust alta.
        "trust": config.TRUST["alta"],
        "origem": (
            "webscraping autônomo; validação de existência em fontes "
            "editoriais: " + "; ".join(f["url"] for f in validacao["fontes"][:3])
        ),
        "validado_por": "motor",
        "corpo": corpo,
    })

    pdf_result: dict | None = None
    if baixar_pdf:
        pasta = pasta_pdf or (config.SIDE_PRJS_DIR / "coletados" / "docs" / "livros")
        pdfs = find_pdf(titulo, autor)
        if pdfs:
            destino = pasta / f"{slug}.pdf"
            baixado = download_pdf(pdfs[0]["url"], destino)
            pdf_result = {
                "baixado": bool(baixado),
                "path": str(baixado) if baixado else None,
                "fonte": pdfs[0]["url"],
                "status": "ok" if baixado else "falha",
            }
        else:
            pdf_result = {"baixado": False, "path": None, "fonte": None,
                          "status": "sem_pdf_legitimo"}

    return {"status": "ok", "id": slug, "path": str(ref), "validacao": validacao,
            "pdf": pdf_result}


# ---------------------------------------------------------------------------
# CLI
# ---------------------------------------------------------------------------

def main(argv: list[str] | None = None) -> int:
    try:
        sys.stdout.reconfigure(encoding="utf-8", errors="replace")
    except (AttributeError, ValueError):
        pass

    parser = argparse.ArgumentParser(
        prog="python -m harness.webscraper",
        description="Webscraper autônomo do harness: valida, coleta e registra conteúdo.",
    )
    parser.add_argument("titulo", help="título da obra a coletar (entre aspas)")
    parser.add_argument("--autor", default="", help="autor da obra (opcional)")
    parser.add_argument("--pdf", action="store_true", help="tenta baixar PDF oficial")
    parser.add_argument("--busca-apenas", action="store_true",
                        help="apenas busca/valida e imprime, sem gravar na memória")
    parser.add_argument("--pasta-pdf", default=None, help="destino do PDF baixado")
    args = parser.parse_args(argv)

    if args.busca_apenas:
        print("== Validação de existência ==")
        val = validate_fabrication(args.titulo, args.autor)
        print(f"existe: {val['existe']} — {val['detalhe']}")
        for f in val["fontes"]:
            print(f"  - [{f['fonte']}] {f['titulo']} {f['url']}")
        print("\n== PDFs legítimos ==")
        for pdf in find_pdf(args.titulo, args.autor):
            print(f"  - [{pdf['fonte']}] {pdf['titulo']} {pdf['url']}")
        if not val["existe"]:
            print("\n== Fallback por conhecimento indireto (inspeção, sem gravar) ==")
            try:
                fb = fallback_indireto(args.titulo, args.autor, gravar=False)
            except Exception:  # noqa: BLE001
                fb = {
                    "status": "sem_fontes",
                    "slug": _slug_titulo(args.titulo, args.autor),
                    "contexto": "",
                    "temas": [],
                    "fontes_por_tema": {},
                    "status_por_tema": {},
                    "atualidade_por_tema": {},
                    "refs_gravadas": [],
                    "ressalvas": ["falha inesperada no fallback indireto (busca-apenas)"],
                }
            print(f"status: {fb['status']}")
            print(f"contexto: {(fb['contexto'] or '—')[:200]}")
            for item in fb["temas"]:
                tema = item["tema"]
                st = fb["status_por_tema"].get(tema, "nao_confirmado")
                n = len(fb["fontes_por_tema"].get(tema, []))
                print(f"  - [{st}] {tema} (freq {item['frequencia']}, fontes {n})")
        return 0

    pasta_pdf = pathlib.Path(args.pasta_pdf) if args.pasta_pdf else None
    resultado = coletar(args.titulo, args.autor, baixar_pdf=args.pdf, pasta_pdf=pasta_pdf)
    print(json.dumps(resultado, ensure_ascii=False, indent=2, default=str))
    return 0


if __name__ == "__main__":
    sys.exit(main())