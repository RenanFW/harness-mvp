"""Auditoria de segurança web SSRF-safe (v2) — fundação do módulo do
agente pentester / skill security-audit (stdlib only).

Arquivo ÚNICO, organizado por seções e com dataclasses tipadas. A v2 corrige
os problemas da v1 (modelo frágil dict/list, veredito incompleto, segredos por
truncamento, gate por keyword) e introduz PERFIS de teste e um modelo tipado.

Fronteira desta FUNDAÇÃO:
    - MODELO: `Severidade`, `Confianca`, `Finding`, `Relatorio`,
      `agrega_severidade`.
    - PERFIS: `PERFIS`, `perfil_valido`, `fases_do_perfil`,
      `perfil_autorizado`.
    - TRANSPORTE: `Resposta`, `Budget`, `requisicao`, `requisicao_json`,
      `_alvo_valido`, `_AuditRedirectHandler`, redação de segredos.
    - CHECKS OSINT: `audit_rdap`, `audit_dns`, `audit_email`, `audit_ct_logs`.
    - CHECKS DE SUPERFÍCIE (WP4): `audit_cabecalhos`, `audit_cookies`,
      `audit_tls`, `audit_redirects`, `audit_erros_expostos`,
      `audit_well_known`, `audit_cors_passivo`, `audit_mixed_content` e o
      agregador `audit_superficie`.
    - CHECKS ATIVOS READ-ONLY (WP5, GATED): `audit_metodos`,
      `audit_cors_ativo`, `audit_reflexao`, `audit_open_redirect`,
      `audit_graphql`, `audit_openapi`, `audit_paths_sensiveis`,
      `audit_portas`, `audit_segredos_js`, `audit_componentes_osv` e o
      agregador `audit_ativo` (exige perfil `completo` + `perfil_autorizado`).
    - CLI + SELF-TEST.

REGRA DE OURO dos testes ativos: TESTAR, nunca invadir/manipular/extrair.
Tudo é READ-ONLY, bounded (`Budget` + `config.AUDIT_MAX_PROBES` +
`AUDIT_RATE_LIMIT_SEG`) e NUNCA envia payload destrutivo (mantém
`config.AUDIT_PAYLOADS_DESTRUTIVOS`). Segredos achados em JS/arquivos são
REDIGIDOS na evidência (nunca o valor bruto).

Reuso de transporte (NUNCA duplicar a validação SSRF): `harness/webscraper.py`
fornece `_ssrf_valido`, `_host_ips`, `_ip_perigoso`, `USER_AGENT`, `TIMEOUT`,
`MAX_REDIRECTS` e `ssl_ctx`.

Garantias:
    - Toda função de rede é TOLERANTE A FALHA: em erro devolve `erro`, em alvo
      não autorizado devolve `bloqueado`; nenhuma exceção de rede escapa.
    - Anti-fabricação: `Finding` com `confianca=OBSERVADO` EXIGE `evidencia`;
      achado `INFERIDO` é permitido sem evidência, mas fica marcado.
    - Contexto informado pelo solicitante NUNCA vira fato (fica em
      `Relatorio.contexto_informado` com `verificado=False`).
    - Sem emojis. Relatórios informam, nunca aplicam correção.

AUTORIZAÇÃO EM DUAS CAMADAS:
    - `--autorizado` (CLI) é a autorização de ESCOPO: sem ele o perfil
      `completo` é recusado por `perfil_autorizado` (escopo.autorizado=True).
    - A autorização humana da Fase 3 (testes ativos) no fluxo do pipeline é o
      gate `PHASE3_GATE` (`harness/config.py`), consultado pelo AgentPipeline
      quando `testes_ativos=True` (perfil `completo` no pipeline mapeia para
      `testes_ativos=True`). São camadas independentes e complementares.

Uso:
    python -m harness.security <url> --perfil osint|superficial|completo
    python -m harness.security <url> --perfil superficial --porta 443
    python -m harness.security <url> --perfil osint --relatorio
    python -m harness.security <url> --perfil completo --autorizado \
        --permitir-post-forms   # POST em formularios pode alterar estado
    python -m harness.security --self-test

Nota (A1): a porta do TLS é derivada do ESQUEMA da URL (`https` -> 443,
`http` -> 80) por `_porta_tls_padrao`, nunca da lista de portas do perfil. A
lista de portas do perfil é usada SOMENTE pelo scan de `audit_portas`. Em alvo
`http://` sem `--porta`, o TLS é pulado (não faz sentido sondar TLS em HTTP e
o handshake falharia gerando falso positivo).
"""

from __future__ import annotations

import argparse
import hashlib
import html as _html
import http.client
import json
import os
import pathlib
import re
import socket
import ssl
import sys
import time
import warnings
import urllib.error
import urllib.parse
import urllib.request
from dataclasses import dataclass, field, replace
from datetime import datetime, timezone
from enum import Enum

from . import config
from .namer import _RESERVADOS as _RESERVADOS_WINDOWS, slugify
from .webscraper import (
    _host_ips,
    _ip_perigoso,
    _ssrf_valido,
    MAX_REDIRECTS,
    TIMEOUT,
    USER_AGENT,
    ssl_ctx,
)

# ---------------------------------------------------------------------------
# CONSTANTES
# ---------------------------------------------------------------------------

# Versão da ferramenta (v2 — modelo tipado + perfis).
VERSAO = "2.0.0"

# Tamanho máximo do corpo analisado (chars) — evita download gigante.
LIMITE_CORPO = 64 * 1024

# Teto de requisições de REDE por execução (orçamento anti-varredura abusiva).
# Instanciado por execução em `main`/`self_test` via `iniciar_budget`.
LIMITE_REQUISICOES = 200

# Marcadores (case-insensitive) de valor SENSÍVEL em headers/cookies: valores
# que contêm qualquer um destes são REDIGIDOS com `mascarar` (máscara do meio),
# nunca apenas truncados (truncar [:200] NÃO é redigir).
_MARCADORES_SENSIVEIS = (
    "nonce", "token", "key", "secret", "authorization", "set-cookie", "api-key",
)

# Tipos de registro DNS consultados por `audit_dns` via DoH (Cloudflare).
TIPOS_DNS = ("A", "AAAA", "NS", "MX", "TXT", "CAA")

# Endpoint DoH do Cloudflare (retorna JSON com Accept: application/dns-json).
_DOH_BASE = "https://cloudflare-dns.com/dns-query"

# Bootstrap RDAP da IANA (mapa TLD -> servidor RDAP).
_RDAP_BOOTSTRAP = "https://data.iana.org/rdap/dns.json"

# ---------------------------------------------------------------------------
# CONSTANTES DOS CHECKS DE SUPERFÍCIE (WP4)
# ---------------------------------------------------------------------------

# HSTS: idade mínima recomendada (180 dias, em segundos). Abaixo disso -> BAIXA.
_HSTS_MAX_AGE_MIN = 15552000

# Referrer-Policy: valores aceitos (qualquer token da diretiva precisa constar).
_REFERRER_POLICY_VALIDOS = frozenset({
    "no-referrer",
    "no-referrer-when-downgrade",
    "origin",
    "origin-when-cross-origin",
    "same-origin",
    "strict-origin",
    "strict-origin-when-cross-origin",
    "unsafe-url",
})

# Origem canário enviada na sondagem passiva de CORS (não é um domínio real;
# só serve para detectar reflexão de Origin sem credenciais).
_CORS_ORIGIN_CANARIO = "https://canario.audit.invalid"

# Separador de Set-Cookie combinado: vírgula seguida de `nome=` (NÃO separa o
# `,` de `Expires=Wed, 21 Oct ...`, que não tem `=` logo após o token).
_COOKIE_SPLIT_RE = re.compile(r",(?=\s*[^;,\s=]+=)")

# Arquivos bem-conhecidos consultados por `audit_well_known`.
_WELL_KNOWN_ARQUIVOS = (
    "/robots.txt",
    "/sitemap.xml",
    "/.well-known/security.txt",
)

# Trechos de `Disallow` que revelam superfície sensível em robots.txt.
_ROBOTS_SENSIVEIS = ("/admin", "/api", "/backup", "/.git", "/.env")

# Referências de recursos remotos em HTML (mixed content): src/href/action/data
# apontando para http:// (case-insensitive).
_MIXED_CONTENT_RE = re.compile(
    r"""(?:src|href|action|data)\s*=\s*["'](http://[^"'\s>]+)["']""",
    re.I,
)

# ---------------------------------------------------------------------------
# CONSTANTES DOS CHECKS ATIVOS READ-ONLY (WP5, GATED)
# ---------------------------------------------------------------------------

# Origem canário da sondagem ATIVA de CORS (distinta da passiva; não é um
# domínio real de terceiros, então nunca "vaza" para ninguém).
_CORS_ORIGIN_CANARIO_ATIVO = "https://canario-audit.example"

# URL canário usada em open redirect (aponta para domínio reservado .example).
_REDIRECT_CANARIO = "https://canario-audit.example"

# Métodos HTTP considerados perigosos se anunciados/habilitados.
_METODOS_PERIGOSOS = ("PUT", "DELETE", "TRACE", "CONNECT")

# Caminhos comuns de exposição testados de forma BOUNDED (read-only). Cada
# tupla: (caminho relativo, tipo, regex de indício REAL, severidade). As regex
# detectam CONTEÚDO de exposição — nunca são usadas para registrar o valor.
# A severidade é gravada como string (coagida a `Severidade` por `Finding`),
# pois este bloco de constantes fica antes da definição do enum.
_PATHS_SENSIVEIS = (
    (".git/config", "git-config",
     re.compile(r"(?i)\[core\]|repositoryformatversion"), "alta"),
    (".env", "env",
     re.compile(r"(?im)^[A-Z0-9_]{2,}\s*=\s*\S"), "critica"),
    (".git/HEAD", "git-head",
     re.compile(r"(?im)^ref:\s+refs/"), "alta"),
    ("backup.zip", "backup",
     re.compile(r"PK\x03\x04"), "critica"),
    ("db.sql", "sql-dump",
     re.compile(r"(?i)(?:insert into|create table|mysqldump)"), "critica"),
    ("phpinfo.php", "phpinfo",
     re.compile(r"(?i)phpinfo\(\)|PHP Version"), "alta"),
    ("server-status", "server-status",
     re.compile(r"(?i)apache server status|server status"), "alta"),
    ("admin/", "admin-painel",
     re.compile(r"(?i)<form|login|sign in"), "alta"),
    ("wp-config.php", "wp-config",
     re.compile(r"(?i)DB_NAME|DB_PASSWORD|AUTH_KEY"), "critica"),
)

# Caminhos de exposição de API consultados por `audit_openapi` (read-only).
_OPENAPI_PATHS = ("/openapi.json", "/swagger.json", "/api-docs",
                  "/swagger-ui.html")

# Endpoints GraphQL comuns consultados por `audit_graphql`.
_GRAPHQL_PATHS = ("/graphql", "/api/graphql")

# Teto de arquivos JS analisados por `audit_segredos_js`.
_MAX_SCRIPTS_JS = 5

# Padrões de SEGREDO em JS: (tipo, regex). O valor casado NUNCA é registrado;
# a evidência leva só o TIPO e a posição aproximada (anti-extração).
_PADROES_SEGREDO = (
    ("aws-access-key", re.compile(r"AKIA[0-9A-Z]{16}")),
    ("stripe-live-secret", re.compile(r"sk_live_[0-9a-zA-Z]{10,}")),
    ("stripe-test-secret", re.compile(r"sk_test_[0-9a-zA-Z]{10,}")),
    ("google-api-key", re.compile(r"AIza[0-9A-Za-z_-]{35}")),
    ("chave-privada-pem",
     re.compile(r"-----BEGIN [A-Z ]*PRIVATE KEY-----")),
    ("slack-token", re.compile(r"xox[baprs]-[0-9A-Za-z-]{10,}")),
    ("github-token", re.compile(r"ghp_[0-9A-Za-z]{20,}")),
    ("gitlab-token", re.compile(r"glpat-[0-9A-Za-z_-]{20,}")),
    ("jwt", re.compile(r"eyJ[A-Za-z0-9_-]{5,}\.[A-Za-z0-9_-]{5,}\.")),
)

# Source map exposto no fim de um JS.
_SOURCE_MAP_RE = re.compile(r"//#\s*sourceMappingURL=\S+")

# Regex de HTML (best-effort, stdlib only). Formulários/inputs/scripts.
_ATTR_RE = re.compile(r"([A-Za-z_:][\w:.-]*)\s*=\s*[\"']([^\"']*)[\"']")
_FORM_RE = re.compile(r"<form\b([^>]*)>(.*?)</form>", re.I | re.S)
_CAMPO_FORM_RE = re.compile(r"<(?:input|textarea|select)\b([^>]*)>", re.I | re.S)
_SCRIPT_SRC_RE = re.compile(
    r"<script\b[^>]*\bsrc\s*=\s*[\"']([^\"']+)[\"']", re.I,
)
_GENERATOR_RE = re.compile(
    r"<meta\b[^>]*\bname\s*=\s*[\"']generator[\"'][^>]*>", re.I,
)
_GENERATOR_CONTENT_RE = re.compile(
    r"\bcontent\s*=\s*[\"']([^\"']+)[\"']", re.I,
)

# Tipos de <input> para os quais a sonda de reflexão PODE enviar canário.
# Ficam de fora `file`/`submit` (e também button/reset/image/checkbox/radio,
# que disparam ação de UI ou alteram estado de seleção).
_TIPOS_ENVIAVEIS = frozenset({
    "", "text", "search", "url", "email", "tel", "password", "hidden",
    "number", "textarea", "date", "datetime-local", "month", "week", "time",
})

# M6: tipos de campo que a sonda de reflexão NUNCA envia (defesa em
# profundidade), pois disparam ação de UI ou upload/estado.
_TIPOS_BLOQUEADOS = frozenset({"file", "submit", "reset", "button"})

# Bibliotecas reconhecidas para consulta OSV (nome npm -> padrão de extração).
_LIBS_OSV = (
    ("jquery", "jquery"),
    ("bootstrap", "bootstrap"),
    ("react", "react"),
    ("vue", "vue"),
    ("angular", "angular"),
)


# ===========================================================================
# SEÇÃO 1 — MODELO (dataclasses tipadas)
# ===========================================================================

class Severidade(str, Enum):
    """Severidade de um achado. Ordem de agregação: CRITICA é a pior."""

    CRITICA = "critica"
    ALTA = "alta"
    MEDIA = "media"
    BAIXA = "baixa"
    INFO = "info"


# Ordem do MENOR para o MAIOR risco (usada por `agrega_severidade`).
_SEVERIDADE_ORDEM = {
    Severidade.INFO: 0,
    Severidade.BAIXA: 1,
    Severidade.MEDIA: 2,
    Severidade.ALTA: 3,
    Severidade.CRITICA: 4,
}


class Confianca(str, Enum):
    """Confiança do achado: OBSERVADO (há evidência bruta) ou INFERIDO."""

    OBSERVADO = "observado"
    INFERIDO = "inferido"


@dataclass
class Finding:
    """Achado tipado da auditoria.

    Anti-fabricação: `confianca=OBSERVADO` EXIGE `evidencia` não vazia
    (validado em `__post_init__`). Um achado `INFERIDO` pode não ter evidência,
    mas fica marcado como tal (nunca é apresentado como fato observado).

    `evidencia` guarda o valor BRUTO capturado (header, corpo, registro DNS,
    URL) — obrigatório para observados. `recomendacao` informa a correção
    sugerida; o módulo NUNCA a aplica.
    """

    id: str                     # slug estável (ex.: "email-spf-ausente")
    categoria: str              # ex.: "email", "headers", "cookies", "tls"
    titulo: str
    severidade: Severidade
    confianca: Confianca
    descricao: str
    evidencia: str              # valor bruto capturado — OBRIGATÓRIO p/ OBSERVADO
    origem: str                 # URL/requisição de onde veio
    passos_repro: list[str]
    recomendacao: str           # sem correção aplicada
    stride: str = ""            # Spoofing/Tampering/Repudiation/Information disclosure/Denial of service/Elevation of privilege
    owasp: str = ""             # ex.: "A05:2021"
    cwe: str = ""               # ex.: "CWE-693"
    cvss: float | None = None
    referencia: str = ""

    def __post_init__(self) -> None:
        # Coerção tolerante: aceita strings ("alta"/"observado") além dos enums.
        if not isinstance(self.severidade, Severidade):
            self.severidade = Severidade(str(self.severidade))
        if not isinstance(self.confianca, Confianca):
            self.confianca = Confianca(str(self.confianca))
        if not isinstance(self.passos_repro, list):
            self.passos_repro = [str(self.passos_repro)]
        if self.confianca == Confianca.OBSERVADO and not str(self.evidencia).strip():
            raise ValueError(
                "Finding OBSERVADO exige evidencia nao vazia (anti-fabricacao): "
                f"id={self.id!r}"
            )

    def to_dict(self) -> dict:
        """Serializa o achado (enums viram strings)."""
        return {
            "id": self.id,
            "categoria": self.categoria,
            "titulo": self.titulo,
            "severidade": self.severidade.value,
            "confianca": self.confianca.value,
            "stride": self.stride,
            "owasp": self.owasp,
            "cwe": self.cwe,
            "cvss": self.cvss,
            "descricao": self.descricao,
            "evidencia": self.evidencia,
            "origem": self.origem,
            "passos_repro": list(self.passos_repro),
            "recomendacao": self.recomendacao,
            "referencia": self.referencia,
        }


@dataclass
class Relatorio:
    """Relatório consolidado de uma execução de auditoria.

    `contexto_informado` guarda o que o solicitante AFIRMOU (cada item com
    `texto` e `verificado=False`): contexto informado NUNCA vira fato — só
    evidência observada (`Finding` com `confianca=OBSERVADO`) conta.
    """

    alvo: str
    perfil: str
    data_inicio: str
    data_fim: str
    escopo: dict = field(default_factory=dict)
    contexto_informado: list[dict] = field(default_factory=list)
    findings: list[Finding] = field(default_factory=list)
    fases_executadas: list[str] = field(default_factory=list)
    versao_ferramenta: str = VERSAO
    status_por_categoria: dict[str, str] = field(default_factory=dict)

    def resumo_por_severidade(self) -> dict:
        """Contagem por severidade (todas as chaves, zero quando ausente)."""
        resumo = {s.value: 0 for s in Severidade}
        for f in self.findings:
            resumo[f.severidade.value] += 1
        return resumo

    def to_dict(self) -> dict:
        """Serializa o relatório (findings como dicts)."""
        return {
            "alvo": self.alvo,
            "perfil": self.perfil,
            "data_inicio": self.data_inicio,
            "data_fim": self.data_fim,
            "escopo": dict(self.escopo),
            "contexto_informado": [dict(c) for c in self.contexto_informado],
            "findings": [f.to_dict() for f in self.findings],
            "resumo_severidade": self.resumo_por_severidade(),
            "fases_executadas": list(self.fases_executadas),
            "versao_ferramenta": self.versao_ferramenta,
            "status_por_categoria": dict(self.status_por_categoria),
        }


def agrega_severidade(severidades: list[Severidade]) -> Severidade:
    """Pior severidade da lista (CRITICA > ALTA > MEDIA > BAIXA > INFO).

    Lista vazia -> INFO (nenhum achado). Aceita strings e faz coerção.
    """
    pior = Severidade.INFO
    for s in severidades:
        if not isinstance(s, Severidade):
            s = Severidade(str(s))
        if _SEVERIDADE_ORDEM[s] > _SEVERIDADE_ORDEM[pior]:
            pior = s
    return pior


# ===========================================================================
# SEÇÃO 2 — PERFIS
# ===========================================================================

PERFIS: dict[str, dict] = {
    "osint": {
        "fases": ["passivo"],
        "requer_gate": False,
        "portas": [443],
        "descricao": "Inteligência pública, sem contato com a aplicação",
    },
    "superficial": {
        "fases": ["passivo", "superficie"],
        "requer_gate": False,
        "portas": [80, 443],
        "descricao": "Não invasivo: leitura HTTP normal (GET/HEAD)",
    },
    "completo": {
        "fases": ["passivo", "superficie", "ativo"],
        "requer_gate": True,
        "portas": [80, 443, 8080, 8443],
        "descricao": "Tudo disponível + testes ativos read-only autorizados",
    },
}


def perfil_valido(p: str) -> bool:
    """True se `p` é um perfil conhecido."""
    return str(p) in PERFIS


def fases_do_perfil(p: str) -> list[str]:
    """Fases do perfil (lista vazia se o perfil for inválido)."""
    perfil = PERFIS.get(str(p))
    if not perfil:
        return []
    return list(perfil["fases"])


def perfil_autorizado(perfil: str, escopo: dict | None) -> tuple[bool, str]:
    """Gate de autorização por perfil.

    O perfil `completo` exige `escopo` com a fase "ativo" E `autorizado is
    True`; sem isso retorna `(False, motivo)`. Os demais perfis liberam.
    """
    if not perfil_valido(perfil):
        return (False, f"perfil invalido: {perfil!r}")
    if not PERFIS[str(perfil)]["requer_gate"]:
        return (True, "")
    dados = escopo or {}
    fases = dados.get("fases") or []
    if "ativo" not in fases:
        return (False, "perfil 'completo' exige escopo com a fase 'ativo'")
    if dados.get("autorizado") is not True:
        return (
            False,
            "perfil 'completo' exige autorizacao explicita (escopo.autorizado=True)",
        )
    return (True, "")


def _porta_tls_padrao(url: str) -> int:
    """Porta TLS padrão de uma URL (A1): porta explícita, senão `http` -> 80 e
    `https` -> 443. NUNCA usa a lista de portas do perfil. Tolerante a URL
    inválida (cai em 443). Nunca levanta."""
    try:
        par = urllib.parse.urlparse(str(url))
        if par.port:
            return int(par.port)
        if par.scheme.lower() == "http":
            return 80
    except (ValueError, TypeError):
        pass
    return 443


# ===========================================================================
# SEÇÃO 3 — TRANSPORTE (SSRF-safe, budget, rate-limit, DoH)
# ===========================================================================

def _alvo_valido(url: str, allow_loopback: bool = False) -> bool:
    """True se a URL é auditável.

    Alvo EXTERNO (resolvido fora de loopback/privado/link-local/etc.) -> ok.
    Alvo INTERNO -> somente se o host estiver em `config.ALLOWED_AUDIT_TARGETS`
    E `config.AUDIT_ALLOW_INTERNAL` for True (default seguro: bloqueia).
    `allow_loopback=True` é EXCLUSIVO de testes/servidor local (nunca afeta a
    produção) e delega ao `_ssrf_valido` do webscraper, que exige TODOS os IPs
    resolvidos loopback.
    """
    try:
        host = urllib.parse.urlparse(url).hostname
    except ValueError:
        return False
    if not host:
        return False
    if _ssrf_valido(url, allow_loopback=allow_loopback):
        return True
    # Interno/indeterminado: libera apenas com allowlist explícita.
    return bool(config.AUDIT_ALLOW_INTERNAL and host in config.ALLOWED_AUDIT_TARGETS)


def _host_interno(host: str) -> bool:
    """True se `host` resolve para IP interno/perigoso (reuso do webscraper)."""
    return any(_ip_perigoso(ip) for ip in _host_ips(host))


class _AuditRedirectHandler(urllib.request.HTTPRedirectHandler):
    """Segue redirects revalidando cada hop com `_alvo_valido` (anti-SSRF
    ALLOWLIST-AWARE) e teto de `MAX_REDIRECTS`.

    Um site externo NÃO pode redirecionar a auditoria para serviços internos
    (127.0.0.1, RFC-1918): o redirect inválido levanta URLError antes de
    conectar. `allow_loopback` é repassado ao validador (testes locais).
    """

    max_redirections = MAX_REDIRECTS

    def __init__(self, allow_loopback: bool = False,
                 registros: list | None = None):
        super().__init__()
        self._allow_loopback = allow_loopback
        # Lista opcional que acumula `(status, novo_url)` por hop seguido;
        # permite reconstruir a cadeia de redirects em `audit_redirects`.
        self._registros = registros

    def redirect_request(self, req, fp, code, msg, headers, newurl):
        if not _alvo_valido(newurl, allow_loopback=self._allow_loopback):
            raise urllib.error.URLError(
                f"redirect bloqueado (SSRF/alvo não autorizado): {newurl!r}"
            )
        if self._registros is not None:
            self._registros.append((int(code), str(newurl)))
        return super().redirect_request(req, fp, code, msg, headers, newurl)


class _RedirectNaoSeguido(Exception):
    """Sinaliza um redirect 3xx capturado SEM seguir (open redirect).

    Carrega status, destino (`Location`) e headers para inspeção read-only.
    Como o destino NÃO é seguido, não há nova conexão (sem SSRF).
    """

    def __init__(self, code, newurl, headers):
        super().__init__(str(newurl))
        self.code = int(code)
        self.newurl = str(newurl)
        self.headers = {
            str(k).lower(): str(v) for k, v in dict(headers or {}).items()
        }


class _AuditRedirectSemSeguir(urllib.request.HTTPRedirectHandler):
    """Captura o primeiro redirect 3xx e PARA (`seguir_redirects=False`).

    Usado por `audit_open_redirect` para inspecionar o `Location` sem seguir
    para o destino — a ausência de follow elimina o risco de SSRF.
    """

    def redirect_request(self, req, fp, code, msg, headers, newurl):
        raise _RedirectNaoSeguido(code, newurl, headers)


@dataclass
class Resposta:
    """Resposta HTTP tipada e tolerante a falha.

    `erro` e `bloqueado` são mutuamente exclusivos com `status` preenchido:
    - sucesso       -> `status`/`headers`/`set_cookie`/`final_url`/`body`
    - falha de rede -> `erro`
    - alvo negado   -> `bloqueado`
    """

    status: int | None = None
    headers: dict = field(default_factory=dict)
    set_cookie: list[str] = field(default_factory=list)
    final_url: str = ""
    body: str = ""
    erro: str = ""
    bloqueado: str = ""

    @property
    def ok(self) -> bool:
        """True quando houve resposta HTTP sem erro/bloqueio."""
        return self.status is not None and not self.erro and not self.bloqueado


class Budget:
    """Orçamento de requisições de rede por execução (anti-varredura)."""

    def __init__(self, max_requisicoes: int):
        self.max_requisicoes = max(0, int(max_requisicoes))
        self.usados = 0

    def consumir(self) -> bool:
        """Consome 1 requisição; False quando o teto já foi atingido."""
        if self.usados >= self.max_requisicoes:
            return False
        self.usados += 1
        return True

    @property
    def contador(self) -> int:
        """Quantidade de requisições já consumidas."""
        return self.usados

    @property
    def restantes(self) -> int:
        """Quantidade restante antes do teto."""
        return max(0, self.max_requisicoes - self.usados)


# Budget ATIVO do módulo (instanciado por execução via `iniciar_budget`).
# None = sem teto (uso programático/testes que não declaram orçamento).
_BUDGET: Budget | None = None


def iniciar_budget(max_requisicoes: int) -> Budget:
    """Instancia e ativa o budget da execução atual; retorna o `Budget`."""
    global _BUDGET
    _BUDGET = Budget(max_requisicoes)
    return _BUDGET


def _consome_budget() -> bool:
    """Consome 1 requisição do budget ativo; True se não houver budget."""
    if _BUDGET is None:
        return True
    return _BUDGET.consumir()


def _pausa() -> None:
    """Rate-limit entre requisições (`AUDIT_RATE_LIMIT_SEG`). Nunca levanta."""
    try:
        time.sleep(config.AUDIT_RATE_LIMIT_SEG)
    except (TypeError, ValueError, OSError):
        pass


def requisicao(
    url: str,
    metodo: str = "GET",
    timeout: int = TIMEOUT,
    retries: int = 2,
    allow_loopback: bool = False,
    headers_extra: dict | None = None,
    registrar_redirects: list | None = None,
    dados: bytes | None = None,
    seguir_redirects: bool = True,
) -> Resposta:
    """Requisição HTTP anti-SSRF e tolerante a falha.

    Ordem: budget -> `_alvo_valido` -> urllib com `HTTPSHandler(ssl_ctx)` e
    `_AuditRedirectHandler` (revalida cada hop). Captura TODOS os Set-Cookie
    (`get_all`), trunca o corpo em 64KB e devolve `Resposta`. Erros de rede
    viram `Resposta.erro` (nunca exceção para o chamador).

    `registrar_redirects` (opcional) acumula `(status, url)` por hop seguido,
    permitindo reconstruir a cadeia em `audit_redirects` sem duplicar o
    transporte. `dados` envia corpo (POST/form) com o `Content-Type` do
    `headers_extra`. `seguir_redirects=False` captura o PRIMEIRO 3xx sem
    seguir (usado por `audit_open_redirect`), devolvendo a `Resposta` com o
    status 3xx e o header `Location`.
    """
    if not _consome_budget():
        return Resposta(
            status=None, headers={}, set_cookie=[], final_url=url, body="",
            bloqueado="budget de requisicoes esgotado",
        )
    if not _alvo_valido(url, allow_loopback=allow_loopback):
        return Resposta(
            status=None, headers={}, set_cookie=[], final_url=url, body="",
            bloqueado="SSRF/alvo não autorizado",
        )
    cabecalhos = {
        "User-Agent": USER_AGENT,
        "Accept": (
            "text/html,application/xhtml+xml,"
            "application/json;q=0.9,*/*;q=0.8"
        ),
    }
    cabecalhos.update(headers_extra or {})
    try:
        req = urllib.request.Request(
            url, data=dados, method=metodo, headers=cabecalhos,
        )
        if seguir_redirects:
            manipulador_redirect = _AuditRedirectHandler(
                allow_loopback=allow_loopback, registros=registrar_redirects,
            )
        else:
            manipulador_redirect = _AuditRedirectSemSeguir()
        abridor = urllib.request.build_opener(
            urllib.request.HTTPSHandler(context=ssl_ctx),
            manipulador_redirect,
        )
    except (ValueError, TypeError) as exc:
        return Resposta(
            status=None, headers={}, set_cookie=[], final_url=url, body="",
            erro=str(exc),
        )
    ultimo_erro: Exception | None = None
    for tentativa in range(retries + 1):
        try:
            with abridor.open(req, timeout=timeout) as resp:
                corpo = resp.read(LIMITE_CORPO + 1024)
                headers = {k.lower(): v for k, v in resp.headers.items()}
                # Multiple Set-Cookie: o dict acima colapsa duplicatas; a
                # lista crua preserva TODOS os cookies.
                set_cookie: list[str] = []
                try:
                    set_cookie = [
                        str(c) for c in (resp.headers.get_all("Set-Cookie") or [])
                    ]
                except AttributeError:
                    sc = resp.headers.get("Set-Cookie")
                    if sc:
                        set_cookie = [sc]
                return Resposta(
                    status=resp.status,
                    headers=headers,
                    set_cookie=set_cookie,
                    final_url=resp.geturl(),
                    body=corpo.decode("utf-8", errors="replace")[:LIMITE_CORPO],
                )
        except _RedirectNaoSeguido as exc:
            sc = exc.headers.get("set-cookie")
            return Resposta(
                status=exc.code,
                headers=exc.headers,
                set_cookie=[sc] if sc else [],
                final_url=url,
                body="",
            )
        except (urllib.error.URLError, TimeoutError, ConnectionError,
                ssl.SSLError, OSError, OverflowError,
                http.client.HTTPException) as exc:
            ultimo_erro = exc
            if tentativa < retries:
                time.sleep(0.5 * (tentativa + 1))
    return Resposta(
        status=None, headers={}, set_cookie=[], final_url=url, body="",
        erro=str(ultimo_erro or "falha de rede"),
    )


def requisicao_json(
    url: str, timeout: int = 10, allow_loopback: bool = False
) -> dict | list | None:
    """GET que espera JSON (DoH/crt.sh) e devolve o objeto ou None.

    Envia `Accept: application/dns-json, application/json`. Tolerante: erro,
    bloqueio, status não-2xx ou JSON inválido -> None.
    """
    resp = requisicao(
        url,
        metodo="GET",
        timeout=timeout,
        retries=1,
        allow_loopback=allow_loopback,
        headers_extra={"Accept": "application/dns-json, application/json"},
    )
    if not resp.ok or not (200 <= (resp.status or 0) < 300):
        return None
    try:
        return json.loads(resp.body)
    except (json.JSONDecodeError, ValueError):
        return None


def mascarar(valor: str) -> str:
    """Redige um segredo com máscara do meio.

    Valores curtos (len <= 8) são considerados seguros e retornados como
    estão; maiores retornam `valor[:4] + "***" + valor[-4:]` — o núcleo do
    segredo é substituído (redação, não truncamento).
    """
    texto = str(valor)
    if len(texto) <= 8:
        return texto
    return texto[:4] + "***" + texto[-4:]


def mascarar_sempre(valor: str) -> str:
    """Máscara INCONDICIONAL de um segredo (B11) — usada para VALOR de cookie.

    Diferente de `mascarar`, NUNCA devolve o valor curto como está: valores de
    até 8 chars viram `*` × len; maiores preservam apenas as bordas. Um valor
    de cookie é sempre segredo em potencial, então nunca é exibido."""
    texto = str(valor)
    if not texto:
        return ""
    if len(texto) <= 8:
        return "*" * len(texto)
    return texto[:4] + "***" + texto[-4:]


def redigir_header(nome: str, valor: str) -> str:
    """Valor de header para relatório: mascara se nome OU valor contém
    marcador sensível (nonce/token/key/secret/authorization/set-cookie/
    api-key); demais são truncados em 200 chars.

    B11: quando o header é de cookie (nome contém 'cookie'), o valor é
    mascarado SEMPRE (`mascarar_sempre`), nunca exibido mesmo se curto."""
    v = str(valor)
    nome_l = str(nome).lower()
    alvo = f"{nome} {v}".lower()
    if "cookie" in nome_l:
        return mascarar_sempre(v)
    if any(m in alvo for m in _MARCADORES_SENSIVEIS):
        return mascarar(v)
    return v[:200]


# ===========================================================================
# SEÇÃO 4 — CHECKS OSINT
# ===========================================================================

def _vcard_fn(entidade: dict) -> str:
    """Extrai o nome formatado ('fn') do vcardArray de uma entidade RDAP."""
    vcard = entidade.get("vcardArray")
    if not isinstance(vcard, (list, tuple)) or len(vcard) < 2:
        return ""
    for item in vcard[1]:
        if (isinstance(item, (list, tuple)) and len(item) >= 4
                and item[0] == "fn"):
            return str(item[3])
    return ""


def audit_rdap(dominio: str) -> dict:
    """Reconhecimento passivo via RDAP (sem contato com o alvo).

    Bootstrap em `_RDAP_BOOTSTRAP` via `requisicao_json`, acha a base_url do
    TLD e consulta `base_url + "/domain/" + dominio`. Retorna {"dominio",
    "tld", "rdap_url", "registrar", "criado", "expira", "nameservers",
    "status", "erros"} e, em falha, também "erro". Nunca levanta.
    """
    dominio = str(dominio).strip().lower().rstrip(".")
    tld = dominio.rsplit(".", 1)[-1] if "." in dominio else dominio
    base: dict = {
        "dominio": dominio,
        "tld": tld,
        "rdap_url": "",
        "registrar": "",
        "criado": "",
        "expira": "",
        "nameservers": [],
        "status": [],
        "erros": [],
    }
    boot = requisicao_json(_RDAP_BOOTSTRAP)
    if not isinstance(boot, dict):
        base["erros"].append("bootstrap RDAP indisponivel (data.iana.org)")
        return {**base, "erro": "bootstrap RDAP indisponivel"}
    base_url = ""
    for servico in boot.get("services") or []:
        if not isinstance(servico, (list, tuple)) or len(servico) < 2:
            continue
        tlds, urls = servico[0], servico[1]
        if not isinstance(tlds, (list, tuple)):
            tlds = [tlds]
        if tld not in tlds:
            continue
        urls_list = urls if isinstance(urls, (list, tuple)) else [urls]
        for u in urls_list:
            if isinstance(u, str) and u:
                base_url = u
                break
        if base_url:
            break
    if not base_url:
        base["erros"].append(f"nenhum servidor RDAP para o TLD {tld!r}")
        return {**base, "erro": f"nenhum servidor RDAP para o TLD {tld!r}"}
    rdap_url = base_url.rstrip("/") + "/domain/" + dominio
    base["rdap_url"] = rdap_url
    dados = requisicao_json(rdap_url)
    if not isinstance(dados, dict):
        base["erros"].append(f"consulta RDAP falhou para {dominio}")
        return {**base, "erro": f"consulta RDAP falhou para {dominio}"}
    base["status"] = dados.get("status") or []
    for ent in dados.get("entities") or []:
        if not isinstance(ent, dict):
            continue
        if "registrar" in (ent.get("roles") or []):
            base["registrar"] = _vcard_fn(ent)
            if base["registrar"]:
                break
    for ev in dados.get("events") or []:
        if not isinstance(ev, dict):
            continue
        acao = ev.get("eventAction")
        data_ev = ev.get("eventDate", "")
        if acao == "registration" and not base["criado"]:
            base["criado"] = data_ev
        elif acao == "expiration" and not base["expira"]:
            base["expira"] = data_ev
    base["nameservers"] = [
        ns.get("ldhName", "") for ns in (dados.get("nameservers") or [])
        if isinstance(ns, dict) and ns.get("ldhName")
    ]
    return base


def _doh_url(nome: str, tipo: str) -> str:
    """URL DoH do Cloudflare para `nome`/`tipo`."""
    return (
        f"{_DOH_BASE}?name={urllib.parse.quote(nome)}"
        f"&type={urllib.parse.quote(tipo)}"
    )


def _consulta_doh(nome: str, tipo: str) -> tuple[list[str], str]:
    """Consulta um registro via DoH; retorna (valores, erro). Tolerante."""
    dados = requisicao_json(_doh_url(nome, tipo))
    if not isinstance(dados, dict):
        return [], f"DoH {tipo} de {nome} falhou"
    status = dados.get("Status")
    if status not in (0, None):
        return [], f"DoH {tipo} de {nome} status {status}"
    respostas = dados.get("Answer") or []
    valores = [
        str(a.get("data", ""))
        for a in respostas
        if isinstance(a, dict) and a.get("data")
    ]
    return valores, ""


def audit_dns(dominio: str) -> dict:
    """Resolve A/AAAA/NS/MX/TXT/CAA via DoH (Cloudflare JSON).

    Retorna um dict com uma chave por tipo (lista de valores brutos) mais
    "dominio", "erros" (mensagens por tipo que falhou) e "lookup_ok" (dict
    tipo -> bool com o SUCESSO da consulta daquele tipo). O "lookup_ok" é o
    que permite distinguir "registro ausente" de "lookup falhou" em
    `audit_email`. Nunca levanta.
    """
    dominio = str(dominio).strip().lower().rstrip(".")
    resultado: dict = {"dominio": dominio, "erros": [], "lookup_ok": {}}
    for tipo in TIPOS_DNS:
        valores, erro = _consulta_doh(dominio, tipo)
        resultado[tipo] = valores
        resultado["lookup_ok"][tipo] = not erro
        if erro:
            resultado["erros"].append(erro)
    return resultado


def audit_email(dominio: str, dns_result: dict) -> list[Finding]:
    """Verifica SPF e DMARC; ausência vira `Finding` SÓ com lookup bem-sucedido.

    SPF: TXT do domínio começando com `v=spf1`.
    DMARC: TXT de `_dmarc.<dominio>` começando com `v=DMARC1`. Se
    `dns_result` já trouxer a chave "DMARC"/"_dmarc" (lista de TXT), ela é
    usada; senão a consulta DoH é feita com `_consulta_doh` (tolerante).

    D2 (anti-falso-positivo): "ausente" só é afirmado quando o lookup daquele
    item teve SUCESSO e não retornou o registro correspondente. Se o lookup
    falhou/indisponível, NÃO emite finding de ausência e registra a falha em
    `dns_result["erros"]` (quando aplicável), seguindo adiante. Presença de
    SPF/DMARC nunca gera finding. A ausência OBSERVADA usa evidência
    "ausente; origem=<url>".
    """
    dominio = str(dominio).strip().lower().rstrip(".")
    dados = dns_result if isinstance(dns_result, dict) else {}
    txts = [str(t) for t in (dados.get("TXT") or [])]
    lookup_ok = dados.get("lookup_ok") or {}
    findings: list[Finding] = []

    origem_spf = _doh_url(dominio, "TXT")
    tem_spf = any(t.lower().startswith("v=spf1") for t in txts)
    # Sucesso do lookup TXT: metadado estruturado de `audit_dns` quando
    # disponível; senão assume-se sucesso se o chamador forneceu a chave "TXT".
    if "lookup_ok" in dados:
        spf_ok = bool(lookup_ok.get("TXT"))
    else:
        spf_ok = "TXT" in dados
    # Lookup falhou -> não afirma ausência (falha já consta em "erros").
    if spf_ok and not tem_spf:
        findings.append(Finding(
            id="email-spf-ausente",
            categoria="email",
            titulo="SPF ausente",
            severidade=Severidade.MEDIA,
            confianca=Confianca.OBSERVADO,
            stride="Spoofing",
            cwe="CWE-290",
            descricao=(
                "Nenhum registro TXT com 'v=spf1' encontrado para o domínio. "
                "Sem SPF, remetentes podem falsificar o domínio com mais "
                "facilidade em mensagens de e-mail."
            ),
            evidencia=f"ausente; origem={origem_spf}",
            origem=origem_spf,
            passos_repro=[
                f"Consultar TXT de {dominio} via DoH",
                "Verificar se algum TXT comeca com 'v=spf1'",
            ],
            recomendacao=(
                "Publicar registro TXT SPF autorizando apenas os remetentes "
                "legítimos (sem correção automática)."
            ),
            referencia="",
        ))

    dmarc_txts = dados.get("DMARC")
    if dmarc_txts is None:
        dmarc_txts = dados.get("_dmarc")
    nome_dmarc = "_dmarc." + dominio
    origem_dmarc = _doh_url(nome_dmarc, "TXT")
    if dmarc_txts is not None:
        # Valor já fornecido pelo chamador -> lookup considerado bem-sucedido.
        dmarc_ok = True
    else:
        dmarc_txts, dmarc_erro = _consulta_doh(nome_dmarc, "TXT")
        dmarc_ok = not dmarc_erro
        if dmarc_erro:
            # Registra a falha do lookup e NÃO afirma ausência (D2).
            erros = dados.get("erros")
            if isinstance(erros, list):
                erros.append(dmarc_erro)
    tem_dmarc = any(str(t).lower().startswith("v=dmarc1") for t in dmarc_txts)
    if dmarc_ok and not tem_dmarc:
        findings.append(Finding(
            id="email-dmarc-ausente",
            categoria="email",
            titulo="DMARC ausente",
            severidade=Severidade.MEDIA,
            confianca=Confianca.OBSERVADO,
            stride="Spoofing",
            cwe="CWE-290",
            descricao=(
                "Nenhum registro TXT 'v=DMARC1' encontrado em "
                f"'{nome_dmarc}'. Sem DMARC, não há política de alinhamento/"
                "rejeição para mensagens falsificadas."
            ),
            evidencia=f"ausente; origem={origem_dmarc}",
            origem=origem_dmarc,
            passos_repro=[
                f"Consultar TXT de {nome_dmarc} via DoH",
                "Verificar se algum TXT comeca com 'v=DMARC1'",
            ],
            recomendacao=(
                "Publicar registro DMARC (ex.: p=quarantine/reject) e "
                "acompanhar relatórios agregados."
            ),
            referencia="",
        ))
    return findings


def audit_ct_logs(dominio: str) -> dict:
    """Subdomínios observados nos logs de Certificate Transparency (crt.sh).

    Consulta `crt.sh/?q=%.<dominio>&output=json` via `requisicao_json` e
    extrai `name_value` (split por quebra de linha), normalizando, removendo
    wildcards e mantendo apenas o domínio e subdomínios dele. Tolerante:
    falha -> "erros" com a lista "subdominios" vazia.
    """
    dominio = str(dominio).strip().lower().rstrip(".")
    resultado: dict = {"dominio": dominio, "subdominios": [], "erros": []}
    url = f"https://crt.sh/?q=%25.{urllib.parse.quote(dominio)}&output=json"
    dados = requisicao_json(url, timeout=15)
    if not isinstance(dados, list):
        resultado["erros"].append("crt.sh indisponivel ou resposta invalida")
        return resultado
    nomes: set[str] = set()
    for item in dados:
        if not isinstance(item, dict):
            continue
        for nome in str(item.get("name_value", "")).split("\n"):
            nome = nome.strip().lower().rstrip(".")
            if not nome or nome.startswith("*"):
                continue
            if nome == dominio or nome.endswith("." + dominio):
                nomes.add(nome)
    resultado["subdominios"] = sorted(nomes)
    return resultado


# --- WP4 (superfície, implementado) / WP5 (ativo, ainda stub) --------------

# ===========================================================================
# WP4 — CHECK DE SUPERFÍCIE HTTP (A-H)
# ===========================================================================
#
# Perfil superficial: leitura HTTP normal (GET/HEAD) sem exploração ativa.
# Todos os checks são TOLERANTES a falha (nenhuma exceção de rede escapa),
# usam `Budget`/`_pausa`/`requisicao` e produzem apenas `Finding` OBSERVADO
# com `evidencia` preenchida. Segredos são redigidos com `redigir_header`/
# `mascarar`; o módulo informa, nunca corrige.


def _buscar(url: str, allow_loopback: bool = False) -> Resposta:
    """GET tolerante que nunca levanta (falha -> `Resposta.erro`)."""
    try:
        _pausa()
        return requisicao(url, allow_loopback=allow_loopback)
    except Exception:  # noqa: BLE001 — superfície nunca derruba a auditoria
        return Resposta(erro="falha de rede na requisicao", final_url=url)


def _novo_finding(
    id: str,
    categoria: str,
    titulo: str,
    severidade: Severidade,
    descricao: str,
    evidencia: str,
    origem: str,
    recomendacao: str = "",
    stride: str = "",
    owasp: str = "",
    cwe: str = "",
    cvss: float | None = None,
    referencia: str = "",
    passos_repro: list[str] | None = None,
) -> Finding:
    """Atalho para `Finding` OBSERVADO (evidência sempre presente)."""
    return Finding(
        id=id,
        categoria=categoria,
        titulo=titulo,
        severidade=severidade,
        confianca=Confianca.OBSERVADO,
        descricao=descricao,
        evidencia=evidencia,
        origem=origem,
        passos_repro=list(passos_repro) if passos_repro else [],
        recomendacao=recomendacao,
        stride=stride,
        owasp=owasp,
        cwe=cwe,
        cvss=cvss,
        referencia=referencia,
    )


def _snippet(texto: str, pos: int, janela: int = 120) -> str:
    """Trecho de ~`janela` chars ao redor de `pos`, sem quebras de linha."""
    inicio = max(0, pos - janela // 2)
    fim = min(len(texto), pos + janela // 2)
    return texto[inicio:fim].replace("\r", " ").replace("\n", " ").strip()


# ---------------------------------------------------------------------------
# Helpers dos testes ATIVOS read-only (WP5)
# ---------------------------------------------------------------------------

def _dedup(findings: list[Finding]) -> list[Finding]:
    """Remove Findings repetidos pelo `id`, preservando a primeira ordem."""
    vistos: set[str] = set()
    unicos: list[Finding] = []
    for f in findings:
        if f.id in vistos:
            continue
        vistos.add(f.id)
        unicos.append(f)
    return unicos


def _tem_payload_destrutivo(*partes) -> bool:
    """True se qualquer parte contém marcador de `AUDIT_PAYLOADS_DESTRUTIVOS`.

    Fronteira da REGRA DE OURO: nenhuma sonda ativa deve carregar payload
    destrutivo. As sondas do módulo usam apenas canários/benignos, mas a guarda
    é explícita (defesa em profundidade) para futuras extensões.
    """
    texto = " ".join(str(p) for p in partes).lower()
    return any(str(m).lower() in texto
               for m in config.AUDIT_PAYLOADS_DESTRUTIVOS)


def _origem_base(url: str) -> str:
    """`scheme://netloc` da URL (cai para a própria URL se não houver netloc)."""
    par = urllib.parse.urlparse(url)
    return f"{par.scheme}://{par.netloc}" if par.netloc else url


def _slug_arquivo(url: str) -> str:
    """Slug estável do arquivo a partir do path da URL (N3).

    Usado para distinguir achados do MESMO tipo em arquivos DIFERENTES (ex.:
    `/lib-b.js` -> `lib-b-js`), de modo que a deduplicação por `id` não
    descarte o segundo arquivo. Nunca levanta.
    """
    try:
        caminho = urllib.parse.urlparse(str(url)).path or str(url)
        nome = pathlib.PurePosixPath(caminho.replace("\\", "/")).name or caminho
        slug = re.sub(r"[^a-z0-9]+", "-", nome.lower()).strip("-")
        return slug or "js"
    except (ValueError, TypeError):
        return "js"


def _novo_canario() -> str:
    """Canário único e alfanumérico para sondas (nunca colide com o alvo)."""
    return "audit" + os.urandom(4).hex()


def _canario_refletido(canario: str, corpo: str) -> bool:
    """True se `canario` aparece no corpo bruto ou em forma escapada.

    Cobre texto cru, entidades HTML (decimais/hex) e percent-encoding. Um
    canário alfanumérico nunca contém caracteres que precisem de escape, mas
    a checagem é conservadora para refletir o que o servidor devolveu.
    """
    if not corpo or not canario:
        return False
    if canario in corpo:
        return True
    try:
        if canario in _html.unescape(corpo):
            return True
    except (TypeError, ValueError):
        pass
    entidade_dec = "".join(f"&#{ord(c)};" for c in canario)
    entidade_hex = "".join(f"&#x{ord(c):x};" for c in canario)
    if entidade_dec in corpo or entidade_hex in corpo:
        return True
    if urllib.parse.quote(canario) in corpo:
        return True
    return False


def _com_query(url: str, itens: list[tuple[str, str]]) -> str:
    """Reconstrói a URL trocando a query pelos pares `itens`."""
    par = urllib.parse.urlparse(url)
    nova_query = urllib.parse.urlencode(itens, doseq=True)
    return urllib.parse.urlunparse(par._replace(query=nova_query))


def _parse_forms(texto_html: str) -> list[dict]:
    """Extrai formulários (action, method e campos nome/type) de um HTML.

    Best-effort por regex (stdlib only); nunca levanta.
    """
    forms: list[dict] = []
    for m in _FORM_RE.finditer(texto_html or ""):
        attrs = {k.lower(): v for k, v in _ATTR_RE.findall(m.group(1))}
        method = (attrs.get("method") or "get").strip().lower()
        campos: list[tuple[str, str]] = []
        for cm in _CAMPO_FORM_RE.finditer(m.group(2)):
            cattrs = {k.lower(): v for k, v in _ATTR_RE.findall(cm.group(1))}
            nome = cattrs.get("name")
            if not nome:
                continue
            tipo = (cattrs.get("type") or "text").strip().lower()
            campos.append((nome, tipo))
        forms.append({
            "action": attrs.get("action", ""),
            "method": method,
            "inputs": campos,
        })
    return forms


def _script_srcs(texto_html: str) -> list[str]:
    """URLs de `<script src=...>` (únicas, ordem de aparição)."""
    vistos: set[str] = set()
    urls: list[str] = []
    for src in _SCRIPT_SRC_RE.findall(texto_html or ""):
        if src not in vistos:
            vistos.add(src)
            urls.append(src)
    return urls


def _avaliar_cors(
    resposta: Resposta, url: str, canario: str,
) -> list[Finding]:
    """Avalia headers CORS de uma `Resposta` (compartilhado passivo/ativo).

    Reflete o canário -> ALTA (CWE-942); wildcard + credenciais -> ALTA;
    wildcard sem credenciais -> BAIXA. Evidência: ACAO/ACAC.
    """
    findings: list[Finding] = []
    if not resposta.ok:
        return findings
    h = {str(k).lower(): str(v) for k, v in (resposta.headers or {}).items()}
    acao = h.get("access-control-allow-origin")
    acac = (h.get("access-control-allow-credentials") or "").strip().lower()
    if not acao:
        return findings
    origem = resposta.final_url or url
    evidencia = (
        f"Access-Control-Allow-Origin={acao}; "
        f"Access-Control-Allow-Credentials={acac or 'ausente'}; origem={origem}"
    )
    if acao.strip() == "*" and acac == "true":
        findings.append(_novo_finding(
            id="cors-wildcard-credenciais",
            categoria="cors",
            titulo="CORS com wildcard e credenciais",
            severidade=Severidade.ALTA,
            descricao=(
                "Access-Control-Allow-Origin '*' combinado com "
                "Allow-Credentials true expõe respostas autenticadas a qualquer "
                "origem."
            ),
            evidencia=evidencia,
            origem=origem,
            stride="Information disclosure",
            owasp="A05:2021",
            cwe="CWE-942",
            recomendacao=(
                "Usar allowlist de origens exatas e nunca combinar '*' com "
                "credenciais."
            ),
            passos_repro=[f"GET {origem} com Origin: {canario}"],
        ))
    elif acao.strip() == canario:
        findings.append(_novo_finding(
            id="cors-origem-refletida",
            categoria="cors",
            titulo="CORS reflete a Origin",
            severidade=Severidade.ALTA,
            descricao=(
                "O servidor reflete a Origin recebida em "
                "Access-Control-Allow-Origin, autorizando qualquer site a ler "
                "respostas (incluindo com credenciais, se habilitadas)."
            ),
            evidencia=evidencia,
            origem=origem,
            stride="Information disclosure",
            owasp="A05:2021",
            cwe="CWE-942",
            recomendacao=(
                "Validar a Origin contra allowlist explícita em vez de refleti-la."
            ),
            passos_repro=[f"GET {origem} com Origin: {canario}"],
        ))
    elif acao.strip() == "*":
        findings.append(_novo_finding(
            id="cors-wildcard",
            categoria="cors",
            titulo="CORS com wildcard",
            severidade=Severidade.BAIXA,
            descricao=(
                "Access-Control-Allow-Origin '*' permite leitura por qualquer "
                "origem (sem credenciais). Avaliar se os dados são públicos."
            ),
            evidencia=evidencia,
            origem=origem,
            stride="Information disclosure",
            owasp="A05:2021",
            cwe="CWE-942",
            recomendacao="Restringir a allowlist de origens quando possível.",
            passos_repro=[f"GET {origem}"],
        ))
    return findings


def _evidencia_sensivel(status: int, tipo: str, corpo: str) -> str:
    """Evidência REDIGIDA de path sensível: só tipo (+ nome de chave), nunca valor.

    Para categorias que costumam conter segredo (`env`/`wp-config`), expõe
    apenas o NOME da variável; o valor é sempre reportado como REDIGIDO.
    """
    if tipo in ("env", "wp-config"):
        m = re.search(r"(?im)^([A-Z0-9_]{2,})\s*=", corpo or "")
        chave = m.group(1) if m else "?"
        return f"status={status}; tipo={tipo}; chave={chave}; valor=REDIGIDO"
    return f"status={status}; tipo={tipo}; valor=REDIGIDO"


def _extrair_componentes(texto_html: str) -> list[tuple[str, str]]:
    """Extrai pares (pacote npm, versão) de libs reconhecidas no HTML/JS.

    Fontes: `<script src>` (jquery/bootstrap/react/vue/angular) e
    `<meta name="generator">`. Só devolve quando há versão explícita.
    """
    achados: set[tuple[str, str]] = set()
    for src in _script_srcs(texto_html or ""):
        alvo = src.lower()
        for marcador, pacote in _LIBS_OSV:
            if marcador not in alvo:
                continue
            m = re.search(
                rf"{marcador}[@\-/_.]?v?(\d+\.\d+(?:\.\d+)?)", src, re.I,
            )
            if m:
                achados.add((pacote, m.group(1)))
    for meta in _GENERATOR_RE.findall(texto_html or ""):
        cm = _GENERATOR_CONTENT_RE.search(meta)
        if not cm:
            continue
        gm = re.search(r"([A-Za-z][\w.-]*)\s+(\d+\.\d+(?:\.\d+)?)", cm.group(1))
        if gm:
            achados.add((gm.group(1).lower(), gm.group(2)))
    return sorted(achados)


def _severidade_osv(vulns: list) -> Severidade:
    """Severidade do achado OSV: ALTA se houver HIGH/CRITICAL; senão MEDIA."""
    for v in vulns:
        if not isinstance(v, dict):
            continue
        ds = v.get("database_specific") or {}
        sev = str(ds.get("severity", "")).lower()
        if sev in ("high", "critical"):
            return Severidade.ALTA
    return Severidade.MEDIA


# ---------------------------------------------------------------------------
# CHECK A — headers de segurança (qualidade, não só presença)
# ---------------------------------------------------------------------------

def audit_cabecalhos(
    url: str,
    resposta: Resposta | None = None,
    allow_loopback: bool = False,
) -> list[Finding]:
    """Avalia headers de segurança da resposta (A05:2021 / CWE).

    HSTS (ausência, max-age curto, sem includeSubDomains), CSP (ausente ou com
    unsafe-inline/unsafe-eval/wildcard), X-Frame-Options, X-Content-Type-Options,
    Referrer-Policy, Permissions-Policy e vazamento de banner. Evidência é o
    valor BRUTO do header (redigido) ou "ausente"; nunca expõe segredo.
    """
    findings: list[Finding] = []
    resp = resposta if resposta is not None else _buscar(url, allow_loopback)
    if not resp.ok:
        return findings
    h = {str(k).lower(): str(v) for k, v in (resp.headers or {}).items()}
    origem = resp.final_url or url
    # B11: HSTS só faz sentido em alvo HTTPS — em HTTP a ausência não é falha.
    alvo_https = urllib.parse.urlparse(str(url)).scheme.lower() == "https"

    # --- HSTS -------------------------------------------------------------
    hsts = h.get("strict-transport-security")
    if not hsts and alvo_https:
        findings.append(_novo_finding(
            id="header-hsts-ausente",
            categoria="headers",
            titulo="HSTS ausente",
            severidade=Severidade.MEDIA,
            descricao=(
                "O header Strict-Transport-Security não foi enviado. Sem HSTS, "
                "o navegador pode ser rebaixado de HTTPS para HTTP por um "
                "atacante em rede (SSL stripping)."
            ),
            evidencia=f"ausente; origem={origem}",
            origem=origem,
            stride="Tampering",
            owasp="A05:2021",
            cwe="CWE-319",
            recomendacao=(
                "Enviar Strict-Transport-Security com max-age de pelo menos "
                "180 dias e includeSubDomains (sem correção automática)."
            ),
            passos_repro=[f"GET {origem}", "Inspecionar o header HSTS"],
        ))
    elif hsts:
        ev_hsts = redigir_header("Strict-Transport-Security", hsts)
        m = re.search(r"max-age\s*=\s*(\d+)", hsts, re.I)
        max_age = int(m.group(1)) if m else 0
        if max_age < _HSTS_MAX_AGE_MIN:
            findings.append(_novo_finding(
                id="header-hsts-fraco",
                categoria="headers",
                titulo="HSTS com max-age baixo",
                severidade=Severidade.BAIXA,
                descricao=(
                    f"Strict-Transport-Security com max-age={max_age} (< 180 "
                    "dias). Política curta reduz a proteção contra downgrade."
                ),
                evidencia=f"{ev_hsts}; origem={origem}",
                origem=origem,
                stride="Tampering",
                owasp="A05:2021",
                cwe="CWE-319",
                recomendacao="Elevar max-age para >= 15552000 (180 dias).",
                passos_repro=[f"GET {origem}", "Ler max-age do HSTS"],
            ))
        if "includesubdomains" not in hsts.replace(" ", "").lower():
            findings.append(_novo_finding(
                id="header-hsts-sem-subdominios",
                categoria="headers",
                titulo="HSTS sem includeSubDomains",
                severidade=Severidade.BAIXA,
                descricao=(
                    "HSTS sem 'includeSubDomains': subdomínios permanecem "
                    "vulneráveis a downgrade/SSL stripping."
                ),
                evidencia=f"{ev_hsts}; origem={origem}",
                origem=origem,
                stride="Tampering",
                owasp="A05:2021",
                cwe="CWE-319",
                recomendacao="Adicionar includeSubDomains ao HSTS.",
                passos_repro=[f"GET {origem}", "Verificar includeSubDomains"],
            ))

    # --- CSP --------------------------------------------------------------
    csp = h.get("content-security-policy")
    if not csp:
        findings.append(_novo_finding(
            id="header-csp-ausente",
            categoria="headers",
            titulo="CSP ausente",
            severidade=Severidade.MEDIA,
            descricao=(
                "Não há Content-Security-Policy. A ausência dificulta a "
                "mitigação de XSS/injeção de conteúdo no navegador."
            ),
            evidencia=f"ausente; origem={origem}",
            origem=origem,
            stride="Tampering",
            owasp="A05:2021",
            cwe="CWE-693",
            recomendacao=(
                "Publicar CSP restritiva (default-src 'self'; sem "
                "'unsafe-inline'/'unsafe-eval' nem wildcard)."
            ),
            passos_repro=[f"GET {origem}", "Inspecionar Content-Security-Policy"],
        ))
    else:
        csp_l = csp.lower()
        ev_csp = redigir_header("Content-Security-Policy", csp)
        # B11: detecta 'unsafe-inline'/'unsafe-eval' com OU sem aspas.
        if re.search(r"(?<![\w-])'?unsafe-inline'?(?![\w-])", csp, re.I):
            findings.append(_novo_finding(
                id="header-csp-unsafe-inline",
                categoria="headers",
                titulo="CSP com 'unsafe-inline'",
                severidade=Severidade.ALTA,
                descricao=(
                    "CSP permite 'unsafe-inline', o que anula boa parte da "
                    "proteção contra XSS em scripts/estilos inline."
                ),
                evidencia=f"{ev_csp}; origem={origem}",
                origem=origem,
                stride="Tampering",
                owasp="A05:2021",
                cwe="CWE-693",
                recomendacao="Remover 'unsafe-inline' e usar nonces/hashes.",
                passos_repro=[f"GET {origem}", "Procurar 'unsafe-inline' na CSP"],
            ))
        if re.search(r"(?<![\w-])'?unsafe-eval'?(?![\w-])", csp, re.I):
            findings.append(_novo_finding(
                id="header-csp-unsafe-eval",
                categoria="headers",
                titulo="CSP com 'unsafe-eval'",
                severidade=Severidade.MEDIA,
                descricao=(
                    "CSP permite 'unsafe-eval', habilitando execução de código "
                    "por eval()/Function() e ampliando o impacto de XSS."
                ),
                evidencia=f"{ev_csp}; origem={origem}",
                origem=origem,
                stride="Tampering",
                owasp="A05:2021",
                cwe="CWE-693",
                recomendacao="Remover 'unsafe-eval' da CSP.",
                passos_repro=[f"GET {origem}", "Procurar 'unsafe-eval' na CSP"],
            ))
        # N4: '*' em `frame-ancestors` NAO conta como wildcard de ORIGEM de
        # recursos (tem finding proprio, MEDIA). Exclui essa diretiva da
        # checagem para nao gerar `header-csp-wildcard` (ALTA) indevidamente.
        csp_sem_frame = re.sub(
            r"(?i)frame-ancestors[^;]*(;|$)", r"\1", csp,
        )
        if re.search(r"(?:^|[\s;])\*(?:\s|;|$)", csp_sem_frame):
            findings.append(_novo_finding(
                id="header-csp-wildcard",
                categoria="headers",
                titulo="CSP com wildcard '*'",
                severidade=Severidade.ALTA,
                descricao=(
                    "CSP usa '*' como origem, permitindo carregar recursos de "
                    "qualquer domínio e enfraquecendo a política."
                ),
                evidencia=f"{ev_csp}; origem={origem}",
                origem=origem,
                stride="Tampering",
                owasp="A05:2021",
                cwe="CWE-693",
                recomendacao="Restringir a CSP a origens explícitas e confiáveis.",
                passos_repro=[f"GET {origem}", "Procurar wildcard na CSP"],
            ))
        # B8: `frame-ancestors *` libera embutir a página em QUALQUER origem
        # (clickjacking), mesmo com a diretiva presente.
        if re.search(r"frame-ancestors\s[^;]*\*", csp, re.I):
            findings.append(_novo_finding(
                id="header-csp-frame-ancestors-wildcard",
                categoria="headers",
                titulo="CSP com frame-ancestors wildcard",
                severidade=Severidade.MEDIA,
                descricao=(
                    "A diretiva frame-ancestors usa '*', permitindo que "
                    "qualquer site embuta a página em iframe (clickjacking), "
                    "anulando a proteção contra enquadramento."
                ),
                evidencia=f"{ev_csp}; origem={origem}",
                origem=origem,
                stride="Tampering",
                owasp="A05:2021",
                cwe="CWE-1021",
                recomendacao=(
                    "Trocar '*' por origens explícitas ou 'self' em "
                    "frame-ancestors."
                ),
                passos_repro=[
                    f"GET {origem}", "Procurar 'frame-ancestors *' na CSP",
                ],
            ))

    # --- X-Frame-Options / frame-ancestors --------------------------------
    xfo = h.get("x-frame-options")
    tem_frame_ancestors = bool(csp and "frame-ancestors" in csp.lower())
    if not xfo and not tem_frame_ancestors:
        findings.append(_novo_finding(
            id="header-xfo-ausente",
            categoria="headers",
            titulo="X-Frame-Options ausente",
            severidade=Severidade.MEDIA,
            descricao=(
                "Sem X-Frame-Options e sem 'frame-ancestors' na CSP, a página "
                "pode ser embutida em iframe de terceiros (clickjacking)."
            ),
            evidencia=f"ausente; origem={origem}",
            origem=origem,
            stride="Tampering",
            owasp="A05:2021",
            cwe="CWE-1021",
            recomendacao=(
                "Enviar X-Frame-Options: DENY/SAMEORIGIN ou CSP com "
                "frame-ancestors 'self'."
            ),
            passos_repro=[f"GET {origem}", "Inspecionar X-Frame-Options/CSP"],
        ))
    # B8: valor INVÁLIDO de X-Frame-Options (ex.: ALLOWALL) -> navegador
    # ignora a proteção (clickjacking). Válidos: DENY, SAMEORIGIN e
    # ALLOW-FROM <uri> (obsoleto, mas reconhecido).
    if xfo:
        xfo_norm = xfo.strip().lower()
        valido = (
            xfo_norm in ("deny", "sameorigin")
            or xfo_norm.startswith("allow-from ")
        )
        if not valido:
            findings.append(_novo_finding(
                id="header-xfo-invalido",
                categoria="headers",
                titulo="X-Frame-Options com valor inválido",
                severidade=Severidade.MEDIA,
                descricao=(
                    "O valor de X-Frame-Options não é reconhecido "
                    "(DENY/SAMEORIGIN/ALLOW-FROM), então a proteção contra "
                    "clickjacking é ignorada pelo navegador."
                ),
                evidencia=(
                    f"{redigir_header('X-Frame-Options', xfo)}; "
                    f"origem={origem}"
                ),
                origem=origem,
                stride="Tampering",
                owasp="A05:2021",
                cwe="CWE-1021",
                recomendacao="Usar X-Frame-Options: DENY ou SAMEORIGIN.",
                passos_repro=[
                    f"GET {origem}", "Comparar o valor com a especificação",
                ],
            ))

    # --- X-Content-Type-Options -------------------------------------------
    xcto = h.get("x-content-type-options")
    if not xcto or xcto.strip().lower() != "nosniff":
        findings.append(_novo_finding(
            id="header-xcto-ausente",
            categoria="headers",
            titulo="X-Content-Type-Options ausente/incorreto",
            severidade=Severidade.MEDIA,
            descricao=(
                "X-Content-Type-Options está ausente ou diferente de "
                "'nosniff', permitindo MIME sniffing e execução inesperada."
            ),
            evidencia=(
                f"{redigir_header('X-Content-Type-Options', xcto)}; "
                f"origem={origem}"
            ),
            origem=origem,
            stride="Tampering",
            owasp="A05:2021",
            cwe="CWE-16",
            recomendacao="Enviar X-Content-Type-Options: nosniff.",
            passos_repro=[f"GET {origem}", "Inspecionar X-Content-Type-Options"],
        ))

    # --- Referrer-Policy --------------------------------------------------
    rp = h.get("referrer-policy")
    if not rp:
        findings.append(_novo_finding(
            id="header-referrer-policy-ausente",
            categoria="headers",
            titulo="Referrer-Policy ausente",
            severidade=Severidade.BAIXA,
            descricao=(
                "Sem Referrer-Policy, URLs completas podem vazar no header "
                "Referer para terceiros."
            ),
            evidencia=f"ausente; origem={origem}",
            origem=origem,
            stride="Information disclosure",
            owasp="A05:2021",
            cwe="CWE-200",
            recomendacao="Enviar Referrer-Policy restritiva (ex.: no-referrer).",
            passos_repro=[f"GET {origem}", "Inspecionar Referrer-Policy"],
        ))
    else:
        tokens = {
            t.strip().lower() for t in rp.split(",") if t.strip()
        }
        if not tokens or not tokens.issubset(_REFERRER_POLICY_VALIDOS):
            findings.append(_novo_finding(
                id="header-referrer-policy-invalida",
                categoria="headers",
                titulo="Referrer-Policy com valor inválido",
                severidade=Severidade.BAIXA,
                descricao=(
                    "O valor de Referrer-Policy não corresponde a nenhuma "
                    "diretiva conhecida, então o navegador pode ignorá-lo."
                ),
                evidencia=(
                    f"{redigir_header('Referrer-Policy', rp)}; origem={origem}"
                ),
                origem=origem,
                stride="Information disclosure",
                owasp="A05:2021",
                cwe="CWE-200",
                recomendacao="Usar um valor válido (ex.: strict-origin-when-cross-origin).",
                passos_repro=[f"GET {origem}", "Comparar valor com a especificação"],
            ))

    # --- Permissions-Policy -----------------------------------------------
    if not h.get("permissions-policy"):
        findings.append(_novo_finding(
            id="header-permissions-policy-ausente",
            categoria="headers",
            titulo="Permissions-Policy ausente",
            severidade=Severidade.BAIXA,
            descricao=(
                "Sem Permissions-Policy, recursos sensíveis do navegador "
                "(câmera, microfone, geolocalização) não são restringidos."
            ),
            evidencia=f"ausente; origem={origem}",
            origem=origem,
            stride="Information disclosure",
            owasp="A05:2021",
            cwe="CWE-16",
            recomendacao="Enviar Permissions-Policy restringindo recursos não usados.",
            passos_repro=[f"GET {origem}", "Inspecionar Permissions-Policy"],
        ))

    # --- Banner leakage ---------------------------------------------------
    for nome in ("server", "x-powered-by", "x-aspnet-version", "x-generator"):
        valor = h.get(nome)
        if not valor:
            continue
        slug = nome.replace(".", "-")
        findings.append(_novo_finding(
            id=f"header-banner-{slug}",
            categoria="headers",
            titulo=f"Banner '{nome}' exposto",
            severidade=Severidade.BAIXA,
            descricao=(
                f"O header '{nome}' revela tecnologia/versão do servidor, "
                "facilitando a escolha de exploits específicos."
            ),
            evidencia=f"{redigir_header(nome, valor)}; origem={origem}",
            origem=origem,
            stride="Information disclosure",
            owasp="A05:2021",
            cwe="CWE-200",
            recomendacao=f"Remover ou minimizar o header '{nome}'.",
            passos_repro=[f"GET {origem}", f"Inspecionar o header {nome}"],
        ))

    return findings


# ---------------------------------------------------------------------------
# CHECK B — cookies (Secure/HttpOnly/SameSite e prefixos)
# ---------------------------------------------------------------------------

def _dividir_set_cookie(valor: str) -> list[str]:
    """Separa um header Set-Cookie combinado, preservando o `,` de Expires."""
    return [p for p in _COOKIE_SPLIT_RE.split(str(valor)) if p.strip()]


def _parse_cookie(cru: str) -> tuple[str, str, dict]:
    """Parsea um único Set-Cookie em (nome, valor, atributos em minúsculas)."""
    partes = [p.strip() for p in str(cru).split(";")]
    primeiro = partes[0] if partes else ""
    if "=" in primeiro:
        nome, valor = primeiro.split("=", 1)
    else:
        nome, valor = primeiro, ""
    attrs: dict[str, str] = {}
    for parte in partes[1:]:
        if "=" in parte:
            k, v = parte.split("=", 1)
            attrs[k.strip().lower()] = v.strip()
        elif parte:
            attrs[parte.strip().lower()] = ""
    return nome.strip(), valor.strip(), attrs


def audit_cookies(
    url: str,
    resposta: Resposta | None = None,
    allow_loopback: bool = False,
) -> list[Finding]:
    """Avalia flags de cookies em `Set-Cookie`.

    Sem Secure (CWE-614), sem HttpOnly (CWE-1004), sem SameSite (CWE-1275),
    SameSite=None sem Secure (ALTA) e violação dos prefixos `__Host-`/`__Secure-`.
    Evidência: nome + flags; o VALOR do cookie sai redigido com `mascarar`.
    """
    findings: list[Finding] = []
    resp = resposta if resposta is not None else _buscar(url, allow_loopback)
    if not resp.ok:
        return findings
    origem = resp.final_url or url

    for cru in (resp.set_cookie or []):
        for cookie in _dividir_set_cookie(cru):
            nome, valor, attrs = _parse_cookie(cookie)
            if not nome:
                continue
            nome_l = nome.lower()
            flags = [p.strip() for p in cookie.split(";")[1:]]
            # B11: valor de cookie é mascarado SEMPRE (mesmo <= 8 chars).
            ev_base = (
                f"cookie {nome}={mascarar_sempre(valor)}; "
                f"flags=[{'; '.join(flags) or 'nenhuma'}]; origem={origem}"
            )
            tem_secure = "secure" in attrs
            tem_httponly = "httponly" in attrs
            samesite = attrs.get("samesite")

            if not tem_secure:
                findings.append(_novo_finding(
                    id="cookie-sem-secure",
                    categoria="cookies",
                    titulo=f"Cookie '{nome}' sem Secure",
                    severidade=Severidade.MEDIA,
                    descricao=(
                        "Cookie sem a flag Secure pode ser transmitido em HTTP "
                        "e interceptado em rede."
                    ),
                    evidencia=ev_base,
                    origem=origem,
                    stride="Information disclosure",
                    owasp="A05:2021",
                    cwe="CWE-614",
                    recomendacao="Adicionar a flag Secure ao cookie.",
                    passos_repro=[f"GET {origem}", f"Inspecionar Set-Cookie {nome}"],
                ))
            if not tem_httponly:
                findings.append(_novo_finding(
                    id="cookie-sem-httponly",
                    categoria="cookies",
                    titulo=f"Cookie '{nome}' sem HttpOnly",
                    severidade=Severidade.MEDIA,
                    descricao=(
                        "Cookie sem HttpOnly pode ser lido por JavaScript, "
                        "ampliando o impacto de um XSS."
                    ),
                    evidencia=ev_base,
                    origem=origem,
                    stride="Information disclosure",
                    owasp="A05:2021",
                    cwe="CWE-1004",
                    recomendacao="Adicionar a flag HttpOnly ao cookie.",
                    passos_repro=[f"GET {origem}", f"Inspecionar Set-Cookie {nome}"],
                ))
            if not samesite:
                findings.append(_novo_finding(
                    id="cookie-sem-samesite",
                    categoria="cookies",
                    titulo=f"Cookie '{nome}' sem SameSite",
                    severidade=Severidade.BAIXA,
                    descricao=(
                        "Cookie sem SameSite pode ser enviado em requisições "
                        "cross-site, favorecendo CSRF."
                    ),
                    evidencia=ev_base,
                    origem=origem,
                    stride="Tampering",
                    owasp="A05:2021",
                    cwe="CWE-1275",
                    recomendacao="Definir SameSite=Lax ou Strict (None exige Secure).",
                    passos_repro=[f"GET {origem}", f"Inspecionar Set-Cookie {nome}"],
                ))
            if samesite and samesite.strip().lower() == "none" and not tem_secure:
                findings.append(_novo_finding(
                    id="cookie-samesite-none-sem-secure",
                    categoria="cookies",
                    titulo=f"Cookie '{nome}' SameSite=None sem Secure",
                    severidade=Severidade.ALTA,
                    descricao=(
                        "SameSite=None sem Secure: o navegador moderno rejeita o "
                        "cookie e, quando aceito, ele trafega sem proteção."
                    ),
                    evidencia=ev_base,
                    origem=origem,
                    stride="Tampering",
                    owasp="A05:2021",
                    cwe="CWE-1275",
                    recomendacao="Adicionar Secure ao cookie com SameSite=None.",
                    passos_repro=[f"GET {origem}", f"Inspecionar Set-Cookie {nome}"],
                ))

            # Prefixos com requisitos de segurança próprios.
            if nome_l.startswith("__host-"):
                if not tem_secure or attrs.get("path") != "/" or "domain" in attrs:
                    findings.append(_novo_finding(
                        id="cookie-prefixo-host-invalido",
                        categoria="cookies",
                        titulo=f"Cookie '{nome}' viola o prefixo __Host-",
                        severidade=Severidade.MEDIA,
                        descricao=(
                            "__Host- exige Secure, Path=/ e ausência de Domain; "
                            "o cookie atual não cumpre todos os requisitos."
                        ),
                        evidencia=ev_base,
                        origem=origem,
                        stride="Tampering",
                        owasp="A05:2021",
                        cwe="CWE-614",
                        recomendacao=(
                            "Ajustar o cookie para Secure; Path=/; sem Domain."
                        ),
                        passos_repro=[f"GET {origem}", f"Inspecionar Set-Cookie {nome}"],
                    ))
            elif nome_l.startswith("__secure-") and not tem_secure:
                findings.append(_novo_finding(
                    id="cookie-prefixo-secure-invalido",
                    categoria="cookies",
                    titulo=f"Cookie '{nome}' viola o prefixo __Secure-",
                    severidade=Severidade.MEDIA,
                    descricao=(
                        "__Secure- exige a flag Secure; o cookie atual não a "
                        "possui."
                    ),
                    evidencia=ev_base,
                    origem=origem,
                    stride="Tampering",
                    owasp="A05:2021",
                    cwe="CWE-614",
                    recomendacao="Adicionar a flag Secure ao cookie __Secure-.",
                    passos_repro=[f"GET {origem}", f"Inspecionar Set-Cookie {nome}"],
                ))

    return findings


# ---------------------------------------------------------------------------
# CHECK C — TLS (certificado, versão, cipher) — helpers puros testáveis
# ---------------------------------------------------------------------------

def _dias_para_expirar(
    not_after: str, agora: datetime | None = None
) -> float | None:
    """Dias até `notAfter` (formato OpenSSL); None se a data for inválida."""
    if not not_after:
        return None
    try:
        data = datetime.strptime(str(not_after), "%b %d %H:%M:%S %Y %Z")
    except (ValueError, TypeError):
        return None
    data = data.replace(tzinfo=timezone.utc)
    ref = agora or datetime.now(timezone.utc)
    return (data - ref).total_seconds() / 86400.0


def _cipher_fraco(nome: str) -> bool:
    """True se o nome do cipher contém marcador obsoleto/fraco."""
    n = str(nome or "").upper()
    return any(t in n for t in ("RC4", "3DES", "DES", "NULL", "EXPORT", "MD5"))


def _nome_cert(campo) -> str:
    """Formata o campo subject/issuer do certificado em texto legível."""
    try:
        partes = []
        for rdn in campo or []:
            for chave, valor in rdn:
                partes.append(f"{chave}={valor}")
        return ", ".join(partes)
    except (TypeError, ValueError):
        return ""


def _handshake_versao(host: str, porta: int, versao) -> bool:
    """Tenta handshake TLS forçando `versao`; True se o servidor ACEITAR.

    Nunca levanta (qualquer falha de rede/TLS -> False).
    """
    try:
        ctx = ssl.SSLContext(ssl.PROTOCOL_TLS_CLIENT)
        ctx.check_hostname = False
        ctx.verify_mode = ssl.CERT_NONE
        with warnings.catch_warnings():
            # TLSv1/TLSv1.1 são deprecados no Python, mas precisamos testá-los.
            warnings.simplefilter("ignore", DeprecationWarning)
            ctx.minimum_version = versao
            ctx.maximum_version = versao
        with socket.create_connection((host, int(porta)), timeout=TIMEOUT) as sock:
            with ctx.wrap_socket(sock, server_hostname=host):
                return True
    except Exception:  # noqa: BLE001 — versão obsoleta nunca derruba o check
        return False


def audit_tls(
    host: str,
    porta: int = 443,
    allow_loopback: bool = False,
) -> list[Finding]:
    """Avalia TLS: certificado (expiração), versão e cipher negociados.

    Handshake com `ssl.create_default_context()` (CERT_REQUIRED +
    check_hostname). Falha -> ALTA (CWE-295). Certificado expirado -> ALTA;
    expirando em <= 30 dias -> MEDIA. TLSv1/TLSv1.1 aceitos -> ALTA; cipher
    fraco -> ALTA (CWE-327). Alvo interno sem allowlist é bloqueado; nunca
    levanta.
    """
    findings: list[Finding] = []
    if not host:
        return findings
    alvo = f"https://{host}:{int(porta)}"
    if not _alvo_valido(alvo, allow_loopback=allow_loopback):
        return findings
    origem = alvo

    cert: dict = {}
    versao = ""
    cipher: tuple = ("", "", 0)
    try:
        ctx = ssl.create_default_context()
        with socket.create_connection(
            (host, int(porta)), timeout=TIMEOUT
        ) as sock:
            with ctx.wrap_socket(sock, server_hostname=host) as ssock:
                cert = ssock.getpeercert() or {}
                versao = ssock.version() or ""
                cipher = ssock.cipher() or ("", "", 0)
    except (ssl.SSLCertVerificationError, ssl.CertificateError) as exc:
        findings.append(_novo_finding(
            id="tls-certificado-invalido",
            categoria="tls",
            titulo="Certificado TLS inválido",
            severidade=Severidade.ALTA,
            descricao=(
                "O handshake TLS falhou na validação do certificado "
                "(expirado, hostname divergente ou CA não confiável)."
            ),
            evidencia=f"{type(exc).__name__}: {exc}",
            origem=origem,
            stride="Spoofing",
            owasp="A02:2021",
            cwe="CWE-295",
            recomendacao="Instalar cadeia válida e renovar o certificado.",
            passos_repro=[f"openssl s_client -connect {host}:{int(porta)}"],
        ))
        return findings
    except (ssl.SSLError, OSError, ValueError, socket.timeout) as exc:
        findings.append(_novo_finding(
            id="tls-handshake-falhou",
            categoria="tls",
            titulo="Handshake TLS falhou",
            severidade=Severidade.ALTA,
            descricao=(
                "Não foi possível estabelecer TLS verificado com o host "
                "(serviço indisponível, protocolo incompatível ou erro de rede)."
            ),
            evidencia=f"{type(exc).__name__}: {exc}",
            origem=origem,
            stride="Spoofing",
            owasp="A02:2021",
            cwe="CWE-295",
            recomendacao="Verificar disponibilidade e configuração TLS do serviço.",
            passos_repro=[f"openssl s_client -connect {host}:{int(porta)}"],
        ))
        return findings

    ev_cert = (
        f"subject={_nome_cert(cert.get('subject'))}; "
        f"issuer={_nome_cert(cert.get('issuer'))}; "
        f"notBefore={cert.get('notBefore')}; notAfter={cert.get('notAfter')}; "
        f"versao={versao}; cipher={cipher[0]}; origem={origem}"
    )
    dias = _dias_para_expirar(cert.get("notAfter", ""))
    if dias is not None:
        if dias < 0:
            findings.append(_novo_finding(
                id="tls-certificado-expirado",
                categoria="tls",
                titulo="Certificado TLS expirado",
                severidade=Severidade.ALTA,
                descricao=(
                    "O certificado do servidor está expirado, quebrando a "
                    "confiança e a validação no cliente."
                ),
                evidencia=ev_cert,
                origem=origem,
                stride="Spoofing",
                owasp="A02:2021",
                cwe="CWE-295",
                recomendacao="Renovar o certificado imediatamente.",
                passos_repro=[f"openssl s_client -connect {host}:{int(porta)}"],
            ))
        elif dias <= 30:
            findings.append(_novo_finding(
                id="tls-certificado-expirando",
                categoria="tls",
                titulo="Certificado TLS expira em breve",
                severidade=Severidade.MEDIA,
                descricao=(
                    f"O certificado expira em {dias:.1f} dia(s) (<= 30). "
                    "Renove antes do vencimento para evitar indisponibilidade."
                ),
                evidencia=ev_cert,
                origem=origem,
                stride="Denial of service",
                owasp="A02:2021",
                cwe="CWE-295",
                recomendacao="Agendar a renovação do certificado.",
                passos_repro=[f"openssl s_client -connect {host}:{int(porta)}"],
            ))

    # Versões obsoletas: TLSv1 / TLSv1.1 aceitos -> ALTA.
    for versao_enum, rotulo in (
        (ssl.TLSVersion.TLSv1, "TLSv1"),
        (ssl.TLSVersion.TLSv1_1, "TLSv1.1"),
    ):
        if _handshake_versao(host, porta, versao_enum):
            findings.append(_novo_finding(
                id=f"tls-versao-obsoleta-{rotulo.lower().replace('.', '-')}",
                categoria="tls",
                titulo=f"Versão obsoleta {rotulo} aceita",
                severidade=Severidade.ALTA,
                descricao=(
                    f"O servidor aceitou handshake em {rotulo}, versão obsoleta "
                    "e vulnerável a ataques conhecidos."
                ),
                evidencia=f"{rotulo} aceito; origem={origem}",
                origem=origem,
                stride="Tampering",
                owasp="A02:2021",
                cwe="CWE-327",
                recomendacao="Desabilitar TLSv1/TLSv1.1 e exigir TLSv1.2+.",
                passos_repro=[
                    f"openssl s_client -connect {host}:{int(porta)} -{rotulo}",
                ],
            ))

    if cipher and _cipher_fraco(cipher[0]):
        findings.append(_novo_finding(
            id="tls-cipher-fraco",
            categoria="tls",
            titulo="Cipher fraco negociado",
            severidade=Severidade.ALTA,
            descricao=(
                f"O cipher negociado '{cipher[0]}' usa primitiva obsoleta/frágil "
                "(RC4/3DES/DES/NULL/EXPORT/MD5)."
            ),
            evidencia=ev_cert,
            origem=origem,
            stride="Tampering",
            owasp="A02:2021",
            cwe="CWE-327",
            recomendacao="Restringir a suíte a ciphers modernos (AEAD).",
            passos_repro=[f"openssl s_client -connect {host}:{int(porta)}"],
        ))

    return findings


# ---------------------------------------------------------------------------
# CHECK D — cadeia de redirects
# ---------------------------------------------------------------------------

def _dominio_base(host: str) -> str:
    """Domínio normalizado para comparar cross-domain (B11): trata `www.` e o
    apex como o MESMO domínio (ex.: `www.a.com` == `a.com`)."""
    h = (host or "").lower().rstrip(".")
    return h[4:] if h.startswith("www.") else h


def _avaliar_cadeia_redirects(
    inicial: str,
    registros: list[tuple[int, str]],
    final_url: str,
    status_final: int | None,
) -> list[Finding]:
    """Avalia a cadeia reconstruída: downgrade, cross-domain e loop.

    `registros` é a lista `(status, novo_url)` por hop. Cadeia limpa (sem
    problemas) -> nenhum Finding. Evidência: sequência de URLs/status.
    """
    registros = list(registros or [])
    if not registros and (final_url or inicial) == inicial:
        return []

    cadeia = [inicial] + [novo for _, novo in registros]
    if final_url and final_url not in cadeia:
        cadeia.append(final_url)

    partes: list[str] = []
    for i, u in enumerate(cadeia):
        status = registros[i][0] if i < len(registros) else status_final
        partes.append(f"{u} [{status if status is not None else '?'}]")
    evidencia = " -> ".join(partes)

    findings: list[Finding] = []
    origem = inicial

    # Downgrade https -> http.
    if urllib.parse.urlparse(inicial).scheme == "https":
        for _, novo in registros:
            if urllib.parse.urlparse(novo).scheme == "http":
                findings.append(_novo_finding(
                    id="redirect-downgrade",
                    categoria="redirects",
                    titulo="Redirect de HTTPS para HTTP",
                    severidade=Severidade.ALTA,
                    descricao=(
                        "A cadeia rebaixa a conexão de HTTPS para HTTP, "
                        "permitindo interceptação e modificação do tráfego."
                    ),
                    evidencia=evidencia,
                    origem=origem,
                    stride="Tampering",
                    owasp="A02:2021",
                    cwe="CWE-319",
                    recomendacao="Remover redirecionamentos para HTTP e usar HSTS.",
                    passos_repro=[f"GET {inicial}", "Seguir a cadeia de redirects"],
                ))
                break

    # Cross-domain (host muda no meio da cadeia). B11: `www.` e apex do MESMO
    # domínio NÃO contam como cross-domain (ex.: www.a.com -> a.com).
    hosts = [urllib.parse.urlparse(u).hostname for u in cadeia]
    for a, b in zip(hosts, hosts[1:]):
        if a and b and _dominio_base(a) != _dominio_base(b):
            findings.append(_novo_finding(
                id="redirect-cross-domain",
                categoria="redirects",
                titulo="Redirect cross-domain",
                severidade=Severidade.BAIXA,
                descricao=(
                    "A cadeia envia o usuário para outro domínio, o que pode "
                    "vazar informações (Referer) ou viabilizar open redirect."
                ),
                evidencia=evidencia,
                origem=origem,
                stride="Information disclosure",
                owasp="A01:2021",
                cwe="CWE-601",
                recomendacao="Redirecionar apenas para domínios confiáveis e explícitos.",
                passos_repro=[f"GET {inicial}", "Observar a mudança de host"],
            ))
            break

    # Loop: mesmo URL aparece mais de 2 vezes na cadeia.
    contagem: dict[str, int] = {}
    for u in cadeia:
        contagem[u] = contagem.get(u, 0) + 1
    if any(v > 2 for v in contagem.values()):
        findings.append(_novo_finding(
            id="redirect-loop",
            categoria="redirects",
            titulo="Loop de redirects",
            severidade=Severidade.MEDIA,
            descricao=(
                "A cadeia repete o mesmo URL mais de duas vezes, indicando "
                "loop de redirecionamento que consome recursos do cliente."
            ),
            evidencia=evidencia,
            origem=origem,
            stride="Denial of service",
            owasp="A05:2021",
            cwe="CWE-835",
            recomendacao="Corrigir a regra de redirect que cria o loop.",
            passos_repro=[f"GET {inicial}", "Contar repetições de URL na cadeia"],
        ))

    return findings


def audit_redirects(url: str, allow_loopback: bool = False) -> list[Finding]:
    """Rastreia a cadeia de redirects (via `registrar_redirects`) e avalia.

    Downgrade https->http (ALTA), cross-domain (BAIXA) e loop (MEDIA). Cadeia
    limpa -> nenhum Finding. Nunca levanta.
    """
    registros: list[tuple[int, str]] = []
    try:
        _pausa()
        resp = requisicao(
            url, allow_loopback=allow_loopback, registrar_redirects=registros,
        )
    except Exception:  # noqa: BLE001 — redirects nunca derrubam o check
        return []
    return _avaliar_cadeia_redirects(
        url, registros, resp.final_url or url, resp.status,
    )


# ---------------------------------------------------------------------------
# CHECK E — erros/stack traces/caminhos expostos no corpo
# ---------------------------------------------------------------------------

# Assinaturas de erro/caminho exposto no corpo: (id, regex, severidade, CWE,
# título, descrição). Stack trace/SQL -> ALTA; caminho/aviso -> MEDIA;
# "Internal Server Error" isolado -> BAIXA (indício).
_ASSINATURAS_EXPOSTAS = (
    (
        "erro-traceback-python",
        re.compile(r"traceback \(most recent call last\)", re.I),
        Severidade.ALTA,
        "CWE-209",
        "Stack trace Python exposto",
        "O corpo da resposta contém um traceback Python, revelando caminhos, "
        "versões e a estrutura interna da aplicação.",
    ),
    (
        "erro-php-fatal",
        re.compile(r"\bfatal error\b", re.I),
        Severidade.ALTA,
        "CWE-209",
        "Erro fatal PHP exposto",
        "O corpo da resposta contém 'Fatal error' do PHP, revelando detalhes "
        "internos de execução.",
    ),
    (
        "erro-sqlstate",
        re.compile(r"\bSQLSTATE\b", re.I),
        Severidade.ALTA,
        "CWE-209",
        "Erro de banco (SQLSTATE) exposto",
        "O corpo da resposta contém um código SQLSTATE, indicando erro de "
        "banco de dados exposto ao usuário.",
    ),
    (
        "erro-mysql-syntax",
        re.compile(r"\bmysql\b[^\n]{0,40}\bsyntax\b", re.I),
        Severidade.ALTA,
        "CWE-209",
        "Erro de sintaxe MySQL exposto",
        "O corpo da resposta contém mensagem de sintaxe MySQL, expondo "
        "detalhes de consultas/estrutura do banco.",
    ),
    (
        "erro-stack-java-dotnet",
        re.compile(r"\bat (?:system|com|org\.spring)\b", re.I),
        Severidade.ALTA,
        "CWE-209",
        "Stack trace Java/.NET exposto",
        "O corpo da resposta contém linhas de stack trace Java/.NET "
        "('at System.'/'at com.'/'at org.spring').",
    ),
    (
        "erro-php-warning",
        re.compile(r"\b(?:warning|notice)\s*:", re.I),
        Severidade.MEDIA,
        "CWE-209",
        "Aviso PHP exposto",
        "O corpo da resposta contém 'Warning:'/'Notice:' do PHP, revelando "
        "detalhes internos.",
    ),
    (
        "erro-caminho-windows",
        # D1: exige drive + separador + caractere de caminho REAL. Sem o 3o
        # elemento, `d:\"` do payload RSC do Next.js casaria (falso positivo);
        # caminhos reais como `C:\Users\` e `D:\inetpub\wwwroot` seguem casando.
        re.compile(r"[A-Za-z]:\\[A-Za-z0-9_.\\ -]"),
        Severidade.MEDIA,
        "CWE-209",
        "Caminho absoluto Windows exposto",
        "O corpo da resposta contém um caminho absoluto Windows (ex.: C:\\), "
        "revelando a estrutura de diretórios do servidor.",
    ),
    (
        "erro-caminho-unix",
        re.compile(r"/(?:var/www|home)/"),
        Severidade.MEDIA,
        "CWE-209",
        "Caminho absoluto Unix exposto",
        "O corpo da resposta contém um caminho absoluto Unix (ex.: /var/www/, "
        "/home/), revelando a estrutura de diretórios do servidor.",
    ),
    (
        "erro-500",
        re.compile(r"internal server error", re.I),
        Severidade.BAIXA,
        "CWE-209",
        "Erro interno do servidor exposto",
        "O corpo da resposta contém 'Internal Server Error' (indício de erro "
        "interno não tratado).",
    ),
)


def audit_erros_expostos(
    url: str,
    resposta: Resposta | None = None,
    allow_loopback: bool = False,
) -> list[Finding]:
    """Procura assinaturas de erro/stack trace/caminho no corpo.

    Stack trace/SQL -> ALTA (CWE-209); caminho/aviso -> MEDIA; 'Internal
    Server Error' isolado -> BAIXA. Evidência: snippet de ~120 chars.
    """
    findings: list[Finding] = []
    resp = resposta if resposta is not None else _buscar(url, allow_loopback)
    if not resp.ok or not resp.body:
        return findings
    corpo = resp.body
    origem = resp.final_url or url
    vistos: set[str] = set()
    for fid, rx, severidade, cwe, titulo, descricao in _ASSINATURAS_EXPOSTAS:
        if fid in vistos:
            continue
        m = rx.search(corpo)
        if not m:
            continue
        vistos.add(fid)
        findings.append(_novo_finding(
            id=fid,
            categoria="erros",
            titulo=titulo,
            severidade=severidade,
            descricao=descricao,
            evidencia=f"{_snippet(corpo, m.start())}; origem={origem}",
            origem=origem,
            stride="Information disclosure",
            owasp="A05:2021",
            cwe=cwe,
            recomendacao=(
                "Tratar erros no servidor e responder mensagens genéricas, sem "
                "stack trace, SQL ou caminhos internos."
            ),
            passos_repro=[f"GET {origem}", "Inspecionar o corpo da resposta"],
        ))
    return findings


# ---------------------------------------------------------------------------
# CHECK F — arquivos bem-conhecidos (robots/sitemap/security.txt)
# ---------------------------------------------------------------------------

def audit_well_known(
    url: str, allow_loopback: bool = False
) -> tuple[list[Finding], dict]:
    """Consulta /robots.txt, /sitemap.xml e /.well-known/security.txt.

    Ausência de security.txt -> INFO; robots.txt com `Disallow` sensível ->
    MEDIA; directory listing ("Index of /") -> MEDIA. Tolerante a 404. Retorna
    `(findings, dados)` com os arquivos encontráveis para o relatório.
    """
    findings: list[Finding] = []
    par = urllib.parse.urlparse(url)
    base = f"{par.scheme}://{par.netloc}" if par.netloc else url
    dados: dict = {}
    corpos: dict[str, tuple[str, str]] = {}

    for caminho in _WELL_KNOWN_ARQUIVOS:
        rota = base + caminho
        _pausa()
        try:
            r = requisicao(rota, allow_loopback=allow_loopback, retries=0)
        except Exception:  # noqa: BLE001 — well-known nunca derruba o check
            r = Resposta(erro="falha de rede", final_url=rota)
        encontrado = bool(
            r.ok and r.status and 200 <= r.status < 400 and (r.body or "")
        )
        dados[caminho] = {
            "url": rota,
            "status": r.status,
            "encontrado": encontrado,
            "tamanho": len(r.body or ""),
        }
        if encontrado:
            corpos[caminho] = (r.final_url or rota, r.body)

    # security.txt ausente -> INFO (superfície de contato ausente).
    if not dados.get("/.well-known/security.txt", {}).get("encontrado"):
        origem = dados.get("/.well-known/security.txt", {}).get("url", base)
        findings.append(_novo_finding(
            id="well-known-security-txt-ausente",
            categoria="well-known",
            titulo="security.txt ausente",
            severidade=Severidade.INFO,
            descricao=(
                "Não há /.well-known/security.txt, então não há canal publicado "
                "para reporte responsável de vulnerabilidades."
            ),
            evidencia=f"ausente; origem={origem}",
            origem=origem,
            cwe="CWE-1059",
            recomendacao=(
                "Publicar /.well-known/security.txt (RFC 9116) com contato de "
                "segurança e validade."
            ),
            passos_repro=[f"GET {origem}"],
        ))

    # robots.txt com Disallow sensível -> revela superfície.
    if "/robots.txt" in corpos:
        origem_r, corpo_r = corpos["/robots.txt"]
        disallows = re.findall(r"(?im)^\s*disallow\s*:\s*(\S*)", corpo_r)
        sensiveis = sorted({
            d for d in disallows
            if any(d.lower().startswith(s) for s in _ROBOTS_SENSIVEIS)
        })
        if sensiveis:
            findings.append(_novo_finding(
                id="well-known-robots-sensivel",
                categoria="well-known",
                titulo="robots.txt revela caminhos sensíveis",
                severidade=Severidade.MEDIA,
                descricao=(
                    "O robots.txt lista caminhos sensíveis em Disallow, "
                    "revelando a superfície administrativa/interna a atacantes."
                ),
                evidencia=(
                    f"Disallow: {', '.join(sensiveis)}; origem={origem_r}"
                ),
                origem=origem_r,
                stride="Information disclosure",
                owasp="A05:2021",
                cwe="CWE-200",
                recomendacao=(
                    "Não listar caminhos sensíveis no robots.txt; proteger por "
                    "autenticação/controle de acesso."
                ),
                passos_repro=[f"GET {origem_r}", "Ler diretivas Disallow"],
            ))

    # Directory listing em qualquer arquivo encontrado.
    for caminho, (origem_l, corpo_l) in corpos.items():
        if "index of /" in corpo_l.lower():
            findings.append(_novo_finding(
                id="well-known-directory-listing",
                categoria="well-known",
                titulo="Directory listing habilitado",
                severidade=Severidade.MEDIA,
                descricao=(
                    "A resposta expõe um índice de diretório ('Index of /'), "
                    "listando arquivos que deveriam ser privados."
                ),
                evidencia=f"{_snippet(corpo_l, corpo_l.lower().find('index of /'))}; origem={origem_l}",
                origem=origem_l,
                stride="Information disclosure",
                owasp="A05:2021",
                cwe="CWE-548",
                recomendacao="Desabilitar listagem de diretórios no servidor.",
                passos_repro=[f"GET {origem_l}"],
            ))
            break

    return findings, dados


# ---------------------------------------------------------------------------
# CHECK G — CORS passivo (somente leitura da resposta)
# ---------------------------------------------------------------------------

def audit_cors_passivo(
    url: str,
    resposta: Resposta | None = None,
    allow_loopback: bool = False,
) -> list[Finding]:
    """Detecta CORS permissivo já presente na resposta.

    `Access-Control-Allow-Origin: *` com `Allow-Credentials: true` -> ALTA; se o
    servidor REFLETE a Origin canário enviada -> ALTA (CWE-942). Uma sondagem
    ativa com Origin arbitrário é WP5; aqui a leitura é passiva (a resposta
    pode ser fornecida) e só o canário é enviado quando é preciso buscar.
    """
    if resposta is None:
        try:
            _pausa()
            resposta = requisicao(
                url,
                allow_loopback=allow_loopback,
                headers_extra={"Origin": _CORS_ORIGIN_CANARIO},
            )
        except Exception:  # noqa: BLE001 — CORS nunca derruba o check
            return []
    return _avaliar_cors(resposta, url, _CORS_ORIGIN_CANARIO)


# ---------------------------------------------------------------------------
# CHECK H — conteúdo misto (HTTP dentro de página HTTPS)
# ---------------------------------------------------------------------------

def _urls_mistas(corpo: str) -> list[str]:
    """URLs http:// referenciadas em src/href/action/data (únicas, ordenadas)."""
    return sorted(set(_MIXED_CONTENT_RE.findall(corpo or "")))


def audit_mixed_content(
    url: str,
    resposta: Resposta | None = None,
    allow_loopback: bool = False,
) -> list[Finding]:
    """Detecta recursos http:// referenciados em página HTTPS (CWE-311)."""
    if urllib.parse.urlparse(url).scheme != "https":
        return []
    resp = resposta if resposta is not None else _buscar(url, allow_loopback)
    if not resp.ok or not resp.body:
        return []
    urls = _urls_mistas(resp.body)
    if not urls:
        return []
    origem = resp.final_url or url
    amostra = urls[:10]
    return [_novo_finding(
        id="mixed-content-http",
        categoria="mixed-content",
        titulo="Conteúdo misto (recursos HTTP em página HTTPS)",
        severidade=Severidade.MEDIA,
        descricao=(
            "A página é servida via HTTPS mas referencia recursos via HTTP, "
            "permitindo injeção/modificação por um atacante em rede."
        ),
        evidencia=f"{'; '.join(amostra)}; origem={origem}",
        origem=origem,
        stride="Tampering",
        owasp="A02:2021",
        cwe="CWE-311",
        recomendacao="Servir todos os recursos via HTTPS e aplicar upgrade-insecure-requests.",
        passos_repro=[f"GET {origem}", "Buscar referências http:// no HTML"],
    )]


# ---------------------------------------------------------------------------
# AGREGADOR — audit_superficie (tolerante; uma falha não derruba as demais)
# ---------------------------------------------------------------------------

def audit_superficie(
    url: str,
    perfil: str = "superficial",
    porta: int | None = None,
    allow_loopback: bool = False,
) -> list[Finding]:
    """Executa A-H de forma tolerante e devolve a lista consolidada.

    Busca a resposta base uma única vez (compartilhada por A/B/E/H); CORS e
    redirects fazem suas próprias requisições canário; well-known consulta os
    três arquivos; TLS usa socket. Cada check roda em try/except para que uma
    falha não derrube os demais. Respeita Budget/_pausa via transporte.

    A1: a porta do TLS NUNCA sai da lista de portas do perfil. Quando `porta`
    é None, deriva de `_porta_tls_padrao(url)` (https->443, http->80). Em alvo
    `http://` sem `--porta` o TLS é PULADO (sondar TLS em HTTP geraria falso
    positivo de handshake). Com `porta` explícita, o TLS roda na porta dada.
    """
    findings: list[Finding] = []
    par = urllib.parse.urlparse(url)
    porta_explicita = porta is not None
    if porta is None:
        porta = _porta_tls_padrao(url)
    porta_tls = int(porta)
    # TLS só em HTTPS (ou quando o operador deu uma porta explícita).
    usar_tls = par.scheme == "https" or porta_explicita

    resp = _buscar(url, allow_loopback)

    etapas = (
        ("cabecalhos", lambda: audit_cabecalhos(url, resp, allow_loopback)),
        ("cookies", lambda: audit_cookies(url, resp, allow_loopback)),
        ("erros_expostos", lambda: audit_erros_expostos(url, resp, allow_loopback)),
        ("mixed_content", lambda: audit_mixed_content(url, resp, allow_loopback)),
        ("cors_passivo", lambda: audit_cors_passivo(url, None, allow_loopback)),
        ("redirects", lambda: audit_redirects(url, allow_loopback)),
        ("well_known", lambda: audit_well_known(url, allow_loopback)[0]),
    )
    if usar_tls:
        etapas = etapas + (
            ("tls",
             lambda: audit_tls(par.hostname or "", porta_tls, allow_loopback)),
        )
    for _, etapa in etapas:
        try:
            findings.extend(etapa())
        except Exception:  # noqa: BLE001 — um check nunca derruba o agregador
            continue
    return findings


# ===========================================================================
# WP5 — TESTES ATIVOS READ-ONLY (GATED, BOUNDED, ANTI-EXTRAÇÃO)
# ===========================================================================
#
# Perfil `completo` (GATED por `perfil_autorizado`): sondas benignas de
# métodos/CORS/reflexão/open redirect/GraphQL/OpenAPI/paths/portas/segredos JS/
# componentes. Tudo READ-ONLY: nenhuma função envia payload destrutivo (a lista
# `config.AUDIT_PAYLOADS_DESTRUTIVOS` é mantida como fronteira), respeita o
# `Budget`/`AUDIT_MAX_PROBES`/rate-limit e NUNCA registra segredo bruto — a
# evidência é redigida (tipo/posição, nunca o valor).


def audit_metodos(url: str, allow_loopback: bool = False) -> list[Finding]:
    """Sonda OPTIONS/TRACE: métodos perigosos habilitados e TRACE (XST).

    `Allow` com PUT/DELETE/TRACE/CONNECT -> MEDIA (CWE-749). `TRACE` que
    responde 200 ecoando a sonda -> MEDIA (CWE-693, Cross-Site Tracing).
    Evidência: valor de `Allow` / status do TRACE. Tolerante a falha.
    """
    findings: list[Finding] = []
    try:
        _pausa()
        resp_opt = requisicao(
            url, metodo="OPTIONS", allow_loopback=allow_loopback, retries=1,
        )
    except Exception:  # noqa: BLE001 — sonda nunca derruba o check
        resp_opt = Resposta(erro="falha de rede", final_url=url)
    allow = (resp_opt.headers or {}).get("allow", "")
    origem = resp_opt.final_url or url
    if allow:
        metodos = [m.strip().upper() for m in allow.split(",") if m.strip()]
        perigosos = [m for m in _METODOS_PERIGOSOS if m in metodos]
        if perigosos:
            findings.append(_novo_finding(
                id="ativo-metodos-perigosos",
                categoria="metodos",
                titulo="Metodos HTTP perigosos habilitados",
                severidade=Severidade.MEDIA,
                descricao=(
                    "O servidor anuncia métodos potencialmente destrutivos ou "
                    f"inseguros ({', '.join(perigosos)}). PUT/DELETE podem "
                    "modificar recursos e TRACE/CONNECT ampliam a superfície."
                ),
                evidencia=f"Allow: {allow}; origem={origem}",
                origem=origem,
                stride="Tampering",
                owasp="A05:2021",
                cwe="CWE-749",
                recomendacao=(
                    "Desabilitar métodos não usados (PUT/DELETE/TRACE/CONNECT) "
                    "no servidor/proxy."
                ),
                passos_repro=[f"OPTIONS {origem}", "Ler o header Allow"],
            ))

    canario = _novo_canario()
    try:
        _pausa()
        resp_trace = requisicao(
            url, metodo="TRACE", allow_loopback=allow_loopback, retries=0,
            headers_extra={"X-Audit-Probe": canario},
        )
    except Exception:  # noqa: BLE001 — TRACE nunca derruba o check
        resp_trace = Resposta(erro="falha de rede", final_url=url)
    if (resp_trace.ok and resp_trace.status == 200
            and (canario in (resp_trace.body or "")
                 or "TRACE" in (resp_trace.body or "").upper())):
        findings.append(_novo_finding(
            id="ativo-trace-habilitado",
            categoria="metodos",
            titulo="TRACE habilitado (Cross-Site Tracing)",
            severidade=Severidade.MEDIA,
            descricao=(
                "O servidor responde TRACE ecoando a requisição (incluindo "
                "headers), o que viabiliza Cross-Site Tracing (XST) e vazamento "
                "de cookies/credenciais."
            ),
            evidencia=(
                f"status={resp_trace.status}; resposta ecoa a sonda; "
                f"origem={origem}"
            ),
            origem=origem,
            stride="Information disclosure",
            owasp="A05:2021",
            cwe="CWE-693",
            recomendacao="Desabilitar o método TRACE no servidor.",
            passos_repro=[f"TRACE {origem}", "Verificar o eco da requisição"],
        ))
    return _dedup(findings)


def audit_cors_ativo(url: str, allow_loopback: bool = False) -> list[Finding]:
    """Sonda ATIVA de CORS: envia Origin canário e avalia a reflexão.

    ACAO refletindo o canário OU `*` com `Allow-Credentials: true` -> ALTA
    (CWE-942). Evidência: headers ACAO/ACAC. Tolerante a falha.
    """
    try:
        _pausa()
        resp = requisicao(
            url, allow_loopback=allow_loopback, retries=1,
            headers_extra={"Origin": _CORS_ORIGIN_CANARIO_ATIVO},
        )
    except Exception:  # noqa: BLE001 — CORS ativo nunca derruba o check
        return []
    return _avaliar_cors(resp, url, _CORS_ORIGIN_CANARIO_ATIVO)


def audit_reflexao(
    url: str,
    allow_loopback: bool = False,
    permitir_post: bool = False,
) -> list[Finding]:
    """Envia um canário único em cada parâmetro/campo e detecta reflexão.

    Descobre `<form>` (action/method/campos de texto) e parâmetros de query;
    envia o canário no máximo `config.AUDIT_MAX_PROBES` vezes.

    M6: por padrão (`permitir_post=False`) a sonda só toca parâmetros de QUERY
    e formulários `method=get` — NUNCA submete POST a um formulário real
    (evita alterar estado/lockout). Com `permitir_post=True` (opt-in explícito,
    só com autorização) formulários POST são sondados via
    `application/x-www-form-urlencoded`. Campos `file`/`submit`/`reset`/`button`
    nunca são enviados (defesa em profundidade).

    Se o canário reaparecer (bruto ou escapado) -> MEDIA (CWE-79), com NOTA de
    que a reflexão só é XSS se não escapada. Evidência: trecho com o canário
    refletido.
    """
    resp = _buscar(url, allow_loopback)
    if not resp.ok:
        return []
    texto_html = resp.body or ""
    base = resp.final_url or url
    canario = _novo_canario()
    probes = 0

    def _achou(campo: str, corpo: str) -> Finding:
        pos = corpo.find(canario)
        if pos < 0:
            pos = 0
        return _novo_finding(
            id="ativo-reflexao",
            categoria="reflexao",
            titulo="Reflexao de entrada (possivel XSS)",
            severidade=Severidade.MEDIA,
            descricao=(
                f"O valor enviado no campo/parâmetro '{campo}' reaparece na "
                "resposta. Reflexao de entrada NAO prova XSS por si so: se o "
                "contexto nao escapar o valor, pode ser exploravel (CWE-79). "
                "Validar manualmente o contexto de saida."
            ),
            evidencia=f"{_snippet(corpo, pos)}; origem={base}",
            origem=base,
            stride="Tampering",
            owasp="A03:2021",
            cwe="CWE-79",
            recomendacao=(
                "Escapar a saida conforme o contexto (HTML/atributo/JS/URL) e "
                "validar/limitar a entrada no servidor."
            ),
            passos_repro=[
                f"GET {url}",
                f"Enviar o canario '{canario}' em '{campo}'",
                "Procurar o canario na resposta",
            ],
        )

    # 1) Parâmetros de query.
    itens = urllib.parse.parse_qsl(
        urllib.parse.urlparse(url).query, keep_blank_values=True,
    )
    for alvo, _valor in itens:
        if probes >= config.AUDIT_MAX_PROBES:
            break
        novos = [
            (k, canario if k == alvo else v) for k, v in itens
        ]
        if _tem_payload_destrutivo(canario, *[v for _, v in novos]):
            continue
        _pausa()
        try:
            r = requisicao(
                _com_query(url, novos), allow_loopback=allow_loopback,
                retries=0,
            )
        except Exception:  # noqa: BLE001 — sonda nunca derruba o check
            r = Resposta(erro="falha de rede", final_url=url)
        probes += 1
        if r.ok and _canario_refletido(canario, r.body or ""):
            return [_achou(alvo, r.body or "")]

    # 2) Formulários (GET e, se autorizado, POST). Um campo por vez.
    for form in _parse_forms(texto_html):
        if probes >= config.AUDIT_MAX_PROBES:
            break
        metodo = (form.get("method") or "get").strip().lower()
        if metodo not in ("get", "post"):
            continue
        # M6: POST é opt-in; por padrão ignora formulários POST (não altera
        # estado real). GET é sempre permitido (somente leitura).
        if metodo == "post" and not permitir_post:
            continue
        campos = [
            (n, t) for n, t in form["inputs"]
            if t in _TIPOS_ENVIAVEIS and t not in _TIPOS_BLOQUEADOS
        ]
        if not campos:
            continue
        action = urllib.parse.urljoin(base, form["action"] or base)
        for alvo, _tipo in campos:
            if probes >= config.AUDIT_MAX_PROBES:
                break
            dados = {n: "audit" for n, _ in campos}
            dados[alvo] = canario
            if _tem_payload_destrutivo(*dados.values()):
                continue
            _pausa()
            try:
                if metodo == "post":
                    r = requisicao(
                        action, metodo="POST",
                        dados=urllib.parse.urlencode(dados).encode(),
                        headers_extra={
                            "Content-Type":
                                "application/x-www-form-urlencoded",
                        },
                        allow_loopback=allow_loopback, retries=0,
                    )
                else:
                    r = requisicao(
                        _com_query(action, list(dados.items())),
                        allow_loopback=allow_loopback, retries=0,
                    )
            except Exception:  # noqa: BLE001 — sonda nunca derruba o check
                r = Resposta(erro="falha de rede", final_url=action)
            probes += 1
            if r.ok and _canario_refletido(canario, r.body or ""):
                return [_achou(alvo, r.body or "")]
    return []


def audit_open_redirect(url: str, allow_loopback: bool = False) -> list[Finding]:
    """Substitui cada parâmetro de query pela URL canário e observa o `Location`.

    Segue NO MÁXIMO 1 redirect (na prática, captura o primeiro 3xx sem
    seguir). Se `Location` apontar para o canário -> ALTA (open redirect
    confirmado); se contiver o canário de forma parcial -> MEDIA (CWE-601).
    Bounded e SSRF-safe (o destino não é seguido). Tolerante.
    """
    findings: list[Finding] = []
    itens = urllib.parse.parse_qsl(
        urllib.parse.urlparse(url).query, keep_blank_values=True,
    )
    if not itens:
        return []
    probes = 0
    for alvo, _valor in itens:
        if probes >= config.AUDIT_MAX_PROBES:
            break
        novos = [
            (k, _REDIRECT_CANARIO if k == alvo else v) for k, v in itens
        ]
        nova_url = _com_query(url, novos)
        _pausa()
        try:
            r = requisicao(
                nova_url, allow_loopback=allow_loopback, retries=0,
                seguir_redirects=False,
            )
        except Exception:  # noqa: BLE001 — open redirect nunca derruba o check
            r = Resposta(erro="falha de rede", final_url=nova_url)
        probes += 1
        loc = (r.headers or {}).get("location", "")
        if not loc or _REDIRECT_CANARIO not in loc:
            continue
        host_loc = urllib.parse.urlparse(loc).hostname or ""
        exato = host_loc == urllib.parse.urlparse(_REDIRECT_CANARIO).hostname
        findings.append(_novo_finding(
            id="ativo-open-redirect",
            categoria="open-redirect",
            titulo="Open redirect em parametro",
            severidade=Severidade.ALTA if exato else Severidade.MEDIA,
            descricao=(
                f"O parametro '{alvo}' controla o destino de um redirect "
                "(Location aponta para a URL canario), permitindo phishing e "
                "desvio de confianca."
            ),
            evidencia=(
                f"Location={loc}; parametro={alvo}; status={r.status}; "
                f"origem={nova_url}"
            ),
            origem=nova_url,
            stride="Spoofing",
            owasp="A01:2021",
            cwe="CWE-601",
            recomendacao=(
                "Validar o destino contra allowlist e usar caminhos relativos "
                "internos em vez de URLs fornecidas pelo usuario."
            ),
            passos_repro=[
                f"GET {nova_url}",
                "Observar o header Location",
            ],
        ))
        break
    return findings


def audit_graphql(url: str, allow_loopback: bool = False) -> list[Finding]:
    """Testa introspecção GraphQL em /graphql e /api/graphql.

    POST de query de introspecção; se a resposta expuser schema/`__schema` ->
    MEDIA (Information disclosure, CWE-200). Tolerante a 404/erro.
    """
    findings: list[Finding] = []
    base = _origem_base(url)
    payload = json.dumps(
        {"query": "query{__schema{queryType{name}}}"}
    ).encode()
    if _tem_payload_destrutivo(payload.decode("utf-8")):
        return findings
    for caminho in _GRAPHQL_PATHS:
        rota = base + caminho
        _pausa()
        try:
            r = requisicao(
                rota, metodo="POST", dados=payload,
                headers_extra={"Content-Type": "application/json"},
                allow_loopback=allow_loopback, retries=0,
            )
        except Exception:  # noqa: BLE001 — GraphQL nunca derruba o check
            continue
        if not r.ok or not r.body:
            continue
        if "__schema" not in r.body and "queryType" not in r.body:
            continue
        findings.append(_novo_finding(
            id="ativo-graphql-introspeccao",
            categoria="graphql",
            titulo="Introspeccao GraphQL habilitada",
            severidade=Severidade.MEDIA,
            descricao=(
                "O endpoint respondeu a uma query de introspeccao, expondo o "
                "schema (tipos, campos e mutacoes) e ampliando a superficie "
                "para ataques."
            ),
            evidencia=(
                f"POST {rota} -> {r.status}; schema exposto; origem={rota}"
            ),
            origem=rota,
            stride="Information disclosure",
            owasp="A01:2021",
            cwe="CWE-200",
            recomendacao=(
                "Desabilitar introspeccao em producao ou restringi-la a "
                "usuarios autenticados."
            ),
            passos_repro=[
                f"POST {rota} com query de introspeccao",
                "Observar o schema retornado",
            ],
        ))
    return _dedup(findings)


def audit_openapi(url: str, allow_loopback: bool = False) -> list[Finding]:
    """Procura especificações de API expostas (OpenAPI/Swagger).

    GET em `/openapi.json`, `/swagger.json`, `/api-docs`, `/swagger-ui.html`;
    200 com conteúdo de spec -> MEDIA (JSON/RAML) ou BAIXA (UI HTML). Evidência:
    URL + status + indício. Tolerante a 404.
    """
    findings: list[Finding] = []
    base = _origem_base(url)
    for caminho in _OPENAPI_PATHS:
        rota = base + caminho
        _pausa()
        try:
            r = requisicao(rota, allow_loopback=allow_loopback, retries=0)
        except Exception:  # noqa: BLE001 — OpenAPI nunca derruba o check
            continue
        if not r.ok or r.status != 200 or not r.body:
            continue
        corpo = r.body
        baixo = corpo.lower()
        if "openapi" in baixo:
            indicio = "openapi"
        elif "swagger" in baixo:
            indicio = "swagger"
        else:
            continue
        # Exige indício estrutural de spec (paths) ou de UI (swagger-ui).
        if '"paths"' not in corpo and "swagger-ui" not in baixo:
            continue
        severidade = Severidade.BAIXA if "<html" in baixo else Severidade.MEDIA
        findings.append(_novo_finding(
            id="ativo-openapi-exposto",
            categoria="openapi",
            titulo="Especificacao de API exposta",
            severidade=severidade,
            descricao=(
                "Uma especificacao OpenAPI/Swagger esta publicamente "
                "acessivel, revelando endpoints, parametros e modelos da API."
            ),
            evidencia=(
                f"status={r.status}; indicio={indicio}; url={rota}"
            ),
            origem=rota,
            stride="Information disclosure",
            owasp="A05:2021",
            cwe="CWE-200",
            recomendacao=(
                "Restringir o acesso a documentacao/spec de API (auth ou "
                "rede interna) em producao."
            ),
            passos_repro=[f"GET {rota}", "Inspecionar o corpo"],
        ))
        break
    return findings


def audit_paths_sensiveis(
    url: str, allow_loopback: bool = False,
) -> list[Finding]:
    """Confere uma lista BOUNDED de caminhos comuns sensíveis (GET read-only).

    200 com indício de exposição REAL -> `.env`/credenciais/backup/dump ->
    CRITICA (CWE-538); demais exposições -> ALTA. NUNCA baixa/registra o
    conteúdo: a evidência traz apenas status + tipo + valor REDIGIDO. Tolerante.
    """
    findings: list[Finding] = []
    base = _origem_base(url)
    for caminho, tipo, rx, severidade in _PATHS_SENSIVEIS:
        rota = base + "/" + caminho.lstrip("/")
        _pausa()
        try:
            r = requisicao(rota, allow_loopback=allow_loopback, retries=0)
        except Exception:  # noqa: BLE001 — paths nunca derrubam o check
            continue
        if not r.ok or r.status != 200 or not r.body:
            continue
        if not rx.search(r.body):
            continue
        findings.append(_novo_finding(
            id=f"ativo-path-{re.sub(r'[^a-z0-9]+', '-', tipo)}",
            categoria="paths-sensiveis",
            titulo=f"Arquivo sensivel exposto ({tipo})",
            severidade=severidade,
            descricao=(
                f"O caminho '{rota}' respondeu 200 com conteudo compativel com "
                f"'{tipo}', indicando exposicao de arquivo/credencial. O valor "
                "foi REDIGIDO e NAO armazenado."
            ),
            evidencia=(
                f"{_evidencia_sensivel(r.status, tipo, r.body)}; origem={rota}"
            ),
            origem=rota,
            stride="Information disclosure",
            owasp="A05:2021",
            cwe="CWE-538",
            recomendacao=(
                "Bloquear o acesso publico a arquivos de configuracao, "
                "credenciais, backups e metadados de versionamento."
            ),
            passos_repro=[f"GET {rota}", "Inspecionar apenas status/tipo"],
        ))
    return findings


def audit_portas(
    host: str,
    portas: tuple[int, ...] = (80, 443, 8080, 8443),
    allow_loopback: bool = False,
) -> list[Finding]:
    """Verifica portas comuns abertas (INFO, não é Finding de falha).

    `socket.create_connection` com timeout curto (2s) por porta; porta aberta
    -> INFO, com banner trivial via HEAD/`Server` quando disponível. Respeita
    `_alvo_valido` e é tolerante a falha.
    """
    findings: list[Finding] = []
    host_limpo = urllib.parse.urlparse(str(host)).hostname or str(host)
    for porta in portas:
        try:
            porta_int = int(porta)
        except (TypeError, ValueError):
            continue
        alvo = f"http://{host_limpo}:{porta_int}"
        if not _alvo_valido(alvo, allow_loopback=allow_loopback):
            continue
        try:
            with socket.create_connection((host_limpo, porta_int), timeout=2):
                aberta = True
        except (OSError, ValueError, socket.timeout):
            aberta = False
        if not aberta:
            continue
        banner = ""
        try:
            r = requisicao(
                alvo, metodo="HEAD", allow_loopback=allow_loopback, retries=0,
            )
            banner = (r.headers or {}).get("server", "")
        except Exception:  # noqa: BLE001 — banner é best-effort
            banner = ""
        detalhe = f"; banner={banner}" if banner else ""
        findings.append(_novo_finding(
            id=f"ativo-porta-aberta-{porta_int}",
            categoria="portas",
            titulo=f"Porta {porta_int} aberta",
            severidade=Severidade.INFO,
            descricao=(
                f"A porta {porta_int} aceitou conexao. Servicos expostos devem "
                "ser minimizados e protegidos (informativo, nao e falha)."
            ),
            evidencia=f"porta={porta_int} aberta{detalhe}; host={host_limpo}",
            origem=alvo,
            stride="Information disclosure",
            recomendacao=(
                "Expor apenas portas necessarias, com firewall e servico "
                "atualizado."
            ),
            passos_repro=[f"Conectar em {host_limpo}:{porta_int}"],
        ))
    return findings


def audit_segredos_js(
    url: str, html: str | None = None, allow_loopback: bool = False,
) -> list[Finding]:
    """Procura segredos e source maps em ate 5 arquivos JS da pagina.

    Descobre `<script src>` (max. `_MAX_SCRIPTS_JS`, respeitando Budget) e faz
    GET. Padroes de segredo -> ALTA (CWE-798/CWE-540); evidencia REDIGIDA:
    apenas tipo + arquivo + posicao aproximada (NUNCA o valor). Source map
    (`//# sourceMappingURL=`) -> BAIXA (exposicao de codigo-fonte). Tolerante.
    """
    findings: list[Finding] = []
    if html is None:
        resp = _buscar(url, allow_loopback)
        if not resp.ok:
            return []
        html = resp.body
    base = urllib.parse.urlparse(url)
    origem_pagina = (
        f"{base.scheme}://{base.netloc}" if base.netloc else url
    )
    scripts = _script_srcs(html or "")[:_MAX_SCRIPTS_JS]
    for src in scripts:
        # Checagem NÃO consumidora: `requisicao` já consome o budget.
        if _BUDGET is not None and _BUDGET.restantes <= 0:
            break
        rota = urllib.parse.urljoin(url, src)
        _pausa()
        try:
            r = requisicao(rota, allow_loopback=allow_loopback, retries=0)
        except Exception:  # noqa: BLE001 — JS nunca derruba o check
            continue
        if not r.ok or not r.body:
            continue
        corpo = r.body
        for tipo, rx in _PADROES_SEGREDO:
            m = rx.search(corpo)
            if not m:
                continue
            findings.append(_novo_finding(
                id=f"ativo-segredo-js-{tipo}-{_slug_arquivo(rota)}",
                categoria="segredos-js",
                titulo=f"Possivel segredo exposto em JS ({tipo})",
                severidade=Severidade.ALTA,
                descricao=(
                    f"O arquivo JS '{rota}' contem um valor compativel com "
                    f"'{tipo}'. O valor foi REDIGIDO e NAO armazenado; expor "
                    "credenciais no cliente permite abuso direto."
                ),
                evidencia=(
                    f"tipo={tipo}; arquivo={rota}; posicao~={m.start()}; "
                    "valor=REDIGIDO"
                ),
                origem=rota,
                stride="Information disclosure",
                owasp="A02:2021",
                cwe="CWE-798",
                recomendacao=(
                    "Remover segredos do front-end, rotacionar a credencial e "
                    "servir via backend/proxy autenticado."
                ),
                passos_repro=[f"GET {rota}", f"Procurar padrao {tipo}"],
            ))
        sm = _SOURCE_MAP_RE.search(corpo)
        if sm:
            findings.append(_novo_finding(
                id="ativo-source-map-exposto",
                categoria="segredos-js",
                titulo="Source map exposto",
                severidade=Severidade.BAIXA,
                descricao=(
                    f"O arquivo JS '{rota}' referencia um source map, permitindo "
                    "reconstruir o codigo-fonte original."
                ),
                evidencia=(
                    f"tipo=sourceMappingURL; arquivo={rota}; "
                    f"posicao~={sm.start()}"
                ),
                origem=rota,
                stride="Information disclosure",
                owasp="A05:2021",
                cwe="CWE-540",
                recomendacao=(
                    "Nao publicar source maps em producao ou restringi-los."
                ),
                passos_repro=[f"GET {rota}", "Procurar sourceMappingURL"],
            ))
        # M5: NÃO interromper por causa de finding. Um source map (BAIXA) no
        # primeiro JS não pode esconder um segredo (ALTA) no segundo; o loop
        # segue até `_MAX_SCRIPTS_JS`/Budget e acumula todos os achados.
    return _dedup(findings)


def audit_componentes_osv(
    url: str, html: str | None = None, allow_loopback: bool = False,
) -> list[Finding]:
    """Consulta a API OSV (read-only) para libs/versoes detectadas no HTML.

    Extrai (pacote, versao) de jquery/bootstrap/react/vue/angular e
    `<meta generator>`; para cada par faz POST `api.osv.dev/v1/query`. Se
    houver vulnerabilidades -> ALTA (HIGH/CRITICAL) ou MEDIA. Best-effort:
    qualquer falha e ignorada (nunca derruba). Sem versao -> ignora.
    """
    findings: list[Finding] = []
    if html is None:
        resp = _buscar(url, allow_loopback)
        if not resp.ok:
            return []
        html = resp.body
    componentes = _extrair_componentes(html or "")
    for pacote, versao in componentes:
        payload = json.dumps({
            "package": {"name": pacote, "ecosystem": "npm"},
            "version": versao,
        }).encode()
        if _tem_payload_destrutivo(payload.decode("utf-8")):
            continue
        _pausa()
        try:
            r = requisicao(
                "https://api.osv.dev/v1/query", metodo="POST", dados=payload,
                headers_extra={"Content-Type": "application/json"},
                allow_loopback=allow_loopback, retries=1,
            )
        except Exception:  # noqa: BLE001 — OSV nunca derruba o check
            continue
        if not r.ok or not r.body:
            continue
        try:
            dados = json.loads(r.body)
        except (json.JSONDecodeError, ValueError):
            continue
        vulns = dados.get("vulns") if isinstance(dados, dict) else None
        if not vulns:
            continue
        if not isinstance(vulns, list):
            continue
        ids = [
            str(v.get("id", "")) for v in vulns if isinstance(v, dict)
        ]
        ids = [i for i in ids if i][:5]
        findings.append(_novo_finding(
            id=f"ativo-componente-vulneravel-{re.sub(r'[^a-z0-9]+', '-', pacote)}",
            categoria="componentes",
            titulo=f"Componente vulneravel: {pacote} {versao}",
            severidade=_severidade_osv(vulns),
            descricao=(
                f"A versao {versao} de '{pacote}' possui "
                f"{len(vulns)} vulnerabilidade(s) conhecida(s) no OSV."
            ),
            evidencia=(
                f"pacote={pacote}@{versao}; vulns={len(vulns)}; "
                f"ids={', '.join(ids) or 'n/d'}"
            ),
            origem=url,
            stride="Elevation of privilege",
            owasp="A06:2021",
            cwe="CWE-1104",
            recomendacao=(
                f"Atualizar '{pacote}' para uma versao corrigida e acompanhar "
                "avisos do OSV."
            ),
            passos_repro=[
                "POST https://api.osv.dev/v1/query",
                f"consultar {pacote}@{versao}",
            ],
        ))
    return _dedup(findings)


def audit_ativo(
    url: str,
    perfil: str,
    escopo: dict | None,
    porta: int | None = None,
    allow_loopback: bool = False,
    permitir_post: bool = False,
) -> list[Finding]:
    """Agrega os testes ativos read-only (GATED) com try/except por etapa.

    Defesa em profundidade: se `perfil != "completo"` OU `perfil_autorizado`
    negar, devolve `[]` (o CLI já barra antes). Com autorização, roda os checks
    1-10 de forma tolerante (uma falha não derruba as demais) e devolve os
    Findings deduplicados. Bounded pelo Budget e `AUDIT_MAX_PROBES`.

    A1: `porta=None` deriva de `_porta_tls_padrao(url)`; a lista de portas do
    perfil (completo) alimenta SOMENTE o scan de `audit_portas`. M6:
    `permitir_post` é repassado a `audit_reflexao` (default False).
    """
    if str(perfil) != "completo":
        return []
    autorizado, _motivo = perfil_autorizado(perfil, escopo)
    if not autorizado:
        return []

    host = urllib.parse.urlparse(url).hostname or url
    if porta is None:
        porta_int = _porta_tls_padrao(url)
    else:
        try:
            porta_int = int(porta)
        except (TypeError, ValueError):
            porta_int = _porta_tls_padrao(url)
    portas_scan = set(PERFIS.get("completo", {}).get(
        "portas", (80, 443, 8080, 8443)))
    portas_scan.add(porta_int)
    resp = _buscar(url, allow_loopback)
    html = resp.body if resp.ok else ""

    etapas = (
        ("metodos", lambda: audit_metodos(url, allow_loopback)),
        ("cors_ativo", lambda: audit_cors_ativo(url, allow_loopback)),
        ("reflexao",
         lambda: audit_reflexao(url, allow_loopback, permitir_post)),
        ("open_redirect", lambda: audit_open_redirect(url, allow_loopback)),
        ("graphql", lambda: audit_graphql(url, allow_loopback)),
        ("openapi", lambda: audit_openapi(url, allow_loopback)),
        ("paths_sensiveis",
         lambda: audit_paths_sensiveis(url, allow_loopback)),
        ("portas", lambda: audit_portas(
            host,
            portas=tuple(sorted(portas_scan)),
            allow_loopback=allow_loopback,
        )),
        ("segredos_js",
         lambda: audit_segredos_js(url, html, allow_loopback)),
        ("componentes_osv",
         lambda: audit_componentes_osv(url, html, allow_loopback)),
    )
    findings: list[Finding] = []
    for _nome, etapa in etapas:
        try:
            findings.extend(etapa())
        except Exception:  # noqa: BLE001 — uma etapa nunca derruba o agregador
            continue
    return _dedup(findings)


def executar_fase_passiva(host: str) -> tuple[list[Finding], dict]:
    """Fase passiva (osint): RDAP + DNS + e-mail + CT logs.

    Retorna `(findings, dados_brutos)`: apenas `audit_email` produz Findings
    nesta fundação; RDAP/DNS/CT são dados de reconhecimento.
    """
    dados: dict = {}
    dados["rdap"] = audit_rdap(host)
    dns = audit_dns(host)
    dados["dns"] = dns
    findings = audit_email(host, dns)
    dados["ct"] = audit_ct_logs(host)
    return findings, dados


# ===========================================================================
# SEÇÃO 5 — RELATÓRIO + CLI + SELF-TEST
# ===========================================================================

def _agora() -> str:
    """Timestamp ISO-8601 UTC da execução."""
    return datetime.now(timezone.utc).isoformat()


def _status_por_categoria(findings: list[Finding]) -> dict[str, str]:
    """Pior severidade agregada por categoria (ausente = "ok")."""
    categorias = sorted({f.categoria for f in findings})
    return {
        cat: agrega_severidade(
            [f.severidade for f in findings if f.categoria == cat]
        ).value
        for cat in categorias
    }


def _slug_seguro(alvo: str) -> str:
    """Slug de diretório de relatório que NUNCA levanta.

    B10: o fallback (quando `slugify` recusa) também aplica a checagem de nomes
    reservados do Windows (CON/PRN/AUX/NUL/COM1-9/LPT1-9), para que o slug não
    vire um nome de pasta inválido no Windows.
    """
    try:
        slug = slugify(alvo)
    except Exception:  # noqa: BLE001 — slug nunca derruba o relatório
        slug = re.sub(r"[^a-z0-9-]+", "-", str(alvo).lower()).strip("-")
        slug = slug or "alvo"
    if slug.upper() in _RESERVADOS_WINDOWS:
        slug = f"{slug}-alvo"
    return slug


def _severidade_ordem_valor(sev: Severidade) -> int:
    """Ordem numérica do risco (INFO=0 .. CRITICA=4), tolerante a strings."""
    if not isinstance(sev, Severidade):
        sev = Severidade(str(sev))
    return _SEVERIDADE_ORDEM[sev]


def _normalizar_contexto(contexto) -> list[dict]:
    """Normaliza `contexto_informado` para `[{texto, verificado: False}, ...]`.

    Aceita strings (CLI `--contexto` repetível) ou dicts. O campo `verificado`
    é SEMPRE forçado a `False` (anti-fabricação: contexto informado nunca vira
    afirmação sobre o alvo). Itens vazios são descartados.
    """
    itens: list[dict] = []
    for item in contexto or []:
        if isinstance(item, dict):
            texto = str(item.get("texto", "")).strip()
        else:
            texto = str(item).strip()
        if not texto:
            continue
        itens.append({"texto": texto, "verificado": False})
    return itens


def _veredito_por_categoria(findings: list[Finding]) -> dict[str, str]:
    """Veredito por categoria: "reprovou" se houver severidade >= media.

    Caso contrário "passou" (a categoria tem achados, mas todos abaixo de
    media). Categorias sem achados não entram no mapa.
    """
    mapa: dict[str, str] = {}
    for cat in sorted({f.categoria for f in findings}):
        pior = agrega_severidade(
            [f.severidade for f in findings if f.categoria == cat]
        )
        mapa[cat] = (
            "reprovou"
            if _severidade_ordem_valor(pior)
            >= _severidade_ordem_valor(Severidade.MEDIA)
            else "passou"
        )
    return mapa


def _hash_findings(findings: list[Finding]) -> str:
    """sha256 da IDENTIDADE dos achados (diff/idempotência) — M4.

    Hash de campos DETERMINÍSTICOS por finding: `id`, `categoria`,
    `severidade`, `confianca` e uma versão normalizada de `titulo` (espaços
    colapsados). Ordenado por `id`. Ficam DE FORA os campos voláteis —
    `evidencia`, `passos_repro` e `origem` — que podem conter canário aleatório
    (`audit_reflexao`) ou URLs mutáveis. Assim, duas execuções com canários
    diferentes produzem o MESMO hash (estável), pois o hash é da identidade do
    achado, não do conteúdo volátil capturado.
    """
    canonicos: list[dict] = []
    for f in sorted(findings, key=lambda f: str(f.id)):
        severidade = (
            f.severidade.value if isinstance(f.severidade, Severidade)
            else str(f.severidade)
        )
        confianca = (
            f.confianca.value if isinstance(f.confianca, Confianca)
            else str(f.confianca)
        )
        titulo_norm = " ".join(str(f.titulo).split())
        canonicos.append({
            "id": str(f.id),
            "categoria": str(f.categoria),
            "severidade": severidade,
            "confianca": confianca,
            "titulo": titulo_norm,
        })
    payload = json.dumps(
        canonicos, ensure_ascii=False, sort_keys=True, separators=(",", ":"),
    )
    return hashlib.sha256(payload.encode("utf-8")).hexdigest()


# --- Redação de capturas brutas (OBRIGATÓRIA antes de gravar em raw/) -------

_REDIGIDO = "[REDIGIDO]"
_RE_AUTHORIZATION = re.compile(r"(?i)(authorization\s*[:=]\s*)([^\r\n]+)")
_RE_SET_COOKIE = re.compile(r"(?i)(set-cookie\s*[:=]\s*)([^\r\n]+)")
_RE_COOKIE = re.compile(r"(?i)(?<!set-)(cookie\s*[:=]\s*)([^\r\n]+)")
_RE_BEARER = re.compile(r"(?i)(\bbearer\s+)([A-Za-z0-9._\-]{6,})")
_RE_BASIC = re.compile(r"(?i)(\bbasic\s+)([A-Za-z0-9+/=]{6,})")
_RE_JWT = re.compile(
    r"eyJ[A-Za-z0-9_-]{5,}\.[A-Za-z0-9_-]{5,}\.[A-Za-z0-9_-]+"
)
# A2: aceita aspas opcionais em torno da CHAVE e do VALOR, cobrindo JSON
# (`"api_key": "..."`, `"Set-Cookie": "..."`, `"password": "..."`), além do
# formato `chave=valor`/`chave: valor`. Alternativas mais longas primeiro para
# que `access_token`/`client_secret`/`set-cookie` vençam `token`/`secret`/`cookie`.
_RE_KV_SENSIVEL = re.compile(
    r"""(?ix)
    (?P<prefix>
        ['"]?(?:
            access[_-]?token|client[_-]?secret|private[_-]?key
            |api[_-]?key|apikey|secret|password|passwd
            |set-cookie|authorization|cookie|token|bearer
        )['"]?\s*[:=]\s*
    )
    (?P<valor>['"]?[^\s'",;]+['"]?)
    """
)


def redigir_conteudo(texto: str) -> str:
    """Redige segredos de uma captura bruta ANTES de gravar em `raw/`.

    Mascara o valor de `Authorization`, `Set-Cookie`/`Cookie`, `Bearer`/`Basic`,
    JWT e pares `api_key`/`secret`/`password`/`token`/`access_token`/
    `client_secret`/`private_key`. Suporta chave/valor ENTRE ASPAS (JSON), ex.:
    `{"api_key": "SEGREDO"}` e `"Set-Cookie": "sid=SEGREDO; HttpOnly"`. É
    OBRIGATÓRIO: capturas brutas nunca preservam segredo em disco (nunca o
    valor bruto).
    """
    if texto is None:
        return ""
    s = str(texto)
    s = _RE_SET_COOKIE.sub(r"\1" + _REDIGIDO, s)
    s = _RE_COOKIE.sub(r"\1" + _REDIGIDO, s)
    s = _RE_AUTHORIZATION.sub(r"\1" + _REDIGIDO, s)
    s = _RE_BEARER.sub(r"\1" + _REDIGIDO, s)
    s = _RE_BASIC.sub(r"\1" + _REDIGIDO, s)
    s = _RE_JWT.sub(_REDIGIDO, s)
    s = _RE_KV_SENSIVEL.sub(
        lambda m: m.group("prefix") + _REDIGIDO, s,
    )
    return s


def _nome_captura_seguro(nome: str) -> str:
    """Nome de arquivo de captura sem path traversal (apenas basename)."""
    base = pathlib.PurePosixPath(str(nome).replace("\\", "/")).name
    base = re.sub(r"[^A-Za-z0-9._-]+", "_", base).strip("._")
    return base or "captura.txt"


def _md_celula(valor) -> str:
    """Valor seguro para uma célula de tabela Markdown (escapa `|`/quebra)."""
    return (
        str(valor if valor is not None else "")
        .replace("|", "\\|")
        .replace("\n", " ")
    )


def _redigir_finding(f: Finding) -> Finding:
    """Cópia do `Finding` com os campos textuais de saída REDIGIDOS (M3).

    Aplica `redigir_conteudo` a `evidencia`, `descricao` e `recomendacao` no
    momento de RENDERIZAR/SERIALIZAR — nunca no objeto em memória. Assim
    `achados.json`, `README.md` e `resultados.md` (versionados) nunca levam um
    segredo vazado em corpo/stack trace (ex.: `DB_PASSWORD=...`).
    """
    return replace(
        f,
        evidencia=redigir_conteudo(f.evidencia),
        descricao=redigir_conteudo(f.descricao),
        recomendacao=redigir_conteudo(f.recomendacao),
    )


def _redigir_contexto(contexto: list[dict]) -> list[dict]:
    """Cópia do `contexto_informado` com o campo `texto` REDIGIDO (N1).

    O contexto informado nunca vira fato e também não pode vazar segredo para
    os artefatos versionados (`README.md`, `achados.json`, `runs/`). Aplica
    `redigir_conteudo` a `texto` na SAÍDA, preservando `verificado=False`.
    """
    itens: list[dict] = []
    for item in contexto or []:
        if isinstance(item, dict):
            novo = dict(item)
        else:
            novo = {"texto": str(item), "verificado": False}
        novo["texto"] = redigir_conteudo(str(novo.get("texto", "")))
        itens.append(novo)
    return itens


def _render_finding_md(f: Finding) -> list[str]:
    """Bloco Markdown de um achado (informa, NUNCA diz que corrigiu)."""
    linhas = [
        f"### [{f.id}] {f.titulo}",
        "",
        f"- **Severidade**: {f.severidade.value}",
        f"- **Confianca**: {f.confianca.value}",
        f"- **STRIDE**: {f.stride or '-'}",
        f"- **OWASP**: {f.owasp or '-'}",
        f"- **CWE**: {f.cwe or '-'}",
        f"- **CVSS**: {f.cvss if f.cvss is not None else '-'}",
        f"- **Descricao**: {f.descricao}",
        f"- **Evidencia**: {f.evidencia or '-'}",
        f"- **Origem**: {f.origem or '-'}",
        f"- **Passos de reproducao**: {'; '.join(f.passos_repro) or '-'}",
        f"- **Recomendacao**: {f.recomendacao or '-'}",
    ]
    if f.referencia:
        linhas.append(f"- **Referencia**: {f.referencia}")
    linhas.append("")
    return linhas


def _render_readme(
    alvo: str,
    perfil: str,
    data: str,
    escopo: dict,
    contexto: list[dict],
    findings: list[Finding],
    resumo: dict,
    veredito: dict[str, str],
    fases: list[str],
) -> str:
    """Monta o README.md humano (anti-fabricação: fatos só de findings)."""
    L = [
        "# Relatorio de auditoria de seguranca web (v2)",
        "",
        f"- **Alvo**: {alvo}",
        f"- **Data**: {data}",
        f"- **Perfil**: {perfil}",
        f"- **Versao da ferramenta**: {VERSAO}",
        f"- **Fases executadas**: {', '.join(str(x) for x in fases) or 'nenhuma'}",
        "",
        "## Escopo",
        "",
    ]
    if escopo:
        L += [
            f"- **Hosts**: {', '.join(map(str, escopo.get('hosts') or [])) or '-'}",
            f"- **Fases**: {', '.join(map(str, escopo.get('fases') or [])) or '-'}",
            f"- **Expira**: {escopo.get('expira') or '-'}",
            f"- **Operador**: {escopo.get('operador') or '-'}",
        ]
    else:
        L.append("escopo não registrado")
    L += [
        "",
        "## Contexto informado (NÃO verificado)",
        "",
        "> AVISO: os itens abaixo foram informados pelo solicitante e NÃO foram "
        "verificados pela auditoria. Eles NÃO são afirmações sobre o alvo e não "
        "substituem evidência observada.",
        "",
    ]
    if contexto:
        for item in contexto:
            L.append(f"- (não verificado) {item.get('texto', '')}")
    else:
        L.append("nenhum")
    L += [
        "",
        "## Resumo por severidade",
        "",
        "| Severidade | Total |",
        "| --- | --- |",
    ]
    for sev in ("critica", "alta", "media", "baixa", "info"):
        L.append(f"| {sev} | {resumo.get(sev, 0)} |")
    L += [
        "",
        "## Veredito por categoria",
        "",
        "| Categoria | Veredito |",
        "| --- | --- |",
    ]
    if veredito:
        for cat, v in sorted(veredito.items()):
            L.append(f"| {_md_celula(cat)} | {v} |")
    else:
        L.append("| (nenhuma) | inconclusivo |")
    L += [
        "",
        "## Achados (observados)",
        "",
        "> Fatos derivados exclusivamente dos achados tipados (finding.id).",
        "",
    ]
    observados = [f for f in findings if f.confianca != Confianca.INFERIDO]
    if observados:
        for f in observados:
            L += _render_finding_md(f)
    else:
        L.append("nenhum achado observado.")
    L += [
        "",
        "## Inferências (NÃO verificadas)",
        "",
        "> Itens inferidos, sem evidência observada direta; NÃO tratados como "
        "fato.",
        "",
    ]
    inferidos = [f for f in findings if f.confianca == Confianca.INFERIDO]
    if inferidos:
        for f in inferidos:
            L += _render_finding_md(f)
    else:
        L.append("nenhuma inferência.")
    L += [
        "",
        "---",
        "",
        "Gerado automaticamente a partir dos achados; itens 'informado pelo "
        "solicitante' não foram verificados.",
        "",
    ]
    return "\n".join(L)


def _render_resultados(
    findings: list[Finding], veredito: dict[str, str]
) -> str:
    """Monta o resultados.md (tabela por categoria + reprovados detalhados)."""
    L = [
        "# Resultados",
        "",
        "| Categoria | Veredito | Severidade | Confiança | Achado |",
        "| --- | --- | --- | --- | --- |",
    ]
    if findings:
        for f in findings:
            L.append(
                f"| {_md_celula(f.categoria)} "
                f"| {veredito.get(f.categoria, 'inconclusivo')} "
                f"| {f.severidade.value} | {f.confianca.value} "
                f"| {_md_celula(f.id)} - {_md_celula(f.titulo)} |"
            )
    else:
        L.append("| (nenhuma) | inconclusivo | - | - | - |")
    L += ["", "## Reprovados (severidade >= media)", ""]
    reprovados = [
        f for f in findings
        if _severidade_ordem_valor(f.severidade)
        >= _severidade_ordem_valor(Severidade.MEDIA)
    ]
    if reprovados:
        for f in reprovados:
            L += [
                f"### [{f.id}] {f.titulo}",
                "",
                f"- **Categoria**: {f.categoria}",
                f"- **Severidade**: {f.severidade.value}",
                f"- **Confianca**: {f.confianca.value}",
                f"- **Detalhe**: {f.descricao}",
                f"- **Evidencia**: {f.evidencia or '-'}",
                f"- **Origem**: {f.origem or '-'}",
                f"- **Recomendacao**: {f.recomendacao or '-'}",
                "",
            ]
    else:
        L.append("nenhum reprovado.")
    return "\n".join(L)


def _timestamp_run() -> str:
    """Timestamp de run ordenável (`YYYYMMDD-HHMMSS`, UTC)."""
    return datetime.now(timezone.utc).strftime("%Y%m%d-%H%M%S")


def _proximo_run_dir(runs_dir: pathlib.Path) -> pathlib.Path:
    """Cria (e devolve) um diretório de run único sob `runs/`.

    Sufixo numérico em caso de colisão no mesmo segundo (`<ts>-1`, `<ts>-2`),
    preservando a ordenação lexicográfica por recência.
    """
    runs_dir.mkdir(parents=True, exist_ok=True)
    base = _timestamp_run()
    candidato = runs_dir / base
    sufixo = 1
    while candidato.exists():
        candidato = runs_dir / f"{base}-{sufixo}"
        sufixo += 1
    candidato.mkdir(parents=True, exist_ok=True)
    return candidato


def _pontos_findings(findings: list) -> set[tuple[str, str]]:
    """Conjunto `(id, severidade)` a partir de findings (objetos ou dicts)."""
    pontos: set[tuple[str, str]] = set()
    for f in findings or []:
        if isinstance(f, dict):
            pontos.add((str(f.get("id", "")), str(f.get("severidade", ""))))
        else:
            pontos.add((str(f.id), f.severidade.value))
    return pontos


def _registrar_run_e_diff(
    dir_rel: pathlib.Path, payload: dict, findings: list[Finding]
) -> None:
    """Grava `runs/<ts>/achados.json` e o `diff.md` vs. run anterior.

    Sem run anterior -> "primeira execução (sem diff)". Caso contrário lista
    `novos`, `resolvidos` e `persistentes` por `(id, severidade)`.
    """
    runs_dir = dir_rel / "runs"
    atual = _proximo_run_dir(runs_dir)
    texto_json = json.dumps(payload, ensure_ascii=False, indent=2, default=str)
    (atual / "achados.json").write_text(texto_json, encoding="utf-8")

    anteriores = sorted(
        [d for d in runs_dir.iterdir() if d.is_dir() and d != atual],
        key=lambda d: d.name,
    )
    if not anteriores:
        (atual / "diff.md").write_text(
            "# Diff da execucao\n\nprimeira execução (sem diff)\n",
            encoding="utf-8",
        )
        return

    anterior = anteriores[-1]
    findings_ant: list = []
    try:
        dados_ant = json.loads(
            (anterior / "achados.json").read_text(encoding="utf-8")
        )
        if isinstance(dados_ant, dict):
            findings_ant = dados_ant.get("findings") or []
    except (OSError, ValueError, TypeError):
        findings_ant = []

    atual_set = _pontos_findings(findings)
    ant_set = _pontos_findings(findings_ant)
    novos = sorted(atual_set - ant_set)
    resolvidos = sorted(ant_set - atual_set)
    persistentes = sorted(atual_set & ant_set)

    L = [
        "# Diff da execucao",
        "",
        f"Execucao anterior: {anterior.name}",
        "",
        "## Novos",
        "",
    ]
    L.extend([f"- {i} ({s})" for i, s in novos] or ["- nenhum"])
    L += ["", "## Resolvidos", ""]
    L.extend([f"- {i} ({s})" for i, s in resolvidos] or ["- nenhum"])
    L += ["", "## Persistentes", ""]
    L.extend([f"- {i} ({s})" for i, s in persistentes] or ["- nenhum"])
    (atual / "diff.md").write_text("\n".join(L) + "\n", encoding="utf-8")


def gerar_relatorio(
    alvo: str,
    perfil: str,
    findings: list[Finding],
    escopo: dict | None = None,
    contexto_informado=None,
    saida_dir: pathlib.Path | None = None,
    porta: int = 443,
    capturas: dict[str, str] | None = None,
) -> pathlib.Path:
    """Gera o relatório v2 (README.md, resultados.md, achados.json, raw/, runs/).

    Anti-fabricação: TODO conteúdo factual deriva dos `Finding` tipados. O
    `contexto_informado` fica num campo separado, sempre `verificado=False`, e
    NUNCA é apresentado como afirmação sobre o alvo. Capturas brutas
    (`capturas`) passam por `redigir_conteudo` antes de ir para `raw/`.

    M3: na RENDERIZAÇÃO/SERIALIZAÇÃO, `evidencia`/`descricao`/`recomendacao` de
    cada finding passam por `_redigir_finding` (o objeto em memória NÃO é
    alterado). Assim README.md/resultados.md/achados.json (versionados) nunca
    levam segredo bruto.

    `saida_dir` default = `config.AUDIT_REPORT_DIR / _slug_seguro(alvo)`.
    `porta` é a porta auditada (metadado de execução). Retorna o diretório.
    """
    dados = list(findings or [])
    # M3: cópia redigida usada SOMENTE na saída (memória intacta).
    dados_saida = [_redigir_finding(f) for f in dados]
    escopo_norm = dict(escopo) if isinstance(escopo, dict) else {}
    contexto_norm = _normalizar_contexto(contexto_informado)
    # N1: contexto informado também é redigido na SAÍDA (README/achados.json/
    # runs/) — nunca vaza segredo em artefato versionado. O normalizado segue
    # com verificado=False.
    contexto_saida = _redigir_contexto(contexto_norm)
    data = _agora()
    resumo = {s.value: 0 for s in Severidade}
    for f in dados:
        resumo[f.severidade.value] += 1
    veredito = _veredito_por_categoria(dados)
    hash_findings = _hash_findings(dados)
    fases = list(escopo_norm.get("fases") or fases_do_perfil(perfil) or [])

    dir_rel = (
        pathlib.Path(saida_dir)
        if saida_dir is not None
        else config.AUDIT_REPORT_DIR / _slug_seguro(alvo)
    )
    dir_rel.mkdir(parents=True, exist_ok=True)
    dir_raw = dir_rel / "raw"
    dir_raw.mkdir(parents=True, exist_ok=True)

    # 1) README.md (findings de saída REDIGIDOS — M3)
    (dir_rel / "README.md").write_text(
        _render_readme(
            alvo, perfil, data, escopo_norm, contexto_saida, dados_saida,
            resumo, veredito, fases,
        ),
        encoding="utf-8",
    )
    # 2) resultados.md (redigido — M3)
    (dir_rel / "resultados.md").write_text(
        _render_resultados(dados_saida, veredito), encoding="utf-8",
    )
    # 3) achados.json
    payload = {
        "schema": "harness.security.relatorio/2.0",
        "alvo": str(alvo),
        "perfil": str(perfil),
        "data": data,
        "escopo": escopo_norm,
        "contexto_informado": contexto_saida,
        "resumo_severidade": resumo,
        "veredito_por_categoria": veredito,
        # M3: findings SERIALIZADOS já redigidos (achados.json versionado).
        "findings": [f.to_dict() for f in dados_saida],
        "versao_ferramenta": VERSAO,
        "hash_findings": hash_findings,
    }
    texto_json = json.dumps(payload, ensure_ascii=False, indent=2, default=str)
    (dir_rel / "achados.json").write_text(texto_json, encoding="utf-8")

    # 4) raw/ (capturas REDIGIDAS — nunca segredo bruto em disco)
    for nome, conteudo in (capturas or {}).items():
        seguro = _nome_captura_seguro(nome)
        (dir_raw / seguro).write_text(
            redigir_conteudo(conteudo), encoding="utf-8",
        )

    # 5) runs/<timestamp>/achados.json + diff.md
    _registrar_run_e_diff(dir_rel, payload, dados)

    return dir_rel


def salvar_relatorio(rel: Relatorio) -> pathlib.Path:
    """Compatibilidade: gera o relatório v2 a partir de um `Relatorio`.

    Mantido para chamadas antigas; delega para `gerar_relatorio`.
    """
    return gerar_relatorio(
        rel.alvo,
        rel.perfil,
        rel.findings,
        escopo=rel.escopo,
        contexto_informado=rel.contexto_informado,
        saida_dir=None,
    )


def self_test() -> int:
    """Sobe um servidor HTTP local intencionalmente mal configurado e valida
    transporte, redirect handler, budget e o caminho de loopback.

    Imprime `SELF-TEST OK`/`SELF-TEST FAIL` e retorna 0/1. NUNCA usa rede
    externa (o smoke de osint é offline, com DNS já fornecido).
    """
    from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
    import threading

    class _Handler(BaseHTTPRequestHandler):
        def log_message(self, *args):  # noqa: N802 — silencia o log padrão
            return

        def _responder(self, status, body=b"", extras=None):
            self.send_response(status)
            for nome, valor in (extras or []):
                self.send_header(nome, valor)
            self.send_header("Content-Type", "text/plain; charset=utf-8")
            self.end_headers()
            if body:
                self.wfile.write(body)

        def do_GET(self):
            rota = urllib.parse.urlparse(self.path).path
            qs = urllib.parse.parse_qs(urllib.parse.urlparse(self.path).query)
            if rota == "/form":
                # Formulário refletindo o parâmetro de query (sonda de
                # reflexão) e com POST action="/echo".
                valor = qs.get("q", [""])[0]
                corpo = (
                    "<html><body>q=" + valor + "<form action=\"/echo\" "
                    "method=\"post\"><input name=\"q\" type=\"text\">"
                    "</form></body></html>"
                ).encode("utf-8")
                self._responder(
                    200, corpo,
                    [("Content-Type", "text/html; charset=utf-8")],
                )
            elif rota == "/com-js":
                self._responder(
                    200,
                    b'<html><script src="/app.js"></script></html>',
                    [("Content-Type", "text/html; charset=utf-8")],
                )
            elif rota == "/app.js":
                self._responder(
                    200,
                    b'var k="AKIAABCDEFGHIJKLMNOP";\n'
                    b"//# sourceMappingURL=app.js.map\n",
                    [("Content-Type", "application/javascript")],
                )
            elif rota == "/.env":
                self._responder(200, b"DB_PASSWORD=supersecret123\nAPP_KEY=x\n")
            elif rota == "/openapi.json":
                self._responder(
                    200,
                    b'{"openapi":"3.0.0","paths":{"/x":{}}}',
                    [("Content-Type", "application/json")],
                )
            elif rota == "/redirect-open":
                self.send_response(302)
                self.send_header("Location", qs.get("next", ["/"])[0])
                self.end_headers()
            elif self.path == "/headers":
                # Sem nenhum header de segurança e com banner exposto.
                self._responder(200, b"sem headers", [("Server", "Teste/1.0")])
            elif self.path == "/hsts-fraco":
                self._responder(
                    200, b"hsts",
                    [("Strict-Transport-Security", "max-age=100")],
                )
            elif self.path == "/cookie":
                self.send_response(200)
                self.send_header("Set-Cookie", "sid=abcdef123456; Path=/")
                self.send_header("Content-Type", "text/plain")
                self.end_headers()
                self.wfile.write(b"cookie")
            elif self.path == "/cookie-host":
                self.send_response(200)
                self.send_header(
                    "Set-Cookie",
                    "__Host-a=b; Path=/; Domain=exemplo.com",
                )
                self.send_header("Content-Type", "text/plain")
                self.end_headers()
                self.wfile.write(b"cookie-host")
            elif self.path == "/erro":
                self._responder(
                    200,
                    b"Traceback (most recent call last):\n"
                    b'  File "C:\\\\app.py", line 1',
                )
            elif self.path == "/mixed":
                self._responder(
                    200,
                    b'<html><script src="http://cdn.example/x.js"></script>'
                    b'</html>',
                    [("Content-Type", "text/html; charset=utf-8")],
                )
            elif self.path == "/redirect":
                self.send_response(302)
                self.send_header("Location", "/")
                self.end_headers()
            elif self.path == "/loop":
                self.send_response(302)
                self.send_header("Location", "/loop")
                self.end_headers()
            elif self.path == "/cors":
                origem = self.headers.get("Origin", "*")
                self._responder(
                    200, b"cors", [("Access-Control-Allow-Origin", origem)]
                )
            elif self.path == "/robots.txt":
                self._responder(
                    200, b"User-agent: *\nDisallow: /admin\nDisallow: /\n"
                )
            elif self.path == "/listing/":
                self._responder(200, b"<title>Index of /</title>")
            elif self.path in ("/sitemap.xml", "/.well-known/security.txt"):
                self._responder(404, b"nao encontrado")
            else:
                self._responder(200, b"raiz")

        def do_HEAD(self):
            self._responder(200)

        def do_OPTIONS(self):
            # Anuncia métodos perigosos (sonda de `audit_metodos`).
            self._responder(
                200, b"", [("Allow", "GET, POST, PUT, DELETE, OPTIONS")],
            )

        def do_TRACE(self):
            # Ecoa a requisição (Cross-Site Tracing).
            self._responder(200, b"TRACE " + self.path.encode("utf-8"))

        def do_POST(self):
            rota = urllib.parse.urlparse(self.path).path
            tamanho = int(self.headers.get("Content-Length") or 0)
            corpo = self.rfile.read(tamanho) if tamanho else b""
            if rota == "/graphql":
                self._responder(
                    200,
                    b'{"data":{"__schema":{"queryType":{"name":"Query"}}}}',
                    [("Content-Type", "application/json")],
                )
            elif rota == "/echo":
                self._responder(200, corpo)
            else:
                self._responder(404, b"nao encontrado")

    servidor = ThreadingHTTPServer(("127.0.0.1", 0), _Handler)
    thread = threading.Thread(target=servidor.serve_forever, daemon=True)
    thread.start()
    base = f"http://127.0.0.1:{servidor.server_address[1]}"
    resultados: list[tuple[str, bool]] = []
    try:
        iniciar_budget(LIMITE_REQUISICOES)
        r = requisicao(base + "/", allow_loopback=True)
        resultados.append((
            "transporte status/body",
            r.status == 200 and "raiz" in r.body and not r.erro
            and not r.bloqueado,
        ))
        resultados.append((
            "headers presentes",
            "content-type" in {k.lower() for k in r.headers},
        ))
        r_redir = requisicao(base + "/redirect", allow_loopback=True)
        resultados.append((
            "redirect handler segue hop",
            r_redir.status == 200
            and r_redir.final_url.rstrip("/") == base.rstrip("/"),
        ))
        r_cookie = requisicao(base + "/cookie", allow_loopback=True)
        resultados.append((
            "set-cookie capturado",
            any("sid=" in c for c in r_cookie.set_cookie),
        ))
        r_neg = requisicao(base + "/", allow_loopback=False)
        resultados.append((
            "loopback bloqueado sem allow_loopback",
            bool(r_neg.bloqueado),
        ))
        b = Budget(2)
        ok_budget = (
            b.consumir() and b.consumir()
            and (not b.consumir()) and b.contador == 2 and b.restantes == 0
        )
        resultados.append(("budget consome ate o teto", ok_budget))
        iniciar_budget(1)
        r_ok = requisicao(base + "/", allow_loopback=True)
        r_exc = requisicao(base + "/", allow_loopback=True)
        resultados.append((
            "budget bloqueia requisicao alem do teto",
            r_ok.status == 200 and "budget" in r_exc.bloqueado,
        ))

        # --- WP4: checks de superfície contra o servidor local -------------
        iniciar_budget(LIMITE_REQUISICOES)

        f_a = audit_cabecalhos(base + "/headers", allow_loopback=True)
        ids_a = {f.id for f in f_a}
        # B11: o servidor local é http, então HSTS ausente NÃO é reportado;
        # em https (Resposta fornecida, sem rede) a ausência é reportada.
        f_a_https = audit_cabecalhos(
            "https://exemplo.com",
            resposta=Resposta(
                status=200, headers={}, final_url="https://exemplo.com",
            ),
        )
        resultados.append((
            "check A headers ausentes e banner",
            {"header-csp-ausente", "header-xfo-ausente",
             "header-xcto-ausente", "header-referrer-policy-ausente",
             "header-permissions-policy-ausente",
             "header-banner-server"} <= ids_a
            and "header-hsts-ausente" not in ids_a
            and any(f.id == "header-hsts-ausente" for f in f_a_https)
            and all(f.evidencia.strip() for f in f_a)
            and all(f.confianca == Confianca.OBSERVADO for f in f_a),
        ))
        f_hsts = audit_cabecalhos(base + "/hsts-fraco", allow_loopback=True)
        resultados.append((
            "check A HSTS fraco (BAIXA)",
            any(f.id == "header-hsts-fraco"
                and f.severidade == Severidade.BAIXA for f in f_hsts),
        ))

        f_b = audit_cookies(base + "/cookie", allow_loopback=True)
        ids_b = {f.id for f in f_b}
        resultados.append((
            "check B cookies sem flags",
            {"cookie-sem-secure", "cookie-sem-httponly",
             "cookie-sem-samesite"} <= ids_b
            and all(f.evidencia.strip() for f in f_b),
        ))
        f_bh = audit_cookies(base + "/cookie-host", allow_loopback=True)
        resultados.append((
            "check B prefixo __Host- invalido",
            any(f.id == "cookie-prefixo-host-invalido" for f in f_bh),
        ))

        f_e = audit_erros_expostos(base + "/erro", allow_loopback=True)
        resultados.append((
            "check E traceback exposto (ALTA)",
            any(f.id == "erro-traceback-python"
                and f.severidade == Severidade.ALTA for f in f_e),
        ))

        f_f, d_f = audit_well_known(base, allow_loopback=True)
        resultados.append((
            "check F well-known (robots/security.txt)",
            any(f.id == "well-known-security-txt-ausente" for f in f_f)
            and any(f.id == "well-known-robots-sensivel" for f in f_f)
            and d_f.get("/.well-known/security.txt", {}).get("encontrado")
            is False,
        ))

        f_g = audit_cors_passivo(base + "/cors", allow_loopback=True)
        resultados.append((
            "check G CORS origem refletida (ALTA)",
            any(f.id == "cors-origem-refletida"
                and f.severidade == Severidade.ALTA for f in f_g),
        ))

        r_mixed = requisicao(base + "/mixed", allow_loopback=True)
        f_h = audit_mixed_content(
            base.replace("http://", "https://") + "/mixed",
            resposta=r_mixed,
        )
        resultados.append((
            "check H mixed content (MEDIA)",
            any(f.id == "mixed-content-http"
                and f.severidade == Severidade.MEDIA for f in f_h),
        ))

        f_r = audit_redirects(base + "/redirect", allow_loopback=True)
        f_loop = audit_redirects(base + "/loop", allow_loopback=True)
        resultados.append((
            "check D redirect limpo/loop",
            f_r == [] and any(f.id == "redirect-loop" for f in f_loop),
        ))
        f_down = _avaliar_cadeia_redirects(
            "https://exemplo.com/", [(301, "http://exemplo.com/")],
            "http://exemplo.com/", 200,
        )
        resultados.append((
            "check D downgrade https->http (helper ALTA)",
            any(f.id == "redirect-downgrade"
                and f.severidade == Severidade.ALTA for f in f_down),
        ))

        agora = datetime(2026, 1, 1, tzinfo=timezone.utc)
        dias = _dias_para_expirar("Jan 10 00:00:00 2026 GMT", agora)
        resultados.append((
            "check C helpers expiracao/cipher/versao",
            dias is not None and abs(dias - 9.0) < 0.01
            and _cipher_fraco("RC4-SHA") is True
            and _cipher_fraco("DES-CBC3-SHA") is True
            and _cipher_fraco("ECDHE-RSA-AES128-GCM-SHA256") is False
            and _handshake_versao("127.0.0.1", 1, ssl.TLSVersion.TLSv1) is False,
        ))

        # Agregador tolerante (uma passada completa no servidor local).
        f_agg = audit_superficie(
            base + "/headers", "superficial", allow_loopback=True,
        )
        resultados.append((
            "agregador audit_superficie consolida findings",
            isinstance(f_agg, list) and len(f_agg) >= 5
            and all(isinstance(f, Finding) for f in f_agg),
        ))

        # --- WP5: checks ativos read-only contra o servidor local ----------
        iniciar_budget(LIMITE_REQUISICOES)

        f_met = audit_metodos(base + "/adv", allow_loopback=True)
        resultados.append((
            "ativo metodos perigosos + TRACE",
            any(f.id == "ativo-metodos-perigosos" for f in f_met)
            and any(f.id == "ativo-trace-habilitado" for f in f_met),
        ))

        f_cors = audit_cors_ativo(base + "/cors", allow_loopback=True)
        resultados.append((
            "ativo CORS reflete origem (ALTA)",
            any(f.id == "cors-origem-refletida"
                and f.severidade == Severidade.ALTA for f in f_cors),
        ))

        f_refl = audit_reflexao(base + "/form?q=x", allow_loopback=True)
        resultados.append((
            "ativo reflexao (MEDIA)",
            any(f.id == "ativo-reflexao"
                and f.severidade == Severidade.MEDIA for f in f_refl),
        ))

        f_ored = audit_open_redirect(
            base + "/redirect-open?next=/x", allow_loopback=True,
        )
        resultados.append((
            "ativo open redirect (ALTA)",
            any(f.id == "ativo-open-redirect"
                and f.severidade == Severidade.ALTA for f in f_ored),
        ))

        f_gql = audit_graphql(base, allow_loopback=True)
        resultados.append((
            "ativo GraphQL introspeccao (MEDIA)",
            any(f.id == "ativo-graphql-introspeccao"
                and f.severidade == Severidade.MEDIA for f in f_gql),
        ))

        f_oa = audit_openapi(base, allow_loopback=True)
        resultados.append((
            "ativo OpenAPI exposto",
            any(f.id == "ativo-openapi-exposto" for f in f_oa),
        ))

        f_path = audit_paths_sensiveis(base, allow_loopback=True)
        f_path_env = [f for f in f_path if f.id == "ativo-path-env"]
        resultados.append((
            "ativo path sensivel .env (CRITICA, redigido)",
            bool(f_path_env)
            and any(f.severidade == Severidade.CRITICA for f in f_path_env)
            and all("supersecret123" not in f.evidencia for f in f_path_env),
        ))

        f_js = audit_segredos_js(base + "/com-js", allow_loopback=True)
        f_js_seg = [
            f for f in f_js
            if f.id.startswith("ativo-segredo-js-aws-access-key-")
        ]
        resultados.append((
            "ativo segredo JS (ALTA, redigido) + source map",
            bool(f_js_seg)
            and any(f.severidade == Severidade.ALTA for f in f_js_seg)
            and all("AKIAABCDEFGHIJKLMNOP" not in f.evidencia for f in f_js)
            and any(f.id == "ativo-source-map-exposto" for f in f_js),
        ))

        f_ports = audit_portas(
            "127.0.0.1", portas=(servidor.server_address[1],),
            allow_loopback=True,
        )
        resultados.append((
            "ativo porta aberta (INFO)",
            any(f.severidade == Severidade.INFO for f in f_ports),
        ))

        f_ativo = audit_ativo(
            base + "/form?q=x", "completo",
            {"fases": ["passivo", "superficie", "ativo"], "autorizado": True},
            porta=servidor.server_address[1], allow_loopback=True,
        )
        resultados.append((
            "agregador audit_ativo gated roda e consolida",
            isinstance(f_ativo, list) and len(f_ativo) >= 4
            and all(isinstance(f, Finding) for f in f_ativo),
        ))

        # Smoke de osint OFFLINE (DNS já fornecido; sem rede externa).
        fs = audit_email("localhost", {
            "TXT": ["v=spf1 -all"],
            "DMARC": ["v=DMARC1; p=reject"],
        })
        resultados.append(("osint email sem achados (offline)", fs == []))
    finally:
        iniciar_budget(LIMITE_REQUISICOES)
        servidor.shutdown()
        servidor.server_close()

    falhas = [nome for nome, ok in resultados if not ok]
    for nome, ok in resultados:
        print(f"  [{'OK' if ok else 'FALHOU'}] {nome}")
    if falhas:
        print("SELF-TEST FAIL")
        return 1
    print("SELF-TEST OK")
    return 0


def main(argv: list[str] | None = None) -> int:
    """CLI: `python -m harness.security <url> [--perfil ...] [--porta N]
    [--relatorio] [--permitir-post-forms] [--self-test]`.

    Sem `--perfil` (e sem `--self-test`) é ERRO: o módulo NÃO assume o perfil
    (quem pergunta é o agente/hub) — imprime as opções ativas. Roda as fases do
    perfil (passivo/osint + superfície WP4 + ativo WP5 GATED) e imprime JSON.
    Console Windows: stdout reconfigurado para UTF-8.

    Autorização: `--autorizado` é a autorização de ESCOPO do perfil `completo`
    (escopo.autorizado=True). A autorização humana da Fase 3 no fluxo do
    pipeline é o gate `PHASE3_GATE` (consultado pelo AgentPipeline quando
    `testes_ativos=True`), camada independente e complementar.

    `--permitir-post-forms` (M6, default OFF) liga o POST em formulários na
    sonda de reflexão — ação que PODE ALTERAR ESTADO; só usar com autorização.
    """
    try:
        sys.stdout.reconfigure(encoding="utf-8", errors="replace")
    except (AttributeError, ValueError):
        pass
    parser = argparse.ArgumentParser(
        prog="python -m harness.security",
        description=(
            "Auditoria de seguranca web SSRF-safe e deterministica (agente "
            "pentester / skill security-audit)."
        ),
    )
    parser.add_argument(
        "url", nargs="?",
        help="URL ou dominio alvo (ex.: https://exemplo.com)",
    )
    parser.add_argument(
        "--perfil", choices=sorted(PERFIS), default=None,
        help="osint | superficial | completo (obrigatorio fora do --self-test)",
    )
    parser.add_argument(
        "--porta", type=int, default=None,
        help="porta do TLS (default: 443 para https, 80 para http)",
    )
    parser.add_argument(
        "--relatorio", action="store_true",
        help="grava o relatorio em docs/auditorias/<slug>/",
    )
    parser.add_argument(
        "--self-test", dest="self_test", action="store_true",
        help="roda o self-test local (servidor 127.0.0.1) e sai",
    )
    parser.add_argument(
        "--autorizado", action="store_true",
        help=(
            "autorizacao de ESCOPO: marca escopo.autorizado=True (exigido "
            "pelo perfil completo); a Fase 3 no pipeline usa o PHASE3_GATE"
        ),
    )
    parser.add_argument(
        "--permitir-post-forms", dest="permitir_post_forms",
        action="store_true",
        help=(
            "M6: permite POST em formularios na sonda de reflexao (default "
            "OFF; pode ALTERAR ESTADO — usar so com autorizacao)"
        ),
    )
    parser.add_argument("--operador", default="", help="operador responsavel")
    parser.add_argument("--expira", default="", help="validade da autorizacao")
    parser.add_argument(
        "--contexto", action="append", default=[], metavar="TEXTO",
        help=(
            "contexto informado pelo solicitante (repetivel); fica marcado "
            "como NAO verificado e NUNCA vira afirmacao sobre o alvo"
        ),
    )
    args = parser.parse_args(argv)

    if args.self_test:
        return self_test()

    if not args.url:
        print("erro: informe a URL/dominio alvo")
        print(
            "uso: python -m harness.security <url> "
            "--perfil osint|superficial|completo [--porta N] [--relatorio]"
        )
        return 1
    if not args.perfil:
        print("erro: informe --perfil (o modulo NAO assume o perfil)")
        print("perfis disponiveis:")
        for nome in sorted(PERFIS):
            p = PERFIS[nome]
            portas = ",".join(str(x) for x in p["portas"])
            gate = "sim" if p["requer_gate"] else "nao"
            print(
                f"  {nome}: fases={','.join(p['fases'])}; portas={portas}; "
                f"gate={gate}; {p['descricao']}"
            )
        return 1

    url = str(args.url).strip()
    if not url.lower().startswith(("http://", "https://")):
        url = "https://" + url
    host = urllib.parse.urlparse(url).hostname or url
    # A1: a porta do TLS deriva do ESQUEMA da URL (nunca da lista de portas do
    # perfil, que é usada SOMENTE pelo scan de audit_portas). `None` deixa os
    # agregadores derivarem via `_porta_tls_padrao`.
    porta = args.porta
    porta_efetiva = porta if porta is not None else _porta_tls_padrao(url)

    escopo = {
        "hosts": [host],
        "fases": fases_do_perfil(args.perfil),
        "expira": args.expira,
        "operador": args.operador,
        "autorizado": bool(args.autorizado),
    }
    autorizado, motivo = perfil_autorizado(args.perfil, escopo)
    if not autorizado:
        print(json.dumps(
            {"erro": motivo, "perfil": args.perfil, "escopo": escopo},
            ensure_ascii=False, indent=2,
        ))
        return 1

    iniciar_budget(LIMITE_REQUISICOES)
    data_inicio = _agora()
    findings: list[Finding] = []
    dados: dict = {}
    fases_executadas: list[str] = []
    for fase in fases_do_perfil(args.perfil):
        if fase == "passivo":
            f_fase, d_fase = executar_fase_passiva(host)
            findings.extend(f_fase)
            dados["passivo"] = d_fase
            fases_executadas.append("passivo")
        elif fase == "superficie":
            f_superficie = audit_superficie(url, args.perfil, porta)
            findings.extend(f_superficie)
            dados["superficie"] = {
                "perfil": args.perfil,
                "porta": porta_efetiva,
                "findings": len(f_superficie),
            }
            fases_executadas.append("superficie")
        elif fase == "ativo":
            f_ativo = audit_ativo(
                url, args.perfil, escopo, porta,
                permitir_post=args.permitir_post_forms,
            )
            findings.extend(f_ativo)
            dados["ativo"] = {
                "perfil": args.perfil,
                "porta": porta_efetiva,
                "findings": len(f_ativo),
            }
            fases_executadas.append("ativo")
    data_fim = _agora()

    contexto_informado = _normalizar_contexto(args.contexto)
    relatorio = Relatorio(
        alvo=url,
        perfil=args.perfil,
        data_inicio=data_inicio,
        data_fim=data_fim,
        escopo=escopo,
        contexto_informado=contexto_informado,
        findings=findings,
        fases_executadas=fases_executadas,
        versao_ferramenta=VERSAO,
        status_por_categoria=_status_por_categoria(findings),
    )
    saida = relatorio.to_dict()
    # N2: o JSON impresso no stdout também usa os findings REDIGIDOS; o objeto
    # em memória permanece intacto (mesma disciplina de `gerar_relatorio`).
    saida["findings"] = [
        f.to_dict() for f in (_redigir_finding(x) for x in findings)
    ]
    # N1 (defesa em profundidade): contexto informado também sai redigido.
    saida["contexto_informado"] = _redigir_contexto(contexto_informado)
    saida["dados"] = dados
    if args.relatorio:
        try:
            dir_rel = gerar_relatorio(
                url,
                args.perfil,
                findings,
                escopo=escopo,
                contexto_informado=contexto_informado,
                porta=porta_efetiva,
            )
            saida["relatorio"] = str(dir_rel)
            saida["relatorio_dir"] = str(dir_rel)
        except Exception as exc:  # noqa: BLE001 — relatório nunca derruba a CLI
            print(json.dumps(
                {"erro": f"falha ao gerar relatorio: {exc}"},
                ensure_ascii=False, indent=2,
            ))
            return 1
    print(json.dumps(saida, ensure_ascii=False, indent=2, default=str))
    return 0


if __name__ == "__main__":
    sys.exit(main())
