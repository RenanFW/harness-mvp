"""RAG interno sobre referências (Item 7) — capacidade consultiva do harness.

Dado um tema/contexto (query), busca as referências validadas relevantes em
`memory/references/`, filtra por trust e produz uma **síntese estruturada** do
conhecimento — respondendo como aquilo se aplica ao harness — citando as
referências de origem. Capacidade consultiva **sem inventar conteúdo**: a
síntese é determinística (stdlib-only), montada a partir das seções reais das
referências; não gera texto além do que está nelas.

Trust (confiança/anti-fraude de agente):
  - `alta`  — validado editorialmente (>= 2 fontes) -> fonte de conhecimento
              confirmado. É o ÚNICO trust que alimenta a síntese estruturada
              e o resumo.
  - `media` — conhecimento indireto por tópicos -> fonte parcial. Permanece
              DISPONÍVEL na busca (aparece em `fontes`) mas NÃO confirma
              conhecimento: não alimenta a síntese nem o resumo.
  - `fraca` — não validado / alerta de fabricação -> NUNCA apresentado como
              conhecimento confirmado (também não alimenta a síntese).
  Referências SEM o campo `trust` no frontmatter assumem `fraca` (conservador,
  consistente com a política de trust de `harness/memory.py`).

Uso:
    from harness.rag_refs import consultar
    consultar("padrao agentic design harness", limit=5, incluir_fraca=False)
"""

from __future__ import annotations

import pathlib
import re

from . import config
from . import frontmatter
from .memory import recuperavel_de as _recuperavel_de_canonico

# Seções do corpo das referências que alimentam a síntese estruturada. Cada
# chave do dict de saída mapeia para uma seção correspondente por PREFIXO
# (aproximado, com normalização de acentos/case): uma seção real casa se o
# título normalizado COMEÇA com alguma variante (ex.: `aplicacao no motor
# (semantic-cache-first, sandbox)` casa com `aplicacao no motor`). Outras
# seções (ex.: Sumário) ficam como contexto/trechos, mas não viram uma chave
# fixa da síntese.
_SECOES_SINTESE = {
    "conceitos_chave": ("conceitos-chave", "conceitos"),
    "padroes_acionaveis": ("padroes acionaveis", "padroes e regras acionaveis",
                           "padroes e regras", "padroes acionaveis (aplicaveis ao motor)"),
    "aplicacao_no_motor": ("aplicacao no motor", "aplicacao no harness",
                           "aplicacao no motor (semantic-cache-first", "aplicacao"),
    "pontos_de_atencao": ("pontos de atencao",),
}

_ACENTOS = str.maketrans(
    "áàâãäéèêëíìîïóòôõöúùûüç",
    "aaaaaeeeeiiiiooooouuuuc",
)


def _tokens(texto: str) -> set[str]:
    """Tokens de busca: palavras alfanuméricas >= 3 chars, minúsculas e sem
    acentos (heurística de `Memory.search`, com normalização de acentos para
    lidar com conteúdo em português das referências)."""
    return {w for w in re.findall(r"[a-z0-9]{3,}", texto.lower().translate(_ACENTOS))}


def _trust_de(campos: dict) -> str:
    """Trust de uma referência a partir do frontmatter; sem o campo -> fraca
    (conservador, consistente com `trust_de` de harness/memory.py)."""
    t = str(campos.get("trust") or "").strip().lower().translate(_ACENTOS)
    return t if t in config.TRUST else config.TRUST_DEFAULT


def _recuperavel_de(campos: dict) -> bool:
    """Recuperabilidade de uma referência a partir do frontmatter.

    Delega para o helper canônico `harness.memory.recuperavel_de` (mesma
    coerção do leitor episódico) — evita reimplementar a lógica e divergir.
    Chave AUSENTE/`None` -> default `True` (não perde referência antiga);
    `False`/`0` (Python) ou strings `false`/`0`/`no`/`nao`/`não` -> NÃO
    recuperável (ex.: alerta de fabricação).
    """
    return _recuperavel_de_canonico(campos)


def _parse_frontmatter(texto: str) -> dict | None:
    """Extrai os campos do bloco `--- ... ---` (id, tipo, titulo, autor, tags,
    trust, origem, ...) via parser COMPARTILHADO (harness/frontmatter.py).
    Exige id/tipo/titulo (como o webscraper). Aceita campos em qualquer ordem
    e valores com `:` interno."""
    campos, _ = frontmatter.parse(texto)
    if not campos:
        return None
    for campo in ("id", "tipo", "titulo"):
        if campo not in campos:
            return None
    return campos


def _normalizar_secao(titulo: str) -> str:
    return titulo.lower().strip().translate(_ACENTOS).strip()


def _parse_secoes(corpo: str) -> dict[str, str]:
    """Extrai o corpo por seção `## <título>`. Retorna dict título-normalizado
    (sem `## `, minúsculo, sem acentos) -> conteúdo da seção."""
    secoes: dict[str, str] = {}
    titulo_atual: str | None = None
    buffer: list[str] = []
    for linha in corpo.splitlines():
        if linha.startswith("## "):
            if titulo_atual is not None:
                secoes[titulo_atual] = "\n".join(buffer).strip()
            titulo_atual = _normalizar_secao(linha[3:])
            buffer = []
        elif titulo_atual is not None:
            buffer.append(linha)
    if titulo_atual is not None:
        secoes[titulo_atual] = "\n".join(buffer).strip()
    return secoes


def _tags_para_tokens(campos: dict) -> set[str]:
    """Tokens das tags (ex.: `[agentic, patterns, ...]`) e de keywords."""
    tags_texto = campos.get("tags", "").strip("[]")
    kw = campos.get("keywords", "")
    return _tokens(f"{tags_texto} {kw}")


def _ler_referencias(diretorio: pathlib.Path | None = None) -> list[dict]:
    """Lista/ler todas as referências em `memory/references/*.md` (exceto
    `index.md`), parseando frontmatter e seções do corpo. Tolerante a
    diretório ausente ou vazio (retorna [])."""
    diretorio = pathlib.Path(diretorio) if diretorio is not None else (
        config.MEMORY_DIR / "references")
    refs: list[dict] = []
    if not diretorio.is_dir():
        return refs
    for arquivo in sorted(diretorio.glob("*.md")):
        if arquivo.name == "index.md":
            continue
        try:
            texto = arquivo.read_text(encoding="utf-8", errors="replace")
        except OSError:
            continue
        campos = _parse_frontmatter(texto)
        if not campos:
            continue
        secoes = _parse_secoes(texto)
        refs.append({
            "id": arquivo.stem,
            "file": str(arquivo),
            "meta": campos,
            "titulo": campos.get("titulo", ""),
            "autor": campos.get("autor") or campos.get("autores") or "—",
            "trust": _trust_de(campos),
            "recuperavel": _recuperavel_de(campos),
            "origem": campos.get("origem", ""),
            "tags": campos.get("tags", ""),
            "secoes": secoes,
        })
    return refs


def _score_ref(query_tokens: set[str], ref: dict) -> int:
    """Pontua uma referência pela interseção de termos com tags (peso 3),
    título (2) e corpo/seções (1) — mesma heurística de `Memory.search`."""
    tags_tokens = _tags_para_tokens(ref["meta"])
    title_tokens = _tokens(ref["titulo"])
    corpo = "\n".join(ref["secoes"].values()) if ref["secoes"] else ""
    body_tokens = _tokens(corpo)
    return (
        3 * len(query_tokens & tags_tokens)
        + 2 * len(query_tokens & title_tokens)
        + 1 * len(query_tokens & body_tokens)
    )


def _extrair_trecho(secao: str, limite: int | None = None) -> str:
    """Trecho objetivo de uma seção (primeira linha não-vazia até o limite)."""
    limite = config.TRECHO_LIMITE if limite is None else limite
    trecho = secao.strip().splitlines()
    trecho = [l.strip().lstrip("- ") for l in trecho if l.strip()]
    if not trecho:
        return ""
    return " ".join(trecho)[:limite]


def _buscar(
    query: str,
    diretorio: pathlib.Path | None = None,
    incluir_fraca: bool = False,
    limit: int = 5,
) -> list[dict]:
    """Busca por relevância (RAG) sobre as referências, ranked por score.

    Filtro de trust: por padrão retorna apenas `alta`/`media`. Se
    `incluir_fraca=True`, inclui também `fraca` marcadas como não-confirmadas
    (para depuração). Nunca apresenta `fraca` como conhecimento confirmado.

    Dois conceitos separados por item:
      - `confirmada`/`nao_confirmada` — confiável para EXIBIÇÃO na busca
        (alta/media são confiáveis para buscar; fraca não).
      - `sintetizavel` — confiável para SÍNTESE (SOMENTE `alta`). `media` é
        consultável mas não alimenta a síntese estruturada nem o resumo.

    Referências NÃO-recuperáveis (`recuperavel: false`, ex.: alerta de
    fabricação — Item 2.1) são SEMPRE excluídas da busca, inclusive com
    `incluir_fraca=True`: nunca aparecem em `fontes` nem alimentam a síntese.
    """
    q_tokens = _tokens(query)
    if not q_tokens:
        return []

    scored: list[tuple[int, dict]] = []
    for ref in _ler_referencias(diretorio):
        # Não-recuperável nunca é candidato (fabricação explícita é tóxica).
        if not ref.get("recuperavel", config.RECUPERAVEL_DEFAULT):
            continue
        score = _score_ref(q_tokens, ref)
        if not score:
            continue
        confiavel = ref["trust"] in ("alta", "media")
        if not confiavel and not incluir_fraca:
            continue
        item = dict(ref)
        item["score"] = score
        item["confirmada"] = confiavel
        item["nao_confirmada"] = not confiavel
        # Síntese exige trust alta E recuperabilidade (defesa em profundidade).
        item["sintetizavel"] = ref["trust"] == "alta" and ref["recuperavel"]
        scored.append((score, item))

    scored.sort(key=lambda item: item[0], reverse=True)
    return [item for _score, item in scored[:limit]]


def _sintese_deterministica(fontes: list[dict]) -> dict:
    """Monta a síntese estruturada a partir das seções REAIS das referências
    top, citando a origem (id). Não inventa conteúdo: cada item é extraído da
    seção correspondente da referência.

    A síntese é alimentada APENAS por referências de trust `alta`
    (`sintetizavel=True`). Referências `media` (e `fraca`) permanecem na
    busca (`fontes`) mas NÃO confirmam conhecimento nem alimentam a síntese.
    """
    out = {
        "conceitos_chave": [],
        "padroes_acionaveis": [],
        "aplicacao_no_motor": [],
        "pontos_de_atencao": [],
    }
    for ref in fontes:
        # Só síntese a partir de referências de trust alta (sintetizavel);
        # media (consultável) e fraca (depuração) não alimentam conhecimento.
        if not ref["sintetizavel"]:
            continue
        secoes = ref["secoes"]
        for chave, variantes in _SECOES_SINTESE.items():
            for variante in variantes:
                # Matching por PREFIXO (aproximado): títulos reais podem ter
                # sufixo entre parênteses (ex.: `aplicacao no motor
                # (semantic-cache-first)`), então basta a seção normalizada
                # COMEÇAR com a variante para alimentar a síntese.
                secao_casa = next(
                    (secao for secao in secoes
                     if secao.startswith(variante) and secoes[secao].strip()),
                    None,
                )
                if secao_casa is not None:
                    out[chave].append({
                        "id": ref["id"],
                        "trust": ref["trust"],
                        "titulo": ref["titulo"],
                        "trecho": _extrair_trecho(secoes[secao_casa]),
                    })
                    break
    return out


def _montar_resumo(sintese: dict, fontes: list[dict]) -> str:
    """Parágrafo objetivo, determinístico, montado a partir dos conceitos/
    padrões extraídos — NUNCA inventa além do conteúdo das referências.

    A síntese baseia-se APENAS nas referências de trust `alta` (`sintetizavel`).
    Referências `media` presentes em `fontes` são apenas consultáveis e não
    contam como base da síntese — o resumo deixa isso explícito.
    """
    partes: list[str] = []
    n = len([f for f in fontes if f["sintetizavel"]])
    partes.append(
        f"Síntese baseada em {n} referência(s) de trust alta sobre o "
        "contexto consultado."
    )
    medias = [f for f in fontes if f["trust"] == "media"]
    if medias:
        partes.append(
            f"{len(medias)} referência(s) de trust media estão disponíveis "
            "para consulta, mas não confirmam conhecimento nem alimentam a "
            "síntese."
        )
    conceitos = sintese["conceitos_chave"]
    if conceitos:
        itens = []
        for c in conceitos[:2]:
            itens.append(f"{c['trecho']} [{c['id']}]")
        partes.append("Conceitos: " + " | ".join(itens))
    padroes = sintese["padroes_acionaveis"]
    if padroes:
        itens = []
        for p in padroes[:2]:
            itens.append(f"{p['trecho']} [{p['id']}]")
        partes.append("Padrões acionáveis: " + " | ".join(itens))
    aplicacao = sintese["aplicacao_no_motor"]
    if aplicacao:
        itens = []
        for a in aplicacao[:2]:
            itens.append(f"{a['trecho']} [{a['id']}]")
        partes.append("Aplicação no harness: " + " | ".join(itens))
    return " ".join(partes)


def consultar(
    query: str,
    limit: int = 5,
    incluir_fraca: bool = False,
    diretorio: pathlib.Path | None = None,
) -> dict:
    """Capacidade consultiva (RAG) sobre as referências validadas.

    Retorna dict com:
      - `query` (normalizada);
      - `fontes`: referências relevantes com score, id, titulo, autor, trust,
        confirmada/nao_confirmada, sintetizavel e trechos das seções;
      - `sintese`: dict estruturado com conceitos_chave, padroes_acionaveis,
        aplicacao_no_motor e pontos_de_atencao (com citação da origem `id`) —
        alimentada SOMENTE por referências de trust `alta`;
      - `resumo`: parágrafo objetivo determinístico (baseado só em `alta`);
      - `advertencias`: lista de avisos (fraca incluída; lacuna de alta/media;
        media sem base alta para síntese; query vazia).

    Política de síntese: apenas trust `alta` alimenta a síntese e o resumo.
    Referências `media` permanecem disponíveis na busca (`fontes`) e são
    consultáveis, mas NÃO confirmam conhecimento nem alimentam a síntese.

    `query` vazia ou sem termos de busca gera uma advertência e `fontes` vazio.
    """
    limit = max(1, min(int(limit), config.MAX_REF_QUERY_LIMIT))
    q_normalizada = " ".join(sorted(_tokens(query)))
    advertencias: list[str] = []

    if not q_normalizada:
        return {
            "query": q_normalizada,
            "fontes": [],
            "sintese": {
                "conceitos_chave": [],
                "padroes_acionaveis": [],
                "aplicacao_no_motor": [],
                "pontos_de_atencao": [],
            },
            "resumo": "Nenhuma busca realizada: query vazia ou sem termos de busca.",
            "advertencias": ["query vazia ou sem termos de busca"],
        }

    fontes = _buscar(query, diretorio=diretorio, incluir_fraca=incluir_fraca, limit=limit)

    if incluir_fraca:
        advertencias.append(
            "incluir_fraca=True: referências de trust fraca foram incluídas "
            "apenas para depuração e NÃO devem ser tratadas como conhecimento "
            "confirmado."
        )
    if not any(f["confirmada"] for f in fontes):
        advertencias.append(
            "nenhuma fonte de trust alta/media encontrada para a consulta — "
            "lacuna de conhecimento validado (referências sem o campo trust "
            "valem fraca por default)."
        )
    elif not any(f["sintetizavel"] for f in fontes):
        # Há material consultável (media) mas nenhuma base alta para síntese:
        # não é lacuna total, mas a síntese fica vazia/sem base alta.
        n_media = len([f for f in fontes if f["trust"] == "media"])
        advertencias.append(
            f"nenhuma referência de trust alta para a síntese — há {n_media} "
            "referência(s) de trust media consultável(eis), porém elas não "
            "confirmam conhecimento: a síntese fica vazia/sem base alta."
        )
    fracas = [f for f in fontes if not f["confirmada"]]
    if fracas:
        advertencias.append(
            f"{len(fracas)} referência(s) de trust fraca na resposta "
            "(não-confirmadas, somente para depuração)."
        )

    sintese = _sintese_deterministica(fontes)
    return {
        "query": q_normalizada,
        "fontes": fontes,
        "sintese": sintese,
        "resumo": _montar_resumo(sintese, fontes),
        "advertencias": advertencias,
    }
