"""Observabilidade do hub (Item 6) — métricas de EVOLUÇÃO do aprendizado.

Agrega dados que JÁ existem em três fontes somente-leitura e expõe um
panorama estruturado (serializável em JSON) da evolução do aprendizado do
hub:

- `memory/episodic/*.md` — registros episódicos (via `Memory().list_records()`);
- `logs/harness_history.json` — histórico de comandos executados;
- `memory/agents/playbook.json` — playbook compilado (via `load_playbook()`,
  passado como parâmetro — este módulo não chama `agents` diretamente).

Não substitui `harness/eval_memory.py` (saúde da memória: reuso/órfãos):
este módulo mede a EVOLUÇÃO do aprendizado do HUB — quantas tarefas foram
processadas, quantas resolveram direto via memória vs. delegadas, atividade
ao longo do tempo (proxy), re-trabalho de revisão e a evolução do playbook.

Somente leitura: nenhum registro, histórico ou playbook é alterado.

## Heurísticas (documentadas no próprio panorama — honestidade)

- **Direto vs. delegado** (proxy, não uso real): não há log persistido de
  delegação hoje; inferimos a partir do rótulo de agente e do corpo do
  registro. Um registro é **delegado** se o rótulo `agente` menciona
  `implementer` ou `reviewer` (multi-tag, ex.: `hub/implementer/reviewer`),
  OU o corpo/keywords mencionam `EM_IMPLEMENTACAO`/`EM_REVISAO` ou
  `implementer`/`reviewer`. Caso contrário (hub/brain/web/pipeline sem
  etapas de implementação/revisão — ex.: consulta respondida direto da
  memória) é **direto**.
- **Tempo médio** (proxy de atividade): não há timestamps de início/fim
  persistidos; a métrica é a atividade diária — média de registros por dia
  ativo, calculada da distribuição por `data`.
- **Re-trabalho** (proxy): a presença da sub-seção `achados_da_revisao` no
  Resultado de um registro indica que houve achados de revisão naquela
  execução. Não distinguimos "corrigidos" de "não bloqueantes" — contamos a
  ocorrência do marcador (e os bullets sob ele). A curva por agente vem do
  playbook quando disponível (`n_achados` = achados distintos agregados);
  sem playbook, é derivada dos episódios (`n_achados` = nº de registros do
  agente com a seção `achados_da_revisao`) — semântica documentada na chave.
  A chave `retrabalho.fonte_curva` indica a fonte usada para a curva por
  agente: `"playbook"` ou `"episodios"`.
"""

from __future__ import annotations

import datetime
import json
import pathlib
import re

from . import config
from .memory import trust_de

# Documentação da heurística embutida no próprio panorama (honestidade: o
# consumidor do JSON sabe exatamente o que cada métrica significa e que são
# proxies, não medições reais).
_METRICA = (
    "Métricas de EVOLUÇÃO do aprendizado do hub, agregadas de três fontes "
    "somente-leitura (episódicos, histórico de comandos e playbook). "
    "Heurísticas (proxies documentados, não uso real): "
    "(1) direto vs. delegado — não há log persistido de delegação; "
    "'delegado' = rótulo de agente menciona implementer/reviewer (multi-tag) "
    "OU corpo/keywords mencionam EM_IMPLEMENTACAO/EM_REVISAO ou "
    "implementer/reviewer; 'direto' = sem essas marcas (ex.: consulta "
    "respondida direto da memória). "
    "(2) tempo — sem timestamps de início/fim; proxy = atividade diária "
    "(média de registros por dia ativo, da distribuição por data). "
    "(3) re-trabalho — registro com a sub-seção 'achados_da_revisao' no "
    "Resultado conta como execução com achados de revisão; não distinguimos "
    "corrigidos de não-bloqueantes. "
    "A curva por agente ('retrabalho.por_agente') vem do playbook quando "
    "disponível ('retrabalho.fonte_curva: playbook', n_achados = achados "
    "distintos agregados no playbook); sem playbook, é derivada dos "
    "episódios ('retrabalho.fonte_curva: episodios', n_achados = nº de "
    "registros do agente com a seção 'achados_da_revisao'). "
    "A distribuição por agente conta CADA menção no rótulo multi-tag "
    "(hub/implementer/reviewer soma 1 para cada um). "
    "Nenhum registro, histórico ou playbook é alterado."
)

# Separadores de rótulo composto de agente (multi-tag): "hub+reviewer+implementer",
# "documenter/hub", "implementer, web" -> cada menção conta para TODOS os
# agentes citados (mesma regra de `harness/agents.py` — reimplementada aqui
# para o observability não depender de função privada do módulo vizinho).
_AGENTE_SEP_RE = re.compile(r"[\s+/+,]+")


def _agentes_de(rotulo: str) -> list[str]:
    """Normaliza o rótulo de agente de um registro numa lista de agentes
    individuais. Rótulos compostos são separados por `+`, `/`, `,` ou espaço.
    Retorna [] se o rótulo for vazio/ausente."""
    if not rotulo:
        return []
    partes = (p.strip().lower() for p in _AGENTE_SEP_RE.split(rotulo))
    return [p for p in partes if p]


def _keywords_de(rec: dict) -> list[str]:
    """Keywords de um registro (frontmatter `keywords: [a, b]`), normalizadas:
    sem colchetes, sem aspas e minúsculas."""
    texto = rec.get("meta", {}).get("keywords", "").strip("[]")
    return [
        k.strip().strip("'\"").lower()
        for k in texto.split(",")
        if k.strip().strip("'\"")
    ]


def _e_delegado(rec: dict) -> bool:
    """Heurística direto vs. delegado (proxy documentado em `_METRICA`).

    Delegado = o rótulo `agente` menciona `implementer`/`reviewer`, ou o
    corpo/keywords mencionam `EM_IMPLEMENTACAO`/`EM_REVISAO` ou
    `implementer`/`reviewer`. Direto = sem essas marcas (ex.: registro de
    consulta respondida direto da memória, agente hub/brain/web)."""
    meta = rec.get("meta", {}) or {}
    corpo = rec.get("body", "") or ""
    marcadores = ("implementer", "reviewer", "em_implementacao", "em_revisao")
    if any(a in marcadores for a in _agentes_de(meta.get("agente", ""))):
        return True
    texto = (corpo + " " + " ".join(_keywords_de(rec))).lower()
    return any(m in texto for m in marcadores)


def _achados_de(rec: dict) -> tuple[bool, int]:
    """(tem_achados, n_bullets) de um registro: presença da sub-seção
    `achados_da_revisao` no Resultado e quantos itens `- ...` há sob ela."""
    linhas = (rec.get("body", "") or "").splitlines()
    achou = False
    em_achados = False
    n = 0
    for linha in linhas:
        if "achados_da_revisao" in linha.lower():
            achou = True
            em_achados = True
            continue
        if linha.strip().startswith("## "):
            em_achados = False
            continue
        if em_achados and linha.strip().startswith("- "):
            n += 1
    return achou, n


def _top_n(contagem: dict[str, int], rotulo: str) -> list[dict]:
    """Top-N (`config.MAX_OBS_LIMIT`; 0/None = sem topo) de uma contagem,
    ordenado por ocorrências desc e chave (determinístico). As DISTRIBUIÇÕES
    completas não passam por aqui — só as listas de topo/curva."""
    itens = [{"chave": k, "n": v} for k, v in contagem.items()]
    itens.sort(key=lambda x: (-x["n"], x["chave"]))
    if config.MAX_OBS_LIMIT:
        itens = itens[: config.MAX_OBS_LIMIT]
    for item in itens:
        item[rotulo] = item.pop("chave")
    return itens


def _historico(history_file: pathlib.Path) -> dict:
    """Agrega `logs/harness_history.json` (lista de {command, status, detail}):
    contagens por status normalizado. Arquivo ausente/JSON inválido/formato
    inesperado -> zerado, nunca quebra."""
    base = {
        "arquivo": str(history_file),
        "n_total": 0,
        "n_finished": 0,
        "n_error": 0,
        "n_blocked": 0,
        "n_started": 0,
        "n_outros": 0,
    }
    try:
        if not history_file.exists():
            return base
        dados = json.loads(history_file.read_text(encoding="utf-8"))
    except (OSError, json.JSONDecodeError):
        base["invalido"] = True
        return base
    if not isinstance(dados, list):
        base["invalido"] = True
        return base
    base["n_total"] = len(dados)
    for item in dados:
        if not isinstance(item, dict):
            base["n_outros"] += 1
            continue
        status = str(item.get("status") or "").strip().lower()
        if status in ("finished", "completed", "ok"):
            base["n_finished"] += 1
        elif status in ("error", "retry"):
            base["n_error"] += 1
        elif status == "blocked":
            base["n_blocked"] += 1
        elif status == "started":
            base["n_started"] += 1
        else:
            base["n_outros"] += 1
    return base


def _curva_do_playbook(playbook: dict) -> list[dict]:
    """Curva `learned.por_agente` do playbook resumida por agente:
    n_licoes (distintas), n_licoes_fracas, n_licoes_confiaveis,
    n_validacoes e n_achados (distintos agregados). Ordenada por achados
    desc (onde há mais re-trabalho/atenção primeiro), top-N configurado."""
    learned = playbook.get("learned")
    if not isinstance(learned, dict):
        return []
    raw = learned.get("por_agente")
    if not isinstance(raw, dict):
        return []
    itens: list[dict] = []
    for nome in sorted(raw):
        al = raw[nome] or {}
        itens.append({
            "agente": nome,
            "n_licoes": len(al.get("licoes") or []),
            "n_licoes_fracas": int(al.get("n_licoes_fracas", 0) or 0),
            "n_licoes_confiaveis": int(al.get("n_licoes_confiaveis", 0) or 0),
            "n_validacoes": len(al.get("validacoes_comuns") or []),
            "n_achados": len(al.get("achados") or []),
        })
    itens.sort(key=lambda x: (-x["n_achados"], x["agente"]))
    if config.MAX_OBS_LIMIT:
        itens = itens[: config.MAX_OBS_LIMIT]
    return itens


def _retrabalho_dos_episodios(records: list[dict]) -> list[dict]:
    """Fallback sem playbook: por agente (multi-tag), nº de registros e nº de
    registros com a seção `achados_da_revisao` (re-trabalho). Semântica
    documentada na chave — diferente do `n_achados` do playbook (distintos
    agregados)."""
    cont: dict[str, dict] = {}
    for rec in records:
        tem, _n = _achados_de(rec)
        for nome in _agentes_de(rec.get("meta", {}).get("agente", "")):
            b = cont.setdefault(nome, {
                "agente": nome,
                "n_registros": 0,
                "n_achados": 0,
            })
            b["n_registros"] += 1
            if tem:
                b["n_achados"] += 1
    itens = sorted(cont.values(), key=lambda x: (-x["n_achados"], x["agente"]))
    if config.MAX_OBS_LIMIT:
        itens = itens[: config.MAX_OBS_LIMIT]
    return itens


def _sugestoes(*, total: int, por_trust: dict, n_delegados: int,
               com_achados: int, historico: dict,
               playbook_info: dict, curva: list[dict]) -> list[str]:
    """Sugestões objetivas derivadas do panorama (nunca opiniões)."""
    s: list[str] = []
    if total == 0:
        s.append("memória episódica vazia — sem dados para evolução do aprendizado")
    else:
        fracas = por_trust.get("fraca", 0)
        if fracas:
            s.append(
                f"{fracas} registro(s) com trust fraca — candidato(s) a revisão "
                "(memória não tratada como conhecimento confirmado)"
            )
        if n_delegados == 0:
            s.append(
                "nenhum registro delegado detectado — a heurística direto/"
                "delegado pode subestimar delegação (sem rótulo nem etapas "
                "EM_IMPLEMENTACAO/EM_REVISAO)"
            )
        if com_achados:
            s.append(
                f"{com_achados} registro(s) com achados de revisão — indicador "
                "de re-trabalho (ver 'retrabalho')"
            )
        if playbook_info.get("existe") and curva:
            pior = max(curva, key=lambda x: (x["n_licoes_fracas"], x["agente"]))
            if pior["n_licoes_fracas"]:
                s.append(
                    f"agente {pior['agente']} com {pior['n_licoes_fracas']} "
                    "lição(ões) fraca(s) no playbook — avaliar"
                )
    if historico.get("n_blocked"):
        s.append(
            f"{historico['n_blocked']} comando(s) bloqueado(s) no histórico — "
            "revisar uso/políticas"
        )
    if historico.get("n_error"):
        s.append(f"{historico['n_error']} comando(s) com erro no histórico")
    return s


def _playbook_info(playbook) -> dict:
    """Resumo do playbook (se passado): schema_version, data, nº de agentes e
    a curva por agente (`learned.por_agente`). `existe: False` se ausente ou
    inválido — o panorama nunca quebra sem playbook."""
    if not isinstance(playbook, dict) or not playbook:
        return {"existe": False, "por_agente": []}
    learned = playbook.get("learned")
    if not isinstance(learned, dict):
        learned = {}
    curva = _curva_do_playbook(playbook)
    return {
        "existe": True,
        "schema_version": str(playbook.get("schema_version", "") or ""),
        "data": str(playbook.get("data", "") or ""),
        "n_agentes": len(playbook.get("agents") or {}),
        "n_episodios_playbook": len(learned.get("episodes") or []),
        "n_licoes_playbook": len(learned.get("licoes") or []),
        "n_validacoes_comuns": len(learned.get("validacoes_comuns") or []),
        "n_block_reasons": len(learned.get("block_reasons") or []),
        "por_agente": curva,
    }


def _saude_segura(memory, playbook, history_file) -> dict:
    """Saúde determinística do sistema (Etapa 3, `harness/health.py`) exposta
    no panorama de forma TOLERANTE. O import é TARDIO (dentro da função) para
    evitar circularidade entre `observability` e `health`; qualquer falha vira
    saúde NEUTRA coerente (`health.saude_neutra`: 0.5/DEGRADADO) — o panorama
    nunca quebra por causa do detector."""
    try:
        from . import health
    except Exception:  # noqa: BLE001 — import tardio falhou: neutro coerente
        return {
            "score": 0.5,
            "nivel": "DEGRADADO",
            "fatores": {},
            "metrica": "saúde indisponível (falha tolerada no import) — neutra",
        }
    try:
        return health.gerar_saude(
            memory=memory, playbook=playbook, history_file=history_file
        )
    except Exception:  # noqa: BLE001 — detector jamais derruba o panorama
        return health.saude_neutra("falha tolerada no detector")


def gerar_panorama(memory, playbook=None, history_file=None) -> dict:
    """Panorama estruturado (serializável em JSON) da EVOLUÇÃO do aprendizado
    do hub. Somente leitura — nenhuma fonte é alterada.

    Parâmetros:
      memory        — instância de `harness.memory.Memory` (registros
                      episódicos via `list_records()`).
      playbook      — dict de `harness.agents.load_playbook()` (ou None);
                      tolerado ausente/inválido.
      history_file  — caminho de `logs/harness_history.json`; padrão
                      `config.HISTORY_FILE`; ausente/inválido é tolerado.

    Chaves do retorno: `gerado_em`, `metrica`, `resumo_geral`, `top`,
    `resolucao`, `tempo`, `retrabalho` (inclui `fonte_curva`: `"playbook"` ou
    `"episodios"`), `playbook`, `historico`, `fontes`, `sugestoes` e `saude`
    (detector determinístico de degradação — Etapa 3, `harness/health.py`).
    Distribuições completas em `resumo_geral.por_*`; as listas
    de topo (`top`, `playbook.por_agente`, `retrabalho.por_agente`) levam o
    corte `config.MAX_OBS_LIMIT`.
    """
    history_file = pathlib.Path(history_file) if history_file else config.HISTORY_FILE
    records = memory.list_records()

    # (a) resumo geral: total + distribuições por status/trust/agente/data
    por_status: dict[str, int] = {}
    por_trust: dict[str, int] = {}
    por_agente: dict[str, int] = {}
    por_data: dict[str, int] = {}
    for rec in records:
        meta = rec.get("meta", {}) or {}
        status = str(meta.get("status") or "").strip() or "desconhecido"
        por_status[status] = por_status.get(status, 0) + 1
        t = trust_de(meta)
        por_trust[t] = por_trust.get(t, 0) + 1
        for nome in _agentes_de(meta.get("agente", "")):
            por_agente[nome] = por_agente.get(nome, 0) + 1
        data = str(meta.get("data") or "").strip() or "sem-data"
        por_data[data] = por_data.get(data, 0) + 1
    total = len(records)

    # (b) resolução de tarefas (direta via memória vs. delegada) — heurística
    n_delegados = sum(1 for rec in records if _e_delegado(rec))
    n_diretos = total - n_delegados
    taxa_delegacao = round(n_delegados / total, 3) if total else 0.0
    taxa_direta = round(n_diretos / total, 3) if total else 0.0

    # (c) tempo (proxy de atividade diária — sem timestamps de início/fim)
    n_dias_ativos = len(por_data)
    media_por_dia = round(total / n_dias_ativos, 2) if n_dias_ativos else 0.0

    # (d) re-trabalho: registros com achados de revisão + curva por agente
    com_achados = 0
    n_achados_totais = 0
    for rec in records:
        tem, n = _achados_de(rec)
        if tem:
            com_achados += 1
            n_achados_totais += n
    sem_achados = total - com_achados
    taxa_com_achados = round(com_achados / total, 3) if total else 0.0

    # (e) evolução do playbook
    playbook_info = _playbook_info(playbook)
    # Curva por agente: playbook quando disponível (n_achados = achados
    # distintos agregados); fallback = derivação dos episódios (n_achados =
    # nº de registros do agente com a seção achados_da_revisao). A chave
    # `fonte_curva` marca a fonte usada (semântica do n_achados depende dela).
    if playbook_info.get("existe"):
        curva = playbook_info["por_agente"]
        fonte_curva = "playbook"
    else:
        curva = _retrabalho_dos_episodios(records)
        fonte_curva = "episodios"

    # histórico de comandos (fonte agregada)
    hist = _historico(history_file)

    # top-N de exibição (distribuições completas ficam em resumo_geral)
    top = {
        "agentes": _top_n(por_agente, "agente"),
        "dias": _top_n(por_data, "dia"),
    }

    sugestoes = _sugestoes(
        total=total,
        por_trust=por_trust,
        n_delegados=n_delegados,
        com_achados=com_achados,
        historico=hist,
        playbook_info=playbook_info,
        curva=curva,
    )

    return {
        "gerado_em": datetime.datetime.now().isoformat(timespec="seconds"),
        "metrica": _METRICA,
        "resumo_geral": {
            "total_registros": total,
            "por_status": por_status,
            "por_trust": por_trust,
            "por_agente": por_agente,
            "por_data": por_data,
        },
        "top": top,
        "resolucao": {
            "n_diretos": n_diretos,
            "n_delegados": n_delegados,
            "taxa_delegacao": taxa_delegacao,
            "taxa_direta": taxa_direta,
            "heuristica": (
                "delegado = rótulo de agente menciona implementer/reviewer "
                "(multi-tag) OU corpo/keywords mencionam EM_IMPLEMENTACAO/"
                "EM_REVISAO ou implementer/reviewer; direto = sem essas marcas "
                "(ex.: consulta respondida direto da memória). Proxy: não há "
                "log persistido de delegação (ver 'metrica')."
            ),
        },
        "tempo": {
            "proxy": (
                "atividade diária — sem timestamps de início/fim persistidos; "
                "média de registros por dia ativo (distribuição por data)"
            ),
            "n_dias_ativos": n_dias_ativos,
            "media_registros_por_dia": media_por_dia,
        },
        "retrabalho": {
            "com_achados": com_achados,
            "sem_achados": sem_achados,
            "taxa_com_achados": taxa_com_achados,
            "n_achados_totais": n_achados_totais,
            "heuristica": (
                "registro com a sub-seção 'achados_da_revisao' no Resultado "
                "conta como execução com achados de revisão; n_achados_totais "
                "conta os bullets sob o marcador. Não distinguimos corrigidos "
                "de não-bloqueantes (ver 'metrica'). "
                f"fonte_curva: {fonte_curva} — n_achados da curva por agente "
                "significa 'achados distintos agregados' quando a fonte é o "
                "playbook, ou 'nº de registros do agente com a seção "
                "achados_da_revisao' quando é derivada dos episódios."
            ),
            "fonte_curva": fonte_curva,
            "por_agente": curva,
        },
        "playbook": playbook_info,
        "historico": hist,
        "fontes": {
            "episodios": total,
            "historico": hist["n_total"],
            "playbook": playbook_info["existe"],
        },
        "saude": _saude_segura(memory, playbook, history_file),
        "sugestoes": sugestoes,
    }