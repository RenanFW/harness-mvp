"""Testes do aprendizado de agentes e do orquestrador determinístico
(zero dependências). Rode com:
    python tests/agents_test.py
"""

from __future__ import annotations

import os
import pathlib
import re
import subprocess
import sys
import tempfile
import time
import traceback
import uuid
from unittest import mock

sys.path.insert(0, str(pathlib.Path(__file__).resolve().parent.parent))

from harness import config  # noqa: E402
from harness.agents import (  # noqa: E402
    LICAO_RESUMO_CHARS,
    MAX_LICOES_RESUMO,
    AgentLearning,
    HistoryLearner,
    LearnedModel,
    PipelineSpec,
    Playbook,
    _e_licao_meta,
    _extrai_comandos,
    _extrai_secoes,
    compile_playbook,
    licoes_confiaveis_textos,
    parse_agent_file,
    parse_episode,
    resumo_licoes_confiaveis,
)
from harness.pipeline import AgentPipeline, TaskContract  # noqa: E402

# Fixtures auto-contidas (cópia limpa sem memória episódica): a suíte cria
# os dois registros sintéticos em um diretório temporário em vez de depender
# de registros pré-existentes em memory/episodic/ (lição do cleanex — a cópia
# limpa não carrega memória pessoal; o parse_episode testa o MESMO contrato).
# Fixtures auto-contidas (copia limpa sem memoria episodica): a suite usa os
# registros sinteticos em tests/fixtures/ em vez de depender de registros
# pre-existentes em memory/episodic/ (licao do cleanex - a copia limpa nao
# carrega memoria pessoal; o parse_episode testa o MESMO contrato).
_FIXTURES = pathlib.Path(__file__).resolve().parent / "fixtures"
CALCULADORA = _FIXTURES / "registro-calculadora-2026-08-15.md"
VAZIO = _FIXTURES / "registro-2026-08-15-01.md"


def _proc_count_com_marcador(marker: str) -> int:
    """Conta processos python.exe cuja linha de comando contém o marcador
    único (via PowerShell/CIM, só Windows). Retorna -1 se a verificação não
    for possível (PowerShell ausente/timeout): o teste não falha por falta
    de verificação — apenas ignora a checagem."""
    try:
        script = (
            "(Get-CimInstance Win32_Process -Filter \"Name='python.exe'\") | "
            "Where-Object { $_.CommandLine -match '" + marker + "' } | "
            "Measure-Object | Select-Object -ExpandProperty Count"
        )
        out = subprocess.run(
            ["powershell", "-NoProfile", "-NonInteractive", "-Command", script],
            capture_output=True,
            text=True,
            timeout=20,
        )
        return int((out.stdout or "").strip() or 0)
    except (OSError, subprocess.SubprocessError, ValueError, TimeoutError):
        return -1


def _caso_1() -> bool:
    """parse_agent_file de todos os 7 .opencode/agent/*.md reais."""
    specs = []
    for path in sorted(config.OPCODE_AGENT_DIR.glob("*.md")):
        spec = parse_agent_file(path)
        specs.append(spec)
        if spec.name != path.stem:
            return False
        if not {"edit", "bash", "task"} <= set(spec.permissions):
            return False
    return len(specs) == 7


def _caso_2() -> bool:
    """brain.md: permissions.edit é o mapa inline {memory/**: allow, *: deny}."""
    spec = parse_agent_file(config.OPCODE_AGENT_DIR / "brain.md")
    return spec.permissions.get("edit") == {"memory/**": "allow", "*": "deny"}


def _caso_3() -> bool:
    """implementer edita/executa e não delega; hub delega."""
    imp = parse_agent_file(config.OPCODE_AGENT_DIR / "implementer.md")
    hub = parse_agent_file(config.OPCODE_AGENT_DIR / "hub.md")
    return imp.edits and imp.shell and not imp.delegates and hub.delegates


def _caso_3b() -> bool:
    """_perm_nao_deny: mapa inline NEGA-TUDO não conta como permissão; mapa
    com algum allow conta. Antes, QUALQUER dict retornava True (bug)."""
    from harness.agents import _perm_nao_deny  # noqa: E402
    return (
        _perm_nao_deny({"**": "deny"}) is False
        and _perm_nao_deny({}) is False
        and _perm_nao_deny({"*.md": "allow", "**": "deny"}) is True
        and _perm_nao_deny({"*": "ask"}) is True
        and _perm_nao_deny("deny") is False
        and _perm_nao_deny("allow") is True
    )


def _caso_4() -> bool:
    """PipelineSpec.default: 6 estados na ordem, routing e contrato (fonte
    única de máquina — o delivery-protocol não é mais extraído de markdown)."""
    p = PipelineSpec.default()
    esperado = [
        "RECEBIDA",
        "CONSULTANDO_MEMORIA",
        "EM_EXPLORACAO",
        "EM_IMPLEMENTACAO",
        "EM_REVISAO",
        "ENCERRAMENTO",
    ]
    return (
        p.states == esperado
        and p.routing.get("IMPLEMENTACAO") == "implementer"
        and p.routing.get("REVISAO") == "reviewer"
        and p.routing.get("GRAVACAO") == "brain"
        and "status" in p.output_contract
        and "sem segredos" in p.blocking_rules
        and "evidências obrigatórias" in p.blocking_rules
    )


def _caso_5() -> bool:
    """parse_episode do registro-calculadora real."""
    ep = parse_episode(CALCULADORA)
    return (
        ep.agente == "hub"
        and ep.status == "completed"
        and any("EM_REVISAO" in linha for linha in ep.fluxo)
        and bool(ep.validacoes)
    )


def _caso_6() -> bool:
    """Episode completo=True (calculadora) vs False (placeholder vazio)."""
    calc = parse_episode(CALCULADORA)
    vazio = parse_episode(VAZIO)
    return calc.completo and not vazio.completo


def _caso_7() -> bool:
    """_extrai_comandos extrai o comando de validação."""
    cmds = _extrai_comandos("validação: `python tests/calculadora_test.py` → 28/28 PASS")
    return any("python tests/calculadora_test.py" in c for c in cmds)


def _caso_8() -> bool:
    """compile_playbook monta 7 agents e 6 states (não grava em disco)."""
    pb = compile_playbook()
    return len(pb.agents) == 7 and len(pb.pipeline.states) == 6


def _caso_9() -> bool:
    """Playbook.save grava playbook.json + README.md em diretório temp."""
    pb = compile_playbook()
    with tempfile.TemporaryDirectory() as tmp:
        destino = pathlib.Path(tmp) / "agents"
        pb.save(destino)
        ok = (destino / "playbook.json").is_file() and (destino / "README.md").is_file()
        if not ok:
            return False
        import json

        dados = json.loads((destino / "playbook.json").read_text(encoding="utf-8"))
        return len(dados["agents"]) == 7
    return False


def _caso_10() -> bool:
    """TaskContract.validate: completo ok; sem objetivo e sem critérios erram."""
    completo = TaskContract(objetivo="x", escopo="y", criterios_de_aceite=["z"])
    sem_obj = TaskContract(escopo="y", criterios_de_aceite=["z"])
    sem_crit = TaskContract(objetivo="x", escopo="y")
    return (
        completo.validate() == []
        and any("objetivo" in e for e in sem_obj.validate())
        and any("criterios" in e for e in sem_crit.validate())
    )


def _caso_11() -> bool:
    """AgentPipeline sem aprovação: nada executado (sem evidência) -> BLOQUEADA;
    contrato inválido também vira BLOQUEADA. Gravação episódica vai para temp."""
    with tempfile.TemporaryDirectory() as tmp:
        ep = pathlib.Path(tmp) / "episodic"
        with mock.patch.object(config, "EPISODIC_DIR", ep), \
             mock.patch.object(config, "INDEX_FILE", ep / "index.md"):
            pipe = AgentPipeline(approve=lambda cmd: False)
            res = pipe.run_task({
                "objetivo": "tarefa de teste do pipeline",
                "escopo": "tests/",
                "restricoes": ["sem dependências"],
                "criterios_de_aceite": ["testes passam"],
            })
            ok1 = res["status"] == "BLOQUEADA"
            ok2 = res["validacoes_executadas"] == []  # nada aprovado -> nada executado
            pipe2 = AgentPipeline(approve=lambda cmd: False)
            res2 = pipe2.run_task({"objetivo": "sem escopo e sem criterios"})
            ok3 = res2["status"] == "BLOQUEADA"
            return ok1 and ok2 and ok3
    return False


def _caso_12() -> bool:
    """Comando destrutivo ('rm -rf x') no escopo/critérios -> BLOQUEADA."""
    with tempfile.TemporaryDirectory() as tmp:
        ep = pathlib.Path(tmp) / "episodic"
        with mock.patch.object(config, "EPISODIC_DIR", ep), \
             mock.patch.object(config, "INDEX_FILE", ep / "index.md"):
            pipe = AgentPipeline(approve=lambda cmd: True)
            res = pipe.run_task({
                "objetivo": "x",
                "escopo": "rm -rf x",
                "criterios_de_aceite": ["ok"],
            })
            return res["status"] == "BLOQUEADA"
    return False


def _caso_13() -> bool:
    """HistoryLearner/LearnedModel toleram histórico ausente sem exceção."""
    inexistente = config.ROOT / "logs" / "harness_history_nao_existe.json"
    learner = HistoryLearner(inexistente)
    lm = LearnedModel.from_sources(config.EPISODIC_DIR, inexistente)
    return learner.reasons == [] and lm.block_reasons == []


def _caso_14() -> bool:
    """Path traversal: escopo fora do projeto ('../', 'C:/Windows', caminho
    absoluto fora de ROOT) vira RISCO na exploração -> BLOQUEADA; escopo
    interno 'harness/' continua passando na exploração (não bloqueia) e, sem
    evidência de validação executada, encerra BLOQUEADA na consolidação."""
    with tempfile.TemporaryDirectory() as tmp:
        ep = pathlib.Path(tmp) / "episodic"
        with mock.patch.object(config, "EPISODIC_DIR", ep), \
             mock.patch.object(config, "INDEX_FILE", ep / "index.md"):
            for escopo in ("../", "C:/Windows", "D:/x"):
                pipe = AgentPipeline(approve=lambda cmd: False)
                res = pipe.run_task({
                    "objetivo": "x",
                    "escopo": escopo,
                    "criterios_de_aceite": ["ok"],
                })
                if res["status"] != "BLOQUEADA":
                    return False
            pipe = AgentPipeline(approve=lambda cmd: False)
            res = pipe.run_task({
                "objetivo": "x",
                "escopo": "harness/",
                "criterios_de_aceite": ["ok"],
            })
            # escopo interno passa na exploração; sem evidência (nada aprovado/
            # executado) o pipeline encerra BLOQUEADA na consolidação
            return (res["status"] == "BLOQUEADA"
                    and "nenhuma evidência" in res["resumo"])
    return False


def _caso_15() -> bool:
    """Evidência real: com approve=True e um comando seguro no critério
    ('python --version' — sem "/" solto, lição do Git Bash), o comando é
    EXECUTADO de fato: job criado no executor, exit 0 real, evidência
    não-vazia e status APROVADA."""
    with tempfile.TemporaryDirectory() as tmp:
        ep = pathlib.Path(tmp) / "episodic"
        hist = pathlib.Path(tmp) / "history.json"  # evita poluir logs/ real
        with mock.patch.object(config, "EPISODIC_DIR", ep), \
             mock.patch.object(config, "INDEX_FILE", ep / "index.md"), \
             mock.patch.object(config, "HISTORY_FILE", hist):
            pipe = AgentPipeline(approve=lambda cmd: True)
            res = pipe.run_task({
                "objetivo": "validar execução real de comando aprovado",
                "escopo": "tests/",
                "criterios_de_aceite": ["roda `python --version` sem falhas"],
            })
            jobs = pipe.executor.list()
            return (
                res["status"] == "APROVADA"
                and bool(res["validacoes_executadas"])
                and len(jobs) > 0
                and all(j["exit_code"] == 0 for j in jobs)
                and all(j["status"] != "running" for j in jobs)
            )
    return False


def _caso_16() -> bool:
    """_extrai_secoes ignora code blocks: headings dentro de ``` ... ``` não
    viram seções (template markdown do documenter não vaza mais)."""
    texto = (
        "## Formato\n"
        "\n"
        "```markdown\n"
        "## Padrões e regras acionáveis\n"
        "- ...\n"
        "```\n"
        "\n"
        "## Regras\n"
        "- Grava somente em memory/references/.\n"
    )
    secoes = _extrai_secoes(texto)
    return (
        "Padrões e regras acionáveis" not in secoes
        and "Regras" in secoes
        and "Formato" in secoes
        and "- ..." not in secoes.get("Formato", [])
        and "- ..." not in secoes.get("Regras", [])
        and secoes["Regras"] == ["- Grava somente em memory/references/."]
    )


def _caso_17() -> bool:
    """Filtro de lições por PARÁGRAFO: 'sessao de teste'/'tudo ok' (triviais)
    não entram em licoes; parágrafo longo e específico entra. Os fragmentos
    triviais são separados por linha em branco do parágrafo útil para que
    formem parágrafos próprios (agregação por parágrafo, pendência 5)."""
    with tempfile.TemporaryDirectory() as tmp:
        ep_dir = pathlib.Path(tmp) / "episodic"
        ep_dir.mkdir()
        (ep_dir / "reg-1.md").write_text(
            "---\nid: reg-1\nagente: web\nstatus: completed\n---\n\n"
            "## Contrato de entrada\nx\n\n"
            "## Fluxo\nx\n\n"
            "## Resultado\nx\n\n"
            "## Contexto\n"
            "sessao de teste\n"
            "\n"
            "- tudo ok\n"
            "\n"
            "No Git Bash (Windows), passar '/' como argumento solto é convertido "
            "em caminho e isso quebra a divisão via CLI; use REPL ou escape.\n",
            encoding="utf-8",
        )
        inexistente = config.ROOT / "logs" / "nao_existe_history.json"
        lm = LearnedModel.from_sources(ep_dir, inexistente)
        licoes = lm.licoes
        return (
            bool(licoes)
            and all("sessao de teste" not in l for l in licoes)
            and all("tudo ok" not in l for l in licoes)
            and any("Git Bash" in l for l in licoes)
        )


def _caso_18() -> bool:
    """Glob absoluto ('C:/Windows/*.txt', 'D:/*.txt') NÃO crasha (N1): o token
    vira risco na exploração ANTES do glob — que em Python 3.12 levantaria
    NotImplementedError e quebraria o run_task — e run_task retorna BLOQUEADA,
    nunca levanta exceção."""
    with tempfile.TemporaryDirectory() as tmp:
        ep = pathlib.Path(tmp) / "episodic"
        with mock.patch.object(config, "EPISODIC_DIR", ep), \
             mock.patch.object(config, "INDEX_FILE", ep / "index.md"):
            for escopo in ("C:/Windows/*.txt", "D:/*.txt"):
                pipe = AgentPipeline(approve=lambda cmd: False)
                res = pipe.run_task({
                    "objetivo": "x",
                    "escopo": escopo,
                    "criterios_de_aceite": ["ok"],
                })
                if res["status"] != "BLOQUEADA":
                    return False
            return True
    return False


def _caso_19() -> bool:
    """Glob com componente '..' ('../*.md') vira risco ANTES do glob (N2):
    o glob retornaria 0 itens (ou ValueError no 3.13+, silenciado), então o
    escopo não bloqueava; agora o token com '..' é rejeitado na exploração
    -> BLOQUEADA."""
    with tempfile.TemporaryDirectory() as tmp:
        ep = pathlib.Path(tmp) / "episodic"
        with mock.patch.object(config, "EPISODIC_DIR", ep), \
             mock.patch.object(config, "INDEX_FILE", ep / "index.md"):
            pipe = AgentPipeline(approve=lambda cmd: False)
            res = pipe.run_task({
                "objetivo": "x",
                "escopo": "../*.md",
                "criterios_de_aceite": ["ok"],
            })
            return res["status"] == "BLOQUEADA"
    return False


def _caso_20() -> bool:
    """Timeout de comando longo (N3): `_executa_comando` com timeout curto
    retorna timeout=True SEM crash e sem esperar o sleep inteiro. A morte REAL
    do processo é verificada (não é falso positivo do sleep expirar): com
    `time.sleep(60)`, o exit_code chega RAPIDAMENTE (janela curta de 10s, bem
    antes dos 60s) — sinal de que o pipe fechou porque a árvore foi derrubada.
    No Windows, o processo neto com marcador único não sobrevive (taskkill
    /T /F roda ANTES do terminate). Roda 2x para mostrar estabilidade."""
    with tempfile.TemporaryDirectory() as tmp:
        ep = pathlib.Path(tmp) / "episodic"
        hist = pathlib.Path(tmp) / "history.json"  # evita poluir logs/ real
        with mock.patch.object(config, "EPISODIC_DIR", ep), \
             mock.patch.object(config, "INDEX_FILE", ep / "index.md"), \
             mock.patch.object(config, "HISTORY_FILE", hist):
            for _ in range(2):  # estabilidade: mesmo cenário duas vezes
                pipe = AgentPipeline(approve=lambda cmd: True)
                marker = f"harness_sleep_{uuid.uuid4().hex[:8]}"
                if os.name == "nt":
                    # neto python real: cmd.exe (pai) + python (neto) na árvore
                    cmd = f'python -c "import time; time.sleep(60)  # {marker}"'
                else:
                    # POSIX: exec substitui o shell pelo python — o terminate()
                    # (inalterado) mata o processo direto e fecha o pipe
                    cmd = 'exec python -c "import time; time.sleep(60)"'
                t0 = time.monotonic()
                res = pipe._executa_comando(cmd, timeout=1.5)
                elapsed = time.monotonic() - t0
                if not res.get("timeout"):
                    return False
                if elapsed > 8:  # nunca deve esperar o sleep(60) inteiro
                    return False
                jobs = [j for j in pipe.executor.list() if j["command"] == cmd]
                if not jobs:
                    return False
                job = jobs[-1]  # mais recente = iteração atual
                if job["status"] == "running":
                    return False
                # (b) exit_code REAL chega quando o pipe fecha (processo morto),
                # bem antes dos 60s; janela curta de 10s sem timing exato
                deadline = time.monotonic() + 10
                while time.monotonic() < deadline:
                    snap = pipe.executor.get(job["id"]) or {}
                    if snap.get("exit_code") is not None:
                        break
                    time.sleep(0.05)
                else:
                    return False
                # (c) Windows: nenhum processo neto com o marcador sobrevive
                if os.name == "nt":
                    restantes = -1
                    for _ in range(20):  # até ~2s para o taskkill concluir
                        restantes = _proc_count_com_marcador(marker)
                        if restantes == 0:
                            break
                    # -1 = verificação indisponível (PowerShell ausente/timeout):
                    # o teste NÃO falha por falta de verificação — aceita.
                    if restantes == -1:
                        break
                    time.sleep(0.1)
                if restantes not in (0, -1):  # processo neto sobreviveu -> falha
                    return False
            return True
    return False


def _caso_21() -> bool:
    """Contexto trivial NÃO vira episodes[].contexto no playbook (achado B):
    o registro 'sessao de teste' continua trackeado no diretório episódico,
    mas a SERIALIZAÇÃO do playbook filtra a linha trivial; linhas úteis
    continuam aparecendo. Compila com compile_playbook (registro trivial +
    registro útil) e inspeciona o dict serializado."""
    with tempfile.TemporaryDirectory() as tmp:
        raiz = pathlib.Path(tmp) / "root"
        ep_dir = raiz / "memory" / "episodic"
        ep_dir.mkdir(parents=True)
        (ep_dir / "reg-trivial.md").write_text(
            "---\nid: reg-trivial\nagente: web\nstatus: completed\n---\n\n"
            "## Contrato de entrada\ntestar APIs\n\n"
            "## Fluxo\nhealth, exec\n\n"
            "## Resultado\ntudo ok\n\n"
            "## Contexto\nsessao de teste\n",
            encoding="utf-8",
        )
        (ep_dir / "reg-boa.md").write_text(
            "---\nid: reg-boa\nagente: hub\nstatus: completed\n---\n\n"
            "## Contrato de entrada\nx\n\n"
            "## Fluxo\nx\n\n"
            "## Resultado\nx\n\n"
            "## Contexto\n"
            "No Git Bash (Windows), passar '/' como argumento solto é convertido "
            "em caminho e quebra a divisão via CLI; use REPL ou escape.\n",
            encoding="utf-8",
        )
        with mock.patch.object(config, "ROOT", raiz):
            pb = compile_playbook()
        epis = pb.to_dict()["learned"]["episodes"]
        ids = {ep["record_id"] for ep in epis}
        contextos = " ".join(
            " ".join(ep.get("contexto", [])) for ep in epis
        )
        return (
            ids == {"reg-trivial", "reg-boa"}  # episódios permanecem trackeados
            and "sessao de teste" not in contextos
            and "tudo ok" not in contextos
            and "Git Bash" in contextos  # linha útil preservada
        )


def _escreve_episodio(ep_dir: pathlib.Path, record_id: str, agente: str,
                      contexto: list[str],
                      validacoes: list[str] | None = None,
                      achados: list[str] | None = None) -> None:
    """Escreve um episódio fictício COMPLETO em ep_dir/<id>.md (com seções
    Contrato/Fluxo/Resultado/Contexto preenchidas para `completo=True`)."""
    validacoes = validacoes or []
    achados = achados or []
    linhas = [f"---\nid: {record_id}\nagente: {agente}\nstatus: completed\n---\n",
              "\n## Contrato de entrada\n- objetivo fictício\n",
              "\n## Fluxo\n- etapa fictícia\n",
              "\n## Resultado\n"]
    if validacoes:
        linhas.append("  validacoes_executadas:\n")
        for v in validacoes:
            linhas.append(f"    - {v}\n")
    if achados:
        linhas.append("  achados_da_revisao:\n")
        for a in achados:
            linhas.append(f"    - {a}\n")
    if not (validacoes or achados):
        linhas.append("- resultado fictício\n")
    linhas.append("\n## Contexto\n")
    for c in contexto:
        linhas.append(c + "\n")
    (ep_dir / f"{record_id}.md").write_text("".join(linhas), encoding="utf-8")


def _caso_22() -> bool:
    """(a) Agrupamento por agente: lições/validações de cada agente vão para o
    seu próprio bucket em por_agente, sem vazar para outro."""
    with tempfile.TemporaryDirectory() as tmp:
        ep_dir = pathlib.Path(tmp) / "episodic"
        ep_dir.mkdir()
        _escreve_episodio(
            ep_dir, "reg-hub", "hub",
            contexto=["Lição do hub sobre orquestração e delegação com revisão independente."],
            validacoes=["python tests/agentes_test.py"],
        )
        _escreve_episodio(
            ep_dir, "reg-rev", "reviewer",
            contexto=["Lição do reviewer sobre reportar severidade por arquivo e linha."],
        )
        inexistente = config.ROOT / "logs" / "nao_existe.json"
        lm = LearnedModel.from_sources(ep_dir, inexistente)
        pa = lm.por_agente
        hub_licao = [l["texto"] for l in pa["hub"].licoes]
        rev_licao = [l["texto"] for l in pa["reviewer"].licoes]
        return (
            set(pa) == {"hub", "reviewer"}
            and any("orquestração" in t for t in hub_licao)
            and any("severidade" in t for t in rev_licao)
            and not any("severidade" in t for t in hub_licao)
            and pa["hub"].validacoes_comuns
            and pa["hub"].validacoes_comuns[0]["comando"] == "python tests/agentes_test.py"
            and pa["hub"].validacoes_comuns[0]["ocorrencias"] == 1
            and not pa["reviewer"].validacoes_comuns
        )


def _caso_23() -> bool:
    """(b) Soma de ocorrências: mesma lição (texto normalizado) em 2 episódios
    do MESMO agente soma ocorrencias=2 e acumula os origens."""
    with tempfile.TemporaryDirectory() as tmp:
        ep_dir = pathlib.Path(tmp) / "episodic"
        ep_dir.mkdir()
        licao = ("Ao validar no Windows, use PYTHONIOENCODING=utf-8 para o "
                 "console não quebrar acentos na saída.")
        _escreve_episodio(ep_dir, "reg-imp-1", "implementer", contexto=[licao])
        _escreve_episodio(ep_dir, "reg-imp-2", "implementer", contexto=[licao])
        lm = LearnedModel.from_sources(ep_dir, config.ROOT / "logs" / "nao.json")
        licoes = lm.por_agente["implementer"].licoes
        return (
            len(licoes) == 1
            and licoes[0]["ocorrencias"] == 2
            and licoes[0]["origens"] == ["reg-imp-1", "reg-imp-2"]
        )


def _caso_24() -> bool:
    """(c) Multi-agente: 'hub+reviewer+implementer' atribui a lição a CADA um
    dos agentes mencionados (separação por '+')."""
    with tempfile.TemporaryDirectory() as tmp:
        ep_dir = pathlib.Path(tmp) / "episodic"
        ep_dir.mkdir()
        _escreve_episodio(
            ep_dir, "reg-multi", "hub+reviewer+implementer",
            contexto=["Lição conjunta sobre o ciclo Reflection com rodadas de revisão."],
        )
        lm = LearnedModel.from_sources(ep_dir, config.ROOT / "logs" / "nao.json")
        pa = lm.por_agente
        return (
            set(pa) == {"hub", "reviewer", "implementer"}
            and all(len(al.licoes) == 1 for al in pa.values())
            and all(al.licoes[0]["ocorrencias"] == 1 for al in pa.values())
            and all(al.licoes[0]["origens"] == ["reg-multi"] for al in pa.values())
        )


def _caso_25() -> bool:
    """(d) por_agente presente no dict serializado de LearnedModel.to_dict(),
    incluindo o resumo de confiança (n_licoes_fracas/confiaveis — aditivo)."""
    with tempfile.TemporaryDirectory() as tmp:
        ep_dir = pathlib.Path(tmp) / "episodic"
        ep_dir.mkdir()
        _escreve_episodio(
            ep_dir, "reg-hub", "hub",
            contexto=["Lição do hub sobre agregar a curva de aprendizado por agente."],
        )
        lm = LearnedModel.from_sources(ep_dir, config.ROOT / "logs" / "nao.json")
        dados = lm.to_dict()
        return (
            "por_agente" in dados
            and "hub" in dados["por_agente"]
            and set(dados["por_agente"]["hub"]) == {
                "agente", "licoes", "validacoes_comuns", "achados",
                "n_licoes_fracas", "n_licoes_confiaveis",
            }
        )


def _caso_26() -> bool:
    """(e) Listas planas continuam presentes no dict serializado (compatibilidade)."""
    with tempfile.TemporaryDirectory() as tmp:
        ep_dir = pathlib.Path(tmp) / "episodic"
        ep_dir.mkdir()
        _escreve_episodio(
            ep_dir, "reg-hub", "hub",
            contexto=["Lição do hub sobre manter as listas planas por compatibilidade."],
            validacoes=["python tests/agentes_test.py"],
        )
        lm = LearnedModel.from_sources(ep_dir, config.ROOT / "logs" / "nao.json")
        dados = lm.to_dict()
        return (
            "episodes" in dados
            and "licoes" in dados
            and "validacoes_comuns" in dados
            and "block_reasons" in dados
            and "por_agente" in dados
            and isinstance(dados["licoes"], list)
            and isinstance(dados["validacoes_comuns"], list)
        )


def _caso_27() -> bool:
    """parse_episode lê trust/origem/validado_por do frontmatter; registros
    antigos (sem os campos) assumem trust fraca (tolerância à ausência)."""
    with tempfile.TemporaryDirectory() as tmp:
        ep_dir = pathlib.Path(tmp) / "episodic"
        ep_dir.mkdir()
        (ep_dir / "reg-trust.md").write_text(
            "---\nid: reg-trust\nagente: implementer\nstatus: completed\n"
            "trust: alta\norigem: execução via /hub; evidências: tests 26/26\n"
            "validado_por: reviewer\n---\n\n"
            "## Contrato de entrada\nx\n\n## Fluxo\nx\n\n## Resultado\nx\n\n"
            "## Contexto\nlição útil com confiança alta e origem rastreável\n",
            encoding="utf-8",
        )
        (ep_dir / "reg-antigo.md").write_text(
            "---\nid: reg-antigo\nagente: hub\nstatus: completed\n---\n\n"
            "## Contrato de entrada\nx\n\n## Fluxo\nx\n\n## Resultado\nx\n\n"
            "## Contexto\nlição antiga sem campos de confiança explícitos\n",
            encoding="utf-8",
        )
        novo = parse_episode(ep_dir / "reg-trust.md")
        antigo = parse_episode(ep_dir / "reg-antigo.md")
        return (
            novo.trust == "alta"
            and novo.origem == "execução via /hub; evidências: tests 26/26"
            and novo.validado_por == "reviewer"
            and antigo.trust == config.TRUST_DEFAULT  # "fraca"
            and antigo.origem == "" and antigo.validado_por == ""
        )


def _caso_28() -> bool:
    """Agregação: cada lição herda o trust do episódio de origem; o resumo do
    agente expõe n_licoes_fracas e n_licoes_confiaveis."""
    with tempfile.TemporaryDirectory() as tmp:
        ep_dir = pathlib.Path(tmp) / "episodic"
        ep_dir.mkdir()
        # Episódio antigo (sem trust) -> lição fraca
        (ep_dir / "reg-fraca.md").write_text(
            "---\nid: reg-fraca\nagente: implementer\nstatus: completed\n---\n\n"
            "## Contrato de entrada\nx\n\n## Fluxo\nx\n\n## Resultado\nx\n\n"
            "## Contexto\nlição sem origem rastreável de memória antiga não validada\n",
            encoding="utf-8",
        )
        # Episódio com trust alta -> lição alta
        (ep_dir / "reg-forte.md").write_text(
            "---\nid: reg-forte\nagente: implementer\nstatus: completed\n"
            "trust: alta\norigem: execução com evidências de testes\n"
            "validado_por: reviewer\n---\n\n"
            "## Contrato de entrada\nx\n\n## Fluxo\nx\n\n## Resultado\nx\n\n"
            "## Contexto\nlição validada com evidências externas e revisão independente\n",
            encoding="utf-8",
        )
        lm = LearnedModel.from_sources(ep_dir, config.ROOT / "logs" / "nao.json")
        al = lm.por_agente["implementer"]
        trusts = {l["texto"]: l["trust"] for l in al.licoes}
        return (
            al.n_licoes_fracas == 1
            and al.n_licoes_confiaveis == 1
            and "fraca" in trusts.values()
            and "alta" in trusts.values()
            # rastreabilidade: trust_por_origem mapeia cada record_id ao trust
            and all(ep.record_id in l["trust_por_origem"] for l in al.licoes
                    for ep in lm.episodes if ep.record_id in l["origens"])
        )


def _caso_29() -> bool:
    """Mesma lição vinda de episódios com trusts diferentes: o item agregado
    expõe o pior trust (conservador) e soma ocorrências/origens."""
    with tempfile.TemporaryDirectory() as tmp:
        ep_dir = pathlib.Path(tmp) / "episodic"
        ep_dir.mkdir()
        licao = "Lição repetida que veio de fontes com níveis de confiança distintos."
        (ep_dir / "reg-media.md").write_text(
            "---\nid: reg-media\nagente: hub\nstatus: completed\n"
            "trust: media\norigem: validação parcial\nvalidado_por: motor\n---\n\n"
            "## Contrato de entrada\nx\n\n## Fluxo\nx\n\n## Resultado\nx\n\n"
            f"## Contexto\n{licao}\n",
            encoding="utf-8",
        )
        (ep_dir / "reg-fraca.md").write_text(
            "---\nid: reg-fraca\nagente: hub\nstatus: completed\n---\n\n"
            "## Contrato de entrada\nx\n\n## Fluxo\nx\n\n## Resultado\nx\n\n"
            f"## Contexto\n{licao}\n",
            encoding="utf-8",
        )
        lm = LearnedModel.from_sources(ep_dir, config.ROOT / "logs" / "nao.json")
        licoes = lm.por_agente["hub"].licoes
        return (
            len(licoes) == 1
            and licoes[0]["ocorrencias"] == 2
            # origens na ordem lexicográfica dos arquivos (determinística)
            and licoes[0]["origens"] == ["reg-fraca", "reg-media"]
            and licoes[0]["trust"] == "fraca"  # pior trust entre origens
            and licoes[0]["trust_por_origem"] == {
                "reg-fraca": "fraca", "reg-media": "media",
            }
        )


def _caso_30() -> bool:
    """README do playbook expõe lições confiáveis/fracas por agente (trust)."""
    with tempfile.TemporaryDirectory() as tmp:
        raiz = pathlib.Path(tmp) / "root"
        ep_dir = raiz / "memory" / "episodic"
        ep_dir.mkdir(parents=True)
        _escreve_episodio(
            ep_dir, "reg-fraca", "implementer",
            contexto=["lição antiga sem origem rastreável, de memória não validada"],
        )
        (ep_dir / "reg-forte.md").write_text(
            "---\nid: reg-forte\nagente: implementer\nstatus: completed\n"
            "trust: alta\norigem: evidências de testes\nvalidado_por: reviewer\n---\n\n"
            "## Contrato de entrada\nx\n\n## Fluxo\nx\n\n## Resultado\nx\n\n"
            "## Contexto\nlição validada por evidência externa com revisão independente\n",
            encoding="utf-8",
        )
        with mock.patch.object(config, "ROOT", raiz):
            pb = compile_playbook()
        texto = pb._readme()  # noqa: SLF001
        return "confiáveis" in texto and "fracas" in texto


def _caso_31() -> bool:
    """(a) Agregação por parágrafo: um parágrafo quebrado em 3 linhas físicas
    gera UMA lição (não 3), com o texto concatenado (pendência 5)."""
    with tempfile.TemporaryDirectory() as tmp:
        ep_dir = pathlib.Path(tmp) / "episodic"
        ep_dir.mkdir()
        _escreve_episodio(
            ep_dir, "reg-para", "hub",
            contexto=[
                "Decisões do usuário: (1) modelo +",
                "  orquestração determinística (sem LLM); (2) fontes de",
                "  aprendizado = episódicos + .opencode/agent.",
            ],
        )
        lm = LearnedModel.from_sources(ep_dir, config.ROOT / "logs" / "nao.json")
        licoes = lm.por_agente["hub"].licoes
        if len(licoes) != 1:
            return False
        texto = licoes[0]["texto"]
        return (
            texto.startswith("Decisões do usuário: (1) modelo + orquestração")
            and "fontes de aprendizado = episódicos" in texto
            and licoes[0]["ocorrencias"] == 1
        )


def _caso_32() -> bool:
    """(b) Lista com `- item` + continuação indentada: cada item vira UMA lição
    com o texto completo (o item é a base; linhas indentadas seguintes sem novo
    `-` são continuação daquele item)."""
    with tempfile.TemporaryDirectory() as tmp:
        ep_dir = pathlib.Path(tmp) / "episodic"
        ep_dir.mkdir()
        _escreve_episodio(
            ep_dir, "reg-lista", "hub",
            contexto=[
                "- Primeiro item com",
                "  continuação indentada aqui",
                "- Segundo item completo sem continuação",
            ],
        )
        lm = LearnedModel.from_sources(ep_dir, config.ROOT / "logs" / "nao.json")
        licoes = lm.por_agente["hub"].licoes
        textos = [l["texto"] for l in licoes]
        return (
            len(licoes) == 2
            and any("Primeiro item com continuação indentada aqui" in t for t in textos)
            and any("Segundo item completo sem continuação" in t for t in textos)
        )


def _caso_33() -> bool:
    """(c) Parágrafos separados por linha em branco geram lições DISTINTAS
    (cada parágrafo vira uma lição independente)."""
    with tempfile.TemporaryDirectory() as tmp:
        ep_dir = pathlib.Path(tmp) / "episodic"
        ep_dir.mkdir()
        _escreve_episodio(
            ep_dir, "reg-paras", "hub",
            contexto=[
                "Primeiro parágrafo com conteúdo relevante e específico para o playbook.",
                "",
                "Segundo parágrafo com outra lição bem diferente e também específica.",
            ],
        )
        lm = LearnedModel.from_sources(ep_dir, config.ROOT / "logs" / "nao.json")
        textos = [l["texto"] for l in lm.por_agente["hub"].licoes]
        return (
            len(textos) == 2
            and any("Primeiro parágrafo com conteúdo" in t for t in textos)
            and any("Segundo parágrafo com outra lição" in t for t in textos)
            and not any("Segundo parágrafo" in t for t in textos
                        if "Primeiro parágrafo" in t)
        )


def _caso_34() -> bool:
    """(d) _e_licao_util aplicado ao PARÁGRAFO COMPLETO: um fragmento curto que
    sozinho passaria no filtro pode NÃO passar quando concatenado com o resto
    do parágrafo; e um fragmento curto que sozinho não passaria pode passar no
    parágrafo completo. Exemplo: 3 fragmentos curtos (cada um < 25 chars)
    concatenados formam um parágrafo longo que passa."""
    with tempfile.TemporaryDirectory() as tmp:
        ep_dir = pathlib.Path(tmp) / "episodic"
        ep_dir.mkdir()
        # Cada fragmento sozinho tem < 25 chars (não passaria); o parágrafo
        # completo tem > 25 chars e > 4 palavras (passa).
        _escreve_episodio(
            ep_dir, "reg-frag", "hub",
            contexto=[
                "fragmento curto",
                "mais um pedaço",
                "e a conclusão do parágrafo que torna ele longo",
            ],
        )
        lm = LearnedModel.from_sources(ep_dir, config.ROOT / "logs" / "nao.json")
        textos = [l["texto"] for l in lm.por_agente["hub"].licoes]
        return (
            len(textos) == 1
            and "fragmento curto mais um pedaço e a conclusão" in textos[0]
        )


def _caso_35() -> bool:
    """(e) Lista NUMERADA em linhas físicas separadas: itens `1.`/`2.` e
    `(1)`/`(2)` iniciam itens DISTINTOS (não um único parágrafo colapsado),
    com o marcador removido do texto final (mesmo tratamento do bullet)."""
    with tempfile.TemporaryDirectory() as tmp:
        ep_dir = pathlib.Path(tmp) / "episodic"
        ep_dir.mkdir()
        _escreve_episodio(
            ep_dir, "reg-numerada", "hub",
            contexto=[
                "1. Primeiro item numerado com",
                "   continuação indentada aqui",
                "2. Segundo item numerado completo e independente",
                "(1) Terceiro item com parenteses e",
                "    mais continuação indentada",
                "(2) Quarto item com parenteses final da lista",
            ],
        )
        lm = LearnedModel.from_sources(ep_dir, config.ROOT / "logs" / "nao.json")
        textos = [l["texto"] for l in lm.por_agente["hub"].licoes]
        return (
            len(textos) == 4
            and "Primeiro item numerado com continuação indentada aqui" in textos
            and "Segundo item numerado completo e independente" in textos
            and "Terceiro item com parenteses e mais continuação indentada" in textos
            and "Quarto item com parenteses final da lista" in textos
            # marcador removido do texto final (não sobra "1.", "(1)", etc.)
            and not any(t.startswith(("1.", "2.", "(1)", "(2)")) for t in textos)
            and not any(" 1. " in t or " (1) " in t for t in textos)
        )


# ---------------------------------------------------------------------------
# Consistência doc legível (docs/protocolo-delivery.md) x runtime
# (PipelineSpec.default). O doc é a referência HUMANA; o runtime prevalece,
# mas divergência silenciosa é um defeito — estes casos a detectam.
# ---------------------------------------------------------------------------

def _le_doc_delivery(caminho: pathlib.Path | None = None) -> str | None:
    """Lê `docs/protocolo-delivery.md` (ou `caminho` explícito). Ausente/
    ilegível -> None (nunca levanta): o teste que chama retorna False."""
    alvo = caminho or (config.ROOT / "docs" / "protocolo-delivery.md")
    try:
        return alvo.read_text(encoding="utf-8")
    except (OSError, UnicodeError):
        return None


def _secao_doc(texto: str, titulo: str) -> str:
    """Retorna o corpo da seção markdown `## <titulo>` (até o próximo `## `).

    Retorna "" se a seção não existir — o comparador falha em vez de crashar.
    """
    padrao = re.compile(
        r"^##\s+" + re.escape(titulo) + r"[ \t]*\n(.*?)(?=^##\s|\Z)",
        re.M | re.S,
    )
    achado = padrao.search(texto)
    return achado.group(1) if achado else ""


def _bloco_codigo_doc(corpo: str) -> str:
    """Retorna o conteúdo do primeiro bloco cercado por ``` do corpo."""
    achado = re.search(r"```[^\n]*\n(.*?)```", corpo, re.S)
    return achado.group(1) if achado else ""


def _estados_do_doc(texto: str) -> list[str]:
    """Extrai os estados do pipeline do bloco da seção 'Estados da execução'.

    O bloco é `RECEBIDA → CONSULTANDO_MEMORIA → ... → ENCERRAMENTO → APROVADA
    | APROVADA_COM_RESSALVAS | BLOQUEADA`; para no primeiro token com `|`
    (status terminal NÃO é estado da máquina). Preserva conteúdo e ORDEM.
    """
    bloco = _bloco_codigo_doc(_secao_doc(texto, "Estados da execução"))
    estados: list[str] = []
    for token in bloco.split("\u2192"):
        token = token.strip()
        if not token:
            continue
        if "|" in token:  # linha de status final (APROVADA | ... | BLOQUEADA)
            break
        estados.append(token)
    return estados


def _roteamento_do_doc(texto: str) -> dict[str, str]:
    """Extrai os pares (etapa -> agente) da tabela de 'Roteamento'."""
    rotas: dict[str, str] = {}
    secao = _secao_doc(texto, "Roteamento (etapa \u2192 agente)")
    for linha in secao.splitlines():
        linha = linha.strip()
        if not linha.startswith("|"):
            continue
        celulas = [c.strip() for c in linha.strip("|").split("|")]
        if len(celulas) < 2:
            continue
        etapa, agente = celulas[0], celulas[1]
        if not etapa or not agente or set(etapa) <= {"-", ":"}:
            continue  # linha separadora do cabeçalho markdown
        if etapa.lower() == "etapa" or agente.lower() == "agente":
            continue  # cabeçalho
        rotas[etapa] = agente
    return rotas


def _contrato_saida_do_doc(texto: str) -> list[str]:
    """Extrai, EM ORDEM, os campos do bloco yaml de 'Contrato de saída'."""
    bloco = _bloco_codigo_doc(_secao_doc(texto, "Contrato de saída"))
    campos: list[str] = []
    for linha in bloco.splitlines():
        achado = re.match(r"^([A-Za-z_][A-Za-z0-9_]*)\s*:", linha)
        if achado:
            campos.append(achado.group(1))
    return campos


def _caso_36() -> bool:
    """Doc x runtime: os 6 estados de docs/protocolo-delivery.md batem em
    CONTEÚDO e ORDEM com PipelineSpec.default().states."""
    texto = _le_doc_delivery()
    if texto is None:
        return False
    return _estados_do_doc(texto) == PipelineSpec.default().states


def _caso_37() -> bool:
    """Doc x runtime: a tabela de roteamento (etapa -> agente) do doc bate com
    PipelineSpec.default().routing."""
    texto = _le_doc_delivery()
    if texto is None:
        return False
    return _roteamento_do_doc(texto) == PipelineSpec.default().routing


def _caso_38() -> bool:
    """Doc x runtime: os campos do contrato de saída do doc (em ordem) batem
    com PipelineSpec.default().output_contract."""
    texto = _le_doc_delivery()
    if texto is None:
        return False
    return _contrato_saida_do_doc(texto) == PipelineSpec.default().output_contract


def _caso_39() -> bool:
    """Tolerância: doc ausente/ilegível -> None (nunca crash) e os extratores
    devolvem vazio; um doc sem as seções também não crasha (retorno vazio)."""
    inexistente = config.ROOT / "docs" / "__nao_existe_protocolo__.md"
    if _le_doc_delivery(inexistente) is not None:
        return False
    texto_sem_secoes = "# Documento qualquer\n\nNada aqui.\n"
    return (
        _estados_do_doc(texto_sem_secoes) == []
        and _roteamento_do_doc(texto_sem_secoes) == {}
        and _contrato_saida_do_doc(texto_sem_secoes) == []
    )


def _licao_resumo(texto: str, trust: str, ocorrencias: int = 1) -> dict:
    """Item de lição no formato do playbook (learned.por_agente.*.licoes)."""
    return {
        "texto": texto,
        "ocorrencias": ocorrencias,
        "origens": ["reg-fake"],
        "trust_por_origem": {"reg-fake": trust},
        "trust": trust,
    }


def _playbook_licoes(licoes_por_agente: dict) -> dict:
    """Playbook mínimo (só learned.por_agente) para testar o resumo de lições."""
    por_agente = {
        agente: {
            "agente": agente,
            "licoes": licoes,
            "validacoes_comuns": [],
            "achados": [],
            "n_licoes_fracas": 0,
            "n_licoes_confiaveis": 0,
        }
        for agente, licoes in licoes_por_agente.items()
    }
    return {"learned": {"por_agente": por_agente}}


def _caso_40() -> bool:
    """resumo_licoes_confiaveis: inclui lições alta/media e EXCLUI fraca."""
    pb = _playbook_licoes({"implementer": [
        _licao_resumo("Lição validada por evidências externas de testes.", "alta", 3),
        _licao_resumo("Lição parcialmente validada por fonte pública.", "media", 2),
        _licao_resumo("Lição não validada que nunca deve ser exposta.", "fraca", 9),
    ]})
    texto = resumo_licoes_confiaveis("implementer", pb)
    return (
        "Lição validada por evidências" in texto
        and "Lição parcialmente validada" in texto
        and "Lição não validada" not in texto
        # fraca com maior ocorrência NÃO desloca as confiáveis nem aparece
        and "- (x9)" not in texto
    )


def _caso_41() -> bool:
    """resumo_licoes_confiaveis: respeita MAX_LICOES_RESUMO (top por ocorrência)
    e encurta cada texto a LICAO_RESUMO_CHARS (uma linha por lição)."""
    # 8 lições confiáveis (texto longo) com ocorrências 10..3
    licoes = [
        _licao_resumo("Li" + str(i) + " " + "a" * 300, "alta", 10 - i)
        for i in range(8)
    ]
    texto = resumo_licoes_confiaveis("implementer", _playbook_licoes({"implementer": licoes}))
    linhas = [l for l in texto.splitlines() if l.startswith("- (x")]
    if len(linhas) != MAX_LICOES_RESUMO:
        return False
    ocorrencias = []
    for linha in linhas:
        m = re.match(r"^- \(x(\d+)\) (.*)$", linha)
        if not m:
            return False
        ocorrencias.append(int(m.group(1)))
        corpo = m.group(2)
        if len(corpo) > LICAO_RESUMO_CHARS:
            return False
        if not corpo.endswith("..."):  # texto maior que o limite -> truncado
            return False
    # top-5 por ocorrência decrescente (10,9,8,7,6) — determinístico
    return ocorrencias == [10, 9, 8, 7, 6]


def _caso_42() -> bool:
    """resumo_licoes_confiaveis: vazio sem playbook/sem lições/só fracas/agente
    inexistente; tolerante a playbook ausente em disco."""
    with tempfile.TemporaryDirectory() as tmp:
        inexistente = pathlib.Path(tmp) / "playbook_nao_existe.json"
        with mock.patch.object(config, "PLAYBOOK_FILE", inexistente):
            sem_arquivo = resumo_licoes_confiaveis("implementer")
    sem_licoes = resumo_licoes_confiaveis("implementer", _playbook_licoes({"implementer": []}))
    sem_agente = resumo_licoes_confiaveis("implementer", _playbook_licoes({"hub": [
        _licao_resumo("Lição do hub suficientemente longa para o filtro.", "alta"),
    ]}))
    agente_vazio = resumo_licoes_confiaveis("", _playbook_licoes({"hub": [
        _licao_resumo("Lição do hub suficientemente longa para o filtro.", "alta"),
    ]}))
    so_fraca = resumo_licoes_confiaveis("implementer", _playbook_licoes({"implementer": [
        _licao_resumo("Lição não validada de memória antiga qualquer.", "fraca"),
    ]}))
    return (
        sem_arquivo == ""
        and sem_licoes == ""
        and sem_agente == ""
        and agente_vazio == ""
        and so_fraca == ""
    )


def _caso_43() -> bool:
    """resumo_licoes_confiaveis: tolerante a playbook malformado (nunca crasha)
    e aceita o objeto Playbook compilado (via to_dict), igual ao dict."""
    malformados = [
        {},
        {"learned": None},
        {"learned": {}},
        {"learned": {"por_agente": None}},
        {"learned": {"por_agente": {"implementer": None}}},
        {"learned": {"por_agente": {"implementer": {"licoes": "x"}}}},
        {"learned": {"por_agente": {"implementer": {"licoes": [None, 1, {}]}}}},
    ]
    for pb in malformados:
        if resumo_licoes_confiaveis("implementer", pb) != "":
            return False
    # objeto Playbook compilado e seu dict produzem o mesmo resumo
    obj = compile_playbook()
    return (
        resumo_licoes_confiaveis("implementer", obj)
        == resumo_licoes_confiaveis("implementer", obj.to_dict())
    )


def _playbook_objeto(licoes_por_agente: dict) -> Playbook:
    """Playbook mínimo (objeto) com a curva `learned.por_agente` — para testar
    `Playbook._resumo()` / `to_dict()` sem tocar a memória real."""
    por_agente = {
        agente: AgentLearning(agente=agente, licoes=licoes)
        for agente, licoes in licoes_por_agente.items()
    }
    return Playbook(learned=LearnedModel(por_agente=por_agente))


def _caso_44() -> bool:
    """(M3) Lição META (autorreferente ao playbook) fica FORA do feed
    (`resumo_licoes_confiaveis`/`_resumo`/`licoes_confiaveis_textos`), mas
    PERMANECE no playbook serializado (não é removida da memória)."""
    meta = _licao_resumo(
        "Este Contexto alimenta a curva por agente na próxima compilação do "
        "playbook.",
        "media", 7,
    )
    boa = _licao_resumo(
        "Lição operacional validada sobre como validar no Windows.", "alta", 2
    )
    pb = _playbook_licoes({"implementer": [meta, boa]})
    texto = resumo_licoes_confiaveis("implementer", pb)
    resumo_md = _playbook_objeto({"implementer": [meta, boa]})._resumo()
    cru = pb["learned"]["por_agente"]["implementer"]["licoes"]
    return (
        "curva por agente" not in texto
        and "Lição operacional" in texto
        and "curva por agente" not in resumo_md
        and "Lição operacional" in resumo_md
        # meta permanece no playbook cru (não removida da memória)
        and any("curva por agente" in l["texto"] for l in cru)
        and _e_licao_meta(meta["texto"])
        and not _e_licao_meta(boa["texto"])
        and licoes_confiaveis_textos(pb)
        == ["Lição operacional validada sobre como validar no Windows."]
    )


def _caso_45() -> bool:
    """(M2) `resumo.md` (`_resumo`) inclui até MAX_LICOES_RESUMO (5) lições
    confiáveis por agente, cada uma encurtada a LICAO_RESUMO_CHARS."""
    licoes = [
        _licao_resumo(f"Lição confiável {i} " + "a" * 300, "alta", 10 - i)
        for i in range(8)
    ]
    texto = _playbook_objeto({"implementer": licoes})._resumo()
    bloco = texto.split("### implementer", 1)[1]
    linhas = [l for l in bloco.splitlines() if l.startswith("- (x")]
    if len(linhas) != MAX_LICOES_RESUMO:
        print(f"    FALHOU: {len(linhas)} lições (esperado {MAX_LICOES_RESUMO})")
        return False
    for linha in linhas:
        m = re.match(r"^- \(x(\d+)\) (.*)$", linha)
        if not m:
            return False
        if len(m.group(2)) > LICAO_RESUMO_CHARS:
            return False
        if not m.group(2).endswith("..."):  # texto maior -> truncado
            return False
    return True


def _caso_46() -> bool:
    """(B1/B2) Dedup pelo texto JÁ encurtado (dois textos longos com o mesmo
    prefixo de 120 chars -> UMA linha) e `ocorrencias` inválida/0 -> (x1),
    nunca (x0)."""
    prefixo = "L" + "x" * 130  # > LICAO_RESUMO_CHARS
    a = _licao_resumo(prefixo + " final A", "alta", 3)
    b = _licao_resumo(prefixo + " final B", "alta", 2)
    zero = _licao_resumo(
        "Lição sem ocorrências válidas registradas no playbook.", "media", 0
    )
    texto = resumo_licoes_confiaveis(
        "implementer", _playbook_licoes({"implementer": [a, b, zero]})
    )
    linhas = [l for l in texto.splitlines() if l.startswith("- (x")]
    return (
        len(linhas) == 2  # a e b colapsam em 1; zero é a 2ª
        and not any("(x0)" in l for l in linhas)
        and sum(1 for l in linhas if "(x1)" in l) == 1
    )


CASES = [
    ("parse_agent_file 7 agentes reais", _caso_1),
    ("brain permissions.edit mapa inline", _caso_2),
    ("implementer edits/shell/delegates + hub delegates", _caso_3),
    ("_perm_nao_deny nega-tudo não conta; mapa com allow conta", _caso_3b),
    ("PipelineSpec.default (6 states, routing, contrato, blocking)", _caso_4),
    ("parse_episode registro-calculadora real", _caso_5),
    ("Episode completo (calculadora) vs vazio (placeholder)", _caso_6),
    ("_extrai_comandos extrai validacao", _caso_7),
    ("compile_playbook 7 agents e 6 states (sem gravar)", _caso_8),
    ("Playbook.save grava playbook.json + README.md (temp)", _caso_9),
    ("TaskContract.validate (completo/objetivo/criterios)", _caso_10),
    ("AgentPipeline sem aprovacao -> BLOQUEADA (sem evidencia) e contrato invalido (bloqueada)", _caso_11),
    ("AgentPipeline detecta comando destrutivo -> BLOQUEADA", _caso_12),
    ("HistoryLearner tolera history ausente", _caso_13),
    ("AgentPipeline bloqueia path traversal (../, C:/Windows, D:/x)", _caso_14),
    ("AgentPipeline executa comando aprovado (evidência real, APROVADA)", _caso_15),
    ("_extrai_secoes ignora code blocks (fences)", _caso_16),
    ("Lições triviais filtradas; específicas entram", _caso_17),
    ("Glob absoluto nao crasha -> BLOQUEADA (C:/Windows, D:/*.txt) (N1)", _caso_18),
    ("Glob com '..' ('../*.md') -> BLOQUEADA (N2)", _caso_19),
    ("Timeout de comando longo: morte REAL do processo, exit_code rápido, 2x estável (N3)", _caso_20),
    ("Contexto trivial nao vira episode.contexto no playbook (achado B)", _caso_21),
    ("Curva por agente: agrupamento de licoes/validacoes por agente (a)", _caso_22),
    ("Curva por agente: soma de ocorrencias de licao repetida no mesmo agente (b)", _caso_23),
    ("Curva por agente: multi-agente separado corretamente (c)", _caso_24),
    ("Curva por agente: por_agente presente no dict serializado (d)", _caso_25),
    ("Curva por agente: listas planas ainda presentes (e)", _caso_26),
    ("Trust: parse_episode lê trust/origem/validado; ausência -> fraca", _caso_27),
    ("Trust: lição herda trust do episódio + resumo n_licoes_fracas/confiaveis", _caso_28),
    ("Trust: mesma lição de origens diferentes -> pior trust (conservador)", _caso_29),
    ("Trust: README do playbook expõe confiáveis/fracas por agente", _caso_30),
    ("Parágrafo: 3 linhas físicas -> UMA lição concatenada (a)", _caso_31),
    ("Parágrafo: lista `- item` + continuação indentada -> item completo (b)", _caso_32),
    ("Parágrafo: separados por linha em branco -> lições distintas (c)", _caso_33),
    ("Parágrafo: _e_licao_util no parágrafo completo, não no fragmento (d)", _caso_34),
    ("Parágrafo: lista numerada (1./2./(1)/(2)) -> itens distintos (e)", _caso_35),
    ("Doc x runtime: estados do delivery-protocol (conteúdo + ordem)", _caso_36),
    ("Doc x runtime: roteamento etapa->agente do delivery-protocol", _caso_37),
    ("Doc x runtime: contrato de saída do delivery-protocol (campos em ordem)", _caso_38),
    ("Doc x runtime: doc ausente/sem seções tolerado (sem crash)", _caso_39),
    ("Lições confiáveis: alta/media entram, fraca nunca (a)", _caso_40),
    ("Lições confiáveis: limite e encurtamento por linha (b)", _caso_41),
    ("Lições confiáveis: vazio sem playbook/lições/só fracas (c)", _caso_42),
    ("Lições confiáveis: tolerante a malformado e aceita Playbook (d)", _caso_43),
    ("Lições confiáveis: meta-ruído fora do feed, mantido no playbook (M3)", _caso_44),
    ("Resumo: até MAX_LICOES_RESUMO (5) por agente, 120 chars (M2)", _caso_45),
    ("Lições confiáveis: dedup pós-truncamento + sem (x0) (B1/B2)", _caso_46),
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