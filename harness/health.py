"""Self-awareness do harness (Etapa 3) — detector determinístico de degradação.

PRINCÍPIO DE DESIGN (obrigatório): o detector NÃO pode usar a LLM como juiz —
um modelo degradado se auto-avalia como "ótimo". Toda a detecção é
DETERMINÍSTICA, stdlib-only, SOMENTE-LEITURA e derivada de artefatos que o
harness já produz (playbook, memória episódica e histórico de comandos).

Honestidade: cada métrica é um PROXY. O campo `metrica` documenta o cálculo de
cada fator e os pesos da média ponderada — o consumidor do JSON sabe
exatamente o que o número significa e que não é uma medição direta do modelo.

Famílias de sinais:
  A. Sinais passivos (`gerar_saude`): derivados de artefatos já existentes.
  C. Verificador de contradição (`verificar_contradicoes`): compara o que foi
     ALEGADO/registrado contra a EVIDÊNCIA REAL disponível (os `exit_code`
     reais estão nas etapas do pipeline).

Uso:
    python -m harness.health        # imprime o JSON de gerar_saude()
"""

from __future__ import annotations

import json
import pathlib
import re
import shlex
import sys

from . import config
from .agents import parse_episode
from .memory import Memory
from .observability import _achados_de

# Reconhecimento de EVIDÊNCIA REAL em texto de relatório (Achado ALTO — ETAPA 3)
#
# Os agentes LLM gravam `validacoes_executadas` como RELATÓRIO/PROSA, não como
# comando cru: "tests/agents_test.py → 21/21 PASS",
# "python -m harness.agents compile → playbook regenerado". A heurística ANTIGA
# só olhava o 1º token contra uma lista de executáveis, então quase todo
# relatório virava não-substantivo (evidencia_substantiva ≈ 0.056 no repo real)
# e o gate bloqueava a gravação episódica (loop de aprendizado congelado).
#
# Heurística nova (determinística, stdlib-only):
#   1. extrai o executável com `shlex.split` (fallback `str.split` se falhar);
#   2. executável em `_TOKENS_SUBSTANTIVOS` -> substantivo;
#   3. executável trivial (`_TOKENS_TRIVIAIS`: echo/ls/cat/...) -> NÃO
#      substantivo, salvo se o texto referenciar um alvo FORTE de teste
#      (`_ALVO_FORTE_RE`). O `tests/` "solto" (`ls tests/`) é apenas listagem
#      de diretório e, por isso, NÃO conta;
#   4. qualquer outro executável -> substantivo se o texto referenciar um alvo
#      de teste/build real (`_ALVO_RE`: `_test.py`, `tests/` com barra,
#      `harness.selfcheck`, `py_compile`, `unittest`, `pytest`, `coverage`,
#      `lint`) — é o caso dos relatórios com `tests/..._test.py`;
#   5. `git` é INSPEÇÃO/de VCS (`git status`/`diff`/`log`) -> NÃO substantivo.
#
# M1: a safelist de risco (`RISCO_BAIXO_AUTO_SAFELIST`, que inclui git) NÃO é
# fonte de evidência substantiva.
_TOKENS_SUBSTANTIVOS = (
    "python", "python3", "py", "pytest", "npm", "npx", "node",
    "make", "gcc", "g++", "cmake", "coverage", "pip", "pip3",
    "py_compile", "compile", "build", "tox", "unittest", "lint",
)
# Executáveis de FACHADA: o primeiro token é trivial e NÃO transforma um
# comando em validação (ex.: `echo test`, `ls tests/`, `cat x`).
_TOKENS_TRIVIAIS = frozenset({
    "echo", "printf", "ls", "dir", "type", "cat", "help", "test",
})
# Alvos/marcadores de teste/build REAIS referenciados em texto de relatório.
# `_ALVO_FORTE_RE` é o subconjunto SEM o `tests/` "solto" (diretório): usado no
# executável trivial, onde a mera listagem `ls tests/` NÃO pode contar como
# validação. Um ARQUIVO sob `tests/` (ex.: `cat tests/x_test.py`) conta.
_ALVO_FORTE_RE = re.compile(
    r"_test\.py|tests/\S+\.\w+|harness\.selfcheck|py_compile|\bunittest\b|"
    r"\bpytest\b|\bcoverage\b|\blint\b",
    re.IGNORECASE,
)
_ALVO_RE = re.compile(
    r"_test\.py|tests/|harness\.selfcheck|py_compile|\bunittest\b|"
    r"\bpytest\b|\bcoverage\b|\blint\b",
    re.IGNORECASE,
)

# Executáveis reconhecidos como COMANDO de validação nos critérios de aceite
# (A1 — extração ESTRITA). Um candidato só é aceito quando o PRIMEIRO token é
# um destes E os argumentos não contêm palavras de prosa/conectores. `go` foi
# REMOVIDO (Achado BAIXA A1): é ambíguo (verbo "ir" vs. toolchain) e gerava
# extração espúria de prosa como "suportar go 1.22 no build".
_EXECUTAVEIS_CRITERIO = (
    "python", "python3", "py", "pytest", "npm", "node", "git", "make",
    "gcc", "g++", "pip", "pip3", "npx", "tox", "coverage", "unittest",
    "cmake", "cargo", "javac", "mvn",
)
# Versão "pura" (ex.: `3.12`, `18`, `v1.22`): um argumento assim NÃO é
# subcomando/arquivo real. Rejeita prosa como "usar node 18 para rodar",
# "instalar python 3.12 e rodar" (Achado BAIXA A1).
_RE_VERSAO = re.compile(r"^v?\d+(?:\.\d+)*$")

# Palavras de prosa/conectores que denunciam texto solto (não comando). Ex.:
# "o codigo deve ser python e funcionar" -> candidato "python e funcionar"
# contém "e"/"funcionar" -> REJEITADO (era o falso-positivo A1, que gerava um
# achado "alta" espúrio a partir de prosa).
_PALAVRAS_PROSA_CMD = frozenset({
    "e", "eh", "é", "ou", "depois", "antes", "para", "pra", "por", "com",
    "sem", "que", "deve", "devem", "deveria", "devera", "deverá", "ser",
    "sendo", "funcionar", "funciona", "sucesso", "os", "as", "um", "uma",
    "no", "na", "do", "da", "dos", "das", "em", "se", "entao", "então",
    "ainda", "tambem", "também", "nao", "não", "todo", "toda", "cada",
    "quando", "onde", "porque", "pois", "mas", "como", "isso", "isto",
    "este", "esta", "esse", "essa", "precisa", "precisam", "pode", "podem",
    "the", "and", "or", "then", "should", "must", "a", "an", "to", "of",
})


def _candidato_comando_estrito(candidato: str) -> bool:
    """True se o candidato é um comando ESTRITO.

    Regras:
      1. primeiro token em `_EXECUTAVEIS_CRITERIO`;
      2. nenhum argumento é palavra de prosa/conector;
      3. quando há argumentos, ao menos UM é "real" (não apenas versão). Ex.:
         `node 18`, `python 3.12` e `pip v20.0` são REJEITADOS (só versão);
         `node index.js`, `python -m py_compile x.py` e `git push` são aceitos
         (subcomando/flag/arquivo real). Sem argumentos (ex.: `pytest` entre
         backticks) o candidato é aceito pelas regras 1-2."""
    tokens = str(candidato or "").split()
    if not tokens:
        return False
    if tokens[0].strip("`").lower() not in _EXECUTAVEIS_CRITERIO:
        return False
    args = tokens[1:]
    for tok in args:
        limpo = tok.strip("`'\"").lower()
        if limpo and limpo in _PALAVRAS_PROSA_CMD:
            return False
    # Exige ao menos um argumento que NÃO seja apenas uma versão (`18`,
    # `3.12`, `v1.22`). Isso rejeita prosa do tipo "usar node 18 para rodar".
    if args and not any(
        not _RE_VERSAO.match(tok.strip("`'\"")) for tok in args
    ):
        return False
    return True


def extrai_comandos_criterios(text: str) -> list[str]:
    """Extração ESTRITA (A1) de comandos de validação de um texto de critérios.

    Substitui, nos critérios de aceite, o extrator frouxo
    `agents._extrai_comandos` (regex `python|git|... <resto da linha>`), que
    capturava PROSA: "o codigo deve ser python e funcionar" virava o "comando"
    `python e funcionar` e gerava um achado "alta" falso (Achado MÉDIA A1).

    Regras:
      1. Trechos entre backticks são a fonte mais confiável (aceita comando de
         1 token, ex.: `pytest`);
      2. fora dos backticks, varre por um EXECUTÁVEL conhecido
         (`_EXECUTAVEIS_CRITERIO`) e consome tokens até encontrar uma palavra
         de prosa/conector, exigindo ao menos 1 argumento (não captura
         "python" solto em prosa);
      3. candidatos ambíguos são DESCARTADOS (não geram achado algum — nem
         "alta" nem bloqueio).
    """
    texto = str(text or "")
    comandos: list[str] = []

    def _add(cmd: str) -> None:
        cmd = cmd.strip()
        if cmd and cmd not in comandos:
            comandos.append(cmd)

    # 1. Backticks (mais confiável).
    for m in re.finditer(r"`([^`]+)`", texto):
        cand = m.group(1).strip()
        if _candidato_comando_estrito(cand):
            _add(cand)

    # 2. Texto fora dos backticks.
    sem_backticks = re.sub(r"`[^`]+`", " ", texto)
    tokens = sem_backticks.split()
    i = 0
    while i < len(tokens):
        base = tokens[i].strip("`").lower()
        if base in _EXECUTAVEIS_CRITERIO:
            j = i + 1
            args: list[str] = []
            while j < len(tokens):
                tok = tokens[j].strip("`")
                if tok.strip("`'\"").lower() in _PALAVRAS_PROSA_CMD:
                    break
                args.append(tok)
                j += 1
            if args:
                candidato = " ".join([tokens[i].strip("`")] + args)
                if _candidato_comando_estrito(candidato):
                    _add(candidato)
                i = j
                continue
        i += 1
    return comandos

# Marcadores de delegação (mesma heurística de observability): rótulo de
# agente OU corpo/keywords que mencionam implementer/reviewer ou as etapas
# EM_IMPLEMENTACAO/EM_REVISAO.
_MARCADORES_DELEGACAO = (
    "implementer", "reviewer", "em_implementacao", "em_revisao",
)


def _normaliza_playbook(playbook):
    """Aceita dict de `load_playbook()` OU um `Playbook` (to_dict). Qualquer
    outra coisa -> None (tratado como ausência de playbook)."""
    if playbook is None:
        return None
    if isinstance(playbook, dict):
        return playbook
    to_dict = getattr(playbook, "to_dict", None)
    if callable(to_dict):
        try:
            d = to_dict()
        except Exception:  # noqa: BLE001 — tolerância a fonte inválida
            return None
        return d if isinstance(d, dict) else None
    return None


def _episodio(rec: dict):
    """Parseia um registro episódico em `agents.Episode` (reusa
    `parse_episode`, que calcula `Episode.completo`). Tolerante: falha de
    leitura -> None (o chamador cai na lógica de corpo bruto)."""
    caminho = rec.get("file")
    if not caminho:
        return None
    try:
        return parse_episode(caminho)
    except (OSError, ValueError, TypeError):
        return None


def _agentes_de(rotulo: str) -> list[str]:
    """Normaliza o rótulo multi-tag de agente em lista individual (mesma
    regra de `agents.py`/`observability.py`: separadores `+`, `/`, `,`, espaço)."""
    if not rotulo:
        return []
    partes = (p.strip().lower() for p in re.split(r"[\s+/+,]+", rotulo))
    return [p for p in partes if p]


def _keywords_de(rec: dict) -> list[str]:
    """Keywords do frontmatter (`keywords: [a, b]`), normalizadas."""
    texto = str(rec.get("meta", {}).get("keywords", "")).strip("[]")
    return [k.strip().strip("'\"").lower() for k in texto.split(",")
            if k.strip().strip("'\"")]


def _e_delegado(rec: dict) -> bool:
    """Proxy de execução DELEGADA: rótulo de agente menciona
    implementer/reviewer OU corpo/keywords mencionam as etapas/agentes de
    implementação/revisão. Mesma heurística de `observability._e_delegado`."""
    meta = rec.get("meta", {}) or {}
    if any(a in _MARCADORES_DELEGACAO
           for a in _agentes_de(meta.get("agente", ""))):
        return True
    texto = (str(rec.get("body", "") or "") + " "
             + " ".join(_keywords_de(rec))).lower()
    return any(m in texto for m in _MARCADORES_DELEGACAO)


def _e_registro_pipeline(rec: dict) -> bool:
    """True se o registro é de MÁQUINA (gravado pelo próprio pipeline):
    rótulo de agente contém `pipeline`. A saúde mede a qualidade dos agentes
    LLM (hub/implementer/reviewer/...), NÃO do gravador determinístico — os
    registros que o pipeline grava sobre si mesmo são IGNORADOS nos fatores de
    qualidade (Achado ALTA: sem isso, o detector se auto-bloqueia no bootstrap
    porque lê os próprios registros como degradados)."""
    meta = rec.get("meta", {}) or {}
    return any("pipeline" in a for a in _agentes_de(meta.get("agente", "")))


def _tem_secao_achados(rec: dict) -> bool:
    """True se o corpo do registro tem a sub-seção `achados_da_revisao` COM
    pelo menos um bullet real (`- ...`) sob ela. Reusa a lógica de
    `observability._achados_de` (mesma semântica do panorama): a mera presença
    do cabeçalho NÃO conta — um reviewer que só carimba o rótulo sem registrar
    achados é lido como rubber-stamp, não como revisão real. Proxy de
    re-trabalho/revisão."""
    tem, n_bullets = _achados_de(rec)
    return bool(tem and n_bullets > 0)


def _validacao_substantiva(cmd: str) -> bool:
    """True se `cmd` é uma VALIDAÇÃO REAL (substantiva — TESTE/BUILD/
    COMPILE/LINT), robusta a comandos de RELATÓRIO gravados pelos agentes LLM.

    Heurística (ver bloco `_TOKENS_SUBSTANTIVOS`):
      - executável = 1º token de `shlex.split` (fallback `str.split`); sem
        tokens -> False;
      - `git` é VCS/inspeção -> False (`git status`/`diff`/`log`);
      - executável trivial (`echo`/`printf`/`ls`/`dir`/`type`/`cat`/`help`/
        `test`) -> False, salvo alvo FORTE de teste no texto (`_ALVO_FORTE_RE`).
        `ls tests/`, `type tests.txt`, `echo test` e `cat x` NÃO contam;
      - executável em `_TOKENS_SUBSTANTIVOS` -> True. Cobre tanto comando cru
        (`python tests/x_test.py`, `pytest tests/ -q`, `npm test`, `make`) como
        relatórios (`python -m harness.agents compile`,
        `python -m harness.selfcheck`);
      - demais executáveis -> True se o texto referenciar alvo de teste real
        (`_ALVO_RE`): é o caso de "tests/agents_test.py → 21/21 PASS".

    M1: INSPEÇÃO git NÃO conta; a safelist de risco (`RISCO_BAIXO_AUTO_SAFELIST`)
    NÃO é fonte de evidência substantiva.

    Comando vazio/trivial -> False."""
    texto = str(cmd or "").strip()
    if not texto:
        return False
    try:
        tokens = shlex.split(texto)
    except ValueError:
        tokens = texto.split()
    if not tokens:
        return False
    exe = tokens[0].strip("`'\"").lower()
    # Inspeção de VCS nunca é validação (M1): git status/diff/log.
    if exe == "git":
        return False
    # Fachada: executável trivial só conta com alvo FORTE de teste. Isso
    # preserva `ls tests/` (listagem) como NÃO substantivo.
    if exe in _TOKENS_TRIVIAIS:
        return bool(_ALVO_FORTE_RE.search(texto))
    # Ferramenta de teste/build/compile/lint como executável -> substantivo.
    if exe in _TOKENS_SUBSTANTIVOS:
        return True
    # Relatório/prosa: conta se referenciar alvo de teste real no texto.
    return bool(_ALVO_RE.search(texto))


def _historico(history_file: pathlib.Path) -> dict:
    """Agrega o histórico de comandos (mesma normalização de
    `observability._historico`): contagens por status. Ausente/JSON
    inválido/formato inesperado -> zerado, nunca quebra."""
    base = {"n_total": 0, "n_error": 0, "n_blocked": 0}
    try:
        if not history_file.exists():
            return base
        dados = json.loads(history_file.read_text(encoding="utf-8"))
    except (OSError, json.JSONDecodeError):
        return base
    if not isinstance(dados, list):
        return base
    base["n_total"] = len(dados)
    for item in dados:
        if not isinstance(item, dict):
            continue
        status = str(item.get("status") or "").strip().lower()
        if status in ("error", "retry"):
            base["n_error"] += 1
        elif status == "blocked":
            base["n_blocked"] += 1
    return base


def _por_agente_llm(playbook) -> dict:
    """`learned.por_agente` do playbook SEM os buckets de agentes de MÁQUINA
    (rótulo contém `pipeline`). A saúde reflete a qualidade dos agentes LLM,
    não do gravador determinístico (ver `_e_registro_pipeline`)."""
    pb = _normaliza_playbook(playbook)
    if not isinstance(pb, dict):
        return {}
    learned = pb.get("learned")
    if not isinstance(learned, dict):
        return {}
    por_agente = learned.get("por_agente")
    if not isinstance(por_agente, dict):
        return {}
    return {
        nome: al for nome, al in por_agente.items()
        if "pipeline" not in str(nome).lower()
    }


def _fator_licoes_confiaveis(playbook) -> float:
    """Fator 1: razão de lições confiáveis do playbook.

    `soma(n_licoes_confiaveis) / max(1, soma(n_licoes_confiaveis) +
    soma(n_licoes_fracas))` sobre `learned.por_agente`, IGNORANDO os buckets de
    agente de MÁQUINA (`pipeline`) — a saúde mede os agentes LLM. Sem dados ->
    neutro 0.5. Proxy: quantas lições agregadas vêm de memória validada
    (alta/media) e não de memória fraca."""
    por_agente = _por_agente_llm(playbook)
    if not por_agente:
        return 0.5
    confiaveis = 0
    fracas = 0
    for al in por_agente.values():
        if not isinstance(al, dict):
            continue
        confiaveis += int(al.get("n_licoes_confiaveis", 0) or 0)
        fracas += int(al.get("n_licoes_fracas", 0) or 0)
    total = confiaveis + fracas
    if total <= 0:
        return 0.5
    return confiaveis / total


def _fator_evidencia(episodios: list) -> float:
    """Fator 2: fração das `validacoes_executadas` dos episódicos que são
    SUBSTANTIVAS vs triviais (`_validacao_substantiva`). Sem validações ->
    neutro 0.5. Proxy: a evidência registrada é validação de verdade
    (testes/build/compile/lint) ou comando de fachada (`echo`/`ls`). O
    reconhecimento é robusto a RELATÓRIOS dos agentes LLM: aceita tanto comando
    cru (`pytest tests/`) quanto relatório com alvo real
    ("tests/agents_test.py → 21/21 PASS")."""
    total = 0
    substantivas = 0
    for ep in episodios:
        for cmd in getattr(ep, "validacoes", []) or []:
            total += 1
            if _validacao_substantiva(cmd):
                substantivas += 1
    if total <= 0:
        return 0.5
    return substantivas / total


def _fator_contrato(episodios: list) -> float:
    """Fator 3: fração de registros com as 4 seções preenchidas (reusa
    `Episode.completo` de `agents.parse_episode`). Sem registros -> neutro 0.5.
    Proxy: o contrato de saída foi entregue inteiro (Contrato/Fluxo/Resultado/
    Contexto), não apenas um stub."""
    episodios = [ep for ep in episodios if ep is not None]
    if not episodios:
        return 0.5
    completos = sum(1 for ep in episodios if getattr(ep, "completo", False))
    return completos / len(episodios)


def _fator_historico(history_file: pathlib.Path) -> float:
    """Fator 4: `1 - (n_error + n_blocked) / max(1, n_total)` do histórico.
    Sem histórico (ausente/vazio/inválido) -> neutro 0.5. Proxy: proporção de
    comandos que terminaram bem vs erros/bloqueios."""
    hist = _historico(history_file)
    if hist["n_total"] <= 0:
        return 0.5
    ruins = hist["n_error"] + hist["n_blocked"]
    return 1.0 - (ruins / hist["n_total"])


def _fator_achados(records: list[dict]) -> float:
    """Fator 5: fração dos registros DELEGADOS que têm a seção
    `achados_da_revisao` presente. Sem registros delegados -> neutro 0.5.

    Interpretação documentada: ausência TOTAL de achados em execuções
    delegadas é sinal de rubber-stamp (o reviewer apenas carimba, não revisa
    de fato). O fator é maior quando há achados registrados nas execuções
    delegadas."""
    delegados = [rec for rec in records if _e_delegado(rec)]
    if not delegados:
        return 0.5
    com_achados = sum(1 for rec in delegados if _tem_secao_achados(rec))
    return com_achados / len(delegados)


def _nivel(score: float) -> str:
    """Nível por limiares de `config.py` (SAUDE_LIMIAR_*)."""
    if score >= config.SAUDE_LIMIAR_SAUDAVEL:
        return "SAUDAVEL"
    if score >= config.SAUDE_LIMIAR_ATENCAO:
        return "ATENCAO"
    if score >= config.SAUDE_LIMIAR_DEGRADADO:
        return "DEGRADADO"
    return "CRITICO"


def saude_neutra(motivo: str = "fonte ausente/inválida") -> dict:
    """Saúde NEUTRA de fallback: score 0.5 com nível COERENTE com os limiares
    (`_nivel(0.5)` -> DEGRADADO). Usada quando o detector NÃO pode ser
    calculado (ex.: falha inesperada/import), para o consumidor nunca receber
    um dict malformado. O pipeline e o panorama usam esta forma no except —
    nunca crasham por causa do detector."""
    score = 0.5
    return {
        "score": score,
        "nivel": _nivel(score),
        "fatores": {},
        # `indisponivel=True` diferencia "não foi possível medir" de um
        # bootstrap real (sem fontes). `bootstrap=False` mantém o comportamento
        # conservador no anti-envenomamento (falha do detector NÃO libera
        # gravação).
        "fontes_presentes": 0,
        "fontes_llm": 0,
        "fontes": {},
        "bootstrap": False,
        "indisponivel": True,
        "metrica": f"saúde neutra ({motivo}) — fatores indisponíveis",
    }


def _metrica() -> str:
    """Documentação honesta dos fatores e pesos (proxy, não medição direta)."""
    pesos = ", ".join(
        f"{nome}={peso:g}" for nome, peso in config.SAUDE_PESOS.items()
    )
    return (
        "Saúde determinística do harness/modelo (PROXY — NÃO usa LLM como "
        "juiz: um modelo degradado se auto-avalia como 'ótimo'): somente "
        "leitura, stdlib-only, derivada de artefatos já produzidos. "
        "Fatores 0..1 (neutro 0.5 quando a fonte está ausente/sem dados): "
        "(1) licoes_confiaveis = soma(n_licoes_confiaveis) / "
        "max(1, soma(n_licoes_confiaveis)+soma(n_licoes_fracas)) em "
        "learned.por_agente do playbook (proxy de memória validada); "
        "(2) evidencia_substantiva = fração de validacoes_executadas dos "
        "episódicos que são validação REAL (TESTE/BUILD/COMPILE/LINT), "
        "robusta a RELATÓRIOS: executável (shlex.split) em "
        "(python/python3/py/pytest/npm/npx/node/make/gcc/g++/cmake/coverage/"
        "pip/pip3/py_compile/compile/build/tox/unittest/lint) OU alvo de teste "
        "real referenciado no texto (_test.py, tests/ com barra, "
        "harness.selfcheck, py_compile, unittest, pytest, coverage, lint); "
        "comandos de fachada (echo/printf/ls/dir/type/cat/help/test) NÃO "
        "contam, salvo alvo FORTE de teste (o `ls tests/` de listagem NÃO "
        "conta), e INSPEÇÃO GIT (git status/diff/log) NÃO conta (é inspeção, "
        "não validação) — a safelist de risco NÃO é fonte de substantivo "
        "(proxy de evidência real); "
        "(3) contrato_completo = fração de registros com as 4 seções "
        "(Contrato/Fluxo/Resultado/Contexto) preenchidas, via "
        "Episode.completo (proxy de contrato entregue inteiro); "
        "(4) historico_saudavel = 1 - (n_error + n_blocked)/max(1,n_total) "
        "do histórico de comandos (proxy de operação sem erros/bloqueios); "
        "(5) achados_reviewer = fração de registros DELEGADOS "
        "(implementer/reviewer) com a sub-seção 'achados_da_revisao' E ao "
        "menos um bullet real ('- ...') sob ela — cabeçalho vazio ou ausência "
        "TOTAL de achados em execuções delegadas é sinal de "
        "rubber-stamp (proxy de revisão real). "
        "IMPORTANTE (Achado ALTA): os fatores derivados de episódicos "
        "(1), (2), (3) e (5) IGNORAM registros de MÁQUINA cujo agente contém "
        "'pipeline' (gravador determinístico) — a saúde mede os agentes LLM, "
        "não o pipeline sobre si mesmo; sem isso o detector lê os próprios "
        "registros como degradados e se auto-bloqueia no bootstrap. "
        f"score = média ponderada ({pesos}). "
        f"Níveis: >= {config.SAUDE_LIMIAR_SAUDAVEL} SAUDAVEL; "
        f">= {config.SAUDE_LIMIAR_ATENCAO} ATENCAO; "
        f">= {config.SAUDE_LIMIAR_DEGRADADO} DEGRADADO; senão CRITICO. "
        "Saída adicional (anti-envenomamento): fontes_presentes = nº de "
        "fatores com FONTE REAL (0..5); fontes_llm = nº de fontes derivadas "
        "de registros/playbook NÃO-máquina (0..4, exclui o histórico); "
        "fontes = presença por fator; bootstrap=True quando NÃO há fonte de "
        "LLM (harness novo OU só registros de máquina) — nesse estado o "
        "pipeline NÃO bloqueia a gravação."
    )


def gerar_saude(memory=None, playbook=None, history_file=None) -> dict:
    """Calcula a saúde determinística (Família A — sinais passivos).

    Parâmetros (todos opcionais):
      memory       — instância de `harness.memory.Memory` (default: cria).
                     Usa `list_records()`.
      playbook     — dict de `harness.agents.load_playbook()` (default: carrega
                     se None). `Playbook` também é aceito.
      history_file — caminho de `logs/harness_history.json` (default:
                     `config.HISTORY_FILE`).

    Tolerante à ausência/invalidez de QUALQUER fonte (nunca crash; ausência
    vira fator neutro 0.5).

    Retorno: `{"score": float 0..1, "nivel": str, "fatores": {...},
    "fontes_presentes": int, "fontes_llm": int, "fontes": {...},
    "bootstrap": bool, "metrica": str}`. Registros de MÁQUINA (agente contém
    'pipeline') são ignorados nos fatores de qualidade.
    """
    history_path = pathlib.Path(history_file) if history_file else config.HISTORY_FILE

    # Playbook: usa o passado; se None, tenta carregar (tolerante).
    pb = _normaliza_playbook(playbook)
    if pb is None:
        try:
            from .agents import load_playbook
            pb = load_playbook()
        except Exception:  # noqa: BLE001 — fonte ausente/inválida -> neutro
            pb = None

    # Memória: usa a passada; se None, cria (tolerante).
    records: list[dict] = []
    try:
        mem = memory if memory is not None else Memory()
        records = mem.list_records() or []
    except Exception:  # noqa: BLE001 — fonte inválida -> sem episódicos
        records = []

    # Achado ALTA (auto-bloqueio de bootstrap): registros de MÁQUINA (agente
    # contém 'pipeline') são IGNORADOS nos fatores derivados de episódicos e no
    # playbook. A saúde mede os agentes LLM; os registros que o pipeline grava
    # sobre si mesmo não podem derrubar a própria saúde nem fechar o loop.
    records_llm = [rec for rec in records if not _e_registro_pipeline(rec)]

    # Episódios parseados (para validações/contrato). Guarda None para os que
    # falharem — `_fator_contrato` filtra.
    episodios = [_episodio(rec) for rec in records_llm]

    # Presença de FONTE REAL por fator (não de dado "bom", apenas de fonte
    # existente): distingue "sem fontes" (harness novo -> bootstrap) de
    # "fontes presentes e ruins" (degradação real). Usado pelo
    # anti-envenomamento do pipeline para NÃO bloquear o bootstrap.
    hist = _historico(history_path)
    episodios_validos = [ep for ep in episodios if ep is not None]
    por_agente_llm = _por_agente_llm(pb)
    delegados = [rec for rec in records_llm if _e_delegado(rec)]
    fontes = {
        "licoes_confiaveis": bool(por_agente_llm),
        "evidencia_substantiva": any(
            getattr(ep, "validacoes", None) for ep in episodios_validos
        ),
        "contrato_completo": bool(episodios_validos),
        "historico_saudavel": hist["n_total"] > 0,
        "achados_reviewer": bool(delegados),
    }
    fontes_presentes = sum(1 for presente in fontes.values() if presente)
    # Fontes de LLM: excluem o histórico (comandos) e derivam de registros/
    # playbook NÃO-máquina. `bootstrap=True` quando NÃO há nenhuma fonte de
    # LLM (harness novo OU só registros de máquina do pipeline): a carência
    # defensiva impede que o pipeline bloqueie a gravação lendo os próprios
    # registros (Achado ALTA).
    fontes_llm = sum(
        1 for fator in (
            "licoes_confiaveis", "evidencia_substantiva",
            "contrato_completo", "achados_reviewer",
        ) if fontes[fator]
    )
    bootstrap = fontes_llm == 0

    fatores = {
        "licoes_confiaveis": _fator_licoes_confiaveis(pb),
        "evidencia_substantiva": _fator_evidencia(episodios),
        "contrato_completo": _fator_contrato(episodios),
        "historico_saudavel": _fator_historico(history_path),
        "achados_reviewer": _fator_achados(records_llm),
    }

    peso_total = sum(config.SAUDE_PESOS.values()) or 1.0
    score = sum(
        fatores[nome] * config.SAUDE_PESOS.get(nome, 0.0)
        for nome in fatores
    ) / peso_total
    score = round(max(0.0, min(1.0, score)), 3)

    return {
        "score": score,
        "nivel": _nivel(score),
        "fatores": {nome: round(valor, 3) for nome, valor in fatores.items()},
        "fontes_presentes": fontes_presentes,
        "fontes_llm": fontes_llm,
        "fontes": fontes,
        "bootstrap": bootstrap,
        "metrica": _metrica(),
    }


# ---------------------------------------------------------------------------
# Família C — verificador de contradição


def _etapas_por_comando(etapas) -> dict[str, list[dict]]:
    """Indexa as etapas do pipeline (tipo == 'comando') por comando."""
    indice: dict[str, list[dict]] = {}
    for etapa in etapas or []:
        if not isinstance(etapa, dict) or etapa.get("tipo") != "comando":
            continue
        comando = etapa.get("comando")
        if comando is None:
            continue
        indice.setdefault(str(comando), []).append(etapa)
    return indice


def _execucao_observada(execucao) -> bool:
    """True se a execução REAL foi observada: `exit_code` presente (inclusive
    != 0) OU encerrada por cap de saída (`exit_code` None mas o comando rodou).
    `execucao` ausente/`None`/bloqueada NÃO é execução observada."""
    if not isinstance(execucao, dict):
        return False
    if execucao.get("exit_code") is not None:
        return True
    return bool(execucao.get("encerrado_por_cap"))


def _normaliza_comando(cmd) -> str:
    """Normalização conservadora de comando para comparação: colapsa espaços e
    baixa a caixa. Preserva ordem/argumentos (não casa comandos diferentes)."""
    return " ".join(str(cmd or "").split()).lower()


def _comando_coberto(esperado, executados_norm: list[str]) -> bool:
    """True se o comando ESPERADO está coberto por algum comando EXECUTADO.

    Achado BAIXA (normalização): a cobertura NÃO exige igualdade exata.
    Considere coberto quando os dois compartilham o mesmo EXECUTÁVEL/primeiros
    tokens e um é PREFIXO do outro (ignorando flags extras): ex.:
    `pytest tests/x.py` é coberto por `pytest tests/x.py -q` e vice-versa.
    Conservador: executáveis diferentes NUNCA casam (`pytest` não cobre
    `npm test`) e um token divergente no prefixo também não
    (`pytest tests/x.py` != `pytest tests/y.py`)."""
    esp = _normaliza_comando(esperado).split()
    if not esp:
        return False
    for ex in executados_norm:
        et = ex.split()
        if not et or et[0] != esp[0]:
            continue
        n = min(len(et), len(esp))
        if et[:n] == esp[:n]:
            return True
    return False


def verificar_contradicoes(*, validacoes_executadas, status, etapas=None,
                           comandos_criterios=None, criterios=None) -> list[dict]:
    """Compara o que foi ALEGADO/registrado contra a EVIDÊNCIA REAL (os
    `exit_code` reais estão nas etapas do pipeline). Determinístico, sem I/O.

    FILTRO ACIONÁVEL (A1): além das invariantes defensivas, sinaliza CRITÉRIO
    DE ACEITE cuja validação extraída NÃO foi executada. Sem esta checagem o
    verificador seria inerte no pipeline, que só passa dados já grounded
    (`validacoes_executadas` contém apenas comandos com `exit_code` real): os
    critérios são a fonte que ainda "não casou" com a evidência.
    `comandos_criterios` é a lista já extraída; alternativamente, `criterios`
    é aceito e a extração ESTRITA (`extrai_comandos_criterios`) é feita aqui —
    candidatos ambíguos de prosa são DESCARTADOS (não geram achado "alta").

    A cobertura de critério é por PREFIXO de tokens com o mesmo executável
    (`_comando_coberto`): `pytest tests/x.py` é coberto por
    `pytest tests/x.py -q` (Achado BAIXA) — executáveis diferentes não casam.

    Detecta (cada item `{"severidade", "descricao"}`):
      0. critério de aceite com validação extraída NÃO executada ->
         `{"severidade": "alta", "descricao": "critério de aceite sem validação
         executada: <cmd>"}`;
      1. comando em `validacoes_executadas` sem execução real observada nas
         `etapas` (sem `execucao`/`exit_code`) -> evidência não-grounded;
      2. validação alegada como executada mas o runtime não registrou a etapa
         correspondente;
      3. `status == "APROVADA"` com alguma falha real (`exit_code != 0`).

    Sem contradições -> `[]`."""
    contradicoes: list[dict] = []
    etapas = etapas or []
    indice = _etapas_por_comando(etapas)

    esperados = list(comandos_criterios or [])
    if not esperados and criterios:
        esperados = extrai_comandos_criterios(
            " ".join(str(c) for c in criterios)
        )
    executados_norm = [
        _normaliza_comando(cmd) for cmd in (validacoes_executadas or [])
    ]
    for esperado in esperados:
        if not _comando_coberto(esperado, executados_norm):
            contradicoes.append({
                "severidade": "alta",
                "descricao": (
                    "critério de aceite sem validação executada: "
                    f"{esperado}"
                ),
            })

    for cmd in validacoes_executadas or []:
        comando = str(cmd)
        encontradas = indice.get(comando)
        if not encontradas:
            contradicoes.append({
                "severidade": "bloqueante",
                "descricao": (
                    f"contradição: validação alegada como executada, mas o "
                    f"runtime não registrou a etapa correspondente: {comando!r}"
                ),
            })
            continue
        if not any(_execucao_observada(e.get("execucao")) for e in encontradas):
            contradicoes.append({
                "severidade": "bloqueante",
                "descricao": (
                    f"contradição: comando em validacoes_executadas sem "
                    f"execução real observada (exit_code ausente): {comando!r}"
                ),
            })

    status_norm = str(status or "").strip().upper()
    if status_norm == "APROVADA":
        falhas = []
        for etapa in etapas:
            if not isinstance(etapa, dict):
                continue
            execucao = etapa.get("execucao")
            if isinstance(execucao, dict) and execucao.get("exit_code") not in (None, 0):
                falhas.append(str(etapa.get("comando")))
        if falhas:
            contradicoes.append({
                "severidade": "bloqueante",
                "descricao": (
                    "contradição: status APROVADA com falha real "
                    f"(exit_code != 0): {', '.join(f for f in falhas if f)}"
                ),
            })

    return contradicoes


# ---------------------------------------------------------------------------
# CLI


def main(argv: list[str] | None = None) -> int:
    """CLI: imprime o JSON de `gerar_saude()` (padrão dos outros módulos)."""
    # Console Windows (cp1252) não imprime todos os caracteres UTF-8; usa
    # UTF-8 com substituição para nunca quebrar a saída.
    try:
        sys.stdout.reconfigure(encoding="utf-8", errors="replace")
    except (AttributeError, ValueError):
        pass
    _ = argv
    print(json.dumps(gerar_saude(), ensure_ascii=False, indent=2))
    return 0


if __name__ == "__main__":
    sys.exit(main())
