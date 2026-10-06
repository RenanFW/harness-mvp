"""Fecho de avaliação da memória episódica (Evaluation, Ch19 aplicado ao
próprio aprendizado).

Mede se a memória realmente melhora execuções futuras (proxy de reuso),
identifica registros órfãos (candidatos a poda — reportado, NUNCA removido)
e gera um relatório de saúde em `memory/eval/<data>-memoria.md`.

Somente leitura: este módulo não modifica, renomeia nem remove registros. A
poda é sugerida para decisão futura (HITL), não executada.

## Métrica de "reuso" (heurística documentada — não fabricada)

Não há log persistido de buscas hoje (o harness grava *comandos* em
`logs/harness_history.json`, não buscas em `/api/memory/search`). A métrica
mede, portanto, **recuperabilidade potencial** — se consultas conhecidas
"responderiam" com a memória — e o relatório declara isso explicitamente:

- **Consulta candidata** (Q), com origem e contagem:
  1. `historico` — comandos únicos de `logs/harness_history.json` (campo
     `command`): tarefas realmente executadas; `n_misses` = nº de vezes que o
     comando apareceu no histórico. Arquivo ausente/inválido é tolerado ([]).
  2. `amostra` — keywords distintas de todos os registros episódicos:
     consultas de amostra representando os tópicos conhecidos da memória.
- **Hit de reuso (registro)**: o registro aparece em `Memory.search(q)` para
  ao menos uma consulta q de Q que NÃO seja derivada das próprias keywords do
  registro (exclusão evita o auto-hit trivial — um registro sempre "responde"
  pela própria tag). A detecção varre **todos** os registros em uma passada
  (interseção de tokens, mesma pontuação de `search()` sem truncamento): o
  veredito de reuso/órfão é sobre a memória inteira — o top-N de exibição
  (`MAX_EVAL_TOP`/`limit`) afeta apenas `reuso_por_tag` e `top_miss`, nunca a
  detecção (A1).
- **Órfão (candidato a poda)**: registro com `trust` fraca (`trust_de`) e sem
  hit de reuso — nunca recuperado pelas consultas conhecidas. Sinal extra
  `isolada_vocabularmente`: zero interseção de tokens com o restante da
  memória (tal registro jamais seria recuperado por uma consulta que casa
  outra memória). Reportado com `candidato_a_poda: true`; nada é removido.
- **Reuso por tag**: para cada keyword K — `ocorrencias` (registros com K) e
  `fracao_em_resultados` = fração dos resultados de `search(K)` cujas
  keywords contêm K (frequência com que a tag aparece nos resultados da
  consulta representativa; 0.0 sem resultados).
- **Top consultas com miss**: consultas candidatas com 0 resultados em
  `search()` — miss total indica lacuna de memória.
"""

from __future__ import annotations

import datetime
import json
import pathlib
import re

from . import config
from .memory import Memory, recuperavel_de, trust_de

# Documentação da heurística embutida no próprio relatório (honestidade:
# o consumidor do JSON sabe exatamente o que "reuso" significa).
_METRICA = (
    "Reuso = recuperabilidade potencial (proxy, não uso real): não há log "
    "persistido de buscas hoje, então medimos se consultas conhecidas "
    "(comandos do histórico de execução + keywords de amostra) recuperariam "
    "cada registro via Memory.search(). A detecção de hit/órfão varre TODOS "
    "os registros em uma passada (interseção de tokens, mesma pontuação de "
    "search() sem truncamento) — o limite de exibição (MAX_EVAL_TOP/limit) "
    "afeta apenas as seções 'Reuso por tag' e 'Top consultas com miss', "
    "nunca o veredito de reuso/órfão: reuso é medido sobre TODOS os "
    "registros, não sobre um top-N. Um registro tem hit de reuso se aparece "
    "nos resultados de ao menos uma consulta candidata que não é derivada "
    "das próprias keywords dele (evita auto-hit trivial). Órfão = trust "
    "fraca e sem hit. Nenhum registro é alterado ou removido."
)


# ------------------------------------------------------------- helpers básicos

def _tokens(text: str) -> set[str]:
    """Tokens de busca: palavras alfanuméricas >= 3 chars, minúsculas (mesma
    regra do RAG de `memory.py` — reimplementada aqui para não depender de
    função privada do módulo vizinho)."""
    return {w for w in re.findall(r"[a-z0-9]{3,}", text.lower())}


def _keywords(rec: dict) -> list[str]:
    """Keywords de um registro (frontmatter `keywords: [a, b]`), normalizadas:
    sem colchetes, sem aspas simples/duplas literais e minúsculas (A7 — o
    frontmatter real usa `["seguranca", ...]`; sem o strip de aspas a tag
    viraria `'"seguranca"'` no relatório)."""
    texto = rec["meta"].get("keywords", "").strip("[]")
    return [
        k.strip().strip("'\"").lower()
        for k in texto.split(",")
        if k.strip().strip("'\"")
    ]


# ------------------------------------------------------------- fontes de consulta

# Status que indicam TENTATIVA REAL de execução (A4): comandos bloqueados
# nunca rodaram (não "tentaram" a memória) e `started` é registro pré-execução
# (todo job que termina grava depois `finished`/`error`/`retry`; incluí-lo
# dobraria a contagem). Comandos sem status conhecido também não contam —
# conservador: sem evidência de execução, não vira miss de memória.
_STATUS_EXECUTADO = frozenset({"finished", "error", "retry", "completed", "ok"})


def _comandos_do_historico(history_file: pathlib.Path) -> list[str]:
    """Comandos de `logs/harness_history.json` (campo `command`) com status de
    execução real (`_STATUS_EXECUTADO` — `blocked`/`started`/desconhecido são
    ignorados), COM repetições (a contagem alimenta `n_misses`). Arquivo
    ausente, JSON inválido ou formato inesperado -> [] — nunca quebra."""
    try:
        if not history_file.exists():
            return []
        dados = json.loads(history_file.read_text(encoding="utf-8"))
    except (OSError, json.JSONDecodeError):
        return []
    if not isinstance(dados, list):
        return []
    comandos = []
    for item in dados:
        if not isinstance(item, dict):
            continue
        cmd = item.get("command")
        status = str(item.get("status") or "").strip().lower()
        if isinstance(cmd, str) and cmd.strip() and status in _STATUS_EXECUTADO:
            comandos.append(cmd.strip())
    return comandos


def _consultas_candidatas(history_file: pathlib.Path,
                          records: list[dict]) -> list[tuple[str, str, int]]:
    """Consultas candidatas como `(q, origem, n_ocorrencias)`:
    - `("historico", n)`: comandos únicos do histórico (n = vezes no log);
    - `("amostra", 1)`: keywords distintas de todos os registros episódicos.
    Consultas SEM tokens (`_tokens(q)` vazio — ex.: `cd`, `ls`) são puladas
    (A5): `Memory.search()` devolve [] para elas, o que geraria miss falsa no
    `top_miss` e nunca poderia gerar hit — não são lacunas de memória.
    Ordem determinística (histórico primeiro, depois amostra na ordem dos
    registros); primeira aparição mantida quando há duplicata."""
    comandos = _comandos_do_historico(history_file)
    ocorrencias: dict[str, int] = {}
    for cmd in comandos:
        ocorrencias[cmd] = ocorrencias.get(cmd, 0) + 1
    consultas: list[tuple[str, str, int]] = []
    vistos: set[str] = set()
    for q, n in ocorrencias.items():
        vistos.add(q)
        if _tokens(q):
            consultas.append((q, "historico", n))
    for rec in records:
        for kw in _keywords(rec):
            if kw not in vistos:
                vistos.add(kw)
                if _tokens(kw):
                    consultas.append((kw, "amostra", 1))
    return consultas


# ------------------------------------------------------------------ reuso (hit)

def _hits_por_consulta(records: list[dict],
                       consultas: list[tuple[str, str, int]]) -> dict[str, set[str]]:
    """Hit (sem top-N) de cada consulta: UMA varredura sobre TODOS os
    registros, computando a mesma pontuação de `Memory.search()` (keywords 3,
    título 2, corpo 1); hit = score > 0. Equivale a `search(q)` sem
    truncamento — a detecção de reuso/órfão NUNCA depende de top-N (A1):
    o limite de exibição (`MAX_EVAL_TOP`/`limit`) não pode transformar um
    registro recuperável em órfão.

    M2 — PARIDADE com `Memory.search`: registros NÃO-recuperáveis
    (`recuperavel: false`, ex.: alerta de fabricação) são EXCLUÍDOS da
    varredura, exatamente como `Memory.search` os exclui sempre. Sem este
    filtro, um alerta de fabricação poderia contar como "reutilizado"."""
    pares = [(q, _tokens(q)) for (q, _o, _n) in consultas]
    hits: dict[str, set[str]] = {q: set() for (q, _o, _n) in consultas}
    for rec in records:
        meta = rec["meta"]
        # Não-recuperável nunca é candidato (paridade com Memory.search).
        if not recuperavel_de(meta):
            continue
        kw = _tokens(meta.get("keywords", "").strip("[]"))
        body = rec["body"]
        titulo = ""
        for linha in body.splitlines():
            if linha.startswith("# "):
                titulo = linha.lstrip("# ").strip()
                break
        titulo = _tokens(titulo)
        corpo = _tokens(body)
        for q, qtokens in pares:
            score = (3 * len(qtokens & kw)
                     + 2 * len(qtokens & titulo)
                     + 1 * len(qtokens & corpo))
            if score:
                hits[q].add(rec["id"])
    return hits


def _reuso_por_registro(records: list[dict],
                        consultas: list[tuple[str, str, int]],
                        hits: dict[str, set[str]]) -> dict[str, bool]:
    """Hit de reuso por record_id: True se o registro está no conjunto de hits
    de alguma consulta candidata q que NÃO seja derivada das próprias
    keywords do registro (auto-hit trivial excluído). `hits` vem de
    `_hits_por_consulta` — varredura completa, sem top-N (A1)."""
    reuso: dict[str, bool] = {}
    for rec in records:
        rec_id = rec["id"]
        proprias = set(_keywords(rec))
        reuso[rec_id] = False
        for q, origem, _n in consultas:
            if origem == "amostra" and q in proprias:
                continue  # consulta derivada da própria tag -> trivial
            if rec_id in hits.get(q, ()):
                reuso[rec_id] = True
                break
    return reuso


def _isolada_vocabularmente(rec: dict, records: list[dict]) -> bool:
    """True se o registro não compartilha NENHUM token com o restante da
    memória — só seria recuperado por uma consulta que casasse ele próprio."""
    tokens = _tokens(rec["meta"].get("keywords", "") + " " + rec["body"])
    outros: set[str] = set()
    for outro in records:
        if outro["id"] == rec["id"]:
            continue
        outros |= _tokens(outro["meta"].get("keywords", "") + " " + outro["body"])
    return bool(tokens) and not (tokens & outros)


# ------------------------------------------------------------------ reuso (tag)

def _reuso_por_tag(records: list[dict],
                   resultados: dict[str, list[dict]],
                   limit: int) -> list[dict]:
    """Por keyword: ocorrências, resultados da consulta representativa e
    fração dos resultados com a tag nas keywords (frequência de aparição).
    `limit` é apenas EXIBIÇÃO (top-N das tags); `limit<=0` = sem topo (lista
    completa — A8/P7)."""
    tags: dict[str, dict] = {}
    for rec in records:
        for kw in _keywords(rec):
            t = tags.setdefault(
                kw,
                {"tag": kw, "ocorrencias": 0, "resultados": 0,
                 "com_tag": 0, "fracao_em_resultados": 0.0},
            )
            t["ocorrencias"] += 1
    for kw, t in tags.items():
        items = resultados.get(kw) or []
        t["resultados"] = len(items)
        t["com_tag"] = sum(1 for it in items if kw in _keywords(it))
        if t["resultados"]:
            t["fracao_em_resultados"] = round(t["com_tag"] / t["resultados"], 3)
    ranked = sorted(tags.values(), key=lambda t: (-t["ocorrencias"], t["tag"]))
    return ranked[:limit] if limit else ranked


# -------------------------------------------------------------------- relatório

def gerar_relatorio(memory: Memory, history_file: pathlib.Path | None = None,
                    limit: int | None = None) -> dict:
    """Relatório estruturado (dict serializável em JSON) da saúde da memória
    episódica. Somente leitura — nenhum registro é alterado.

    Parâmetros:
      memory        — instância de `harness.memory.Memory` (RAG episódico).
      history_file  — caminho do histórico de comandos; padrão
                      `config.HISTORY_FILE`; ausente/inválido é tolerado.
      limit         — top-N de EXIBIÇÃO (`reuso_por_tag` e `top_miss`);
                      padrão `config.MAX_EVAL_TOP`; `<=0` = sem topo
                      (ilimitado). A detecção de reuso/órfão NÃO usa este
                      limite (A1): ela varre TODOS os registros em uma
                      passada, por interseção de tokens com a mesma pontuação
                      de `Memory.search()` sem truncamento — o veredito de
                      órfão é sobre a memória inteira, nunca sobre um top-N.

    Chaves do retorno: `resumo`, `trust_dist`, `reuso_por_tag`, `orfaos`,
    `top_miss`, `sugestoes` (+ `metrica`, `gerado_em`, `consultas_avaliadas`).
    """
    history_file = pathlib.Path(history_file) if history_file else config.HISTORY_FILE
    # A8: `limit or MAX_EVAL_TOP` foi removido de propósito — 0 significa "sem
    # topo", nunca "vira o padrão 10". Só `None` (não informado) usa o padrão.
    # P7: `limit <= 0` (ex.: -1) = ilimitado (sem corte). Antes um limit
    # NEGATIVO truncava errado (`ranked[:-1]`/`top_miss[:-1]`); agora qualquer
    # valor <= 0 vira 0 ("sem topo"), consistente com o comportamento de
    # `limit=0`. O top-N de exibição usa `if limit` (0 = lista completa) e
    # `busca_limit` já trata <=0 como "toda a memória".
    # (`limit` já foi resolvido acima — a cláusula `is not None` era morta.)
    limit = config.MAX_EVAL_TOP if limit is None else limit
    if limit <= 0:
        limit = 0

    records = memory.list_records()
    consultas = _consultas_candidatas(history_file, records)

    # (A1) Hit/reuso: varredura ÚNICA sobre todos os registros (sem top-N).
    hits = _hits_por_consulta(records, consultas)
    reuso = _reuso_por_registro(records, consultas, hits)

    # Exibição (reuso_por_tag / top_miss): busca com top-N de exibição;
    # `limit=0` (sem topo) usa len(records) — search() com limit=0 retornaria
    # [] (Memory.search faz scored[:0]).
    busca_limit = limit if limit > 0 else max(len(records), 1)
    resultados = {q: memory.search(q, limit=busca_limit) for (q, _o, _n) in consultas}

    # (b) órfãos de memória: trust fraca sem reuso -> candidato a poda
    orfaos: list[dict] = []
    for rec in records:
        if trust_de(rec["meta"]) == "fraca" and not reuso[rec["id"]]:
            orfaos.append({
                "id": rec["id"],
                "trust": "fraca",
                "keywords": _keywords(rec),
                "data": rec["meta"].get("data", ""),
                "candidato_a_poda": True,
                "isolada_vocabularmente": _isolada_vocabularmente(rec, records),
            })

    # (d) distribuição por trust e status
    por_trust: dict[str, int] = {}
    por_status: dict[str, int] = {}
    for rec in records:
        por_trust[trust_de(rec["meta"])] = por_trust.get(trust_de(rec["meta"]), 0) + 1
        status = rec["meta"].get("status") or "desconhecido"
        por_status[status] = por_status.get(status, 0) + 1

    # (c) top consultas com miss (0 resultados em search()) — exibição com
    # top-N; consultas sem tokens já foram puladas em _consultas_candidatas.
    top_miss = [
        {"consulta": q, "n_misses": n, "origem": origem}
        for (q, origem, n) in consultas
        if not resultados[q]
    ]
    top_miss.sort(key=lambda m: (-m["n_misses"], m["consulta"]))
    top_miss = top_miss[:limit] if limit else top_miss

    # (e) resumo geral + sugestões
    total = len(records)
    com_reuso = sum(1 for r in records if reuso[r["id"]])
    taxa_reuso = round(com_reuso / total, 3) if total else 0.0
    n_orfaos = len(orfaos)
    tags = _reuso_por_tag(records, resultados, limit)

    sugestoes: list[str] = []
    if total == 0:
        sugestoes.append("memória episódica vazia — nada a avaliar")
    else:
        if n_orfaos:
            sugestoes.append(
                f"{n_orfaos} registro(s) fraco(s) órfão(s) candidato(s) a poda "
                "(revisão humana/HITL; este módulo é somente leitura)"
            )
        if top_miss:
            sugestoes.append(
                f"{len(top_miss)} consulta(s) com miss total — lacuna(s) de "
                "memória (ver 'Top consultas com miss')"
            )
        dominante = tags[0] if tags else None
        if dominante and dominante["ocorrencias"] / total >= 0.3:
            sugestoes.append(
                f"tag '{dominante['tag']}' domina a memória "
                f"({dominante['ocorrencias']}/{total}) — considerar granularidade"
            )
        if not sugestoes:
            sugestoes.append("memória saudável: sem órfãos fracos e sem misses")

    return {
        "gerado_em": datetime.datetime.now().isoformat(timespec="seconds"),
        "metrica": _METRICA,
        "consultas_avaliadas": len(consultas),
        "resumo": {
            "total_registros": total,
            "com_reuso": com_reuso,
            "sem_reuso": total - com_reuso,
            "orfaos": n_orfaos,
            "taxa_reuso": taxa_reuso,
        },
        "trust_dist": {"por_trust": por_trust, "por_status": por_status},
        "reuso_por_tag": tags,
        "orfaos": orfaos,
        "top_miss": top_miss,
        "sugestoes": sugestoes,
    }


# ------------------------------------------------------------------- markdown

def _render_markdown(r: dict) -> str:
    """Renderiza o relatório em Markdown legível (seções fixas)."""
    L: list[str] = []
    L.append(f"# Relatório de Saúde da Memória — {r['gerado_em']}")
    L.append("")
    L.append("_Fecho de avaliação da memória episódica (Evaluation, Ch19). "
             "Somente leitura: nenhum registro é alterado ou removido._")
    L.append("")

    res = r["resumo"]
    L.append("## Resumo")
    L.append("")
    L.append(f"- Total de registros: {res['total_registros']}")
    L.append(f"- Com reuso (hit): {res['com_reuso']}")
    L.append(f"- Sem reuso: {res['sem_reuso']}")
    L.append(f"- Órfãos fracos (candidatos a poda): {res['orfaos']}")
    L.append(f"- Taxa de reuso: {res['taxa_reuso']}")
    L.append(f"- Consultas avaliadas: {r['consultas_avaliadas']}")
    L.append("")

    L.append("## Métrica")
    L.append("")
    L.append(r["metrica"])
    L.append("")

    L.append("## Distribuição por trust")
    L.append("")
    L.append("| Trust | Registros |")
    L.append("| --- | --- |")
    for trust, n in sorted(r["trust_dist"]["por_trust"].items()):
        L.append(f"| {trust} | {n} |")
    L.append("")
    L.append("| Status | Registros |")
    L.append("| --- | --- |")
    for status, n in sorted(r["trust_dist"]["por_status"].items()):
        L.append(f"| {status} | {n} |")
    L.append("")

    L.append("## Reuso por tag")
    L.append("")
    if not r["reuso_por_tag"]:
        L.append("Nenhuma tag.")
    else:
        L.append("| Tag | Ocorrências | Resultados | Com a tag | Fracao em resultados |")
        L.append("| --- | --- | --- | --- | --- |")
        for t in r["reuso_por_tag"]:
            L.append(
                f"| {t['tag']} | {t['ocorrencias']} | {t['resultados']} "
                f"| {t['com_tag']} | {t['fracao_em_resultados']} |"
            )
    L.append("")

    L.append("## Órfãos candidatos a poda")
    L.append("")
    if not r["orfaos"]:
        L.append("Nenhum (nenhum registro fraco sem reuso).")
    else:
        L.append("Registros com trust fraca nunca recuperados pelas consultas "
                 "conhecidas — reportados para decisão futura (HITL).")
        L.append("")
        L.append("| Id | Data | Keywords | Isolada vocabularmente |")
        L.append("| --- | --- | --- | --- |")
        for o in r["orfaos"]:
            L.append(
                f"| {o['id']} | {o['data']} | {', '.join(o['keywords']) or '-'} "
                f"| {'sim' if o['isolada_vocabularmente'] else 'não'} |"
            )
    L.append("")

    L.append("## Top consultas com miss")
    L.append("")
    if not r["top_miss"]:
        L.append("Nenhuma consulta com 0 resultados.")
    else:
        L.append("| Consulta | Misses | Origem |")
        L.append("| --- | --- | --- |")
        for m in r["top_miss"]:
            L.append(f"| `{m['consulta']}` | {m['n_misses']} | {m['origem']} |")
    L.append("")

    L.append("## Sugestões")
    L.append("")
    for s in r["sugestoes"]:
        L.append(f"- {s}")
    L.append("")
    return "\n".join(L)


def salvar_relatorio(memory: Memory, destino: pathlib.Path,
                     relatorio: dict | None = None) -> pathlib.Path:
    """Grava o relatório em `<destino>/<data>-memoria.md` (Markdown legível)
    e retorna o caminho. `destino` típico: `config.EVAL_DIR` (`memory/eval/`).
    `relatorio` — dict já gerado por `gerar_relatorio()`; se None, é gerado
    aqui. O chamador que JÁ tem o relatório deve passá-lo (A6): evita gerar
    o relatório duas vezes no mesmo request. Somente leitura da memória —
    grava apenas o relatório.
    """
    destino = pathlib.Path(destino)
    destino.mkdir(parents=True, exist_ok=True)
    if relatorio is None:
        relatorio = gerar_relatorio(memory)
    arquivo = destino / f"{datetime.date.today().isoformat()}-memoria.md"
    arquivo.write_text(_render_markdown(relatorio), encoding="utf-8")
    return arquivo
