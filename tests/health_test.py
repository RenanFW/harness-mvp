"""Testes do detector determinístico de degradação (Etapa 3) — `harness/health.py`.

Cobre:
  - `gerar_saude` com fontes vazias -> não crasha, score neutro, nível coerente;
  - cenário SAUDÁVEL vs DEGRADADO (episódicos/playbook/histórico temporários)
    -> níveis diferentes;
  - `verificar_contradicoes`: comando sem execução real, APROVADA com falha e
    caso limpo;
  - integração no pipeline: `saude` presente no contrato; anti-envenomamento
    pula a gravação quando DEGRADADO e grava quando SAUDÁVEL (mock de
    `harness.health.gerar_saude`); detecção de contradição no contrato.

Sem rede; usa `tempfile`/`mock`. Rode com:
    python tests/health_test.py
"""

from __future__ import annotations

import contextlib
import json
import pathlib
import sys
import tempfile
import traceback
from unittest import mock

sys.path.insert(0, str(pathlib.Path(__file__).resolve().parent.parent))

from harness import config  # noqa: E402
from harness.health import (  # noqa: E402
    gerar_saude,
    verificar_contradicoes,
)
from harness.pipeline import AgentPipeline  # noqa: E402

# Saúde neutra de referência (todos os fatores sem dados = 0.5).
_NEUTRO = 0.5


@contextlib.contextmanager
def _isolado(tmp: str):
    """Memory/histórico/playbook isolados em diretório temp (nunca toca o
    projeto real)."""
    ep = pathlib.Path(tmp) / "episodic"
    mem = pathlib.Path(tmp) / "memory"
    agents = pathlib.Path(tmp) / "agents"
    hist = pathlib.Path(tmp) / "history.json"
    with mock.patch.object(config, "EPISODIC_DIR", ep), \
         mock.patch.object(config, "INDEX_FILE", ep / "index.md"), \
         mock.patch.object(config, "MEMORY_DIR", mem), \
         mock.patch.object(config, "AGENTS_DIR", agents), \
         mock.patch.object(config, "PLAYBOOK_FILE", agents / "playbook.json"), \
         mock.patch.object(config, "HISTORY_FILE", hist):
        yield ep


def _escreve(ep, nome: str, conteudo: str) -> None:
    ep.mkdir(parents=True, exist_ok=True)
    (ep / nome).write_text(conteudo, encoding="utf-8")


def _hist(tmp: str, dados: list[dict]) -> pathlib.Path:
    caminho = pathlib.Path(tmp) / "history.json"
    caminho.write_text(json.dumps(dados), encoding="utf-8")
    return caminho


def _playbook(confiaveis: int, fracas: int) -> dict:
    """Playbook mínimo com a curva `learned.por_agente`."""
    return {
        "schema_version": "1.1",
        "data": "2026-09-11",
        "agents": {"implementer": {"name": "implementer"}},
        "learned": {
            "por_agente": {
                "implementer": {
                    "agente": "implementer",
                    "licoes": [],
                    "validacoes_comuns": [],
                    "achados": [],
                    "n_licoes_confiaveis": confiaveis,
                    "n_licoes_fracas": fracas,
                },
            },
        },
    }


_REC_SAUDAVEL = """\
---
id: reg-saudavel-2026-09-11
keywords: [implementacao, revisao]
data: 2026-09-11
agente: implementer/reviewer
status: completed
trust: alta
origem: execução validada por reviewer
validado_por: reviewer
---

# Execução delegada saudável

## Contrato de entrada
objetivo: implementar e revisar a mudança

## Fluxo
EM_IMPLEMENTACAO — implementer; EM_REVISAO — reviewer.

## Resultado
- status: completed
- validacoes_executadas:
  - pytest tests/
- achados_da_revisao:
  - achado de revisão registrado de verdade

## Contexto
- Lição com texto longo o suficiente para ser considerada válida.
"""

_REC_DEGRADADO = """\
---
id: reg-degradado-2026-09-11
keywords: [implementacao]
data: 2026-09-11
agente: implementer
status: completed
trust: fraca
origem: _não informada_
validado_por: _não informado_
---

# Execução delegada degradada

## Contrato de entrada
_não informado_

## Fluxo
_não informado_

## Resultado
- status: completed
- validacoes_executadas:
  - echo ok
  - ls

## Contexto
_não informado_
"""


# M1: episódio "ok em tudo" (contrato completo, trust alta, delegado, histórico
# limpo) cuja defesa de fachada é `echo test` e cujo `achados_da_revisao` está
# vazio. NÃO pode pontuar 1.0/SAUDAVEL: o `test` é apenas um ARGUMENTO de um
# comando trivial (não validação) e o cabeçalho sem bullets é rubber-stamp.
_REC_RUBBER_STAMP = """\
---
id: reg-rubber-2026-09-11
keywords: [implementacao, revisao]
data: 2026-09-11
agente: implementer/reviewer
status: completed
trust: alta
origem: execução validada por reviewer
validado_por: reviewer
---

# Execução delegada ok em tudo

## Contrato de entrada
objetivo: implementar e revisar a mudança
escopo: tests/

## Fluxo
EM_IMPLEMENTACAO — implementer; EM_REVISAO — reviewer.

## Resultado
- status: completed
- validacoes_executadas:
  - echo test
- achados_da_revisao:

## Contexto
- Lição com texto longo o suficiente para ser considerada válida.
"""


# M1 (re-revisão): episódio "ok em tudo" (contrato completo, trust alta,
# delegado, histórico limpo) cuja ÚNICA validação é `git status` (INSPEÇÃO, não
# validação) e com `achados_da_revisao:` VAZIO. NÃO pode pontuar SAUDAVEL: a
# inspeção git não conta como evidência substantiva (M1) e o cabeçalho vazio
# não conta como revisão real.
_REC_GIT_STATUS = """\
---
id: reg-git-2026-09-11
keywords: [implementacao, revisao]
data: 2026-09-11
agente: implementer/reviewer
status: completed
trust: alta
origem: execução validada por reviewer
validado_por: reviewer
---

# Execução com inspeção git como única validação

## Contrato de entrada
objetivo: implementar e revisar a mudança
escopo: tests/

## Fluxo
EM_IMPLEMENTACAO — implementer; EM_REVISAO — reviewer.

## Resultado
- status: completed
- validacoes_executadas:
  - git status
- achados_da_revisao:

## Contexto
- Lição com texto longo o suficiente para ser considerada válida.
"""


# ---------------------------------------------------------------------------
# Casos de gerar_saude


def _caso_a() -> bool:
    """(a) fontes vazias: não crasha; score neutro (0.5); nível COERENTE com os
    limiares de `config.py` (0.5 >= 0.35 -> DEGRADADO); todas as chaves de
    fatores e a metrica presentes."""
    with tempfile.TemporaryDirectory() as tmp:
        with _isolado(tmp) as ep:
            r = gerar_saude(
                memory=None, playbook={}, history_file=ep / "nao_existe.json"
            )
            return (
                isinstance(r, dict)
                and r["score"] == _NEUTRO
                and r["nivel"] == "DEGRADADO"
                and set(r["fatores"]) == {
                    "licoes_confiaveis", "evidencia_substantiva",
                    "contrato_completo", "historico_saudavel",
                    "achados_reviewer",
                }
                and all(v == _NEUTRO for v in r["fatores"].values())
                and isinstance(r["metrica"], str) and len(r["metrica"]) > 50
                and "PROXY" in r["metrica"]
            )


def _caso_b() -> bool:
    """(b) cenário SAUDÁVEL vs DEGRADADO a partir de fontes temporárias reais
    (episódicos + playbook + histórico): níveis diferentes (SAUDAVEL vs
    CRITICO) e fatores coerentes."""
    with tempfile.TemporaryDirectory() as tmp:
        with _isolado(tmp) as ep:
            # --- saudável ---
            _escreve(ep, "reg-saudavel-2026-09-11.md", _REC_SAUDAVEL)
            saudavel = gerar_saude(
                memory=None,
                playbook=_playbook(confiaveis=5, fracas=0),
                history_file=_hist(tmp, [
                    {"command": "pytest tests/", "status": "finished"},
                    {"command": "python -m py_compile x.py", "status": "finished"},
                ]),
            )
            # --- degradado (mesma árvore, troca as fontes) ---
            for p in ep.glob("*.md"):
                if p.name != "index.md":
                    p.unlink()
            _escreve(ep, "reg-degradado-2026-09-11.md", _REC_DEGRADADO)
            degradado = gerar_saude(
                memory=None,
                playbook=_playbook(confiaveis=0, fracas=5),
                history_file=_hist(tmp, [
                    {"command": "pytest tests/", "status": "error"},
                    {"command": "rm -rf x", "status": "blocked"},
                ]),
            )
            return (
                saudavel["nivel"] == "SAUDAVEL"
                and saudavel["score"] == 1.0
                and degradado["nivel"] == "CRITICO"
                and degradado["score"] == 0.0
                and saudavel["fatores"]["licoes_confiaveis"] == 1.0
                and saudavel["fatores"]["evidencia_substantiva"] == 1.0
                and saudavel["fatores"]["contrato_completo"] == 1.0
                and saudavel["fatores"]["historico_saudavel"] == 1.0
                and saudavel["fatores"]["achados_reviewer"] == 1.0
                and degradado["fatores"]["licoes_confiaveis"] == 0.0
                and degradado["fatores"]["evidencia_substantiva"] == 0.0
                and degradado["fatores"]["contrato_completo"] == 0.0
                and degradado["fatores"]["historico_saudavel"] == 0.0
                and degradado["fatores"]["achados_reviewer"] == 0.0
            )


def _caso_c() -> bool:
    """(c) níveis por limiar: fontes ausentes -> neutro DEGRADADO (0.5 >= 0.35);
    um cenário intermediário (playbook bom + histórico limpo, sem registros)
    cai em ATENCAO (>= 0.55 e < 0.75) conforme os pesos de `config.SAUDE_PESOS`."""
    with tempfile.TemporaryDirectory() as tmp:
        with _isolado(tmp) as ep:
            r = gerar_saude(
                memory=None,
                playbook=_playbook(confiaveis=3, fracas=1),  # 0.75
                history_file=_hist(tmp, [{"command": "x", "status": "finished"}]),
            )
            # 0.25*0.75 + 0.25*0.5 + 0.15*0.5 + 0.15*1.0 + 0.20*0.5 ~= 0.637
            return (
                r["nivel"] == "ATENCAO"
                and 0.55 <= r["score"] < 0.75
                and r["fatores"]["licoes_confiaveis"] == 0.75
                and r["fatores"]["historico_saudavel"] == 1.0
            )


# ---------------------------------------------------------------------------
# Casos de verificar_contradicoes


def _caso_d() -> bool:
    """(d) verificar_contradicoes: caso LIMPO (comando com execução real
    observada e status coerente) -> []."""
    etapas = [{
        "tipo": "comando",
        "comando": "python -m py_compile x.py",
        "aprovado": True,
        "execucao": {"exit_code": 0},
    }]
    r = verificar_contradicoes(
        validacoes_executadas=["python -m py_compile x.py"],
        status="APROVADA",
        etapas=etapas,
    )
    return r == []


def _caso_e() -> bool:
    """(e) verificar_contradicoes: comando em `validacoes_executadas` SEM
    execução real (etapa sem exit_code e sem etapa registrada) -> contradição
    bloqueante."""
    # (i) etapa existe, mas sem execução observada
    r1 = verificar_contradicoes(
        validacoes_executadas=["git status"],
        status="APROVADA_COM_RESSALVAS",
        etapas=[{
            "tipo": "comando", "comando": "git status",
            "aprovado": False, "execucao": None,
        }],
    )
    # (ii) nenhuma etapa registrada para o comando alegado
    r2 = verificar_contradicoes(
        validacoes_executadas=["git diff"],
        status="APROVADA",
        etapas=[],
    )
    return (
        len(r1) == 1
        and r1[0]["severidade"] == "bloqueante"
        and "sem execução real" in r1[0]["descricao"]
        and len(r2) == 1
        and r2[0]["severidade"] == "bloqueante"
        and "não registrou a etapa" in r2[0]["descricao"]
    )


def _caso_f() -> bool:
    """(f) verificar_contradicoes: `status == "APROVADA"` com falha real
    (`exit_code != 0`) -> contradição bloqueante; sem etapas -> []."""
    com_falha = verificar_contradicoes(
        validacoes_executadas=["pytest tests/"],
        status="APROVADA",
        etapas=[{
            "tipo": "comando", "comando": "pytest tests/",
            "aprovado": True, "execucao": {"exit_code": 1},
        }],
    )
    vazio = verificar_contradicoes(
        validacoes_executadas=[], status="APROVADA", etapas=None,
    )
    return (
        len(com_falha) == 1
        and "APROVADA com falha real" in com_falha[0]["descricao"]
        and vazio == []
    )


# ---------------------------------------------------------------------------
# Casos de integração no pipeline


def _tarefa(**extra):
    base = {
        "objetivo": "tarefa de teste do detector de saúde",
        "escopo": "tests/",
        "restricoes": ["sem dependências"],
        "criterios_de_aceite": [
            "roda `python -m py_compile harness/config.py` com sucesso"
        ],
        "nivel_de_risco": "baixo",
    }
    base.update(extra)
    return base


def _saude_fake(nivel: str) -> dict:
    return {
        "score": 0.9 if nivel == "SAUDAVEL" else 0.1,
        "nivel": nivel,
        "fatores": {},
        "metrica": "fake",
    }


def _caso_g() -> bool:
    """(g) integração: `saude` presente no contrato de saída (`_consolida`) e
    no caminho de timeout; sem contradições espúrias em execução normal."""
    with tempfile.TemporaryDirectory() as tmp:
        with _isolado(tmp):
            pipe = AgentPipeline(approve=lambda cmd: True, gravar_registro=False)
            res = pipe.run_task(_tarefa())
            return (
                isinstance(res.get("saude"), dict)
                and {"score", "nivel", "fatores", "metrica"} <= set(res["saude"])
                and res["saude"]["nivel"] in ("SAUDAVEL", "ATENCAO",
                                              "DEGRADADO", "CRITICO")
                and not any(
                    "contradição" in a.get("descricao", "")
                    for a in res["achados_da_revisao"]
                )
            )


def _caso_h() -> bool:
    """(h) anti-envenomamento: com saúde DEGRADADA (mock), o pipeline NÃO grava
    registro episódico e registra uma etapa com motivo `saude_baixa`."""
    with tempfile.TemporaryDirectory() as tmp:
        with _isolado(tmp):
            pipe = AgentPipeline(approve=lambda cmd: True, gravar_registro=True)
            with mock.patch(
                "harness.health.gerar_saude",
                return_value=_saude_fake("DEGRADADO"),
            ):
                res = pipe.run_task(_tarefa())
            return (
                res.get("saude", {}).get("nivel") == "DEGRADADO"
                and pipe.memory.list_records() == []
                and any(
                    e.get("motivo") == "saude_baixa"
                    for e in res["etapas"]
                )
            )


def _caso_i() -> bool:
    """(i) anti-envenomamento: com saúde SAUDÁVEL (mock), o pipeline grava o
    registro episódico normalmente (comportamento preservado)."""
    with tempfile.TemporaryDirectory() as tmp:
        with _isolado(tmp):
            pipe = AgentPipeline(approve=lambda cmd: True, gravar_registro=True)
            with mock.patch(
                "harness.health.gerar_saude",
                return_value=_saude_fake("SAUDAVEL"),
            ):
                res = pipe.run_task(_tarefa())
            registros = pipe.memory.list_records()
            return (
                res.get("saude", {}).get("nivel") == "SAUDAVEL"
                and len(registros) == 1
                and not any(
                    e.get("motivo") == "saude_baixa"
                    for e in res["etapas"]
                )
            )


def _caso_j() -> bool:
    """(j) tolerância: falha do detector -> `_saude_atual` devolve saúde NEUTRA
    COERENTE (0.5/DEGRADADO), o pipeline NÃO quebra e, por estar DEGRADADO, NÃO
    grava (anti-envenomamento conservador)."""
    with tempfile.TemporaryDirectory() as tmp:
        with _isolado(tmp):
            pipe = AgentPipeline(approve=lambda cmd: True, gravar_registro=True)
            with mock.patch(
                "harness.health.gerar_saude",
                side_effect=RuntimeError("fonte corrompida"),
            ):
                res = pipe.run_task(_tarefa())
            return (
                res.get("saude", {}).get("nivel") == "DEGRADADO"
                and res.get("saude", {}).get("score") == 0.5
                and pipe.memory.list_records() == []
            )


def _caso_k() -> bool:
    """(k) observabilidade: `gerar_panorama` expõe a chave `saude` (tolerante),
    sem alterar as demais chaves do panorama."""
    from harness.memory import Memory
    from harness.observability import gerar_panorama
    with tempfile.TemporaryDirectory() as tmp:
        with _isolado(tmp):
            r = gerar_panorama(
                Memory(), playbook={}, history_file=pathlib.Path(tmp) / "x.json"
            )
            return (
                isinstance(r.get("saude"), dict)
                and r["saude"]["nivel"] in ("SAUDAVEL", "ATENCAO",
                                             "DEGRADADO", "CRITICO")
                and {"resumo_geral", "resolucao", "retrabalho",
                     "playbook", "historico", "sugestoes"} <= set(r)
            )


def _caso_l() -> bool:
    """(l) caminho de timeout: o contrato de timeout expõe a chave `saude`
    (item 3.4) — avaliado com o tempo já estourado."""
    with tempfile.TemporaryDirectory() as tmp:
        with _isolado(tmp):
            pipe = AgentPipeline(gravar_registro=False)
            pipe._tempo_inicio = 0.0
            pipe._tempo_limite = 0.0
            contrato = pipe._monta_contrato(_tarefa())
            with mock.patch(
                "harness.health.gerar_saude",
                return_value=_saude_fake("SAUDAVEL"),
            ):
                res = pipe._encerra_por_timeout(contrato)
            return (
                res is not None
                and res.get("timeout_safety_net") is True
                and isinstance(res.get("saude"), dict)
                and res["saude"]["nivel"] == "SAUDAVEL"
            )


def _caso_m() -> bool:
    """(m) A1: `verificar_contradicoes` sinaliza critério de aceite cuja
    validação extraída NÃO foi executada (filtro acionável). O caso limpo (todos
    os comandos do critério executados) continua `[]`."""
    # (i) critério com comando não executado -> 1 achado alto
    com_falta = verificar_contradicoes(
        validacoes_executadas=["pytest tests/"],
        status="APROVADA_COM_RESSALVAS",
        etapas=[{
            "tipo": "comando", "comando": "pytest tests/",
            "aprovado": True, "execucao": {"exit_code": 0},
        }],
        comandos_criterios=["pytest tests/", "git push"],
    )
    # (ii) variante via `criterios=` (extração interna)
    via_criterios = verificar_contradicoes(
        validacoes_executadas=[],
        status="APROVADA_COM_RESSALVAS",
        etapas=[],
        criterios=["roda `python -m py_compile harness/config.py` com sucesso"],
    )
    # (iii) critério 100% coberto -> sem achado
    coberto = verificar_contradicoes(
        validacoes_executadas=["pytest tests/"],
        status="APROVADA",
        etapas=[{
            "tipo": "comando", "comando": "pytest tests/",
            "aprovado": True, "execucao": {"exit_code": 0},
        }],
        comandos_criterios=["pytest tests/"],
    )
    return (
        len(com_falta) == 1
        and com_falta[0]["severidade"] == "alta"
        and "critério de aceite sem validação executada" in com_falta[0]["descricao"]
        and "git push" in com_falta[0]["descricao"]
        and len(via_criterios) == 1
        and "python -m py_compile" in via_criterios[0]["descricao"]
        and coberto == []
    )


def _caso_n() -> bool:
    """(n) M1 (rubber-stamp): episódio com 'ok' em tudo, `echo test` como
    validação e `achados_da_revisao:` VAZIO NÃO pontua 1.0/SAUDAVEL. O token
    `test` é apenas argumento de comando trivial (evidência=0) e o cabeçalho sem
    bullets não conta como revisão real (achados=0)."""
    with tempfile.TemporaryDirectory() as tmp:
        with _isolado(tmp) as ep:
            _escreve(ep, "reg-rubber-2026-09-11.md", _REC_RUBBER_STAMP)
            r = gerar_saude(
                memory=None,
                playbook=_playbook(confiaveis=5, fracas=0),
                history_file=_hist(tmp, [
                    {"command": "pytest tests/", "status": "finished"},
                ]),
            )
            return (
                r["score"] < 1.0
                and r["nivel"] != "SAUDAVEL"
                and r["fatores"]["evidencia_substantiva"] == 0.0
                and r["fatores"]["achados_reviewer"] == 0.0
                and r["fatores"]["contrato_completo"] == 1.0
                and r["fontes_presentes"] == 5
                and r["bootstrap"] is False
            )


def _caso_o() -> bool:
    """(o) M2 (bootstrap): harness VAZIO + 1ª tarefa bem-sucedida GRAVA o
    primeiro registro. A saúde de ENTRADA (sem fontes) tem bootstrap=True e o
    anti-envenomamento NÃO bloqueia um harness novo."""
    with tempfile.TemporaryDirectory() as tmp:
        with _isolado(tmp):
            pipe = AgentPipeline(approve=lambda cmd: True, gravar_registro=True)
            res = pipe.run_task(_tarefa())
            registros = pipe.memory.list_records()
            return (
                res["status"] != "BLOQUEADA"
                and len(registros) == 1
                and not any(
                    e.get("motivo") == "saude_baixa"
                    for e in res["etapas"]
                )
            )


def _caso_p() -> bool:
    """(p) M2 (degradação real): harness com fontes PRESENTES e ruins (episódio
    fraco com validação trivial, playbook fraco e histórico com erro/bloqueio)
    BLOQUEIA a gravação — não confunde 'sem fontes' com 'fontes ruins'."""
    with tempfile.TemporaryDirectory() as tmp:
        with _isolado(tmp):
            from harness.memory import Memory
            m = Memory()
            m.record(
                keywords=["implementacao", "revisao"],
                agente="implementer/reviewer",
                tema="execucao degradada",
                entrada="_não informado_",
                fluxo="EM_IMPLEMENTACAO — implementer",
                resultado="validacoes_executadas:\n  - echo ok\n  - ls",
                contexto="_não informado_",
                status="completed",
                trust="fraca",
                origem="_não informada_",
                validado_por="_não informado_",
            )
            (pathlib.Path(tmp) / "history.json").write_text(
                json.dumps([
                    {"command": "x", "status": "error"},
                    {"command": "y", "status": "blocked"},
                ]),
                encoding="utf-8",
            )
            pipe = AgentPipeline(
                playbook=_playbook(confiaveis=0, fracas=5),
                memory=m,
                approve=lambda cmd: True,
                gravar_registro=True,
            )
            res = pipe.run_task(_tarefa())
            return (
                res["status"] != "BLOQUEADA"
                and len(m.list_records()) == 1  # só o episódio pré-existente
                and any(
                    e.get("motivo") == "saude_baixa"
                    for e in res["etapas"]
                )
            )


def _caso_q() -> bool:
    """(q) A1 (integração pelo pipeline): comando de um critério NÃO executado
    (negado por HITL) DISPARA o achado alto em `achados_da_revisao`. O comando
    executado (validacoes_comuns do playbook) mantém o pipeline fora de
    BLOQUEADA."""
    with tempfile.TemporaryDirectory() as tmp:
        with _isolado(tmp):
            pb = _playbook(confiaveis=5, fracas=0)
            pb["learned"]["validacoes_comuns"] = [
                "python -m py_compile harness/config.py"
            ]

            def approve(cmd: str) -> bool:
                return "git push" not in cmd

            pipe = AgentPipeline(
                playbook=pb, approve=approve, gravar_registro=False,
            )
            res = pipe.run_task(_tarefa(
                nivel_de_risco="baixo",
                criterios_de_aceite=["faz `git push` da branch"],
            ))
            achados = res.get("achados_da_revisao", [])
            return (
                res["status"] == "APROVADA_COM_RESSALVAS"
                and any("py_compile" in v for v in res["validacoes_executadas"])
                and "git push" not in res["validacoes_executadas"]
                and any(
                    a.get("severidade") == "alta"
                    and "critério de aceite sem validação executada"
                    in a.get("descricao", "")
                    and "git push" in a.get("descricao", "")
                    for a in achados
                )
            )


def _caso_r() -> bool:
    """(r) Achado ALTA (bootstrap one-shot): DUAS execuções consecutivas num
    harness isolado GRAVAM os dois registros — a 2ª NÃO é bloqueada pelo
    detector lendo os próprios registros do pipeline. A saúde da 2ª execução
    tem `fontes_llm == 0` (só registros de máquina + histórico) e
    `bootstrap=True`, então o anti-envenomamento libera a gravação. O registro
    do pipeline é parseável (validacoes/contexto reais)."""
    with tempfile.TemporaryDirectory() as tmp:
        with _isolado(tmp):
            for _ in range(2):
                pipe = AgentPipeline(approve=lambda cmd: True, gravar_registro=True)
                res = pipe.run_task(_tarefa())
                if res["status"] == "BLOQUEADA":
                    print(f"    FALHOU: status {res['status']}")
                    return False
                if any(e.get("motivo") == "saude_baixa" for e in res["etapas"]):
                    print("    FALHOU: 2ª gravação bloqueada (saude_baixa)")
                    return False
            registros = pipe.memory.list_records()
            if len(registros) != 2:
                print(f"    FALHOU: {len(registros)} registro(s), esperado 2")
                return False
            # O registro do pipeline é parseável (Achado ALTA raiz secundária).
            from harness.agents import parse_episode
            ep = parse_episode(registros[0]["file"])
            if not ep.completo or not ep.validacoes:
                print(f"    FALHOU: registro opaco completo={ep.completo} "
                      f"validacoes={ep.validacoes}")
                return False
            return True


def _caso_s() -> bool:
    """(s) Achado MÉDIA A1: extração ESTRITA de critérios. Prosa solta
    ("o codigo deve ser python e funcionar") NÃO gera achado "alta"; um comando
    real não executado (com ou sem backticks) GERA o achado "alta"."""
    prosa = verificar_contradicoes(
        validacoes_executadas=[], status="APROVADA_COM_RESSALVAS",
        etapas=[], criterios=["o codigo deve ser python e funcionar"],
    )
    real_backtick = verificar_contradicoes(
        validacoes_executadas=[], status="APROVADA_COM_RESSALVAS",
        etapas=[], criterios=["rodar `pytest tests/` no fim"],
    )
    real_sem_backtick = verificar_contradicoes(
        validacoes_executadas=[], status="APROVADA_COM_RESSALVAS",
        etapas=[], criterios=["executar python -m py_compile x.py"],
    )
    return (
        prosa == []
        and len(real_backtick) == 1
        and real_backtick[0]["severidade"] == "alta"
        and "pytest tests/" in real_backtick[0]["descricao"]
        and len(real_sem_backtick) == 1
        and "python -m py_compile x.py" in real_sem_backtick[0]["descricao"]
    )


def _caso_t() -> bool:
    """(t) Achado MÉDIA M1: inspeção git não é evidência substantiva. Episódio
    'ok em tudo' com única validação `git status` e achados vazios NÃO pontua
    SAUDAVEL (fator evidencia_substantiva == 0)."""
    with tempfile.TemporaryDirectory() as tmp:
        with _isolado(tmp) as ep:
            _escreve(ep, "reg-git-2026-09-11.md", _REC_GIT_STATUS)
            r = gerar_saude(
                memory=None,
                playbook=_playbook(confiaveis=5, fracas=0),
                history_file=_hist(tmp, [
                    {"command": "pytest tests/", "status": "finished"},
                ]),
            )
            return (
                r["fatores"]["evidencia_substantiva"] == 0.0
                and r["nivel"] != "SAUDAVEL"
                and r["score"] < 0.75
                and r["fatores"]["achados_reviewer"] == 0.0
            )


def _caso_v() -> bool:
    """(v) Achado ALTA (evidência robusta a relatórios): os registros REAIS dos
    agentes LLM gravam a validação como RELATÓRIO/PROSA. Os 6 exemplos reais
    DEVEM contar como substantivos; comandos de fachada/inspeção NÃO contam."""
    from harness.health import _validacao_substantiva
    reais = [
        "tests/agents_test.py → 21/21 PASS",
        "tests/pipeline_test.py → 16/16 PASS",
        "python -m harness.agents compile",
        "python -m harness.selfcheck",
        "python tests/memory_test.py",
        "pytest tests/x.py -q",
    ]
    fachada = [
        "echo test", "ls tests/", "dir tests", "type tests.txt",
        "cat x", "git status", "git diff", "git log",
    ]
    for c in reais:
        if not _validacao_substantiva(c):
            print(f"    FALHOU: relatório real não contou: {c!r}")
            return False
    for c in fachada:
        if _validacao_substantiva(c):
            print(f"    FALHOU: fachada contou como substantiva: {c!r}")
            return False
    return True


def _caso_w() -> bool:
    """(w) Achado BAIXA A1 (versão ≠ comando): prosa com `node 18`, `go 1.22`
    e `python 3.12` NÃO gera achado (nem entre backticks); comandos reais
    continuam extraídos (`node index.js` gera o achado "alta")."""
    prosa = [
        "usar node 18 para rodar o projeto",
        "suportar go 1.22 no build",
        "instalar python 3.12 e rodar",
    ]
    for c in prosa:
        if verificar_contradicoes(
            validacoes_executadas=[], status="APROVADA_COM_RESSALVAS",
            etapas=[], criterios=[c],
        ):
            print(f"    FALHOU: prosa gerou achado: {c!r}")
            return False
    for c in ["usar `node 18`", "usar `python 3.12`", "usar `go 1.22`"]:
        if verificar_contradicoes(
            validacoes_executadas=[], status="APROVADA_COM_RESSALVAS",
            etapas=[], criterios=[c],
        ):
            print(f"    FALHOU: versão em backtick gerou achado: {c!r}")
            return False
    real = verificar_contradicoes(
        validacoes_executadas=[], status="APROVADA_COM_RESSALVAS",
        etapas=[], criterios=["rodar `node index.js` no fim"],
    )
    return (
        len(real) == 1
        and real[0]["severidade"] == "alta"
        and "node index.js" in real[0]["descricao"]
    )


def _caso_u() -> bool:
    """(u) Achado BAIXA (normalização): cobertura de critério por PREFIXO de
    tokens com o MESMO executável — `pytest tests/x.py` é coberto por
    `pytest tests/x.py -q` (e vice-versa); executável diferente (`npm test`) ou
    token divergente (`pytest tests/y.py`) NÃO são cobertos."""
    def _verifica(executado: str, criterio: str) -> list[dict]:
        return verificar_contradicoes(
            validacoes_executadas=[executado],
            status="APROVADA_COM_RESSALVAS",
            etapas=[{
                "tipo": "comando", "comando": executado,
                "aprovado": True, "execucao": {"exit_code": 0},
            }],
            comandos_criterios=[criterio],
        )
    return (
        _verifica("pytest tests/x.py -q", "pytest tests/x.py") == []
        and _verifica("pytest tests/x.py", "pytest tests/x.py -q") == []
        and len(_verifica("pytest tests/", "npm test")) == 1
        and len(_verifica("pytest tests/x.py", "pytest tests/y.py")) == 1
    )


CASES = [
    ("gerar_saude com fontes vazias: neutro 0.5 / DEGRADADO (a)", _caso_a),
    ("gerar_saude saudável vs degradado: SAUDAVEL vs CRITICO (b)", _caso_b),
    ("gerar_saude: nível intermediário por limiares (c)", _caso_c),
    ("verificar_contradicoes: caso limpo -> [] (d)", _caso_d),
    ("verificar_contradicoes: comando sem execução real (e)", _caso_e),
    ("verificar_contradicoes: APROVADA com falha real (f)", _caso_f),
    ("pipeline: saude no contrato + sem contradição espúria (g)", _caso_g),
    ("pipeline: anti-envenomamento NÃO grava se degradado (h)", _caso_h),
    ("pipeline: grava se saudável (i)", _caso_i),
    ("pipeline: falha do detector -> neutro DEGRADADO e não grava (j)", _caso_j),
    ("observability: panorama expõe saude (k)", _caso_k),
    ("pipeline: saude no contrato de timeout (l)", _caso_l),
    ("A1: contradição de critério sem validação executada (m)", _caso_m),
    ("M1: rubber-stamp (echo test + achados vazio) não pontua 1.0 (n)", _caso_n),
    ("M2: harness vazio (bootstrap) grava o 1º registro (o)", _caso_o),
    ("M2: fontes degradadas bloqueiam a gravação (p)", _caso_p),
    ("A1: contradição dispara de verdade pelo pipeline (q)", _caso_q),
    ("ALTA: duas execuções consecutivas gravam (bootstrap) (r)", _caso_r),
    ("A1: extração estrita de critérios (prosa não gera alta) (s)", _caso_s),
    ("M1: inspeção git não é evidência substantiva (t)", _caso_t),
    ("BAIXA: cobertura de critério por prefixo de tokens (u)", _caso_u),
    ("ALTA: evidência robusta a relatórios dos agentes (v)", _caso_v),
    ("BAIXA A1: argumento-versão (node 18/go 1.22/python 3.12) rejeitado (w)",
     _caso_w),
]


def main_test() -> int:
    passed = 0
    failed = 0
    for name, test in CASES:
        try:
            ok = test()
        except Exception:
            traceback.print_exc()
            ok = False
        if ok:
            passed += 1
            print(f"  [PASS] {name}")
        else:
            failed += 1
            print(f"  [FAIL] {name}")
    print(f"\nRESULTADO: {passed} passaram, {failed} falharam")
    return 1 if failed else 0


if __name__ == "__main__":
    sys.exit(main_test())
