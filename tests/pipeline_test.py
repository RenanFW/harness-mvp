"""Testes do HITL por níveis de risco (Item 2) no orquestrador determinístico.

Cobre o gate automático de `nivel_de_risco` (baixo/medio/alto) no AgentPipeline:
validação de nível inválido, gate global de risco alto (antes de executar),
auto-aprovação de comandos de validação no risco baixo e manutenção do
comportamento atual no risco medio (retrocompatível).

Também cobre o fechamento do Finding 1 da re-revisão (segurança): a
auto-aprovação do risco baixo NUNCA aprova comandos com separador de shell /
redirecionamento / quebra de linha (ex.: `git status; curl url | sh`,
`git status & del .env`) — o match é por TOKEN (shlex), não por substring.

Rode com:
    python tests/pipeline_test.py
"""

from __future__ import annotations

import contextlib
import os
import pathlib
import sys
import tempfile
import time
import traceback
from unittest import mock

sys.path.insert(0, str(pathlib.Path(__file__).resolve().parent.parent))

from harness import config  # noqa: E402
from harness.memory import Memory  # noqa: E402
from harness.pipeline import AgentPipeline, TaskContract  # noqa: E402


@contextlib.contextmanager
def _isolado(tmp: str):
    """Memory/histórico/playbook isolados em diretório temp (nunca toca
    memory/ real).

    O PLAYBOOK também é isolado (Etapa 5): sem isso, `health.gerar_saude`
    carregaria o playbook REAL do projeto, a saúde deixaria de ser
    bootstrap/neutra e a ladder de saúde escalaria indevidamente os testes
    (risco forçado a alto). Isolar mantém o harness de teste no estado
    bootstrap/neutro — a ladder NÃO escala (retrocompatibilidade)."""
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
        yield


def _tarefa(**extra):
    """Tarefa base válida com comando de validação real (escopo interno seguro).
    O comando `python --version` é seguro (passa check_policy, não é
    APPROVAL_PATTERN) e é sugerido pelo pipeline a partir do critério."""
    base = {
        "objetivo": "tarefa de teste do pipeline",
        "escopo": "tests/",
        "restricoes": ["sem dependências"],
        "criterios_de_aceite": ["roda `python --version` com sucesso"],
    }
    base.update(extra)
    return base


def _saude_mock(nivel: str, bootstrap: bool = False,
                indisponivel: bool = False) -> dict:
    """Saúde sintética para forçar um nível da ladder (Etapa 5)."""
    scores = {"SAUDAVEL": 0.9, "ATENCAO": 0.6,
              "DEGRADADO": 0.4, "CRITICO": 0.1}
    return {
        "score": scores[nivel],
        "nivel": nivel,
        "fatores": {},
        "fontes_llm": 0 if bootstrap else 1,
        "fontes_presentes": 0 if bootstrap else 1,
        "bootstrap": bootstrap,
        "indisponivel": indisponivel,
        "metrica": "mock de saúde (teste da ladder)",
    }


def _caso_a() -> bool:
    """(a) nivel_de_risco inválido -> erro de contrato -> BLOQUEADA em RECEBIDA.
    (valores vazios/None são normalizados para 'medio', retrocompatíveis)."""
    with tempfile.TemporaryDirectory() as tmp:
        with _isolado(tmp):
            for invalido in ("critico", "ALTO", "  ", "critico "):
                pipe = AgentPipeline(approve=lambda cmd: True)
                res = pipe.run_task(_tarefa(nivel_de_risco=invalido))
                if res["status"] != "BLOQUEADA":
                    return False
                if "nivel_de_risco inválido" not in res["resumo"]:
                    return False
                # etapa de bloqueio deve ser RECEBIDA (nada explora/executa)
                if res["etapas"][-1]["etapa"] != "RECEBIDA":
                    return False
            return True
    return False


def _caso_b() -> bool:
    """(b) risco alto + approve=None -> BLOQUEADA antes de executar (nenhum
    comando roda). O contrato BLOQUEADA expõe risco/politica conhecidos."""
    with tempfile.TemporaryDirectory() as tmp:
        with _isolado(tmp):
            pipe = AgentPipeline(approve=None)
            res = pipe.run_task(_tarefa(nivel_de_risco="alto"))
            if res["status"] != "BLOQUEADA":
                return False
            if "risco alto exige aprovação humana" not in res["resumo"]:
                return False
            if res["etapas"][-1]["etapa"] != "RECEBIDA":
                return False
            # Ressalva 1: caminho BLOQUEADA expõe risco/politica conhecidos
            if res.get("risco") != "alto":
                return False
            if res.get("politica") != "gate_global":
                return False
            # nenhum comando executado
            if res["validacoes_executadas"]:
                return False
            return True
    return False


def _caso_c() -> bool:
    """(c) risco alto + approve que aprova o gate global -> prossegue (comandos
    rodam se aprovados)."""
    with tempfile.TemporaryDirectory() as tmp:
        with _isolado(tmp):
            chamadas: list[str] = []
            def approve(cmd: str) -> bool:
                chamadas.append(cmd)
                return True  # aprova o gate global E os comandos
            pipe = AgentPipeline(approve=approve)
            res = pipe.run_task(_tarefa(nivel_de_risco="alto"))
            # prossegue (não bloqueia): execução concluída e aprovada
            if res["status"] == "BLOQUEADA":
                return False
            # o gate global foi consultado UMA vez, no início
            if not chamadas or chamadas[0] != config.RISCO_GATE_ALTO:
                return False
            if chamadas.count(config.RISCO_GATE_ALTO) != 1:
                return False
            # demais chamadas são os comandos sugeridos (HITL por comando)
            if not any(c != config.RISCO_GATE_ALTO for c in chamadas):
                return False
            return True
    return False


def _caso_d() -> bool:
    """(d) risco alto + approve que NEGA o gate global -> BLOQUEADA (nenhum
    comando roda). O contrato BLOQUEADA expõe risco/politica conhecidos."""
    with tempfile.TemporaryDirectory() as tmp:
        with _isolado(tmp):
            def approve(cmd: str) -> bool:
                return False  # nega o gate global
            pipe = AgentPipeline(approve=approve)
            res = pipe.run_task(_tarefa(nivel_de_risco="alto"))
            if res["status"] != "BLOQUEADA":
                return False
            if "aprovada" not in res["resumo"] and "global negada" not in res["resumo"]:
                # espera "risco alto: aprovação humana global negada"
                pass
            if "global negada" not in res["resumo"]:
                return False
            if res["etapas"][-1]["etapa"] != "RECEBIDA":
                return False
            if res["validacoes_executadas"]:
                return False
            # Ressalva 1: caminho BLOQUEADA expõe risco/politica conhecidos
            if res.get("risco") != "alto":
                return False
            if res.get("politica") != "gate_global":
                return False
            return True
    return False


def _caso_e() -> bool:
    """(e) risco baixo + approve presente -> comandos de VALIDAÇÃO do safelist
    (RISCO_BAIXO_AUTO_SAFELIST), que passam check_policy e não disparam
    APPROVAL_PATTERNS, aprovados automaticamente, sem passar pelo callback."""
    with tempfile.TemporaryDirectory() as tmp:
        with _isolado(tmp):
            chamadas: list[str] = []
            def approve(cmd: str) -> bool:
                chamadas.append(cmd)
                return True
            pipe = AgentPipeline(approve=approve)
            # comando do safelist: python -m py_compile <arquivo existente>
            res = pipe.run_task(_tarefa(
                nivel_de_risco="baixo",
                criterios_de_aceite=[
                    "roda `python -m py_compile harness/config.py` com sucesso"
                ],
            ))
            # comando de validação do safelist é aprovado automaticamente e executado
            if not res["validacoes_executadas"]:
                return False
            # NENHUM comando de validação deve ter passado pelo callback
            # (auto-aprovação não consulta approve)
            if chamadas:
                return False
            return True
    return False


def _caso_f() -> bool:
    """(f) risco baixo + approve=None -> nega por padrão (seguro): nada de
    auto-aprovação, nada executa; sem evidência -> BLOQUEADA."""
    with tempfile.TemporaryDirectory() as tmp:
        with _isolado(tmp):
            pipe = AgentPipeline(approve=None)
            res = pipe.run_task(_tarefa(nivel_de_risco="baixo"))
            if res["status"] != "BLOQUEADA":
                return False
            # sem approve: nenhum comando executado (sem evidência)
            if res["validacoes_executadas"]:
                return False
            return True
    return False


def _caso_g() -> bool:
    """(g) medio mantém o comportamento atual: cada comando passa pelo callback
    (HITL por comando), sem auto-aprovação e sem gate global."""
    with tempfile.TemporaryDirectory() as tmp:
        with _isolado(tmp):
            chamadas: list[str] = []
            def approve(cmd: str) -> bool:
                chamadas.append(cmd)
                return True
            pipe = AgentPipeline(approve=approve)
            res = pipe.run_task(_tarefa(nivel_de_risco="medio"))
            # nenhum gate global no medio
            if config.RISCO_GATE_ALTO in chamadas:
                return False
            # comandos executados via callback normal
            if not res["validacoes_executadas"]:
                return False
            return True
    return False


def _caso_h() -> bool:
    """Retrocompatibilidade: tarefa SEM nivel_de_risco é tratada como 'medio'
    (mesma política do comportamento atual). Sem evidência (nada executado) ->
    BLOQUEADA, mas ainda expõe risco/politica 'medio'/'hitl_por_comando'."""
    with tempfile.TemporaryDirectory() as tmp:
        with _isolado(tmp):
            pipe = AgentPipeline(approve=lambda cmd: False)
            res = pipe.run_task(_tarefa())
            if res["status"] != "BLOQUEADA":
                return False
            if res.get("risco") != "medio":
                return False
            if res.get("politica") != "hitl_por_comando":
                return False
            return True
    return False


def _caso_i() -> bool:
    """TaskContract.validate: nível válido não gera erro de risco; nível
    inválido gera erro específico."""
    ok = TaskContract(objetivo="x", escopo="y", criterios_de_aceite=["z"],
                      nivel_de_risco="baixo").validate()
    bad = TaskContract(objetivo="x", escopo="y", criterios_de_aceite=["z"],
                       nivel_de_risco="critico").validate()
    return (
        not any("nivel_de_risco" in e for e in ok)
        and any("nivel_de_risco inválido" in e for e in bad)
    )


def _caso_j() -> bool:
    """Risco baixo: comando que dispara APPROVAL_PATTERNS NÃO é auto-aprovado
    (continua exigindo aprovação)."""
    with tempfile.TemporaryDirectory() as tmp:
        with _isolado(tmp):
            chamadas: list[str] = []
            def approve(cmd: str) -> bool:
                chamadas.append(cmd)
                return False  # nega o comando de instalação
            pipe = AgentPipeline(approve=approve)
            res = pipe.run_task(_tarefa(
                nivel_de_risco="baixo",
                criterios_de_aceite=["roda `npm install requests` com sucesso"],
            ))
            # o comando npm install (APPROVAL_PATTERN) exige aprovação -> passou
            # pelo callback (não foi auto-aprovado)
            if not any("npm install" in c for c in chamadas):
                return False
            # e foi negado (approve=False) -> não executou
            if "npm install" in res["validacoes_executadas"]:
                return False
            return True
    return False


def _caso_j2() -> bool:
    """Bloqueio NO MEIO da implementação preserva as evidências reais já
    coletadas: comandos 1 e 2 executam, comando 3 é destrutivo -> BLOQUEADA
    com `validacoes_executadas`/`aprovacoes_solicitadas` preenchidas (antes o
    contrato zerava as listas)."""
    with tempfile.TemporaryDirectory() as tmp:
        with _isolado(tmp):
            pipe = AgentPipeline(approve=lambda cmd: True)
            pipe._sugere_comandos = lambda contrato: [
                "python -m py_compile x.py",  # roda (safelist baixo)
                "git status",                  # roda
                "rm -rf /tmp/x",               # destrutivo -> BLOQUEIA
            ]
            res = pipe.run_task(_tarefa(nivel_de_risco="baixo"))
            if res["status"] != "BLOQUEADA":
                return False
            if "python -m py_compile x.py" not in res["validacoes_executadas"]:
                return False
            if "git status" not in res["validacoes_executadas"]:
                return False
            if len(res["aprovacoes_solicitadas"]) < 2:
                return False
            return True
    return False


def _caso_j3() -> bool:
    """Timeout safety net PRESERVA evidências e grava registro quando houve
    execução: comando aprovado roda (exit 0), tempo estoura -> ressalva com
    `validacoes_executadas` preenchida (antes o contrato zerava)."""
    with tempfile.TemporaryDirectory() as tmp:
        with _isolado(tmp):
            pipe = AgentPipeline(approve=lambda cmd: True, gravar_registro=False)
            res = pipe.run_task({
                "objetivo": "teste timeout com execução",
                "escopo": "tests/",
                "criterios_de_aceite": [
                    'python -c "import time; time.sleep(2)"'
                ],
                "nivel_de_risco": "baixo",
                "tempo_maximo_seg": 1,
            })
            if res["status"] != "APROVADA_COM_RESSALVAS":
                return False
            if not res.get("timeout_safety_net"):
                return False
            if not res["validacoes_executadas"]:
                return False
            return True
    return False


def _caso_k() -> bool:
    """(k) Ressalva 2: risco baixo + approve presente, comando de critério FORA
    do safelist (ex.: `git push`, não-destrutivo) NÃO é auto-aprovado — passa
    pelo callback `approve` (HITL por comando)."""
    with tempfile.TemporaryDirectory() as tmp:
        with _isolado(tmp):
            chamadas: list[str] = []
            def approve(cmd: str) -> bool:
                chamadas.append(cmd)
                return False  # nega o comando fora do safelist
            pipe = AgentPipeline(approve=approve)
            res = pipe.run_task(_tarefa(
                nivel_de_risco="baixo",
                criterios_de_aceite=["faz `git push` da branch"],
            ))
            # git push NÃO está no safelist -> passou pelo callback (não foi
            # auto-aprovado)
            if not any("git push" in c for c in chamadas):
                return False
            # e como o callback negou, não foi executado
            if "git push" in res["validacoes_executadas"]:
                return False
            return True
    return False


def _caso_l() -> bool:
    """(l) Ressalva 2: risco baixo + approve presente, comando de critério
    DENTRO do safelist (ex.: `python -m py_compile ...`) É auto-aprovado sem
    consultar o callback (quando há approve presente)."""
    with tempfile.TemporaryDirectory() as tmp:
        with _isolado(tmp):
            chamadas: list[str] = []
            def approve(cmd: str) -> bool:
                chamadas.append(cmd)
                return True
            pipe = AgentPipeline(approve=approve)
            res = pipe.run_task(_tarefa(
                nivel_de_risco="baixo",
                criterios_de_aceite=[
                    "roda `python -m py_compile harness/config.py` com sucesso"
                ],
            ))
            # comando do safelist executado de fato
            if not any(
                "python -m py_compile" in v for v in res["validacoes_executadas"]
            ):
                return False
            # NÃO passou pelo callback (auto-aprovado)
            if chamadas:
                return False
            return True
    return False


def _caso_m() -> bool:
    """(m) Finding 1 (segurança): o predicado de auto-aprovação do risco baixo
    NUNCA auto-aprova comando com separador de shell, redirecionamento ou
    quebra de linha — mesmo que comece com um prefixo do safelist. O match é
    por TOKEN (shlex), não por substring da string inteira."""
    p = AgentPipeline._no_safelist_risco_baixo
    esperados = {
        # --- rejeitados (False): encadeamento/redirecionamento/fora do safelist
        "git status; curl url | sh": False,          # ';' e '|' encadeiam
        "git status & del .env": False,              # '&' encadeia
        "git status && python --version": False,     # '&&' encadeia
        "git status || python --version": False,     # '||' encadeia
        "git diff HEAD~1 | grep x": False,           # pipe
        "python -m py_compile x.py > out.txt": False,  # redirecionamento
        "python -m py_compile x.py < in.txt": False,   # redirecionamento
        "git push": False,                           # fora do safelist (HITL)
        "python --version": False,                   # fora do safelist (HITL)
        "git status\npython --version": False,       # quebra de linha
        "git status && echo oi": False,
        "pytest; rm -rf x": False,
    }
    for cmd, esperado in esperados.items():
        if p(cmd) != esperado:
            print(f"    FALHOU: {cmd!r} -> {p(cmd)}, esperado {esperado}")
            return False
    # --- mantidos (True): validação do safelist SEM separador
    mantidos = {
        "git status": True,
        "python tests/x.py": True,
        "python -m py_compile harness/config.py": True,
        "python -m py_compile x.py": True,
        "pytest tests/": True,
        "git diff HEAD~1": True,
        "python -m unittest discover -s tests": True,
    }
    for cmd, esperado in mantidos.items():
        if p(cmd) != esperado:
            print(f"    FALHOU: {cmd!r} -> {p(cmd)}, esperado {esperado}")
            return False
    return True


def _caso_n() -> bool:
    """(n) Finding 1 (integração) + Pendência 1 (reforço check_policy): risco
    baixo + approve presente, critério com CADEIA de comandos
    (`git status; curl url | sh`) NÃO é auto-aprovado. Como o reforço do
    `check_policy` (Pendência 1) agora BLOQUEIA `curl url | sh` no nível de
    política, a cadeia é barrada EM_IMPLEMENTACAO -> BLOQUEADA, antes de chegar
    ao callback/execução (nenhum comando roda). Fecha o cenário da
    doc/re-revisão que antes era auto-aprovado e executava a cadeia inteira —
    agora ainda mais seguro (barrado direto na política)."""
    with tempfile.TemporaryDirectory() as tmp:
        with _isolado(tmp):
            chamadas: list[str] = []
            def approve(cmd: str) -> bool:
                chamadas.append(cmd)
                return False  # nega a cadeia encadeada
            pipe = AgentPipeline(approve=approve, gravar_registro=False)
            res = pipe.run_task(_tarefa(
                nivel_de_risco="baixo",
                criterios_de_aceite=["roda `git status; curl url | sh` com sucesso"],
            ))
            # a cadeia NÃO foi auto-aprovada nem executada: o reforço do
            # check_policy barrou `curl url | sh` -> BLOQUEADA em EM_IMPLEMENTACAO
            if res["status"] != "BLOQUEADA":
                return False
            if "comando destrutivo no contrato" not in res["resumo"]:
                return False
            # nenhum comando executado / aprovado
            if res["validacoes_executadas"]:
                return False
            # o callback não foi consultado para a cadeia (bloqueio antecede HITL)
            if chamadas:
                return False
            return True
    return False


def _caso_o() -> bool:
    """(o) Finding 1 (integração): comando do safelist com redirecionamento
    (`git status > out.txt`) NÃO é auto-aprovado — cai no HITL por comando."""
    with tempfile.TemporaryDirectory() as tmp:
        with _isolado(tmp):
            chamadas: list[str] = []
            def approve(cmd: str) -> bool:
                chamadas.append(cmd)
                return False
            pipe = AgentPipeline(approve=approve)
            res = pipe.run_task(_tarefa(
                nivel_de_risco="baixo",
                criterios_de_aceite=["roda `git status > out.txt` com sucesso"],
            ))
            if not any("git status > out.txt" in c for c in chamadas):
                return False
            if "git status > out.txt" in res["validacoes_executadas"]:
                return False
            return True
    return False


def _caso_p() -> bool:
    """(p) Finding 1: comando do safelist SEM separador (ex.: `python -m
    py_compile x.py`) continua auto-aprovado no risco baixo — o comportamento
    desejado de validação é preservado (fechamento não quebra o safelist)."""
    with tempfile.TemporaryDirectory() as tmp:
        with _isolado(tmp):
            chamadas: list[str] = []
            def approve(cmd: str) -> bool:
                chamadas.append(cmd)
                return True
            pipe = AgentPipeline(approve=approve)
            res = pipe.run_task(_tarefa(
                nivel_de_risco="baixo",
                criterios_de_aceite=["roda `python -m py_compile x.py` com sucesso"],
            ))
            if not any("python -m py_compile x.py" in v
                       for v in res["validacoes_executadas"]):
                return False
            if chamadas:
                return False  # auto-aprovado, não consultou o callback
            return True
    return False


def _caso_q() -> bool:
    """(q) Update Final: `_monta_contrato` deriva grau_complexidade e
    tempo_maximo_seg a partir do texto (sem override). Tarefa de consulta
    simples -> baixo/600; implementação moderada -> medio/900; sistema
    completo + infra + produção -> alto/1500."""
    pipe = AgentPipeline(gravar_registro=False)
    # baixo (consulta simples) -> 600
    c1 = pipe._monta_contrato({
        "objetivo": "consulta ao banco e exibe o resultado",
        "escopo": "tests/",
        "criterios_de_aceite": ["roda `python --version`"],
    })
    if c1.grau_complexidade != "baixo" or c1.tempo_maximo_seg != 600:
        print(f"    FALHOU: baixo -> {c1.grau_complexidade}/{c1.tempo_maximo_seg}")
        return False
    # medio (implementação moderada) -> 900
    c2 = pipe._monta_contrato({
        "objetivo": "criar um modulo de api que consulta o banco e "
                    "implementa uma rota de integracao com testes",
        "escopo": "tests/",
        "criterios_de_aceite": ["roda `python --version`"],
    })
    if c2.grau_complexidade != "medio" or c2.tempo_maximo_seg != 900:
        print(f"    FALHOU: medio -> {c2.grau_complexidade}/{c2.tempo_maximo_seg}")
        return False
    # alto (sistema completo + infra + produção) -> 1500
    c3 = pipe._monta_contrato({
        "objetivo": "implementar um sistema completo em producao com docker, "
                    "container, rede, kubernetes, cloud, migracao de esquema, "
                    "seguranca, auth, dados reais, deploy, microservicos, "
                    "orquestracao, enterprise, critico, testes e build",
        "escopo": "tests/",
        "criterios_de_aceite": ["roda `python --version`"],
    })
    if c3.grau_complexidade != "alto" or c3.tempo_maximo_seg != 1500:
        print(f"    FALHOU: alto -> {c3.grau_complexidade}/{c3.tempo_maximo_seg}")
        return False
    return True


def _caso_r() -> bool:
    """(r) Update Final: override explícito respeita. `grau_complexidade`
    explícito no task -> usa o valor (pula o scorer) e deriva o tempo; texto
    de consulta simples NÃO rebaixa para baixo quando override=alto.
    `tempo_maximo_seg` explícito -> respeitado (configurável)."""
    pipe = AgentPipeline(gravar_registro=False)
    # override grau_complexidade=alto (ignora texto de consulta simples)
    c = pipe._monta_contrato({
        "objetivo": "consulta simples ao banco",
        "escopo": "tests/",
        "criterios_de_aceite": ["roda `python --version`"],
        "grau_complexidade": "alto",
    })
    if c.grau_complexidade != "alto" or c.tempo_maximo_seg != 1500:
        print("    FALHOU: override grau=alto")
        return False
    if c.usa_sandbox is not True:
        print("    FALHOU: usa_sandbox True p/ alto")
        return False
    # override tempo_maximo_seg explícito -> respeitado
    c2 = pipe._monta_contrato({
        "objetivo": "consulta simples ao banco",
        "escopo": "tests/",
        "criterios_de_aceite": ["roda `python --version`"],
        "tempo_maximo_seg": 42,
    })
    if c2.tempo_maximo_seg != 42:
        print(f"    FALHOU: tempo explícito -> {c2.tempo_maximo_seg}")
        return False
    # usa_sandbox False para baixo/médio
    c3 = pipe._monta_contrato({
        "objetivo": "consulta simples ao banco",
        "escopo": "tests/",
        "criterios_de_aceite": ["roda `python --version`"],
    })
    if c3.usa_sandbox is not False:
        print("    FALHOU: usa_sandbox False p/ baixo")
        return False
    return True


def _caso_s() -> bool:
    """(s) Update Final: usa_sandbox True SOMENTE para grau alto (baixo/médio
    ficam no host), via `_monta_contrato`."""
    pipe = AgentPipeline(gravar_registro=False)
    for grau, esperado in (("baixo", False), ("medio", False), ("alto", True)):
        c = pipe._monta_contrato({
            "objetivo": "x",
            "escopo": "tests/",
            "criterios_de_aceite": ["roda `python --version`"],
            "grau_complexidade": grau,
        })
        if c.usa_sandbox != esperado:
            print(f"    FALHOU: grau {grau} -> usa_sandbox {c.usa_sandbox}, "
                  f"esperado {esperado}")
            return False
    return True


def _caso_t() -> bool:
    """(t) Update Final: TaskContract.validate valida grau_complexidade e
    tempo_maximo_seg. Grau inválido / tempo <= 0 -> erro."""
    ok = TaskContract(objetivo="x", escopo="y", criterios_de_aceite=["z"],
                      grau_complexidade="alto", tempo_maximo_seg=1500).validate()
    bad_grau = TaskContract(objetivo="x", escopo="y", criterios_de_aceite=["z"],
                            grau_complexidade="critico").validate()
    bad_tempo = TaskContract(objetivo="x", escopo="y", criterios_de_aceite=["z"],
                             tempo_maximo_seg=0).validate()
    return (
        not any("grau_complexidade" in e for e in ok)
        and any("grau_complexidade inválido" in e for e in bad_grau)
        and any("tempo_maximo_seg inválido" in e for e in bad_tempo)
    )


def _caso_u() -> bool:
    """(u) Update Final: timeout safety net (Exception Ch12). Com
    tempo_maximo_seg mínimo, o pipeline encerra com APROVADA_COM_RESSALVAS +
    achado de timeout, preservando as etapas já registradas (não BLOQUEIA, não
    corta no meio). O progresso (etapas) é preservado no resultado."""
    with tempfile.TemporaryDirectory() as tmp:
        with _isolado(tmp):
            # tempo_maximo_seg=1 (mínimo): o decorrido real do pipeline
            # (memória+exploração+implementação) ultrapassa 1s em máquinas
            # lentas; para determinismo, mockamos o tempo decorrido.
            pipe = AgentPipeline(approve=lambda cmd: True,
                                 gravar_registro=False)
            with mock.patch("harness.pipeline.time.monotonic",
                            side_effect=[100.0, 100.0, 102.0, 102.0, 102.0,
                                         102.0, 102.0, 102.0]):
                res = pipe.run_task(_tarefa(
                    tempo_maximo_seg=1,
                    criterios_de_aceite=["roda `python --version` com sucesso"],
                ))
            if res["status"] != "APROVADA_COM_RESSALVAS":
                print(f"    FALHOU: status {res['status']}")
                return False
            if res.get("timeout_safety_net") is not True:
                print("    FALHOU: timeout_safety_net ausente")
                return False
            if not any("timeout safety net" in a.get("descricao", "")
                       for a in res["achados_da_revisao"]):
                print("    FALHOU: achado de timeout ausente")
                return False
            # progresso preservado: etapas registradas até o encerramento
            if not res["etapas"]:
                print("    FALHOU: etapas não preservadas")
                return False
            return True
    return False


def _caso_v() -> bool:
    """(v) Achados 1 e 2 (re-revisão): o BÔNUS de risco é exercitado no
    pipeline (não mais código morto) e `nivel_de_risco="medio"` (default) é
    tratado como NÃO-informado:
      - (a) `nivel_de_risco="alto"` -> scorer aplica bônus +3 (grau sobe além
        do sem sinal);
      - (b) `nivel_de_risco="medio"` (default) -> deriva como não-informado,
        SEM bônus;
      - (c) sem `nivel_de_risco` -> deriva SEM bônus;
      - (d) `grau_complexidade` explícito -> pula o scorer, usa o valor direto.
    """
    pipe = AgentPipeline(gravar_registro=False)
    base = {
        "objetivo": "consulta simples ao banco",
        "escopo": "tests/",
        "criterios_de_aceite": ["roda `python --version`"],
    }
    sem = pipe._monta_contrato(base).grau_complexidade
    com_alto = pipe._monta_contrato({**base, "nivel_de_risco": "alto"}).grau_complexidade
    com_medio = pipe._monta_contrato({**base, "nivel_de_risco": "medio"}).grau_complexidade
    com_grau = pipe._monta_contrato({**base, "grau_complexidade": "alto"}).grau_complexidade
    # (a) bônus aplicado: texto base (baixo) vira médio com risco alto
    if not (sem != com_alto and com_alto == "medio"):
        print(f"    FALHOU (a): sem={sem}, alto={com_alto}")
        return False
    # (b) medio (default) == sem sinal (sem bônus)
    if com_medio != sem:
        print(f"    FALHOU (b): medio={com_medio} != sem={sem}")
        return False
    # (c) sem sinal -> sem bônus (baixo)
    if sem != "baixo":
        print(f"    FALHOU (c): sem={sem}")
        return False
    # (d) override por grau_complexidade pula o scorer
    if com_grau != "alto":
        print(f"    FALHOU (d): grau explicito={com_grau}")
        return False
    return True


def _caso_w() -> bool:
    """(w) Lote 1 (otimização de contexto): `contexto_grau` derivado do grau
    de complexidade no contrato (`_monta_contrato`) e presente no contrato de
    saída (`run_task`), coerente com o grau derivado."""
    pipe = AgentPipeline(gravar_registro=False)
    # override grau=alto -> contexto completo
    c = pipe._monta_contrato({
        "objetivo": "consulta simples ao banco",
        "escopo": "tests/",
        "criterios_de_aceite": ["roda `python --version`"],
        "grau_complexidade": "alto",
    })
    if c.grau_complexidade != "alto" or c.contexto_grau != "completo":
        print(f"    FALHOU: override alto -> {c.grau_complexidade}/{c.contexto_grau}")
        return False
    # sem grau explícito: consulta simples -> baixo -> minimo
    c2 = pipe._monta_contrato({
        "objetivo": "consulta ao banco e exibe o resultado",
        "escopo": "tests/",
        "criterios_de_aceite": ["roda `python --version`"],
    })
    if c2.grau_complexidade != "baixo" or c2.contexto_grau != "minimo":
        print(f"    FALHOU: baixo -> {c2.grau_complexidade}/{c2.contexto_grau}")
        return False
    # implementação moderada -> medio -> padrao
    c3 = pipe._monta_contrato({
        "objetivo": "criar um modulo de api que consulta o banco e "
                    "implementa uma rota de integracao com testes",
        "escopo": "tests/",
        "criterios_de_aceite": ["roda `python --version`"],
    })
    if c3.grau_complexidade != "medio" or c3.contexto_grau != "padrao":
        print(f"    FALHOU: medio -> {c3.grau_complexidade}/{c3.contexto_grau}")
        return False
    # contexto_grau presente no contrato de saída (run_task) e coerente
    with tempfile.TemporaryDirectory() as tmp:
        with _isolado(tmp):
            res = pipe.run_task(_tarefa())  # baixo -> minimo
            if res.get("contexto_grau") != "minimo":
                print(f"    FALHOU: saída contexto_grau = {res.get('contexto_grau')}")
                return False
            if res.get("grau_complexidade") != "baixo":
                print(f"    FALHOU: saída grau = {res.get('grau_complexidade')}")
                return False
    return True


def _caso_x() -> bool:
    """(x) Lote 1: TaskContract.validate valida `contexto_grau` (inválido ->
    erro; válido -> sem erro)."""
    ok = TaskContract(objetivo="x", escopo="y", criterios_de_aceite=["z"],
                      contexto_grau="padrao").validate()
    bad = TaskContract(objetivo="x", escopo="y", criterios_de_aceite=["z"],
                       contexto_grau="gigante").validate()
    return (
        not any("contexto_grau" in e for e in ok)
        and any("contexto_grau inválido" in e for e in bad)
    )


def _caso_y() -> bool:
    """(y) Achado 1 (re-revisão): `RAG_LIMIT_POR_COMPLEXIDADE` usa as chaves
    de CONTEXTO (minimo|padrao|completo) — não de grau — coerente com o lookup
    em CONSULTANDO_MEMORIA (`contrato.contexto_grau`). Contrato alto/completo
    produz RAG limit 6; medio/padrao 4; baixo/minimo 2."""
    # (a) dicionário: chaves de contexto, valores 2/4/6
    esperado = {"minimo": 2, "padrao": 4, "completo": 6}
    if config.RAG_LIMIT_POR_COMPLEXIDADE != esperado:
        print(f"    FALHOU: dict = {config.RAG_LIMIT_POR_COMPLEXIDADE}")
        return False
    # (b) lookup idêntico ao do pipeline: contexto_grau -> limit
    pipe = AgentPipeline(gravar_registro=False)
    for grau, ctx, limit in (("baixo", "minimo", 2),
                             ("medio", "padrao", 4),
                             ("alto", "completo", 6)):
        c = pipe._monta_contrato({
            "objetivo": "x",
            "escopo": "tests/",
            "criterios_de_aceite": ["roda `python --version`"],
            "grau_complexidade": grau,
        })
        obtido = config.RAG_LIMIT_POR_COMPLEXIDADE.get(c.contexto_grau, 4)
        if c.contexto_grau != ctx or obtido != limit:
            print(f"    FALHOU: grau={grau} ctx={c.contexto_grau} "
                  f"limit={obtido} (esperado {ctx}/{limit})")
            return False
    return True


def _caso_z() -> bool:
    """(z) M7: perfil='completo' -> testes_ativos=True e exige PHASE3_GATE.
    Sem approve -> BLOQUEADA; com approve -> segue. `perfil`/`testes_ativos`
    aparecem no contrato de saída."""
    with tempfile.TemporaryDirectory() as tmp:
        with _isolado(tmp):
            # sem approve: o gate da Fase 3 bloqueia antes de executar
            pipe = AgentPipeline(approve=None, gravar_registro=False)
            res = pipe.run_task(_tarefa(perfil="completo"))
            if res["status"] != "BLOQUEADA":
                return False
            if res.get("testes_ativos") is not True:
                return False
            if res.get("perfil") != "completo":
                return False
            if "fase 3" not in res["resumo"]:
                return False
            if res["validacoes_executadas"]:
                return False
            # com approve: PHASE3_GATE é consultado e o pipeline segue
            chamadas: list[str] = []

            def approve(cmd: str) -> bool:
                chamadas.append(cmd)
                return True

            pipe2 = AgentPipeline(approve=approve, gravar_registro=False)
            res2 = pipe2.run_task(_tarefa(perfil="completo"))
            if config.PHASE3_GATE not in chamadas:
                return False
            if res2["status"] == "BLOQUEADA":
                return False
            if res2.get("perfil") != "completo":
                return False
            if res2.get("testes_ativos") is not True:
                return False
            return True
    return False


def _caso_aa() -> bool:
    """(aa) M7: perfis osint/superficial/vazio -> testes_ativos=False e o
    `perfil` informado é exposto no contrato de saída."""
    with tempfile.TemporaryDirectory() as tmp:
        with _isolado(tmp):
            for perfil in ("osint", "superficial", ""):
                pipe = AgentPipeline(
                    approve=lambda cmd: True, gravar_registro=False,
                )
                task = _tarefa(perfil=perfil) if perfil else _tarefa()
                res = pipe.run_task(task)
                if res.get("testes_ativos") is not False:
                    print(f"    FALHOU: perfil={perfil!r} "
                          f"testes_ativos={res.get('testes_ativos')}")
                    return False
                if res.get("perfil") != perfil:
                    print(f"    FALHOU: perfil exposto "
                          f"{res.get('perfil')!r} != {perfil!r}")
                    return False
            return True
    return False


def _caso_ab() -> bool:
    """(ab) M7: `_monta_contrato` mapeia perfil->testes_ativos (completo=True;
    osint/desconhecido=False) e preserva `perfil` (campo aditivo)."""
    pipe = AgentPipeline(gravar_registro=False)

    def _contrato(perfil: str) -> TaskContract:
        return pipe._monta_contrato({
            "objetivo": "x",
            "escopo": "tests/",
            "criterios_de_aceite": ["roda `python --version`"],
            "perfil": perfil,
        })

    c_completo = _contrato("completo")
    c_osint = _contrato("osint")
    c_estranho = _contrato("profundo")
    return (
        c_completo.testes_ativos is True and c_completo.perfil == "completo"
        and c_osint.testes_ativos is False and c_osint.perfil == "osint"
        and c_estranho.testes_ativos is False
        and c_estranho.perfil == "profundo"
    )


def _caso_ac() -> bool:
    """(ac) Item 2.2 — no estado CONSULTANDO_MEMORIA o pipeline passa
    `min_trust=config.RAG_MIN_TRUST` ('media'): registro de trust fraca NÃO
    alimenta o pipeline; o de trust alta alimenta. Verificado pelas ids
    recuperadas na etapa (detalhe de CONSULTANDO_MEMORIA)."""
    with tempfile.TemporaryDirectory() as tmp:
        with _isolado(tmp):
            m = Memory()
            m.record(
                keywords=["tarefa", "teste", "pipeline"], agente="hub",
                tema="memoria alta", entrada="", fluxo="", resultado="",
                contexto="", trust="alta", origem="evidência externa",
                validado_por="reviewer",
            )
            m.record(
                keywords=["tarefa", "teste", "pipeline"], agente="hub",
                tema="memoria fraca", entrada="", fluxo="", resultado="",
                contexto="", trust="fraca", origem="sem validação",
                validado_por="motor",
            )
            pipe = AgentPipeline(memory=m, approve=lambda cmd: True,
                                 gravar_registro=False)
            res = pipe.run_task(_tarefa())
            detalhes = [
                e["detalhe"] for e in res["etapas"]
                if e["etapa"] == "CONSULTANDO_MEMORIA"
            ]
            if not detalhes:
                return False
            detalhe = detalhes[0]
            return (
                config.RAG_MIN_TRUST == "media"
                and "memoria-alta" in detalhe
                and "memoria-fraca" not in detalhe
            )
    return False


def _caso_ad() -> bool:
    """(ad) B2: o contrato BLOQUEADA expõe a chave `saude` (quando calculável),
    com o mesmo shape do sucesso (`score`/`nivel`/`fatores`/`metrica`)."""
    with tempfile.TemporaryDirectory() as tmp:
        with _isolado(tmp):
            # (i) bloqueio por gate de risco alto (approve ausente)
            pipe = AgentPipeline(approve=None)
            res = pipe.run_task(_tarefa(nivel_de_risco="alto"))
            if res["status"] != "BLOQUEADA":
                return False
            saude = res.get("saude")
            if not isinstance(saude, dict):
                print("    FALHOU: saude ausente no BLOQUEADA")
                return False
            if not {"score", "nivel", "fatores", "metrica"} <= set(saude):
                print(f"    FALHOU: shape de saude = {set(saude)}")
                return False
            # (ii) contrato inválido -> BLOQUEADA em RECEBIDA também expõe saude
            res2 = pipe.run_task({"objetivo": "", "escopo": ""})
            if res2["status"] != "BLOQUEADA":
                return False
            if not isinstance(res2.get("saude"), dict):
                return False
            return True
    return False


def _caso_ae() -> bool:
    """(ae) Achado MÉDIA A1 no pipeline: a extração de comandos dos critérios é
    ESTRITA. Prosa ("o codigo deve ser python e funcionar") NÃO vira passo
    sugerido (antes virava `python e funcionar`); um comando real (com ou sem
    backticks) continua sendo extraído."""
    pipe = AgentPipeline(gravar_registro=False)
    prosa = pipe._sugere_comandos(pipe._monta_contrato(_tarefa(
        criterios_de_aceite=["o codigo deve ser python e funcionar"],
    )))
    real = pipe._sugere_comandos(pipe._monta_contrato(_tarefa(
        criterios_de_aceite=["executar `pytest tests/` no fim"],
    )))
    real_sem_backtick = pipe._sugere_comandos(pipe._monta_contrato(_tarefa(
        criterios_de_aceite=["rodar python -m py_compile x.py"],
    )))
    return (
        prosa == []
        and "pytest tests/" in real
        and "python -m py_compile x.py" in real_sem_backtick
    )


def _caso_af() -> bool:
    """(af) Etapa 5 — ladder: SAUDAVEL/ATENCAO NÃO escalam (risco/política
    inalterados), expõem `saude` no contrato e `escalonamento` vazio."""
    with tempfile.TemporaryDirectory() as tmp:
        with _isolado(tmp):
            for nivel in ("SAUDAVEL", "ATENCAO"):
                pipe = AgentPipeline(approve=lambda cmd: True,
                                     gravar_registro=False)
                with mock.patch("harness.pipeline.health.gerar_saude",
                                return_value=_saude_mock(nivel)):
                    res = pipe.run_task(_tarefa(nivel_de_risco="baixo"))
                if res["status"] == "BLOQUEADA":
                    print(f"    FALHOU: {nivel} bloqueou")
                    return False
                if res.get("escalonamento") != []:
                    print(f"    FALHOU: {nivel} escalonou {res.get('escalonamento')}")
                    return False
                if res.get("risco") != "baixo" or res.get("politica") != "auto":
                    print(f"    FALHOU: {nivel} risco/política "
                          f"{res.get('risco')}/{res.get('politica')}")
                    return False
                if res.get("saude", {}).get("nivel") != nivel:
                    print(f"    FALHOU: {nivel} saude {res.get('saude')}")
                    return False
            return True
    return False


def _caso_ag() -> bool:
    """(ag) Etapa 5 — DEGRADADO sem approve: força risco alto (gate_global ->
    BLOQUEADA antes de executar), expõe `usa_sandbox`/`escalonamento` e não roda
    nenhum comando."""
    with tempfile.TemporaryDirectory() as tmp:
        with _isolado(tmp):
            pipe = AgentPipeline(approve=None, gravar_registro=False)
            with mock.patch("harness.pipeline.health.gerar_saude",
                            return_value=_saude_mock("DEGRADADO")):
                res = pipe.run_task(_tarefa(nivel_de_risco="baixo"))
            if res["status"] != "BLOQUEADA":
                return False
            if res.get("risco") != "alto":
                return False
            if res.get("politica") != "gate_global":
                return False
            if res.get("usa_sandbox") is not True:
                return False
            esc = set(res.get("escalonamento") or [])
            if not {"risco_alto", "sandbox", "memoria_bloqueada"} <= esc:
                print(f"    FALHOU: escalonamento {esc}")
                return False
            if res["validacoes_executadas"]:
                return False
            return True
    return False


def _caso_ah() -> bool:
    """(ah) Etapa 5 — DEGRADADO com approve do RISCO_GATE prossegue endurecido
    (`usa_sandbox=True`) e a gravação de memória é BLOQUEADA pelo
    anti-envenomamento (saúde de ENTRADA)."""
    with tempfile.TemporaryDirectory() as tmp:
        with _isolado(tmp):
            chamadas: list[str] = []

            def approve(cmd: str) -> bool:
                chamadas.append(cmd)
                return True

            pipe = AgentPipeline(approve=approve, gravar_registro=True)
            with mock.patch("harness.pipeline.health.gerar_saude",
                            return_value=_saude_mock("DEGRADADO")):
                res = pipe.run_task(_tarefa(nivel_de_risco="baixo"))
            if res["status"] == "BLOQUEADA":
                return False
            if config.RISCO_GATE_ALTO not in chamadas:
                print("    FALHOU: RISCO_GATE não consultado")
                return False
            if res.get("usa_sandbox") is not True:
                return False
            if not res.get("validacoes_executadas"):
                return False
            if pipe.memory.list_records() != []:
                print("    FALHOU: memória gravada em DEGRADADO")
                return False
            if not any(e.get("motivo") == "saude_baixa"
                       for e in res["etapas"]):
                print("    FALHOU: etapa de bloqueio de memória ausente")
                return False
            return True
    return False


def _caso_ai() -> bool:
    """(ai) Etapa 5 — CRITICO sem `approve(SAUDE_GATE_CRITICO)` (ausente ou
    negado) -> BLOQUEADA sem executar; expõe `saude`/`escalonamento`."""
    with tempfile.TemporaryDirectory() as tmp:
        with _isolado(tmp):
            # approve ausente
            pipe = AgentPipeline(approve=None, gravar_registro=False)
            with mock.patch("harness.pipeline.health.gerar_saude",
                            return_value=_saude_mock("CRITICO")):
                res = pipe.run_task(_tarefa(nivel_de_risco="baixo"))
            if res["status"] != "BLOQUEADA":
                return False
            if "CRITICA" not in res["resumo"]:
                return False
            if res["validacoes_executadas"]:
                return False
            if "gate_critico" not in set(res.get("escalonamento") or []):
                return False
            if res.get("saude", {}).get("nivel") != "CRITICO":
                return False
            # approve que NEGA o gate crítico
            chamadas: list[str] = []

            def approve(cmd: str) -> bool:
                chamadas.append(cmd)
                return cmd != config.SAUDE_GATE_CRITICO

            pipe2 = AgentPipeline(approve=approve, gravar_registro=False)
            with mock.patch("harness.pipeline.health.gerar_saude",
                            return_value=_saude_mock("CRITICO")):
                res2 = pipe2.run_task(_tarefa(nivel_de_risco="alto"))
            if res2["status"] != "BLOQUEADA":
                return False
            if "negada" not in res2["resumo"]:
                return False
            if config.SAUDE_GATE_CRITICO not in chamadas:
                return False
            if res2["validacoes_executadas"]:
                return False
            return True
    return False


def _caso_aj() -> bool:
    """(aj) Etapa 5 — CRITICO com o gate crítico E o risk gate aprovados ->
    prossegue endurecido (risco alto/usa_sandbox, `gate_critico` no
    escalonamento e ambos os gates consultados uma vez)."""
    with tempfile.TemporaryDirectory() as tmp:
        with _isolado(tmp):
            chamadas: list[str] = []

            def approve(cmd: str) -> bool:
                chamadas.append(cmd)
                return True

            pipe = AgentPipeline(approve=approve, gravar_registro=False)
            with mock.patch("harness.pipeline.health.gerar_saude",
                            return_value=_saude_mock("CRITICO")):
                res = pipe.run_task(_tarefa(nivel_de_risco="baixo"))
            if res["status"] == "BLOQUEADA":
                return False
            if config.SAUDE_GATE_CRITICO not in chamadas:
                return False
            if chamadas.count(config.SAUDE_GATE_CRITICO) != 1:
                return False
            if config.RISCO_GATE_ALTO not in chamadas:
                return False
            if chamadas.count(config.RISCO_GATE_ALTO) != 1:
                return False
            if res.get("usa_sandbox") is not True:
                return False
            if "gate_critico" not in set(res.get("escalonamento") or []):
                return False
            if not res.get("validacoes_executadas"):
                return False
            return True
    return False


def _caso_ak() -> bool:
    """(ak) Etapa 5 — a ladder usa a saúde de ENTRADA (não a pós-run): o
    snapshot de entrada DEGRADADO escala; a saúde pós-run (SAUDAVEL) só é
    exposta no contrato e NÃO desfaz o escalonamento nem o bloqueio de memória."""
    with tempfile.TemporaryDirectory() as tmp:
        with _isolado(tmp):
            pipe = AgentPipeline(approve=lambda cmd: True, gravar_registro=True)
            estado = {"n": 0}

            def fake_saude(*_args, **_kwargs):
                estado["n"] += 1
                if estado["n"] == 1:  # snapshot de ENTRADA
                    return _saude_mock("DEGRADADO")
                return _saude_mock("SAUDAVEL")  # pós-run

            with mock.patch("harness.pipeline.health.gerar_saude",
                            side_effect=fake_saude):
                res = pipe.run_task(_tarefa(nivel_de_risco="baixo"))
            esc = set(res.get("escalonamento") or [])
            if not {"risco_alto", "sandbox", "memoria_bloqueada"} <= esc:
                print(f"    FALHOU: escalonamento {esc}")
                return False
            if res.get("saude", {}).get("nivel") != "SAUDAVEL":
                print(f"    FALHOU: saude de saída {res.get('saude')}")
                return False
            if pipe.memory.list_records() != []:
                print("    FALHOU: memória gravada (entrada DEGRADADO)")
                return False
            if not any(e.get("motivo") == "saude_baixa"
                       for e in res["etapas"]):
                return False
            return True
    return False


def _caso_al() -> bool:
    """(al) Etapa 5 — retrocompatibilidade: num harness de teste isolado
    (bootstrap/neutro) a ladder NÃO escala (risco/política preservados,
    `escalonamento` vazio) e a execução normal continua."""
    with tempfile.TemporaryDirectory() as tmp:
        with _isolado(tmp):
            pipe = AgentPipeline(approve=lambda cmd: True,
                                 gravar_registro=False)
            saude = pipe._saude_atual()
            if not saude.get("bootstrap"):
                print(f"    FALHOU: harness isolado não é bootstrap "
                      f"({saude.get('nivel')}, fontes_llm="
                      f"{saude.get('fontes_llm')})")
                return False
            res = pipe.run_task(_tarefa(nivel_de_risco="baixo"))
            if res.get("escalonamento") != []:
                print(f"    FALHOU: escalonou sem fontes: "
                      f"{res.get('escalonamento')}")
                return False
            if res.get("risco") != "baixo" or res.get("politica") != "auto":
                return False
            if res["status"] == "BLOQUEADA":
                return False
            return True
    return False


def _caso_am() -> bool:
    """(am) Achado 1 (correção): `run_task` com entrada não-dict NÃO vaza
    `etapas` de uma execução anterior — o log é zerado ANTES da validação de
    tipo, então o early-return devolve apenas a etapa de bloqueio."""
    with tempfile.TemporaryDirectory() as tmp:
        with _isolado(tmp):
            pipe = AgentPipeline(approve=lambda cmd: True,
                                 gravar_registro=False)
            # 1ª execução VÁLIDA popula `_etapas` (várias etapas)
            res1 = pipe.run_task(_tarefa())
            if not res1["etapas"]:
                print("    FALHOU: execução válida sem etapas")
                return False
            # early-return: etapas de res1 NÃO podem contaminar res2
            res2 = pipe.run_task("não é um dict")
            if res2["status"] != "BLOQUEADA":
                print(f"    FALHOU: status {res2['status']}")
                return False
            if len(res2["etapas"]) != 1:
                print(f"    FALHOU: etapas vazadas: {res2['etapas']}")
                return False
            if res2["etapas"][0]["etapa"] != "RECEBIDA":
                print(f"    FALHOU: etapa {res2['etapas'][0]}")
                return False
            if "tarefa deve ser um dict" not in res2["etapas"][0]["detalhe"]:
                return False
            return True
    return False


def _caso_an() -> bool:
    """(an) Achado 2 (correção): `indisponivel=True` (falha do detector) NÃO é
    mais tratado como SAUDAVEL silencioso. A ladder: (i) NÃO escala gates
    (risco/política preservados, `escalonamento` vazio — evita DoS); (ii) expõe
    o motivo (`saude_indisponivel=True` no contrato e em etapa com o texto
    "detector indisponível"); (iii) `_pode_gravar` segue CONSERVADOR (bloqueia)."""
    with tempfile.TemporaryDirectory() as tmp:
        with _isolado(tmp):
            pipe = AgentPipeline(approve=lambda cmd: True,
                                 gravar_registro=False)
            with mock.patch("harness.pipeline.health.gerar_saude",
                            return_value=_saude_mock(
                                "SAUDAVEL", indisponivel=True)):
                res = pipe.run_task(_tarefa(nivel_de_risco="baixo"))
            if res["status"] == "BLOQUEADA":
                print("    FALHOU: indisponível bloqueou a execução (DoS)")
                return False
            if res.get("escalonamento") != []:
                print(f"    FALHOU: indisponível escalou "
                      f"{res.get('escalonamento')}")
                return False
            if res.get("risco") != "baixo" or res.get("politica") != "auto":
                print(f"    FALHOU: risco/política alterados "
                      f"{res.get('risco')}/{res.get('politica')}")
                return False
            if res.get("saude_indisponivel") is not True:
                print("    FALHOU: saude_indisponivel ausente no contrato")
                return False
            if not any(
                e.get("saude_indisponivel")
                and "detector indisponível" in e.get("detalhe", "")
                for e in res["etapas"]
            ):
                print("    FALHOU: etapa não sinaliza detector indisponível")
                return False
            # _pode_gravar segue conservador (indisponível bloqueia gravação)
            if AgentPipeline._pode_gravar(
                _saude_mock("DEGRADADO", indisponivel=True)
            ):
                print("    FALHOU: _pode_gravar liberou em indisponível")
                return False
            return True
    return False


def _caso_ao() -> bool:
    """(ao) Achado 2: `bootstrap=True` (sem fontes de LLM) com nível sintético
    DEGRADADO/CRITICO NÃO escala (risco/política preservados, nenhum gate de
    risco) e PERMITE o bootstrap (o anti-envenomamento não bloqueia a gravação
    — o harness novo precisa gravar o primeiro registro)."""
    with tempfile.TemporaryDirectory() as tmp:
        with _isolado(tmp):
            for nivel in ("DEGRADADO", "CRITICO"):
                chamadas: list[str] = []

                def approve(cmd: str, _chamadas=chamadas) -> bool:
                    _chamadas.append(cmd)
                    return True

                pipe = AgentPipeline(approve=approve, gravar_registro=True)
                with mock.patch("harness.pipeline.health.gerar_saude",
                                return_value=_saude_mock(
                                    nivel, bootstrap=True)):
                    res = pipe.run_task(_tarefa(nivel_de_risco="baixo"))
                if res["status"] == "BLOQUEADA":
                    print(f"    FALHOU: bootstrap {nivel} bloqueou")
                    return False
                if res.get("escalonamento") != []:
                    print(f"    FALHOU: bootstrap {nivel} escalou "
                          f"{res.get('escalonamento')}")
                    return False
                if res.get("risco") != "baixo":
                    print(f"    FALHOU: bootstrap {nivel} risco "
                          f"{res.get('risco')}")
                    return False
                if res.get("saude_indisponivel"):
                    print(f"    FALHOU: bootstrap {nivel} marcado indisponível")
                    return False
                if config.RISCO_GATE_ALTO in chamadas:
                    print(f"    FALHOU: bootstrap {nivel} consultou risk gate")
                    return False
                if pipe.memory.list_records() == []:
                    print(f"    FALHOU: bootstrap {nivel} não gravou registro")
                    return False
    return True


def _caso_ap() -> bool:
    """(ap) M4: `pipeline._licoes()` expõe SOMENTE lições confiáveis
    (alta/media) — nunca `fraca` — e descarta meta-ruído; sem playbook -> []."""
    with tempfile.TemporaryDirectory() as tmp:
        with _isolado(tmp):
            pb = {
                "learned": {"por_agente": {"implementer": {
                    "agente": "implementer",
                    "licoes": [
                        {"texto": "Lição confiável validada por evidências de testes.",
                         "ocorrencias": 2, "trust": "alta",
                         "origens": ["r1"], "trust_por_origem": {"r1": "alta"}},
                        {"texto": "Lição fraca de memória antiga que nunca deve aparecer.",
                         "ocorrencias": 9, "trust": "fraca",
                         "origens": ["r2"], "trust_por_origem": {"r2": "fraca"}},
                        {"texto": "Este Contexto alimenta a curva por agente na "
                                  "próxima compilação do playbook.",
                         "ocorrencias": 7, "trust": "media",
                         "origens": ["r3"], "trust_por_origem": {"r3": "media"}},
                    ],
                }}},
            }
            pipe = AgentPipeline(playbook=pb, gravar_registro=False)
            licoes = pipe._licoes()
            if licoes != ["Lição confiável validada por evidências de testes."]:
                print(f"    FALHOU: _licoes() = {licoes}")
                return False
            if AgentPipeline(gravar_registro=False)._licoes() != []:
                print("    FALHOU: _licoes() sem playbook não é []")
                return False
            return True
    return False


def _caso_aq() -> bool:
    """(aq) Item 7.2: timeout COOPERATIVO de ETAPA. Uma etapa que dorme mais que
    `PIPELINE_ETAPA_TIMEOUT_SEG` encerra com APROVADA_COM_RESSALVAS + achado de
    timeout de etapa, preservando o progresso (`progresso_preservado=True`) e
    sem crash (a thread da etapa não é morta — apenas abandonada como daemon)."""
    with tempfile.TemporaryDirectory() as tmp:
        with _isolado(tmp):
            pipe = AgentPipeline(approve=lambda cmd: True,
                                 gravar_registro=False)

            def _explora_lenta(_escopo):
                time.sleep(3)
                return [], [], False

            pipe._explora = _explora_lenta  # type: ignore[method-assign]
            with mock.patch.object(config, "PIPELINE_ETAPA_TIMEOUT_SEG", 0.2):
                res = pipe.run_task(_tarefa())
            if res["status"] != "APROVADA_COM_RESSALVAS":
                print(f"    FALHOU: status {res['status']}")
                return False
            if res.get("progresso_preservado") is not True:
                print("    FALHOU: progresso_preservado ausente")
                return False
            if res.get("timeout_etapa") != "EM_EXPLORACAO":
                print(f"    FALHOU: timeout_etapa={res.get('timeout_etapa')}")
                return False
            if not any("timeout de etapa" in a.get("descricao", "")
                       for a in res["achados_da_revisao"]):
                print("    FALHOU: achado de timeout de etapa ausente")
                return False
            if not res["etapas"]:
                print("    FALHOU: progresso não preservado (sem etapas)")
                return False
            if res.get("timeout_safety_net"):
                print("    FALHOU: timeout de etapa marcado como safety net total")
                return False
            return True
    return False


def _caso_ar() -> bool:
    """(ar) Item 7.2: a exploração respeita `PIPELINE_EXPLORACAO_MAX_ARQUIVOS`
    (glob patológico) e a etapa registra `truncado=True` quando o cap é
    EXCEDIDO. R2 (correção): total EXATAMENTE igual ao cap NÃO trunca."""
    with tempfile.TemporaryDirectory() as tmp:
        with _isolado(tmp):
            root = pathlib.Path(tmp)
            base = root / "muitos"
            base.mkdir()
            for i in range(10):
                (base / f"f{i}.txt").write_text("x", encoding="utf-8")
            pipe = AgentPipeline(gravar_registro=False)
            # com cap 3 -> no máximo 3 arquivos e truncado=True (excedeu o cap)
            with mock.patch.object(config, "ROOT", root), \
                 mock.patch.object(config, "PIPELINE_EXPLORACAO_MAX_ARQUIVOS", 3):
                arquivos, _riscos, truncado = pipe._explora("muitos")
            if len(arquivos) > 3 or not truncado:
                print(f"    FALHOU: cap/truncado ({len(arquivos)}, {truncado})")
                return False
            # R2: EXATAMENTE o cap (3 arquivos, cap 3) NÃO trunca
            base_exatos = root / "exatos"
            base_exatos.mkdir()
            for i in range(3):
                (base_exatos / f"g{i}.txt").write_text("x", encoding="utf-8")
            with mock.patch.object(config, "ROOT", root), \
                 mock.patch.object(config, "PIPELINE_EXPLORACAO_MAX_ARQUIVOS", 3):
                arquivos_exatos, _r_ex, truncado_exatos = pipe._explora("exatos")
            if len(arquivos_exatos) != 3 or truncado_exatos:
                print(f"    FALHOU: total == cap truncou indevidamente "
                      f"({len(arquivos_exatos)}, {truncado_exatos})")
                return False
            # sem cap efetivo -> todos os 10, sem truncamento
            with mock.patch.object(config, "ROOT", root), \
                 mock.patch.object(config, "PIPELINE_EXPLORACAO_MAX_ARQUIVOS", 100):
                arquivos2, _r2, truncado2 = pipe._explora("muitos")
            if len(arquivos2) != 10 or truncado2:
                print(f"    FALHOU: sem cap ({len(arquivos2)}, {truncado2})")
                return False
            # integração: a etapa de exploração registra o truncamento
            pipe2 = AgentPipeline(approve=lambda cmd: True,
                                  gravar_registro=False)
            pipe2._explora = lambda _e: (["a"], [], True)  # type: ignore[method-assign]
            res = pipe2.run_task(_tarefa())
            etapas = [e for e in res["etapas"]
                      if e["etapa"] == "EM_EXPLORACAO"]
            if not etapas or etapas[0].get("truncado") is not True:
                print(f"    FALHOU: etapa sem truncado: {etapas}")
                return False
            if "TRUNCADA" not in etapas[0].get("detalhe", ""):
                print("    FALHOU: detalhe da etapa sem 'TRUNCADA'")
                return False
            return True
    return False


def _caso_as() -> bool:
    """(as) R3: `PIPELINE_ETAPA_TIMEOUT_SEG` é configurável por env (estilo
    `_env_int`) e GARANTE valor > 0 — valor <= 0/inválido volta ao default
    seguro (300). O default resolvido no import também é > 0."""
    if not (isinstance(config.PIPELINE_ETAPA_TIMEOUT_SEG, int)
            and config.PIPELINE_ETAPA_TIMEOUT_SEG > 0):
        print(f"    FALHOU: default {config.PIPELINE_ETAPA_TIMEOUT_SEG}")
        return False
    with mock.patch.dict(os.environ, {"PIPELINE_ETAPA_TIMEOUT_SEG": "45"}):
        if config._env_int_pos("PIPELINE_ETAPA_TIMEOUT_SEG", 300) != 45:
            print("    FALHOU: env válida (45) não foi respeitada")
            return False
        for ruim in ("0", "-5", "abc", ""):
            with mock.patch.dict(os.environ, {"PIPELINE_ETAPA_TIMEOUT_SEG": ruim}):
                if config._env_int_pos("PIPELINE_ETAPA_TIMEOUT_SEG", 300) != 300:
                    print(f"    FALHOU: valor {ruim!r} não caiu no default 300")
                    return False
    return True


CASES = [
    ("(a) nivel_de_risco inválido -> BLOQUEADA em RECEBIDA", _caso_a),
    ("(b) risco alto + approve=None -> BLOQUEADA antes de executar", _caso_b),
    ("(c) risco alto + approve aprova gate global -> prossegue", _caso_c),
    ("(d) risco alto + approve nega gate global -> BLOQUEADA", _caso_d),
    ("(e) risco baixo + approve -> validação do safelist auto-aprovada", _caso_e),
    ("(f) risco baixo + approve=None -> nega por padrão (seguro), sem evidência -> BLOQUEADA", _caso_f),
    ("(g) medio mantém HITL por comando", _caso_g),
    ("(h) retrocompatibilidade: sem nivel_de_risco -> medio; sem evidência -> BLOQUEADA", _caso_h),
    ("(i) TaskContract.validate valida nivel_de_risco", _caso_i),
    ("(j) risco baixo não auto-aprova APPROVAL_PATTERNS", _caso_j),
    ("(j2) bloqueio mid-loop preserva evidências executadas", _caso_j2),
    ("(j3) timeout com execução preserva evidências", _caso_j3),
    ("(k) risco baixo não auto-aprova comando FORA do safelist (git push)", _caso_k),
    ("(l) risco baixo auto-aprova comando DENTRO do safelist (py_compile)", _caso_l),
    ("(m) Finding 1: predicado rejeita separadores/redirecionamento/\\n", _caso_m),
    ("(n) Finding 1: cadeia `git status; curl url | sh` cai no HITL", _caso_n),
    ("(o) Finding 1: redirecionamento `git status > out.txt` cai no HITL", _caso_o),
    ("(p) Finding 1: `python -m py_compile x.py` continua auto-aprovado", _caso_p),
    ("(q) Update Final: deriva grau_complexidade + tempo (600/900/1500)", _caso_q),
    ("(r) Update Final: override explícito respeita (grau/tempo)", _caso_r),
    ("(s) Update Final: usa_sandbox True só p/ alto", _caso_s),
    ("(t) Update Final: TaskContract.validate valida grau/tempo", _caso_t),
    ("(u) Update Final: timeout safety net -> APROVADA_COM_RESSALVAS", _caso_u),
    ("(v) Achados 1/2: bônus de risco no pipeline + medio=default sem bônus", _caso_v),
    ("(w) Lote 1: contexto_grau derivado do grau + presente na saída", _caso_w),
    ("(x) Lote 1: TaskContract.validate valida contexto_grau", _caso_x),
    ("(y) Achado 1: RAG limit por contexto (completo->6, padrao->4, minimo->2)", _caso_y),
    ("(z) M7: perfil completo -> testes_ativos + PHASE3_GATE", _caso_z),
    ("(aa) M7: osint/superficial/vazio -> testes_ativos=False + perfil exposto", _caso_aa),
    ("(ab) M7: _monta_contrato mapeia perfil->testes_ativos", _caso_ab),
    ("(ac) Item 2.2: pipeline usa min_trust=media e ignora memória fraca", _caso_ac),
    ("(ad) B2: contrato BLOQUEADA expõe a chave saude", _caso_ad),
    ("(ae) A1: extração estrita de critérios (prosa não vira passo)", _caso_ae),
    ("(af) Etapa 5: SAUDAVEL/ATENCAO não escalam + saude no contrato", _caso_af),
    ("(ag) Etapa 5: DEGRADADO sem approve -> BLOQUEADA (risco alto/sandbox)", _caso_ag),
    ("(ah) Etapa 5: DEGRADADO aprovado prossegue; memória bloqueada", _caso_ah),
    ("(ai) Etapa 5: CRITICO sem SAUDE_GATE_CRITICO -> BLOQUEADA", _caso_ai),
    ("(aj) Etapa 5: CRITICO com gate crítico + risk gate -> prossegue", _caso_aj),
    ("(ak) Etapa 5: ladder usa a saúde de ENTRADA (não a pós-run)", _caso_ak),
    ("(al) Etapa 5: harness bootstrap/neutro não escala (retrocompat)", _caso_al),
    ("(am) Achado 1: run_task não-dict não vaza etapas", _caso_am),
    ("(an) Achado 2: indisponível -> ATENCAO, sem escalar, com motivo", _caso_an),
    ("(ao) Achado 2: bootstrap DEGRADADO/CRITICO não escala e permite gravar", _caso_ao),
    ("(ap) M4: _licoes() só expõe confiáveis (sem fraca/meta)", _caso_ap),
    ("(aq) Item 7.2: timeout de etapa -> ressalva preservando progresso", _caso_aq),
    ("(ar) Item 7.2: exploração truncada pelo cap + registro", _caso_ar),
    ("(as) R3: PIPELINE_ETAPA_TIMEOUT_SEG configurável por env e > 0", _caso_as),
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
