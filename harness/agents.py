"""Modelo estruturado dos agentes opencode + aprendizagem episódica (playbook).

O harness local entende e copia o comportamento dos agentes do opencode
(papéis, permissões, pipeline delivery-protocol, validações, lições) para um
sistema interno de agentes locais: modelo estruturado (playbook) +
orquestrador determinístico, sem LLM.

Fontes de verdade (somente leitura):
- `.opencode/agent/*.md` — frontmatter (description/mode/permission) e corpo
  (papel, regras, saída) de cada agente.
- Pipeline do delivery-protocol (estados, roteamento, contrato de saída,
  regras de bloqueio): dado de máquina definido nas constantes `DEFAULT_*`
  deste módulo (fonte única; doc legível em `docs/protocolo-delivery.md`).
- `memory/episodic/*.md` — registros de execuções passadas.

Uso:
    python -m harness.agents compile   # compila e salva o playbook
    python -m harness.agents show      # resumo legível do playbook
    python -m harness.agents show <nome>  # detalhe de um agente
    python -m harness.agents lessons <nome>  # lições confiáveis do agente
"""

from __future__ import annotations

import datetime
import json
import pathlib
import re
import sys
from dataclasses import dataclass, field

from . import config

# Estados padrão do pipeline (delivery-protocol)
DEFAULT_STATES = [
    "RECEBIDA",
    "CONSULTANDO_MEMORIA",
    "EM_EXPLORACAO",
    "EM_IMPLEMENTACAO",
    "EM_REVISAO",
    "ENCERRAMENTO",
]

# Roteamento padrão etapa -> agente
DEFAULT_ROUTING = {
    "MEMORIA": "brain",
    "EXPLORACAO": "explorer",
    "IMPLEMENTACAO": "implementer",
    "REVISAO": "reviewer",
    "GRAVACAO": "brain",
}

# Campos do contrato de saída (delivery-protocol)
DEFAULT_OUTPUT_CONTRACT = [
    "status",
    "resumo",
    "arquivos_alterados",
    "validacoes_executadas",
    "achados_da_revisao",
    "riscos_residuais",
    "aprovacoes_solicitadas",
]

# Regras de bloqueio extraídas de hub.md/skill
DEFAULT_BLOCKING_RULES = [
    "sem segredos",
    "sem comandos destrutivos",
    "sem acesso fora do projeto",
    "evidências obrigatórias",
]

# Cabeçalhos do corpo dos agentes usados na classificação de seções
_RESP_HEADINGS = ("papel", "o que faz", "como extrair")
_RULES_HEADINGS = ("regras",)
_OUTPUT_HEADINGS = ("saída", "saida", "entregar")

# Marcador de conteúdo ausente nos registros episódicos
_NAO_INFORMADO = "_não informado_"

# Marcador de item de lista que inicia um NOVO item/parágrafo na agregação
# (_agrupa_paragrafos): bullet (`- `, `* `), numerado (`1.`, `2)`, ...) e
# numerado com parênteses (`(1)`, `(2)`, ...). O marcador é removido do texto
# final do item (mesmo tratamento do bullet).
_MARCADOR_ITEM = re.compile(r"^\s*(?:[-*]|\d+[.)]|\(\d+\))\s+")

# Heurística de qualidade das lições aprendidas (ver _e_licao_util): um
# PARÁGRAFO do Contexto só vira lição se tiver comprimento mínimo (>= 25 chars
# e >= 4 palavras) e não for frase genérica de sessão de teste. As linhas são
# antes agregadas por parágrafo (_agrupa_paragrafos) para não fragmentar.
_MIN_LICAO_CHARS = 25
_MIN_LICAO_PALAVRAS = 4
_LICAO_GENERICAS = {
    "sessao de teste",
    "tudo ok",
    "teste ok",
    "tudo funcionando",
    "ok",
}

# Resumo de lições CONFIÁVEIS exposto ao implementer na delegação (Etapa 6 —
# fechar o loop de aprendizado). Só lições de trust alta/media entram (o nível
# `fraca` NUNCA é exposto); no máximo `MAX_LICOES_RESUMO` (as de maior
# ocorrência, desempate lexicográfico) e cada texto é encurtado a
# `LICAO_RESUMO_CHARS` para caber em ~1 linha. O `playbook.json` cru NUNCA é
# injetado — este resumo é a única exposição do playbook na delegação.
MAX_LICOES_RESUMO = 5
LICAO_RESUMO_CHARS = 120

# Meta-ruído (achado M3): lições AUTORREFERENTES ao próprio playbook/harness
# (ex.: "alimenta a curva por agente", "compilação do playbook") não agregam
# conhecimento operacional e dominavam o feed — a mesma lição aparecia x7 como
# a 1ª de cada agente. Elas CONTINUAM no playbook (traço histórico, não são
# removidas da memória), mas são filtradas do FEED:
# `resumo_licoes_confiaveis`, `resumo.md` (`_resumo`) e `pipeline._licoes`.
# O match é feito sobre o texto minúsculo e SEM acentos (as variantes
# acentuadas/desacentuadas caem no mesmo padrão).
_META_ACENTOS = str.maketrans(
    "áàâãäéèêëíìîïóòôõöúùûüç",
    "aaaaaeeeeiiiiooooouuuuc",
)
_LICAO_META = (
    "curva por agente",
    "compilacao do playbook",
    "alimenta a curva",
    "playbook e regenerado",
    "este contexto alimenta",
)


def _e_licao_meta(texto: str) -> bool:
    """True se a lição é META (autorreferente ao playbook/harness).

    Usada apenas no FEED de lições (não na compilação do playbook): o item
    continua existindo em `learned.por_agente.*.licoes`, mas não é exposto ao
    implementer (M3)."""
    if not texto:
        return False
    norm = str(texto).lower().translate(_META_ACENTOS)
    return any(padrao in norm for padrao in _LICAO_META)


# ---------------------------------------------------------------------------
# Modelo do agente


@dataclass
class AgentSpec:
    """Modelo de um agente opencode copiado (fonte: .opencode/agent/<nome>.md)."""

    name: str
    description: str = ""
    mode: str = "subagent"  # primary | subagent
    permissions: dict[str, str | dict] = field(default_factory=dict)
    responsibilities: list[str] = field(default_factory=list)
    rules: list[str] = field(default_factory=list)
    output: list[str] = field(default_factory=list)

    @property
    def edits(self) -> bool:
        """edit != deny total (mapa inline nega-tudo NÃO conta como edição)."""
        return _perm_nao_deny(self.permissions.get("edit"))

    @property
    def shell(self) -> bool:
        """bash != deny (mapa inline também conta)."""
        return _perm_nao_deny(self.permissions.get("bash"))

    @property
    def delegates(self) -> bool:
        """task == allow."""
        return self.permissions.get("task") == "allow"

    def to_dict(self) -> dict:
        return {
            "name": self.name,
            "description": self.description,
            "mode": self.mode,
            "permissions": self.permissions,
            "edits": self.edits,
            "shell": self.shell,
            "delegates": self.delegates,
            "responsibilities": self.responsibilities,
            "rules": self.rules,
            "output": self.output,
        }


def parse_agent_file(path) -> AgentSpec:
    """Lê um .md de agente: separa frontmatter (entre ---), parseia as chaves
    e extrai do corpo as seções de responsabilidades, regras e saída."""
    caminho = pathlib.Path(path)
    text = caminho.read_text(encoding="utf-8")
    front, corpo = _split_frontmatter(text)
    meta = _parse_frontmatter(front)
    secoes = _extrai_secoes(corpo)
    responsabilidades, regras, saida = _classifica_secoes(secoes)
    return AgentSpec(
        name=caminho.stem,
        description=str(meta.get("description", "")),
        mode=str(meta.get("mode", "subagent")),
        permissions=_normaliza_permissions(meta.get("permission", {})),
        responsibilities=responsabilidades,
        rules=regras,
        output=saida,
    )


def _normaliza_permissions(perm) -> dict:
    """Mantém apenas edit/bash/task; dicts aninhados (mapa inline) preservados."""
    if not isinstance(perm, dict):
        return {}
    resultado: dict = {}
    for chave in ("edit", "bash", "task"):
        valor = perm.get(chave)
        if valor is None:
            continue
        if isinstance(valor, dict):
            resultado[chave] = {str(k): str(v) for k, v in valor.items()}
        else:
            resultado[chave] = str(valor)
    return resultado


def _perm_nao_deny(valor) -> bool:
    """True se a permissão NÃO é deny total.

    - Valor escalar: `!= "deny"` (allow/ask -> True).
    - Mapa inline (opencode): a permissão é efetiva se houver PELO MENOS um
      valor não-deny (allow/ask) — `edit: {"**": "deny"}` (nega tudo) NÃO
      concede edição (False); `edit: {"*.md": "allow", "**": "deny"}` concede
      (True). Mapa vazio -> False (conservador)."""
    if isinstance(valor, dict):
        if not valor:
            return False
        return any(str(v).lower() != "deny" for v in valor.values())
    return valor != "deny"


# ---------------------------------------------------------------------------
# Pipeline (delivery-protocol)


@dataclass
class PipelineSpec:
    """Estados, roteamento, contrato de saída e regras de bloqueio do fluxo."""

    states: list[str] = field(default_factory=lambda: list(DEFAULT_STATES))
    routing: dict[str, str] = field(default_factory=lambda: dict(DEFAULT_ROUTING))
    output_contract: list[str] = field(default_factory=lambda: list(DEFAULT_OUTPUT_CONTRACT))
    blocking_rules: list[str] = field(default_factory=lambda: list(DEFAULT_BLOCKING_RULES))

    @classmethod
    def default(cls) -> "PipelineSpec":
        """Pipeline do delivery-protocol como dado de máquina (constantes).

        Fonte única dos estados, roteamento, contrato de saída e regras de
        bloqueio. A spec NÃO é mais extraída de markdown (a antiga skill
        `delivery-protocol` foi removida; a doc legível vive em
        `docs/protocolo-delivery.md`, verificada contra estas constantes)."""
        return cls(
            states=list(DEFAULT_STATES),
            routing=dict(DEFAULT_ROUTING),
            output_contract=list(DEFAULT_OUTPUT_CONTRACT),
            blocking_rules=list(DEFAULT_BLOCKING_RULES),
        )


@dataclass
class Episode:
    """Registro episódico de uma execução (fonte: memory/episodic/<id>.md).

    Trust: `trust` (alta|media|fraca), `origem` (rastreável) e
    `validado_por` vêm do frontmatter; registros antigos sem `trust` assumem
    `fraca` (conservador — memória não validada não é conhecimento
    confirmado). Cada lição/achado agregado herda o trust do episódio de
    origem (ver `_acumula_texto`).
    """

    record_id: str
    keywords: list[str] = field(default_factory=list)
    agente: str = ""
    status: str = ""
    trust: str = config.TRUST_DEFAULT
    origem: str = ""
    validado_por: str = ""
    fluxo: list[str] = field(default_factory=list)
    validacoes: list[str] = field(default_factory=list)
    achados: list[str] = field(default_factory=list)
    contexto: list[str] = field(default_factory=list)
    completo: bool = False

    def to_dict(self) -> dict:
        """Serializa para o playbook com o contexto agregado por PARÁGRAFO e
        filtrado (achado B): as linhas de continuação de cada parágrafo são
        unidas (_agrupa_paragrafos) e parágrafos triviais de sessão de teste
        não poluem episodes[].contexto (filtro igual ao das lições, ver
        _contexto_util)."""
        return {
            "record_id": self.record_id,
            "keywords": self.keywords,
            "agente": self.agente,
            "status": self.status,
            "trust": self.trust,
            "origem": self.origem,
            "validado_por": self.validado_por,
            "fluxo": self.fluxo,
            "validacoes": self.validacoes,
            "achados": self.achados,
            "contexto": [c for c in _agrupa_paragrafos(self.contexto) if _contexto_util(c)],
            "completo": self.completo,
        }


def parse_episode(path) -> Episode:
    """Parse de memory/episodic/<id>.md: frontmatter
    (id/keywords/agente/status + trust/origem/validado_por) + seções Contrato
    de entrada, Fluxo, Resultado e Contexto.

    Registros antigos SEM `trust`/`origem`/`validado_por` continuam lendo bem:
    `trust` assume `fraca` (tolerância à ausência, conservador).
    """
    caminho = pathlib.Path(path)
    text = caminho.read_text(encoding="utf-8")
    front, corpo = _split_frontmatter(text)
    meta = _parse_frontmatter(front)
    secoes = _extrai_secoes(corpo)

    keywords = meta.get("keywords") or []
    if isinstance(keywords, str):
        keywords = [keywords]

    trust = str(meta.get("trust") or "").strip().lower()
    if trust not in config.TRUST:
        trust = config.TRUST_DEFAULT

    contrato = secoes.get("Contrato de entrada", [])
    fluxo = [l for l in secoes.get("Fluxo", []) if l.strip()]
    resultado = [l for l in secoes.get("Resultado", []) if l.strip()]
    # Mantém as linhas BRUTAS da seção Contexto (incluindo linhas em branco):
    # o agrupamento por parágrafo (_agrupa_paragrafos) precisa delas para
    # detectar a fronteira entre parágrafos. O registro original permanece
    # intacto em disco; a agregação em lições usa os parágrafos.
    contexto = secoes.get("Contexto", [])
    validacoes, achados = _extrai_validacoes_achados(resultado)

    completo = all(
        _secao_tem_conteudo(s) for s in (contrato, fluxo, resultado, contexto)
    )
    return Episode(
        record_id=str(meta.get("id") or caminho.stem),
        keywords=[str(k) for k in keywords],
        agente=str(meta.get("agente", "")),
        status=str(meta.get("status", "")),
        trust=trust,
        origem=str(meta.get("origem", "") or ""),
        validado_por=str(meta.get("validado_por", "") or ""),
        fluxo=fluxo,
        validacoes=validacoes,
        achados=achados,
        contexto=contexto,
        completo=completo,
    )


def _extrai_validacoes_achados(linhas: list[str]) -> tuple[list[str], list[str]]:
    """Sub-seções 'validacoes_executadas' e 'achados_da_revisao' do Resultado
    (itens '- ...' indentados abaixo do campo)."""
    validacoes: list[str] = []
    achados: list[str] = []
    modo: str | None = None
    base = 0
    for line in linhas:
        if not line.strip():
            continue
        indent = len(line) - len(line.lstrip(" "))
        conteudo = line.strip()
        campo = conteudo.lstrip("-").strip()
        if campo.startswith("validacoes_executadas"):
            modo, base = "validacoes", indent
            continue
        if campo.startswith("achados_da_revisao"):
            modo, base = "achados", indent
            continue
        if modo and indent > base and conteudo.startswith("- "):
            alvo = validacoes if modo == "validacoes" else achados
            alvo.append(conteudo[2:].strip().strip("`").strip())
        elif modo and indent <= base:
            modo = None
    return validacoes, achados


def _secao_tem_conteudo(linhas: list[str]) -> bool:
    return any(l.strip() and l.strip() != _NAO_INFORMADO for l in linhas)


def _extrai_comandos(text: str) -> list[str]:
    """Extrai comandos de validação no formato `python ...`, `git ...`,
    `pytest ...`, `npm ...` ou `node ...` de um texto."""
    comandos: list[str] = []
    for m in re.finditer(r"(?:python|git|pytest|npm|node)\s+[^\n]+", text):
        cmd = m.group(0).strip().strip("`").strip()
        if "`" in cmd:
            cmd = cmd.split("`")[0].strip()
        if cmd and cmd not in comandos:
            comandos.append(cmd)
    return comandos


class HistoryLearner:
    """Extrai motivos de bloqueio do histórico de comandos (se existir)."""

    def __init__(self, history_file: pathlib.Path | None = None) -> None:
        self.history_file = history_file or config.HISTORY_FILE
        self.reasons = self._extract()

    def _extract(self) -> list[str]:
        try:
            if not pathlib.Path(self.history_file).exists():
                return []
            data = json.loads(pathlib.Path(self.history_file).read_text(encoding="utf-8"))
        except (OSError, json.JSONDecodeError, TypeError):
            return []
        motivos: list[str] = []
        for item in data if isinstance(data, list) else []:
            if isinstance(item, dict) and item.get("status") == "blocked":
                detalhe = str(item.get("detail") or "").strip()
                if detalhe and detalhe not in motivos:
                    motivos.append(detalhe)
        return motivos


def _agrupa_paragrafos(linhas: list[str]) -> list[str]:
    """Agrupa as linhas da seção Contexto em PARÁGRAFOS (não em linhas físicas).

    Cada parágrafo/item de lista vira UMA entrada, com as linhas de continuação
    concatenadas (removendo a indentação e juntando com espaço). Isso corrige a
    fragmentação por linha que inflava as contagens de lições: um parágrafo
    quebrado em 3 linhas físicas gerava 3 "lições" fragmentadas; agora vira 1
    lição completa.

    Convenção de fronteira de parágrafo:
    - linha em branco encerra o parágrafo corrente;
    - linha que começa com marcador de item (`- `, `* `, `1.`, `2)`, `(1)`,
      `(2)`, ... — ver `_MARCADOR_ITEM`) inicia um NOVO item (o anterior é
      fechado);
    - linha de continuação (sem linha em branco antes e sem marcador) é
      concatenada ao parágrafo/item anterior.
    O marcador da primeira linha do item é removido do texto final (o texto da
    lição não carrega o bullet nem o número).
    """
    paragrafos: list[str] = []
    atual: list[str] = []
    for linha in linhas:
        if not linha.strip():
            # linha em branco -> fim do parágrafo corrente
            if atual:
                paragrafos.append(" ".join(atual))
                atual = []
            continue
        if _MARCADOR_ITEM.match(linha) is not None:
            # novo item de lista (bullet, numerado ou numerado com parênteses)
            # -> fecha o anterior, inicia um novo (marcador removido)
            if atual:
                paragrafos.append(" ".join(atual))
            conteudo = _MARCADOR_ITEM.sub("", linha).strip()
            atual = [conteudo]
        elif not atual:
            # primeira linha do bloco / após linha em branco
            atual = [linha.strip()]
        else:
            # continuação do parágrafo/item anterior (indentação removida)
            atual.append(linha.strip())
    if atual:
        paragrafos.append(" ".join(atual))
    return [p for p in paragrafos if p.strip()]


def _e_licao_util(texto: str) -> bool:
    """Filtro de qualidade das lições: exige comprimento mínimo (>= 25 chars
    e >= 4 palavras, ver constantes _MIN_LICAO_*) e descarta frases genéricas
    de sessões de teste (ex.: "sessao de teste", "tudo ok") que não agregam
    nada ao playbook.

    O texto recebido já deve ser o PARÁGRAFO COMPLETO (ver _agrupa_paragrafos):
    o filtro é aplicado ao parágrafo inteiro, não a fragmentos de linha — um
    fragmento curto que sozinho passaria pode não passar no parágrafo completo
    (e vice-versa), o que é desejado para uma contagem honesta.
    """
    if not texto:
        return False
    if len(texto) < _MIN_LICAO_CHARS or len(texto.split()) < _MIN_LICAO_PALAVRAS:
        return False
    return texto.lower() not in _LICAO_GENERICAS


def _contexto_util(texto: str) -> bool:
    """Mesmo filtro de qualidade das lições (_e_licao_util) aplicado a cada
    PARÁGRAFO do Contexto na SERIALIZAÇÃO do playbook (achado B): parágrafos
    triviais ("sessao de teste", "tudo ok", curtos demais) não aparecem em
    episodes[].contexto. O registro episódico original é preservado em disco —
    o agrupamento por parágrafo (_agrupa_paragrafos) e a filtragem ocorrem só
    na serialização."""
    norm = texto.strip()
    if norm.startswith("- "):
        norm = norm[2:].strip()
    return _e_licao_util(norm)


# Separadores de rótulo composto de agente (multi-tag): "hub+reviewer+implementer",
# "documenter/hub", "implementer, web" -> cada lição é atribuída a TODOS os
# agentes mencionados. A curva individual por agente preserva mais informação
# do que tratar o rótulo composto como um bucket próprio (ver decisão D1).
_AGENTE_SEP_RE = re.compile(r"[\s+/+,]+")


def _agentes_de(rotulo: str) -> list[str]:
    """Normaliza o rótulo de agente de um episódio numa lista de agentes
    individuais. Rótulos compostos são separados por `+`, `/`, `,` ou espaço;
    cada lição/validação/achado é atribuído a CADA agente mencionado
    (multi-tag). Retorna [] se o rótulo for vazio."""
    if not rotulo:
        return []
    partes = (p.strip().lower() for p in _AGENTE_SEP_RE.split(rotulo))
    return [p for p in partes if p]


def _pior_trust(trusts) -> str:
    """Trust mais conservador de uma coleção (fraca < media < alta).

    Valores ausentes/inválidos são FILTRADOS (não contam como fraca): apenas
    os trusts válidos (alta/media/fraca) entram na comparação; se nenhum for
    válido, o resultado é o default conservador (`fraca`). Usado para marcar
    lições/achados vindos de múltiplos episódios: se qualquer origem é fraca,
    o item agregado é fraca (o harness não trata isso como conhecimento
    confirmado).
    """
    valores = [str(t or "").strip().lower() for t in trusts if str(t or "").strip().lower() in config.TRUST]
    if not valores:
        return config.TRUST_DEFAULT
    return min(valores, key=lambda t: config.TRUST_ORDEM.index(t))


def _acumula_texto(items: list[dict], texto: str, origem: str,
                   trust: str = config.TRUST_DEFAULT) -> None:
    """Soma ocorrências de uma lição/achado (mesmo texto normalizado) dentro
    do mesmo agente, acumulando os record_ids de origem (rastreabilidade) e o
    trust de cada origem (`trust_por_origem`). O item expõe `trust` = pior
    trust entre as origens (conservador)."""
    trust = str(trust or "").strip().lower()
    if trust not in config.TRUST:
        trust = config.TRUST_DEFAULT
    for item in items:
        if item["texto"] == texto:
            item["ocorrencias"] += 1
            if origem not in item["origens"]:
                item["origens"].append(origem)
                item["trust_por_origem"][origem] = trust
            item["trust"] = _pior_trust(item["trust_por_origem"].values())
            return
    items.append({
        "texto": texto,
        "ocorrencias": 1,
        "origens": [origem],
        "trust_por_origem": {origem: trust},
        "trust": trust,
    })


def _acumula_comando(items: list[dict], comando: str) -> None:
    """Soma ocorrências de um comando de validação dentro do mesmo agente."""
    for item in items:
        if item["comando"] == comando:
            item["ocorrencias"] += 1
            return
    items.append({"comando": comando, "ocorrencias": 1})


@dataclass
class AgentLearning:
    """Curva de aprendizado individual de um agente (playbook vivo).

    Para cada agente que aparece nos registros episódicos, agrega:
    - `licoes`: lista de {"texto", "ocorrencias", "origens", "trust",
      "trust_por_origem"} — quantas vezes cada lição apareceu (contagem), de
      quais record_ids episódicos veio e qual o trust da(s) origem(s) (o item
      expõe o pior trust entre origens — conservador);
    - `validacoes_comuns`: lista de {"comando", "ocorrencias"};
    - `achados`: lista de {"texto", "ocorrencias", "origens", "trust",
      "trust_por_origem"} — achados da revisão daquele agente.

    Resumo de confiança exposto na serialização/CLI: `n_licoes_fracas` (lições
    que vêm de memória fraca/não validada) e `n_licoes_confiaveis` (alta ou
    media). A spec do playbook agrega o trust da origem a cada lição/achado.
    """

    agente: str
    licoes: list[dict] = field(default_factory=list)
    validacoes_comuns: list[dict] = field(default_factory=list)
    achados: list[dict] = field(default_factory=list)

    @property
    def n_licoes_fracas(self) -> int:
        """Quantas lições distintas do agente vêm de memória com trust fraca
        (não confiável)."""
        return sum(1 for l in self.licoes if l.get("trust") == config.TRUST["fraca"])

    @property
    def n_licoes_confiaveis(self) -> int:
        """Quantas lições distintas do agente têm trust alta ou media."""
        return sum(1 for l in self.licoes if l.get("trust") in (config.TRUST["alta"], config.TRUST["media"]))

    def to_dict(self) -> dict:
        return {
            "agente": self.agente,
            "licoes": self.licoes,
            "validacoes_comuns": self.validacoes_comuns,
            "achados": self.achados,
            # Resumo de confiança (aditivo — chaves antigas preservadas).
            "n_licoes_fracas": self.n_licoes_fracas,
            "n_licoes_confiaveis": self.n_licoes_confiaveis,
        }


@dataclass
class LearnedModel:
    """Agregação do aprendizado: episódios, validações comuns, lições, motivos
    de bloqueio do histórico e a curva de aprendizado por agente (`por_agente`).

    As listas planas (`licoes`, `validacoes_comuns`) são mantidas para
    compatibilidade (consumidas por `harness/pipeline.py` e pela CLI); a
    agregação individual fica em `por_agente`.
    """

    episodes: list[Episode] = field(default_factory=list)
    validacoes_comuns: list[str] = field(default_factory=list)
    licoes: list[str] = field(default_factory=list)
    block_reasons: list[str] = field(default_factory=list)
    por_agente: dict[str, AgentLearning] = field(default_factory=dict)

    @classmethod
    def from_sources(
        cls,
        episodic_dir: pathlib.Path | None,
        history_file: pathlib.Path | None = None,
    ) -> "LearnedModel":
        """Monta o modelo a partir de memory/episodic/*.md e do histórico
        (tolerante a ausência de qualquer fonte).

        As lições de cada episódio são agregadas por PARÁGRAFO
        (_agrupa_paragrafos): linhas de continuação de um mesmo parágrafo são
        concatenadas em uma única lição, evitando a fragmentação que inflava
        as contagens (pendência 5). O filtro _e_licao_util é aplicado ao
        parágrafo completo."""
        episodes: list[Episode] = []
        if episodic_dir and pathlib.Path(episodic_dir).is_dir():
            for path in sorted(pathlib.Path(episodic_dir).glob("*.md")):
                if path.name == "index.md":
                    continue
                try:
                    episodes.append(parse_episode(path))
                except (OSError, ValueError):
                    continue

        contagem: dict[str, int] = {}
        licoes: list[str] = []
        vistos: set[str] = set()
        por_agente: dict[str, AgentLearning] = {}
        for ep in episodes:
            # validações comuns (plano): contadas em TODOS os episódios, sem
            # depender do rótulo de agente (preserva o comportamento original)
            for cmd in _extrai_comandos(" ".join(ep.validacoes)):
                contagem[cmd] = contagem.get(cmd, 0) + 1
            agentes = _agentes_de(ep.agente)
            if not agentes:
                continue
            # bucket da curva: criado para CADA agente mencionado no rótulo,
            # mesmo que o episódio ainda não acumule conteúdo (rastreabilidade —
            # o agente foi citado, mesmo sem lições até agora).
            for nome in agentes:
                por_agente.setdefault(nome, AgentLearning(agente=nome))
            # conteúdo por agente: apenas episódios completos contribuem
            if not ep.completo:
                continue
            # Lições por PARÁGRAFO (não por linha física): linhas de continuação
            # de um mesmo parágrafo são concatenadas (_agrupa_paragrafos) ANTES
            # do filtro _e_licao_util e da agregação, corrigindo a fragmentação
            # que inflava as contagens (ver pendência 5).
            licoes_ep: list[str] = []
            for par in _agrupa_paragrafos(ep.contexto):
                norm = par.strip()
                if norm.startswith("- "):
                    norm = norm[2:].strip()
                if not _e_licao_util(norm):
                    continue
                if norm not in vistos:
                    vistos.add(norm)
                    licoes.append(norm)
                licoes_ep.append(norm)
            achados_ep = [a for a in ep.achados if _contexto_util(a)]
            validacoes_ep = _extrai_comandos(" ".join(ep.validacoes))
            for nome in agentes:
                al = por_agente[nome]
                for texto in licoes_ep:
                    # Lição/achado herda o trust do episódio de origem (o item
                    # agregado expõe o pior trust entre as origens).
                    _acumula_texto(al.licoes, texto, ep.record_id, ep.trust)
                for cmd in validacoes_ep:
                    _acumula_comando(al.validacoes_comuns, cmd)
                for texto in achados_ep:
                    _acumula_texto(al.achados, texto, ep.record_id, ep.trust)

        # Ordena a curva de cada agente de forma determinística (mais ocorrências
        # primeiro; desempate lexicográfico) para README/CLI estáveis.
        for al in por_agente.values():
            al.licoes.sort(key=lambda item: (-item["ocorrencias"], item["texto"]))
            al.validacoes_comuns.sort(
                key=lambda item: (-item["ocorrencias"], item["comando"])
            )
            al.achados.sort(key=lambda item: (-item["ocorrencias"], item["texto"]))

        validacoes_comuns = [
            f"{cmd} (x{n})"
            for cmd, n in sorted(contagem.items(), key=lambda kv: (-kv[1], kv[0]))
        ]

        block_reasons = HistoryLearner(history_file).reasons
        return cls(
            episodes=episodes,
            validacoes_comuns=validacoes_comuns,
            licoes=licoes,
            block_reasons=block_reasons,
            por_agente=por_agente,
        )

    def to_dict(self) -> dict:
        return {
            "episodes": [ep.to_dict() for ep in self.episodes],
            "validacoes_comuns": self.validacoes_comuns,
            "licoes": self.licoes,
            "block_reasons": self.block_reasons,
            "por_agente": {nome: al.to_dict() for nome, al in self.por_agente.items()},
        }


# ---------------------------------------------------------------------------
# Playbook


@dataclass
class Playbook:
    """Compilação do comportamento dos agentes opencode + aprendizado."""

    # 1.1: adiciona `learned.por_agente` (curva de aprendizado por agente).
    schema_version: str = "1.1"
    data: str = field(default_factory=lambda: datetime.date.today().isoformat())
    agents: dict[str, AgentSpec] = field(default_factory=dict)
    pipeline: PipelineSpec = field(default_factory=PipelineSpec)
    learned: LearnedModel = field(default_factory=LearnedModel)

    @classmethod
    def compile(cls, root: pathlib.Path | None = None) -> "Playbook":
        """Orquestra a compilação: agentes opencode + pipeline + aprendizado.
        Não grava em disco (salvar é opcional, via save())."""
        root = root or config.ROOT
        agent_dir = root / config.OPCODE_REL_AGENT_DIR
        agents: dict[str, AgentSpec] = {}
        if agent_dir.is_dir():
            for path in sorted(agent_dir.glob("*.md")):
                spec = parse_agent_file(path)
                agents[spec.name] = spec
        pipeline = PipelineSpec.default()
        learned = LearnedModel.from_sources(
            root / "memory" / "episodic",
            root / "logs" / "harness_history.json",
        )
        return cls(agents=agents, pipeline=pipeline, learned=learned)

    def to_dict(self) -> dict:
        return {
            "schema_version": self.schema_version,
            "data": self.data,
            "agents": {name: spec.to_dict() for name, spec in self.agents.items()},
            "pipeline": {
                "states": self.pipeline.states,
                "routing": self.pipeline.routing,
                "output_contract": self.pipeline.output_contract,
                "blocking_rules": self.pipeline.blocking_rules,
            },
            "learned": self.learned.to_dict(),
        }

    def save(self, dir: pathlib.Path) -> None:
        """Grava playbook.json (json indentado) + README.md (resumo) +
        resumo.md (versão ~2KB para o Brain) em `dir`."""
        destino = pathlib.Path(dir)
        destino.mkdir(parents=True, exist_ok=True)
        (destino / "playbook.json").write_text(
            json.dumps(self.to_dict(), ensure_ascii=False, indent=2),
            encoding="utf-8",
        )
        (destino / "README.md").write_text(self._readme(), encoding="utf-8")
        (destino / "resumo.md").write_text(self._resumo(), encoding="utf-8")

    def _readme(self) -> str:
        linhas = [
            "# Playbook — Sistema interno de agentes (aprendido do opencode)",
            "",
            f"Compilado em {self.data} (schema {self.schema_version}).",
            "Fonte de verdade: `.opencode/agent/*.md`, pipeline do "
            "delivery-protocol (constantes em harness/agents.py), memória "
            "episódica e histórico de comandos.",
            "",
            "## Agentes",
            "",
            "| Nome | Modo | Edita | Shell | Delega |",
            "| --- | --- | --- | --- | --- |",
        ]
        for nome in sorted(self.agents):
            a = self.agents[nome]
            linhas.append(
                f"| {a.name} | {a.mode} | {'sim' if a.edits else 'não'} | "
                f"{'sim' if a.shell else 'não'} | {'sim' if a.delegates else 'não'} |"
            )
        linhas += [
            "",
            "## Pipeline",
            "",
            f"- Estados: {', '.join(self.pipeline.states)}",
            f"- Roteamento: {json.dumps(self.pipeline.routing, ensure_ascii=False)}",
            f"- Contrato de saída: {', '.join(self.pipeline.output_contract)}",
            f"- Regras de bloqueio: {', '.join(self.pipeline.blocking_rules)}",
            "",
            "## Lições aprendidas",
            "",
        ]
        if self.learned.licoes:
            linhas += [f"- {l}" for l in self.learned.licoes]
        else:
            linhas.append("- (nenhuma ainda)")
        linhas += ["", "## Validações comuns", ""]
        if self.learned.validacoes_comuns:
            linhas += [f"- {v}" for v in self.learned.validacoes_comuns]
        else:
            linhas.append("- (nenhuma ainda)")
        linhas += ["", "## Curva de aprendizado por agente", ""]
        if self.learned.por_agente:
            for nome in sorted(self.learned.por_agente):
                al = self.learned.por_agente[nome]
                total_licoes = sum(item["ocorrencias"] for item in al.licoes)
                linhas.append(f"### {al.agente}")
                linhas.append("")
                linhas.append(f"- Lições: {len(al.licoes)} "
                              f"({total_licoes} ocorrências) — "
                              f"{al.n_licoes_confiaveis} confiáveis, "
                              f"{al.n_licoes_fracas} fracas")
                top = al.validacoes_comuns[:5]
                if top:
                    linhas.append("- Top validações: " + "; ".join(
                        f"{v['comando']} (x{v['ocorrencias']})" for v in top))
                else:
                    linhas.append("- Top validações: (nenhuma)")
                linhas.append(f"- Achados: {len(al.achados)}")
                linhas.append("")
        else:
            linhas.append("- (nenhum agente com aprendizado ainda)")
        return "\n".join(linhas) + "\n"

    def _resumo(self) -> str:
        """Resumo do playbook para o Brain (nível completo de contexto).

        É a ÚNICA fonte do playbook para o Brain (playbook.json é dado de
        máquina do pipeline determinístico; README.md é o relatório
        detalhado). Conteúdo: papéis/permissões de cada agente (1 linha),
        pipeline (estados/roteamento em 1 linha), validações comuns (lista
        curta) e as lições CONFIÁVEIS por agente — até `MAX_LICOES_RESUMO` (5)
        por agente, cada uma encurtada a `LICAO_RESUMO_CHARS` (120), com a
        MESMA seleção de `resumo_licoes_confiaveis` (trust `alta`/`media`, sem
        `fraca`, sem META e deduplicada — M2/M3/B1/B2).

        O alvo original era ~2KB; com o limite de 5 lições × 120 chars por
        agente o arquivo passa disso, mas a ordenação por ocorrência prioriza
        as lições mais REUSADAS (as mais confiáveis em evidência prática) —
        tradeoff documentado de M2.
        """
        def _encurta(texto: str, limite: int) -> str:
            texto = texto.replace("|", "/").strip()
            if len(texto) > limite:
                texto = texto[: limite - 3] + "..."
            return texto

        linhas = [
            "# Resumo — Sistema interno de agentes (para o Brain)",
            "",
            f"Compilado em {self.data} (schema {self.schema_version}).",
            "Fonte: .opencode/agent/*.md + pipeline do delivery-protocol + "
            "memória episódica.",
            "",
            "## Agentes",
            "",
        ]
        for nome in sorted(self.agents):
            a = self.agents[nome]
            papel = a.description.strip()
            if not papel and a.responsibilities:
                papel = a.responsibilities[0].lstrip("- ").strip()
            linhas.append(
                f"- **{a.name}** ({a.mode}): {_encurta(papel, 70)} — "
                f"edita: {'sim' if a.edits else 'não'}, "
                f"shell: {'sim' if a.shell else 'não'}, "
                f"delega: {'sim' if a.delegates else 'não'}"
            )
        linhas += [
            "",
            "## Pipeline",
            "",
            f"- Estados: {', '.join(self.pipeline.states)}",
            "- Roteamento: " + ", ".join(
                f"{k}->{v}" for k, v in self.pipeline.routing.items()
            ),
            "",
            "## Validações comuns",
            "",
        ]
        if self.learned.validacoes_comuns:
            linhas += [f"- {v}" for v in self.learned.validacoes_comuns[:3]]
        else:
            linhas.append("- (nenhuma ainda)")
        linhas += ["", "## Lições confiáveis por agente", ""]
        tem_licoes = False
        dados = self.to_dict()
        for nome in sorted(self.learned.por_agente):
            confiaveis = _extrai_licoes_confiaveis(nome, dados)
            if not confiaveis:
                continue
            tem_licoes = True
            linhas.append(f"### {nome}")
            for ocorrencias, texto in confiaveis:
                linhas.append(f"- (x{ocorrencias}) {texto}")
        if not tem_licoes:
            linhas.append("- (nenhuma lição confiável ainda)")
        return "\n".join(linhas) + "\n"


def compile_playbook(root: pathlib.Path | None = None) -> Playbook:
    """Conveniência: compila o playbook (monta apenas, não grava em disco)."""
    return Playbook.compile(root)


def load_playbook() -> dict | None:
    """Lê memory/agents/playbook.json se existir. Tolerante à ausência."""
    try:
        if not config.PLAYBOOK_FILE.exists():
            return None
        return json.loads(config.PLAYBOOK_FILE.read_text(encoding="utf-8"))
    except (OSError, json.JSONDecodeError):
        return None


def _encurta_licao(texto: str, limite: int) -> str:
    """Normaliza e encurta o texto de uma lição para caber em UMA linha.

    Colapsa quebras de linha/espaços (`" ".join(texto.split())`), troca `|`
    por `/` (não quebra tabela markdown) e, se ainda exceder `limite`,
    trunca e acrescenta `...`. Retorna "" para entrada vazia."""
    texto = " ".join(str(texto).split()).replace("|", "/").strip()
    if not texto:
        return ""
    if limite <= 3:
        return texto[:limite]
    if len(texto) > limite:
        return texto[: limite - 3] + "..."
    return texto


def _normaliza_playbook_dict(playbook) -> dict | None:
    """Normaliza a entrada de playbook para `dict` (ou None se inválida).

    Aceita um objeto `Playbook` (usa `to_dict()`), um `dict` de
    `load_playbook()`/`Playbook.to_dict()` ou `None` (carrega
    `memory/agents/playbook.json`). Tolerante: ausente/malformado -> None."""
    dados = playbook if playbook is not None else load_playbook()
    if hasattr(dados, "to_dict"):
        dados = dados.to_dict()
    if not isinstance(dados, dict):
        return None
    return dados


def _extrai_licoes_confiaveis(
    agente: str,
    playbook=None,
    *,
    max_licoes: int = MAX_LICOES_RESUMO,
    limite: int = LICAO_RESUMO_CHARS,
) -> list[tuple[int, str]]:
    """Seleciona as lições CONFIÁVEIS (trust `alta`/`media`) de um agente.

    Fonte ÚNICA do feed de lições (`resumo_licoes_confiaveis`, `Playbook._resumo`
    e `licoes_confiaveis_textos`), garantindo a MESMA lógica/constantes:

    - só trust `alta`/`media` (lição `fraca` NUNCA entra);
    - descarta META (`_e_licao_meta`) — o item continua no playbook, mas não no
      feed (M3);
    - encurta o texto a `limite` (`_encurta_licao`) e DEDUPLICA pelo texto já
      encurtado, mantendo a MAIOR ocorrência entre os duplicados (B1: dois
      textos longos com o mesmo prefixo não geram linhas repetidas);
    - normaliza `ocorrencias` inválida/<=0 para 1 (B2: nunca `(x0)`);
    - ordena por ocorrência decrescente (desempate lexicográfico) e limita a
      `max_licoes`.

    Retorna pares `(ocorrencias, texto_encurtado)`. Playbook inválido/agente
    inexistente -> `[]` (nunca levanta)."""
    if not agente:
        return []
    dados = _normaliza_playbook_dict(playbook)
    if dados is None:
        return []
    learned = dados.get("learned")
    if not isinstance(learned, dict):
        return []
    por_agente = learned.get("por_agente")
    if not isinstance(por_agente, dict):
        return []
    curva = por_agente.get(agente)
    if not isinstance(curva, dict):
        return []
    licoes = curva.get("licoes")
    if not isinstance(licoes, list):
        return []

    # Trust mínimo confiável = "media" (fraca < media < alta em TRUST_ORDEM).
    nivel_media = config.TRUST_ORDEM.index(config.TRUST["media"])
    # B1/B2: mapa texto_encurtado -> MAIOR ocorrência (dedup com o melhor valor).
    ocorrencia_por_texto: dict[str, int] = {}
    for item in licoes:
        if not isinstance(item, dict):
            continue
        trust = str(item.get("trust") or "").strip().lower()
        if trust not in config.TRUST:
            continue
        if config.TRUST_ORDEM.index(trust) < nivel_media:
            continue  # lição fraca nunca entra
        texto = _encurta_licao(str(item.get("texto") or ""), limite)
        if not texto:
            continue
        if _e_licao_meta(texto):
            continue  # M3: meta-ruído nunca entra no feed
        try:
            ocorrencias = int(item.get("ocorrencias") or 0)
        except (TypeError, ValueError):
            ocorrencias = 0
        if ocorrencias < 1:
            ocorrencias = 1  # B2: nunca emitir (x0)
        if ocorrencias > ocorrencia_por_texto.get(texto, 0):
            ocorrencia_por_texto[texto] = ocorrencias

    selecionadas = sorted(
        ((n, texto) for texto, n in ocorrencia_por_texto.items()),
        key=lambda par: (-par[0], par[1]),
    )
    return selecionadas[:max_licoes]


def licoes_confiaveis_textos(
    playbook=None, *, limite: int = LICAO_RESUMO_CHARS
) -> list[str]:
    """Textos das lições CONFIÁVEIS (trust `alta`/`media`) de TODOS os agentes.

    Reusa `_extrai_licoes_confiaveis` (mesma seleção do feed: sem `fraca`, sem
    META, encurtada e deduplicada). Usada por
    `harness.pipeline.AgentPipeline._licoes()` para que o Contexto do registro
    episódico do pipeline nunca exponha lição `fraca` (achado M4). Playbook
    ausente/malformado -> `[]` (nunca levanta)."""
    dados = _normaliza_playbook_dict(playbook)
    if dados is None:
        return []
    learned = dados.get("learned")
    if not isinstance(learned, dict):
        return []
    por_agente = learned.get("por_agente")
    if not isinstance(por_agente, dict):
        return []
    textos: list[str] = []
    vistos: set[str] = set()
    for agente in sorted(por_agente):
        for _, texto in _extrai_licoes_confiaveis(
            agente, dados, max_licoes=10**9, limite=limite
        ):
            if texto not in vistos:
                vistos.add(texto)
                textos.append(texto)
    return textos


def resumo_licoes_confiaveis(
    agente: str, playbook: Playbook | dict | None = None
) -> str:
    """Texto compacto (markdown de ~1 linha por lição) com as lições
    CONFIÁVEIS (trust `alta`/`media`) de um agente, para realimentar a
    delegação ao implementer (Etapa 6 — fechar o loop de aprendizado).

    - `playbook`: dict de `load_playbook()`/compilado (`Playbook.to_dict()`) OU
      um objeto `Playbook` (usa `.to_dict()`). Quando `None`, lê
      `memory/agents/playbook.json`. Tolerante: playbook ausente/malformado
      devolve "" (nunca levanta).
    - Retorna no máximo `MAX_LICOES_RESUMO` lições (ordenadas por ocorrência
      decrescente; desempate lexicográfico) — as de maior reuso primeiro.
    - Cada lição é encurtada a `LICAO_RESUMO_CHARS` e deduplicada pelo texto
      encurtado (B1). Lições `fraca` NUNCA entram; lições META
      (autorreferentes ao playbook, ver `_LICAO_META`) também ficam de fora
      (M3). `ocorrencias` inválida é tratada como 1 (nunca `(x0)`, B2).
    - Sem lições confiáveis, retorna "".

    O `playbook.json` cru nunca deve ser injetado no contexto: este resumo é a
    forma suportada de expor o aprendizado na delegação.
    """
    confiaveis = _extrai_licoes_confiaveis(agente, playbook)
    if not confiaveis:
        return ""
    linhas = [f"## Lições confiáveis ({agente})", ""]
    linhas += [f"- (x{n}) {t}" for n, t in confiaveis]
    return "\n".join(linhas) + "\n"


# ---------------------------------------------------------------------------
# Helpers de parsing (YAML-subset e markdown)


def _split_frontmatter(text: str) -> tuple[str, str]:
    """Separa o frontmatter (entre linhas ---) do corpo do documento."""
    linhas = text.splitlines()
    if not linhas or linhas[0].strip() != "---":
        return "", text
    try:
        fim = linhas.index("---", 1)
    except ValueError:
        return "", text
    return "\n".join(linhas[1:fim]), "\n".join(linhas[fim + 1:])


def _parse_frontmatter(text: str) -> dict:
    """Parse de um subconjunto de YAML sem dependências:
    - chaves planas (`description: ...`);
    - blocos aninhados por indentação de 2 espaços (`permission:`);
    - mapas inline (`edit: {memory/**: allow, "*": deny}`);
    - listas (`keywords: [a, b]`);
    - listas em BLOCO (`keywords:\\n  - a\\n  - b`) — convertidas em lista real
      (antes eram parseadas silenciosamente como dicts aninhados com chave
      `- a`);
    - linhas `---` ignoradas;
    - valores com aspas removidas."""
    raiz: dict = {}
    pilha: list[tuple[int, dict]] = [(-1, raiz)]
    # Última chave com valor vazio que criou um dict filho — se o próximo
    # nível for um item de lista (`- a`), esse dict filho vira a lista.
    pendente: tuple[dict, str] | None = None
    for linha in text.splitlines():
        conteudo = linha.strip()
        if not conteudo or conteudo == "---":
            continue
        indent = len(linha) - len(linha.lstrip(" "))
        if conteudo.startswith("-"):
            item = conteudo[1:].strip().strip("'\"")
            while len(pilha) > 1 and indent <= pilha[-1][0]:
                pilha.pop()
            container = pilha[-1][1]
            if (pendente is not None and isinstance(container, dict)
                    and not container):
                # converte o dict filho vazio (da chave sem valor) em lista
                pai_da_chave, chave = pendente
                if (isinstance(pai_da_chave, dict)
                        and pai_da_chave.get(chave) is container):
                    lista = [item]
                    pai_da_chave[chave] = lista
                    pilha[-1] = (pilha[-1][0], lista)
                    pendente = None
                    continue
            if isinstance(container, list):
                container.append(item)
                continue
            # item de lista sem chave pendente (anômalo): vira string no pai
            if isinstance(container, dict):
                container.setdefault("_list", []).append(item)
            continue
        pendente = None
        if ":" in conteudo:
            chave, _, valor = conteudo.partition(":")
            chave = chave.strip().strip("'\"")
            valor = valor.strip()
        else:
            chave, valor = conteudo.strip("'\""), ""
        while pilha and indent <= pilha[-1][0]:
            pilha.pop()
        pai = pilha[-1][1]
        if valor == "":
            filho: dict = {}
            pai[chave] = filho
            pilha.append((indent, filho))
            pendente = (pai, chave)
        else:
            pai[chave] = _parse_value(valor)
    return raiz


def _parse_value(valor: str):
    """Converte o valor: lista `[a, b]`, mapa inline `{k: v, ...}` ou string
    (com aspas removidas)."""
    valor = valor.strip()
    if valor.startswith("[") and valor.endswith("]"):
        partes = _split_top_level(valor[1:-1], ",")
        return [p.strip().strip("'\"") for p in partes if p.strip()]
    if valor.startswith("{") and valor.endswith("}"):
        mapa: dict = {}
        for parte in _split_top_level(valor[1:-1], ","):
            k, _, v = parte.partition(":")
            mapa[k.strip().strip("'\"")] = _parse_value(v.strip())
        return mapa
    return valor.strip("'\"")


def _split_top_level(text: str, sep: str) -> list[str]:
    """Divide `text` pelo separador fora de aspas e de aninhamento [ ] { }."""
    partes: list[str] = []
    atual: list[str] = []
    profundidade = 0
    aspas: str | None = None
    for ch in text:
        if aspas:
            atual.append(ch)
            if ch == aspas:
                aspas = None
        elif ch in "'\"":
            aspas = ch
            atual.append(ch)
        elif ch in "[{":
            profundidade += 1
            atual.append(ch)
        elif ch in "]}":
            profundidade -= 1
            atual.append(ch)
        elif ch == sep and profundidade == 0:
            partes.append("".join(atual).strip())
            atual = []
        else:
            atual.append(ch)
    if atual:
        partes.append("".join(atual).strip())
    return [p for p in partes if p]


def _extrai_secoes(text: str) -> dict[str, list[str]]:
    """Agrupa linhas por heading `## X` até o próximo heading (`##` ou `#`).

    Code blocks (``` ... ```) são ignorados por completo: nem abrem seções
    (headings dentro do fence são exemplo/template, ex.: o template
    ```markdown do documenter) nem alimentam a seção corrente (evita vazar
    conteúdo de template para rules/output).
    """
    secoes: dict[str, list[str]] = {}
    atual: str | None = None
    em_code = False
    for line in text.splitlines():
        if line.strip().startswith("```"):
            em_code = not em_code
            continue
        if em_code:
            continue
        if line.startswith("## "):
            atual = line[3:].strip()
            secoes.setdefault(atual, [])
        elif line.startswith("#"):
            atual = None
        elif atual is not None:
            secoes[atual].append(line)
    return secoes


def _classifica_secoes(secoes: dict[str, list[str]]) -> tuple[list[str], list[str], list[str]]:
    """Classifica headings em responsabilidades, regras e saída."""
    responsabilidades: list[str] = []
    regras: list[str] = []
    saida: list[str] = []
    for heading, linhas in secoes.items():
        h = heading.lower()
        conteudo = [l for l in linhas if l.strip()]
        if any(k in h for k in _RESP_HEADINGS):
            responsabilidades.extend(conteudo)
        elif any(k in h for k in _RULES_HEADINGS):
            regras.extend(conteudo)
        elif any(k in h for k in _OUTPUT_HEADINGS):
            saida.extend(conteudo)
    return responsabilidades, regras, saida


# ---------------------------------------------------------------------------
# CLI


def main(argv: list[str] | None = None) -> int:
    """CLI: `compile` compila e salva o playbook; `show [nome]` exibe resumo."""
    # Console Windows (cp1252) não imprime todos os caracteres UTF-8 do conteúdo
    # (ex.: "→"); usa UTF-8 com substituição para nunca quebrar a saída.
    try:
        sys.stdout.reconfigure(encoding="utf-8", errors="replace")
    except (AttributeError, ValueError):
        pass
    args = list(argv) if argv is not None else sys.argv[1:]
    if not args:
        print("uso: python -m harness.agents compile|show|lessons [nome_agente]")
        return 1
    comando = args[0]
    if comando == "compile":
        try:
            playbook = compile_playbook()
            playbook.save(config.AGENTS_DIR)
            print(f"playbook salvo em {config.PLAYBOOK_FILE}")
            print(f"README.md e resumo.md atualizados em {config.AGENTS_DIR}")
            print(f"agentes: {len(playbook.agents)} | estados: {len(playbook.pipeline.states)}")
            return 0
        except Exception as exc:  # noqa: BLE001
            print(f"erro: {exc}")
            return 1
    if comando == "lessons":
        nome = args[1] if len(args) > 1 else None
        if not nome:
            print("uso: python -m harness.agents lessons <agente>")
            return 1
        texto = resumo_licoes_confiaveis(nome)
        if texto:
            print(texto, end="")
        else:
            print(f"nenhuma lição confiável para {nome}")
        return 0
    if comando == "show":
        playbook = load_playbook()
        if playbook is None:
            print("playbook não compilado; rode: python -m harness.agents compile")
            return 1
        nome = args[1] if len(args) > 1 else None
        if nome:
            spec = playbook.get("agents", {}).get(nome)
            curva = playbook.get("learned", {}).get("por_agente", {}).get(nome)
            if spec is None and curva is None:
                print(f"agente não encontrado: {nome}")
                return 1
            if spec is not None:
                print(f"{spec['name']} ({spec['mode']}) — {spec['description']}")
                print(f"  edita: {spec['edits']} | shell: {spec['shell']} | delega: {spec['delegates']}")
                print(f"  permissões: {spec['permissions']}")
                if spec["rules"]:
                    print("  regras:")
                    for r in spec["rules"]:
                        print(f"    - {r.lstrip('- ').strip()}")
            if curva is not None:
                print(f"  curva de aprendizado ({nome}):")
                for item in curva.get("licoes", []):
                    print(f"    - lição (x{item['ocorrencias']}, trust "
                          f"{item.get('trust', 'fraca')}, origens: "
                          f"{', '.join(item['origens'])}): {item['texto']}")
                for item in curva.get("validacoes_comuns", []):
                    print(f"    - validação (x{item['ocorrencias']}): {item['comando']}")
                for item in curva.get("achados", []):
                    print(f"    - achado (x{item['ocorrencias']}, trust "
                          f"{item.get('trust', 'fraca')}, origens: "
                          f"{', '.join(item['origens'])}): {item['texto']}")
                print(f"  resumo de confiança: "
                      f"{curva.get('n_licoes_confiaveis', 0)} confiáveis, "
                      f"{curva.get('n_licoes_fracas', 0)} fracas")
            return 0
        print(f"playbook {playbook.get('schema_version')} de {playbook.get('data')}")
        print(f"agentes: {len(playbook.get('agents', {}))}")
        for nome, spec in sorted(playbook.get("agents", {}).items()):
            print(f"  {nome} ({spec['mode']}) — edita: {spec['edits']}, "
                  f"shell: {spec['shell']}, delega: {spec['delegates']}")
        pipeline = playbook.get("pipeline", {})
        print(f"estados: {', '.join(pipeline.get('states', []))}")
        print(f"roteamento: {pipeline.get('routing', {})}")
        aprendido = playbook.get("learned", {})
        if aprendido.get("licoes"):
            print("lições:")
            for l in aprendido["licoes"]:
                print(f"  - {l}")
        if aprendido.get("validacoes_comuns"):
            print("validações comuns:")
            for v in aprendido["validacoes_comuns"]:
                print(f"  - {v}")
        por_agente = aprendido.get("por_agente", {})
        if por_agente:
            print("curva por agente (lições/validações/achados — trust):")
            for nome in sorted(por_agente):
                al = por_agente[nome]
                print(f"  {nome}: {len(al.get('licoes', []))} lições "
                      f"({al.get('n_licoes_confiaveis', 0)} confiáveis / "
                      f"{al.get('n_licoes_fracas', 0)} fracas) / "
                      f"{len(al.get('validacoes_comuns', []))} validações / "
                      f"{len(al.get('achados', []))} achados")
        return 0
    print(f"comando desconhecido: {comando}")
    return 1


if __name__ == "__main__":
    sys.exit(main())