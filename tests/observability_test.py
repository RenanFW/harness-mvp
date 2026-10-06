"""Testes da observabilidade do hub (Item 6) — `harness/observability.py`.

Cobre: (a) `gerar_panorama` retorna dict com as chaves esperadas; (b)
distribuições corretas (status/trust/agente/data); (c) heurística direto vs.
delegado; (d) tolera memória vazia/playbook ausente; (e) somente leitura;
(f) agregação do histórico de comandos; (g) fallback da curva por agente sem
playbook; (h) top-N (`MAX_OBS_LIMIT`) nas listas de topo; (i) resumo do
playbook quando disponível.

Rode com:
    python tests/observability_test.py
"""

from __future__ import annotations

import contextlib
import datetime
import json
import pathlib
import sys
import tempfile
from unittest import mock

sys.path.insert(0, str(pathlib.Path(__file__).resolve().parent.parent))

from harness import config  # noqa: E402
from harness.memory import Memory  # noqa: E402
from harness.observability import (  # noqa: E402
    _e_delegado,
    gerar_panorama,
)


@contextlib.contextmanager
def _memoria_tmp(tmp: str):
    """Memory isolada em diretório temp, com os mocks de EPISODIC_DIR/
    INDEX_FILE/MEMORY_DIR ATIVOS até o fim do bloco (nunca toca memory/ real)."""
    ep = pathlib.Path(tmp) / "episodic"
    idx = ep / "index.md"
    mem = pathlib.Path(tmp) / "memory"
    with mock.patch.object(config, "EPISODIC_DIR", ep), \
         mock.patch.object(config, "INDEX_FILE", idx), \
         mock.patch.object(config, "MEMORY_DIR", mem):
        yield Memory()


def _hist(tmp: str, nome: str = "history.json") -> pathlib.Path:
    return pathlib.Path(tmp) / nome


def _hist_inexistente(tmp: str) -> pathlib.Path:
    return _hist(tmp, "history_inexistente.json")


def _playbook_fake() -> dict:
    """Playbook mínimo (estrutura de load_playbook) para os testes."""
    return {
        "schema_version": "1.1",
        "data": "2026-08-19",
        "agents": {"hub": {"name": "hub"}, "implementer": {"name": "implementer"}},
        "learned": {
            "episodes": [{"record_id": "reg-delegado-2026-08-19"}],
            "licoes": ["licao um", "licao dois"],
            "validacoes_comuns": ["python tests/ (x1)"],
            "block_reasons": ["sem segredos"],
            "por_agente": {
                "hub": {
                    "agente": "hub",
                    "licoes": [{"texto": "licao um", "ocorrencias": 1,
                                "trust": "fraca", "origens": ["r1"]}],
                    "validacoes_comuns": [],
                    "achados": [{"texto": "achado hub", "ocorrencias": 1,
                                 "trust": "fraca", "origens": ["r1"]}],
                    "n_licoes_fracas": 1,
                    "n_licoes_confiaveis": 0,
                },
                "implementer": {
                    "agente": "implementer",
                    "licoes": [],
                    "validacoes_comuns": [],
                    "achados": [],
                    "n_licoes_fracas": 0,
                    "n_licoes_confiaveis": 0,
                },
            },
        },
    }


def _escreve(nome: str, conteudo: str) -> None:
    config.EPISODIC_DIR.mkdir(parents=True, exist_ok=True)
    (config.EPISODIC_DIR / nome).write_text(conteudo, encoding="utf-8")


def _popula(m: Memory) -> None:
    """Quatro registros escritos DIRETAMENTE (datas/trust/status explícitos no
    frontmatter) para distribuições determinísticas:
    - delegado (agente hub/implementer/reviewer + etapas + achados);
    - direto (agente hub, consulta respondida da memória, sem achados);
    - direto (agente web, sem trust -> fraca na leitura);
    - delegado (agente hub/implementer/reviewer/brain, ressalvas + achado)."""
    _escreve("reg-delegado-2026-08-19.md", """\
---
id: reg-delegado-2026-08-19
keywords: [fluxo, pipeline]
data: 2026-08-19
agente: hub/implementer/reviewer
status: completed
trust: alta
origem: contrato de saída da execução (implementer+reviewer)
validado_por: reviewer
---

# Tarefa delegada com revisão

## Contrato de entrada
objetivo: exemplo delegado com implementacao e revisao

## Fluxo
1. EM_IMPLEMENTACAO — implementer implementou a mudança.
2. EM_REVISAO — reviewer revisou a mudança.

## Resultado
- status: completed
- achados_da_revisao (não bloqueantes):
  - [QUALIDADE] achado de qualidade um
  - [INFO] achado informativo dois

## Contexto
- Lição: agregação por parágrafo é mais honesta para curvas de aprendizado.
""")
    _escreve("reg-direto-2026-08-19.md", """\
---
id: reg-direto-2026-08-19
keywords: [consulta, memoria]
data: 2026-08-19
agente: hub
status: completed
trust: media
origem: consulta direta à memória
validado_por: brain
---

# Consulta direta resolvida pela memória

## Contrato de entrada
objetivo: consulta respondida direto, sem delegação

## Fluxo
Brain recuperou o registro da memória e respondeu sem delegar.

## Resultado
- status: completed
- resumo: resposta direta

## Contexto
- Sem implementação nem revisão nesta execução.
""")
    _escreve("reg-consulta-2026-08-18.md", """\
---
id: reg-consulta-2026-08-18
keywords: [web, registro]
data: 2026-08-18
agente: web
status: completed
---

# registro avulso

## Contrato de entrada
_não informado_

## Fluxo
_não informado_

## Resultado
_não informado_

## Contexto
_não informado_
""")
    _escreve("reg-ressalvas-2026-08-18.md", """\
---
id: reg-ressalvas-2026-08-18
keywords: [hub, revisao]
data: 2026-08-18
agente: hub/implementer/reviewer/brain
status: aprovada_com_ressalvas
trust: fraca
origem: _não informada_
validado_por: _não informado_
---

# Execução com ressalvas

## Contrato de entrada
objetivo: exemplo com ressalvas de revisão

## Fluxo
EM_IMPLEMENTACAO — implementer; EM_REVISAO — reviewer.

## Resultado
- status: aprovada_com_ressalvas
- achados_da_revisao (não bloqueantes):
  - [RESSALVA] um achado de ressalva

## Contexto
- Lição de exemplo com texto longo o suficiente para o contexto.
""")


def _caso_a() -> bool:
    """(a) gerar_panorama retorna dict com resumo_geral/resolucao/tempo/
    retrabalho/playbook/sugestoes + metrica + historico + top + fontes."""
    with tempfile.TemporaryDirectory() as tmp:
        with _memoria_tmp(tmp) as m:
            _popula(m)
            r = gerar_panorama(m, playbook=_playbook_fake(),
                               history_file=_hist_inexistente(tmp))
            chaves = {"gerado_em", "metrica", "resumo_geral", "top",
                      "resolucao", "tempo", "retrabalho", "playbook",
                      "historico", "fontes", "sugestoes"}
            rg = r["resumo_geral"]
            return (
                chaves <= set(r)
                and {"total_registros", "por_status", "por_trust",
                     "por_agente", "por_data"} <= set(rg)
                and rg["total_registros"] == 4
                and {"n_diretos", "n_delegados", "taxa_delegacao",
                     "taxa_direta"} <= set(r["resolucao"])
                and {"com_achados", "sem_achados", "por_agente"} <= set(r["retrabalho"])
                and r["retrabalho"]["fonte_curva"] == "playbook"
                and r["playbook"]["existe"] is True
                and isinstance(r["sugestoes"], list)
                and isinstance(r["top"]["agentes"], list)
                and isinstance(r["top"]["dias"], list)
                and bool(r["metrica"])  # heurísticas documentadas
            )


def _caso_b() -> bool:
    """(b) distribuições corretas: status/trust/agente (multi-tag)/data."""
    with tempfile.TemporaryDirectory() as tmp:
        with _memoria_tmp(tmp) as m:
            _popula(m)
            r = gerar_panorama(m, playbook=_playbook_fake(),
                               history_file=_hist_inexistente(tmp))
            rg = r["resumo_geral"]
            return (
                rg["por_status"] == {"completed": 3, "aprovada_com_ressalvas": 1}
                and rg["por_trust"] == {"alta": 1, "media": 1, "fraca": 2}
                and rg["por_agente"] == {"hub": 3, "implementer": 2,
                                         "reviewer": 2, "brain": 1, "web": 1}
                and rg["por_data"] == {"2026-08-19": 2, "2026-08-18": 2}
                and r["tempo"]["n_dias_ativos"] == 2
                and r["tempo"]["media_registros_por_dia"] == 2.0
            )


def _caso_c() -> bool:
    """(c) heurística direto vs. delegado: delegado = rótulo de agente com
    implementer/reviewer OU corpo com EM_IMPLEMENTACAO/EM_REVISAO; direto =
    sem essas marcas (consulta/registro avulso)."""
    with tempfile.TemporaryDirectory() as tmp:
        with _memoria_tmp(tmp) as m:
            _popula(m)
            r = gerar_panorama(m, playbook=_playbook_fake(),
                               history_file=_hist_inexistente(tmp))
            res = r["resolucao"]
            recs = {rec["id"]: rec for rec in m.list_records()}
            delegados = {rid for rid, rec in recs.items() if _e_delegado(rec)}
            return (
                res["n_delegados"] == 2
                and res["n_diretos"] == 2
                and res["taxa_delegacao"] == 0.5
                and res["taxa_direta"] == 0.5
                and delegados == {"reg-delegado-2026-08-19",
                                  "reg-ressalvas-2026-08-18"}
                and "delegado" in res["heuristica"]  # proxy documentado
            )


def _caso_d() -> bool:
    """(d) memória vazia + playbook ausente não quebram: total 0, resolução
    0/0, playbook.existe False, sugestão de memória vazia."""
    with tempfile.TemporaryDirectory() as tmp:
        with _memoria_tmp(tmp) as m:
            r = gerar_panorama(m, playbook=None,
                               history_file=_hist_inexistente(tmp))
            return (
                r["resumo_geral"]["total_registros"] == 0
                and r["resumo_geral"]["por_status"] == {}
                and r["resolucao"]["n_diretos"] == 0
                and r["resolucao"]["n_delegados"] == 0
                and r["resolucao"]["taxa_delegacao"] == 0.0
                and r["tempo"]["media_registros_por_dia"] == 0.0
                and r["retrabalho"]["com_achados"] == 0
                and r["retrabalho"]["por_agente"] == []
                and r["retrabalho"]["fonte_curva"] == "episodios"
                and r["playbook"]["existe"] is False
                and r["historico"]["n_total"] == 0
                and any("vazia" in s for s in r["sugestoes"])
            )


def _caso_e() -> bool:
    """(e) somente leitura: registros episódicos e histórico intactos (bytes
    idênticos) após gerar_panorama com playbook e histórico."""
    with tempfile.TemporaryDirectory() as tmp:
        with _memoria_tmp(tmp) as m:
            _popula(m)
            hist = _hist(tmp)
            hist.write_text(json.dumps([
                {"command": "python --version", "status": "finished"},
                {"command": "rm -rf x", "status": "blocked"},
            ]), encoding="utf-8")
            antes_ep = {rec["id"]: rec["file"] for rec in m.list_records()}
            bytes_ep_antes = {pathlib.Path(f).read_bytes() for f in antes_ep.values()}
            bytes_hist_antes = hist.read_bytes()
            r = gerar_panorama(m, playbook=_playbook_fake(), history_file=hist)
            depois = m.list_records()
            bytes_ep_depois = {pathlib.Path(rec["file"]).read_bytes()
                               for rec in depois}
            return (
                len(depois) == len(antes_ep)
                and {rec["id"] for rec in depois} == set(antes_ep)
                and bytes_ep_antes == bytes_ep_depois
                and bytes_hist_antes == hist.read_bytes()
                and r["resumo_geral"]["total_registros"] == 4
            )


def _caso_f() -> bool:
    """(f) histórico agregado: contagens por status normalizado (finished/
    completed/ok -> finished; error/retry -> error; blocked; started)."""
    with tempfile.TemporaryDirectory() as tmp:
        with _memoria_tmp(tmp) as m:
            _popula(m)
            hist = _hist(tmp)
            hist.write_text(json.dumps([
                {"command": "python --version", "status": "finished"},
                {"command": "python --version", "status": "started"},
                {"command": "rm -rf alguma coisa", "status": "blocked"},
                {"command": "python -c x", "status": "error"},
                {"command": "echo ok", "status": "ok"},
                {"command": "x", "status": "outro-status"},
            ]), encoding="utf-8")
            h = gerar_panorama(m, playbook=None, history_file=hist)["historico"]
            return (
                h["n_total"] == 6
                and h["n_finished"] == 2  # finished + ok
                and h["n_error"] == 1
                and h["n_blocked"] == 1
                and h["n_started"] == 1
                and h["n_outros"] == 1
                and "invalido" not in h
            )


def _caso_g() -> bool:
    """(g) playbook ausente com memória populada: playbook.existe False e
    retrabalho.por_agente cai para a derivação dos episódios (n_achados =
    registros com a seção achados_da_revisao por agente, multi-tag)."""
    with tempfile.TemporaryDirectory() as tmp:
        with _memoria_tmp(tmp) as m:
            _popula(m)
            r = gerar_panorama(m, playbook=None,
                               history_file=_hist_inexistente(tmp))
            por_agente = {x["agente"]: x for x in r["retrabalho"]["por_agente"]}
            return (
                r["playbook"]["existe"] is False
                and por_agente["hub"]["n_achados"] == 2
                and por_agente["implementer"]["n_achados"] == 2
                and por_agente["reviewer"]["n_achados"] == 2
                and por_agente["brain"]["n_achados"] == 1
                and por_agente["web"]["n_achados"] == 0
                and r["playbook"]["por_agente"] == []
                and r["retrabalho"]["fonte_curva"] == "episodios"
            )


def _caso_h() -> bool:
    """(h) top-N: MAX_OBS_LIMIT limita as listas de topo (top.agentes,
    top.dias, playbook.por_agente); sem o limite, a lista completa é mantida."""
    with tempfile.TemporaryDirectory() as tmp:
        with _memoria_tmp(tmp) as m:
            _popula(m)
            com_limite = gerar_panorama(m, playbook=_playbook_fake(),
                                        history_file=_hist_inexistente(tmp))
            sem_limite = None
            with mock.patch.object(config, "MAX_OBS_LIMIT", 0):
                sem_limite = gerar_panorama(m, playbook=_playbook_fake(),
                                            history_file=_hist_inexistente(tmp))
            with mock.patch.object(config, "MAX_OBS_LIMIT", 2):
                top2 = gerar_panorama(m, playbook=_playbook_fake(),
                                      history_file=_hist_inexistente(tmp))
            return (
                # limite padrão (10) não corta 5 agentes / 2 dias / 2 do fake
                len(com_limite["top"]["agentes"]) == 5
                and len(com_limite["top"]["dias"]) == 2
                and len(com_limite["playbook"]["por_agente"]) == 2
                # limite 2 corta as listas de topo
                and len(top2["top"]["agentes"]) == 2
                and len(top2["top"]["dias"]) == 2
                and len(top2["playbook"]["por_agente"]) == 2
                # sem limite (0) = lista completa
                and len(sem_limite["top"]["agentes"]) == 5
                and len(sem_limite["playbook"]["por_agente"]) == 2
                # top mais frequente primeiro (determinístico; desempate
                # lexicográfico: ambos os dias têm n=2 -> 2026-08-18 antes)
                and com_limite["top"]["agentes"][0]["agente"] == "hub"
                and com_limite["top"]["agentes"][0]["n"] == 3
                and com_limite["top"]["dias"][0]["dia"] == "2026-08-18"
            )


def _caso_i() -> bool:
    """(i) playbook presente: resumo (schema_version, data, n_agentes,
    episódios/lições/validações/block_reasons) e curva por agente com
    n_licoes/n_licoes_fracas/n_achados; sugestão de lições fracas."""
    with tempfile.TemporaryDirectory() as tmp:
        with _memoria_tmp(tmp) as m:
            _popula(m)
            r = gerar_panorama(m, playbook=_playbook_fake(),
                               history_file=_hist_inexistente(tmp))
            pb = r["playbook"]
            ret = {x["agente"]: x for x in r["retrabalho"]["por_agente"]}
            hub = ret["hub"]
            return (
                pb["existe"] is True
                and pb["schema_version"] == "1.1"
                and pb["data"] == "2026-08-19"
                and pb["n_agentes"] == 2
                and pb["n_episodios_playbook"] == 1
                and pb["n_licoes_playbook"] == 2
                and pb["n_validacoes_comuns"] == 1
                and pb["n_block_reasons"] == 1
                and pb["por_agente"][0]["agente"] == "hub"  # mais achados 1º
                and hub["n_licoes"] == 1
                and hub["n_licoes_fracas"] == 1
                and hub["n_achados"] == 1
                and r["retrabalho"]["fonte_curva"] == "playbook"
                and any("fraca" in s and "hub" in s for s in r["sugestoes"])
            )


def _caso_j() -> bool:
    """(j) retrabalho.fonte_curva presente e correto nos DOIS modos:
    'playbook' quando a curva por agente vem do playbook (n_achados =
    achados distintos agregados) e 'episodios' no fallback sem playbook
    (n_achados = nº de registros com a seção achados_da_revisao)."""
    with tempfile.TemporaryDirectory() as tmp:
        with _memoria_tmp(tmp) as m:
            _popula(m)
            r = gerar_panorama(m, playbook=_playbook_fake(),
                               history_file=_hist_inexistente(tmp))
            modo_playbook = (
                r["retrabalho"]["fonte_curva"] == "playbook"
                and r["retrabalho"]["por_agente"][0]["agente"] == "hub"
                and r["retrabalho"]["por_agente"][0]["n_achados"] == 1
            )
            r2 = gerar_panorama(m, playbook=None,
                                history_file=_hist_inexistente(tmp))
            modo_episodios = (
                r2["retrabalho"]["fonte_curva"] == "episodios"
                and {x["agente"] for x in r2["retrabalho"]["por_agente"]}
                == {"hub", "implementer", "reviewer", "brain", "web"}
                and {x["agente"]: x["n_achados"]
                     for x in r2["retrabalho"]["por_agente"]}["hub"] == 2
            )
            # semântica documentada: a heurística de retrabalho menciona a
            # fonte da curva nos dois modos
            return (
                modo_playbook
                and modo_episodios
                and "fonte_curva" in r["retrabalho"]["heuristica"]
                and "fonte_curva" in r2["retrabalho"]["heuristica"]
                and "playbook" in r["metrica"] and "episodios" in r["metrica"]
            )


CASES = [
    ("gerar_panorama: chaves esperadas + estrutura completa (a)", _caso_a),
    ("distribuições corretas por status/trust/agente/data (b)", _caso_b),
    ("heurística direto vs. delegado (c)", _caso_c),
    ("memória vazia + playbook ausente não quebram (d)", _caso_d),
    ("somente leitura: fontes intactas após o panorama (e)", _caso_e),
    ("histórico de comandos agregado por status (f)", _caso_f),
    ("playbook ausente: fallback da curva por agente dos episódios (g)", _caso_g),
    ("top-N MAX_OBS_LIMIT nas listas de topo (h)", _caso_h),
    ("resumo do playbook + curva por agente (i)", _caso_i),
    ("fonte_curva presente e correta nos dois modos (j)", _caso_j),
]


def main_test() -> int:
    passed = 0
    failed = 0
    for name, test in CASES:
        try:
            ok = test()
        except Exception:
            import traceback
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