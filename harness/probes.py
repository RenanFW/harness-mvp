"""Golden probes (Etapa 4) — sensor ATIVO de qualidade do modelo.

PRINCÍPIO DE DESIGN (obrigatório): o harness NÃO invoca a LLM sozinho (o
pipeline é determinístico). As probes são um PROTOCOLO:

  1. o módulo fornece tarefas de resposta CONHECIDA (`PROBES`);
  2. o hub/agente executa cada probe PELO modelo e submete a resposta ao
     checador;
  3. o checador é 100% DETERMINÍSTICO (stdlib-only) — NUNCA outro LLM julga
     (um modelo degradado se auto-avalia como "ótimo");
  4. o módulo mede a taxa de acerto (`avaliar_resultados`).

As probes são SENSÍVEIS A ESFORÇO: fáceis para um modelo em alto esforço,
falháveis para um modelo "preguiçoso" (a resposta plausível de pattern-match é
diferente da correta). Nenhuma dependência externa.

NORMALIZAÇÃO (documentada): há DUAS normalizações, com propósitos distintos.

- `exato` usa `_normalizar_exato`: lowercase, trim, remoção de TODO espaço em
  branco (interno e externo) e remoção apenas de aspas/backticks EXTERNOS,
  PRESERVANDO pontuação significativa (colchetes, vírgulas, hífens, pontos).
  Assim `[0, 1, 2]` e `[0,1,2]` casam, mas `0-1-2` e `0.1.2` NÃO (têm
  pontuação significativa diferente). Tolerâncias específicas de uma probe são
  declaradas no campo `aceita` (lista de variantes), nunca afrouxando o global.
- `contem`/`booleano` usam `_normalizar`: lowercase, trim, remoção de acentos
  (NFKD), remoção de toda pontuação (inclusive `_`) e colapso de espaços —
  tolerante para respostas em prosa. Probes `contem` podem declarar
  `nao_contem` (substrings proibidas): a resposta é rejeitada se contiver
  qualquer uma delas (ex.: afirmar que a função errada está "certa").

O formato de resposta esperado de cada probe está no campo `formato`; a
resposta correta de referência está em `resposta_esperada` (usada na
auto-checagem/sanity).

CALIBRAÇÃO (leia antes de fixar limiares): a baseline de saúde
(`HEALTH_BASELINE_FILE`, via `salvar_baseline`) deve ser medida com o modelo
em ALTO esforço ANTES de fixar limiares/de margem de degradação
(`PROBE_LIMIAR_*`/`PROBE_MARGEM_DEGRADACAO`). Medir em esforço baixo e usar o
resultado como baseline produz falsos degradados — a própria definição de
"degradado" depende do esforço de referência.

Uso:
    python -m harness.probes list
    python -m harness.probes check <id> "<resposta>"

Import programático:
    from harness.probes import PROBES, get_probe, verificar, avaliar_resultados
"""

from __future__ import annotations

import argparse
import json
import pathlib
import re
import sys
import unicodedata
from datetime import date

from . import config

# ---------------------------------------------------------------------------
# Tipos de checagem determinística
#
#   exato              -> compara a resposta normalizada com `resposta_esperada`;
#   contem             -> a resposta normalizada CONTÉM a esperada normalizada;
#   booleano           -> resposta sim/não comparada à verdade em
#                         `resposta_esperada`;
#   regex_match        -> o checador EXECUTA `re.search(padrao, texto)` para
#                         obter a verdade e compara com a resposta do modelo;
#   regex_alternativas -> o checador EXECUTA `re` em cada alternativa e obtém a
#                         ÚNICA que casa; compara com a resposta;
#   json_valido        -> o checador EXECUTA `json.loads(json_texto)` para obter
#                         a validade e compara com a resposta do modelo.
#
# NUNCA há `eval`/`exec` de entrada: só `re` e `json` sobre dados FIXOS das
# probes. O modelo só fornece texto de resposta; nunca código.

PROBES: list[dict] = [
    {
        "id": "py_lista_range",
        "descricao": "Output de list comprehension com range.",
        "prompt": (
            "Considere o código Python: `resultado = [i for i in range(3)]`. "
            "Qual o valor exato de `resultado`? Responda apenas com a lista."
        ),
        "tipo": "exato",
        "formato": "lista Python, ex.: [0, 1, 2]",
        "resposta_esperada": "[0, 1, 2]",
    },
    {
        "id": "py_comprehension_filtro",
        "descricao": "List comprehension com filtro de pares ao quadrado.",
        "prompt": (
            "Em Python, o que retorna a expressão "
            "`[x*x for x in range(5) if x % 2 == 0]`? "
            "Responda apenas com a lista."
        ),
        "tipo": "exato",
        "formato": "lista Python, ex.: [0, 1, 2]",
        "resposta_esperada": "[0, 4, 16]",
    },
    {
        "id": "py_split_indice",
        "descricao": "Indexação de resultado de split.",
        "prompt": (
            "Considere `s = \"harness-mvp\"`. Qual o valor de `s.split(\"-\")[1]`? "
            "Responda apenas a string."
        ),
        "tipo": "exato",
        "formato": "string sem aspas",
        "resposta_esperada": "mvp",
    },
    {
        "id": "py_slice_bug_output",
        "descricao": "Output real de uma função com bug de fatiamento.",
        "prompt": (
            "Considere a função a seguir e `xs = [10, 20, 30, 40, 50]`:\n"
            "def ultimos_tres(xs):\n"
            "    return xs[len(xs)-3: len(xs)-2]\n"
            "O QUE A FUNÇÃO RETORNA (antes de qualquer correção)? "
            "Responda apenas com a lista."
        ),
        "tipo": "exato",
        "formato": "lista Python",
        "resposta_esperada": "[30]",
    },
    {
        "id": "regex_match_plus",
        "descricao": "Regex `^a+$` contra `aaa`.",
        "prompt": (
            "A regex `^a+$` casa com a string `aaa`? Responda SIM ou NÃO."
        ),
        "tipo": "regex_match",
        "formato": "SIM ou NÃO",
        "padrao": "^a+$",
        "texto": "aaa",
        "resposta_esperada": "sim",
    },
    {
        "id": "regex_match_digitos",
        "descricao": "Regex ancorada de exatamente 2 dígitos contra 3 dígitos.",
        "prompt": (
            "A regex `^[0-9]{2}$` casa com a string `123`? Responda SIM ou NÃO."
        ),
        "tipo": "regex_match",
        "formato": "SIM ou NÃO",
        "padrao": "^[0-9]{2}$",
        "texto": "123",
        "resposta_esperada": "nao",
    },
    {
        "id": "regex_alternativa_unica",
        "descricao": "Qual alternativa casa com a regex ancorada.",
        "prompt": (
            "Qual das strings abaixo casa com a regex `^[a-z]+_[0-9]{3}$`? "
            "Alternativas: `abc_123`, `abc_12`, `ABC_123`, `abc_1234`. "
            "Responda apenas com a alternativa correta."
        ),
        "tipo": "regex_alternativas",
        "formato": "uma das alternativas, sem backticks",
        "padrao": "^[a-z]+_[0-9]{3}$",
        "alternativas": ["abc_123", "abc_12", "ABC_123", "abc_1234"],
        "resposta_esperada": "abc_123",
    },
    {
        "id": "logica_primos_ate_30",
        "descricao": "Contagem de primos no intervalo 1..30.",
        "prompt": (
            "Quantos números primos existem entre 1 e 30, inclusive? "
            "Responda apenas o número."
        ),
        "tipo": "exato",
        "formato": "número inteiro",
        "resposta_esperada": "10",
    },
    {
        "id": "logica_horario",
        "descricao": "Aritmética de horário com virada de hora.",
        "prompt": (
            "Um trem parte às 14:35 e viaja por 2 horas e 50 minutos. "
            "A que horas ele chega? Responda no formato HH:MM (24 horas)."
        ),
        "tipo": "exato",
        "formato": "HH:MM em 24 horas",
        "resposta_esperada": "17:25",
    },
    {
        "id": "logica_booleana",
        "descricao": "Avaliação de expressão booleana com negação.",
        "prompt": (
            "Qual o valor de `not (True and False)`? "
            "Responda SIM se for verdadeiro ou NÃO se for falso."
        ),
        "tipo": "booleano",
        "formato": "SIM ou NÃO",
        "resposta_esperada": "sim",
    },
    {
        "id": "extracao_nivel_risco",
        "descricao": "Extração de campo estruturado a partir de texto curto.",
        "prompt": (
            "Considere a descrição de tarefa: \"Corrigir um erro de digitação "
            "no README.md, sem alterar comportamento.\". Extraia o "
            "`nivel_de_risco` no padrão do harness (baixo|medio|alto). "
            "Responda apenas o valor."
        ),
        "tipo": "exato",
        "formato": "baixo, medio ou alto",
        "resposta_esperada": "baixo",
    },
    {
        "id": "json_valido_aninhado",
        "descricao": "Validade de JSON com aninhamento.",
        "prompt": (
            "O texto a seguir é um JSON válido? `{\"a\": 1, \"b\": [2, 3]}` "
            "Responda SIM ou NÃO."
        ),
        "tipo": "json_valido",
        "formato": "SIM ou NÃO",
        "json_texto": '{"a": 1, "b": [2, 3]}',
        "resposta_esperada": "sim",
    },
    {
        "id": "json_valido_virgula_final",
        "descricao": "JSON com vírgula final (inválido).",
        "prompt": (
            "O texto a seguir é um JSON válido? `{\"a\": 1, \"b\": 2,}` "
            "Responda SIM ou NÃO."
        ),
        "tipo": "json_valido",
        "formato": "SIM ou NÃO",
        "json_texto": '{"a": 1, "b": 2,}',
        "resposta_esperada": "nao",
    },
    {
        "id": "json_campo_status",
        "descricao": "Extração de campo de um JSON curto.",
        "prompt": (
            "Dado o JSON `{\"status\": \"APROVADA\", \"n\": 3}`, qual o valor "
            "do campo `status`? Responda apenas o valor."
        ),
        "tipo": "exato",
        "formato": "string sem aspas",
        "resposta_esperada": "APROVADA",
    },
    {
        "id": "bug_par_impar_funcao",
        "descricao": "Identificação de função com bug de paridade.",
        "prompt": (
            "As duas funções abaixo tentam classificar um inteiro `n` como "
            "'par' ou 'impar'. Exatamente UMA está INCORRETA:\n"
            "def funcao_a(n):\n"
            "    if n % 2 == 0:\n"
            "        return \"par\"\n"
            "    return \"impar\"\n"
            "def funcao_b(n):\n"
            "    if n // 2 == 0:\n"
            "        return \"par\"\n"
            "    return \"impar\"\n"
            "Responda com o nome da função INCORRETA."
        ),
        "tipo": "contem",
        "formato": "nome da função (ex.: funcao_b)",
        "resposta_esperada": "funcao_b",
        # A resposta correta é o NOME da função errada. Frases que AFIRMAM que
        # `funcao_b` está correta contradizem o enunciado e são rejeitadas.
        "nao_contem": ["esta certa", "esta correta", "e certa", "e correta"],
    },
    # --- Armadilhas adicionais (sensíveis a esforço) -----------------------
    # Todas têm resposta plausível-e-errada: um modelo "preguiçoso" de
    # pattern-match erra; um modelo atento acerta. Determinísticas/verificáveis.
    {
        "id": "py_mutavel_default",
        "descricao": "Argumento default mutável compartilhado entre chamadas.",
        "prompt": (
            "Considere o código Python:\n"
            "def f(x, acc=[]):\n"
            "    acc.append(x)\n"
            "    return acc\n"
            "f(1)\n"
            "print(f(2))\n"
            "O que é impresso? Responda apenas com a lista."
        ),
        "tipo": "exato",
        "formato": "lista Python",
        "resposta_esperada": "[1, 2]",
    },
    {
        "id": "py_sort_retorna_none",
        "descricao": "`list.sort()` ordena in-place e retorna None.",
        "prompt": (
            "Considere o código Python:\n"
            "x = [3, 1, 2]\n"
            "y = x.sort()\n"
            "print(y)\n"
            "O que é impresso? Responda apenas com o valor."
        ),
        "tipo": "exato",
        "formato": "valor Python (ex.: None)",
        "resposta_esperada": "None",
    },
    {
        "id": "py_dict_get_vs_colchete",
        "descricao": "`dict.get` com default quando a chave existe com valor falsy.",
        "prompt": (
            "Considere o código Python:\n"
            "d = {\"a\": 0}\n"
            "print(d.get(\"a\", 5))\n"
            "O que é impresso? Responda apenas com o valor."
        ),
        "tipo": "exato",
        "formato": "número inteiro",
        "resposta_esperada": "0",
    },
    {
        "id": "py_is_vs_igual",
        "descricao": "Identidade (`is`) vs igualdade (`==`) entre listas.",
        "prompt": (
            "Considere `a = [1, 2]` e `b = [1, 2]`. A expressão `a is b` é "
            "verdadeira? Responda SIM ou NÃO."
        ),
        "tipo": "booleano",
        "formato": "SIM ou NÃO",
        "resposta_esperada": "nao",
    },
    {
        "id": "py_precedencia_operador",
        "descricao": "Precedência aritmética (exponenciação antes de * / +).",
        "prompt": (
            "Qual o resultado da expressão Python `2 + 3 * 4 ** 2`? "
            "Responda apenas com o número."
        ),
        "tipo": "exato",
        "formato": "número inteiro",
        "resposta_esperada": "50",
    },
    {
        "id": "py_range_fim_exclusivo",
        "descricao": "`range` com passo e fim exclusivo.",
        "prompt": (
            "Qual o valor de `list(range(2, 11, 3))` em Python? "
            "Responda apenas com a lista."
        ),
        "tipo": "exato",
        "formato": "lista Python",
        "resposta_esperada": "[2, 5, 8]",
    },
]

# Índice id -> probe (montado uma vez; erro de id duplicado seria bug de autor).
_POR_ID: dict[str, dict] = {p["id"]: p for p in PROBES}


# ---------------------------------------------------------------------------
# Normalização e checagem determinística


def _normalizar(texto) -> str:
    """Normalização TOLERANTE (para `contem`/`booleano`): lowercase, trim,
    remove acentos (NFKD), remove pontuação (inclusive `_`) e colapsa espaços."""
    s = str(texto if texto is not None else "").strip().lower()
    s = unicodedata.normalize("NFKD", s)
    s = "".join(c for c in s if not unicodedata.combining(c))
    s = re.sub(r"[^a-z0-9\s]", " ", s)
    s = re.sub(r"\s+", " ", s).strip()
    return s


def _normalizar_exato(texto) -> str:
    """Normalização ESTRITA (para `exato`): lowercase, trim, remove TODO
    espaço em branco (interno e externo) e remove apenas aspas/backticks
    EXTERNOS. PRESERVA pontuação significativa (colchetes, vírgulas, hífens,
    pontos): `[0, 1, 2]` e `[0,1,2]` casam, mas `0-1-2` e `0.1.2` NÃO."""
    s = str(texto if texto is not None else "").strip().lower()
    s = s.strip("`'\"")
    s = re.sub(r"\s+", "", s)
    return s


def _limpar_token(texto) -> str:
    """Limpa uma resposta TOKEN preservando a CAIXA (importa em regex).

    Remove espaços nas pontas e backticks/aspas de envolvimento. Não baixa a
    caixa nem remove pontuação interna: `ABC_123` continua `ABC_123` (a
    comparação de `regex_alternativas` é case-sensitive, ao contrário da
    normalização textual de `exato`/`contem`).
    """
    return str(texto if texto is not None else "").strip().strip("`'\"")


def _parse_booleano(texto):
    """Interpreta uma resposta sim/não em bool. Ambigua -> None."""
    s = _normalizar(texto)
    if s in {"sim", "s", "yes", "y", "true", "verdadeiro", "1"}:
        return True
    if s in {"nao", "n", "no", "false", "falso", "0"}:
        return False
    return None


def get_probe(probe_id: str):
    """Retorna o dict da probe com `probe_id` (None se não existir)."""
    return _POR_ID.get(str(probe_id))


def _regex_verdade(probe: dict) -> bool:
    """Verdade do probe `regex_match`, computada com `re` (sem `eval`)."""
    return bool(re.search(probe.get("padrao", ""), probe.get("texto", "")))


def _json_verdade(probe: dict) -> bool:
    """Validade do probe `json_valido`, computada com `json` (sem `eval`)."""
    try:
        json.loads(probe.get("json_texto", ""))
    except (TypeError, ValueError):
        return False
    return True


def _avaliar(probe: dict, resposta) -> tuple[bool, object]:
    """Checa a resposta de uma probe. Retorna (ok, esperado_legivel).

    Determinístico: só `re`/`json`/normalização de string. Nunca `eval`/`exec`.
    """
    tipo = probe.get("tipo")
    if tipo == "exato":
        esperado = probe.get("resposta_esperada")
        recebido = _normalizar_exato(resposta)
        # Tolerância por probe: variantes explícitas do campo `aceita` (nunca
        # afrouxar a normalização global).
        alvos = [_normalizar_exato(esperado)]
        alvos += [_normalizar_exato(v) for v in probe.get("aceita", []) or []]
        return bool(recebido) and recebido in alvos, esperado
    if tipo == "contem":
        esperado = probe.get("resposta_esperada")
        alvo = _normalizar(esperado)
        recebido = _normalizar(resposta)
        if not alvo or alvo not in recebido:
            return False, esperado
        # Substrings proibidas: rejeita resposta que, apesar de conter o alvo,
        # também afirma algo que o contradiz (ex.: "funcao_b esta certa").
        for proibido in probe.get("nao_contem", []) or []:
            termo = _normalizar(proibido)
            if termo and termo in recebido:
                return False, esperado
        return True, esperado
    if tipo == "booleano":
        esperado = probe.get("resposta_esperada")
        verdade = _parse_booleano(esperado)
        obtido = _parse_booleano(resposta)
        return obtido is not None and obtido == verdade, esperado
    if tipo == "regex_match":
        verdade = _regex_verdade(probe)
        obtido = _parse_booleano(resposta)
        esperado = "sim" if verdade else "nao"
        return obtido is not None and obtido == verdade, esperado
    if tipo == "regex_alternativas":
        padrao = probe.get("padrao", "")
        casam = [
            a for a in probe.get("alternativas", [])
            if re.search(padrao, str(a))
        ]
        esperado = casam[0] if casam else None
        obtido = _limpar_token(resposta)
        ok = bool(casam) and any(obtido == str(a) for a in casam)
        return ok, esperado
    if tipo == "json_valido":
        verdade = _json_verdade(probe)
        obtido = _parse_booleano(resposta)
        esperado = "sim" if verdade else "nao"
        return obtido is not None and obtido == verdade, esperado
    # Tipo desconhecido: nunca crasha; reporta erro claro.
    return False, probe.get("resposta_esperada")


def verificar(probe_id: str, resposta) -> dict:
    """Checa `resposta` contra a probe `probe_id` (100% determinístico).

    Retorna `{"id", "ok": bool, "esperado", "recebido"}`. Probe inexistente
    devolve `ok=False` e o campo extra `erro` (não crasha). NUNCA usa
    `eval`/`exec` de entrada: só `re`/`json` sobre os dados fixos das probes.
    """
    probe = get_probe(probe_id)
    if probe is None:
        return {
            "id": probe_id,
            "ok": False,
            "esperado": None,
            "recebido": resposta,
            "erro": f"probe desconhecida: {probe_id!r}",
        }
    ok, esperado = _avaliar(probe, resposta)
    return {
        "id": probe["id"],
        "ok": bool(ok),
        "esperado": esperado,
        "recebido": resposta,
    }


def nivel_por_score(score: float) -> str:
    """Nível por limiares de `config.PROBE_LIMIAR_*` (score 0..1).

    `SEM_DADOS` NÃO vem daqui: é reservado a `avaliar_resultados` com lista
    vazia (sem amostra NÃO é o mesmo que degradação)."""
    if score >= config.PROBE_LIMIAR_SAUDAVEL:
        return "SAUDAVEL"
    if score >= config.PROBE_LIMIAR_ATENCAO:
        return "ATENCAO"
    if score >= config.PROBE_LIMIAR_DEGRADADO:
        return "DEGRADADO"
    return "CRITICO"


def avaliar_resultados(resultados: list[dict]) -> dict:
    """Agrega uma lista de saídas de `verificar` na taxa de acerto.

    Retorna `{"score": float 0..1, "n": int, "n_ok": int, "nivel": str}`.
    Lista vazia -> score 0.0 (conservador), n=0, nivel `SEM_DADOS` (NÃO
    `CRITICO`: ausência de amostra não é degradação).
    """
    itens = list(resultados or [])
    n = len(itens)
    if n == 0:
        return {"score": 0.0, "n": 0, "n_ok": 0, "nivel": "SEM_DADOS"}
    n_ok = sum(
        1 for r in itens if isinstance(r, dict) and r.get("ok")
    )
    score = round(n_ok / n, 4)
    return {
        "score": score,
        "n": n,
        "n_ok": n_ok,
        "nivel": nivel_por_score(score),
    }


# ---------------------------------------------------------------------------
# Calibração de baseline (degradação em relação ao modelo de referência)


def _caminho_baseline(caminho=None) -> pathlib.Path:
    return pathlib.Path(caminho) if caminho else config.HEALTH_BASELINE_FILE


def _carregar_baseline(caminho=None) -> dict:
    """Carrega `{modelo: entrada}` do baseline. Ausente/inválido -> {}."""
    p = _caminho_baseline(caminho)
    try:
        if not p.exists():
            return {}
        dados = json.loads(p.read_text(encoding="utf-8"))
    except (OSError, UnicodeDecodeError, json.JSONDecodeError):
        # Arquivo ausente/ilegível/corrompido (inclusive UTF-8 inválido) não
        # derruba o harness: trata como sem baseline.
        return {}
    if not isinstance(dados, dict):
        return {}
    modelos = dados.get("modelos")
    return modelos if isinstance(modelos, dict) else {}


def salvar_baseline(modelo: str, resultados: list[dict],
                    caminho=None) -> dict:
    """Grava/atualiza a baseline de `modelo` em `HEALTH_BASELINE_FILE`.

    Preserva as entradas dos OUTROS modelos (uma entrada por modelo). Retorna a
    entrada salva `{"modelo", "data", "n", "taxa"}`. `resultados` é a lista de
    saídas de `verificar`; a taxa usa `avaliar_resultados`.

    CALIBRAÇÃO: chame com o modelo em ALTO esforço antes de fixar limiares.
    """
    nome = str(modelo)
    agregado = avaliar_resultados(resultados)
    entrada = {
        "modelo": nome,
        "data": date.today().isoformat(),
        "n": agregado["n"],
        "taxa": agregado["score"],
    }
    modelos = _carregar_baseline(caminho)
    modelos[nome] = entrada
    payload = {"schema_version": "1.0", "modelos": modelos}
    p = _caminho_baseline(caminho)
    p.parent.mkdir(parents=True, exist_ok=True)
    # Escrita ATÔMICA: grava num tmp no MESMO diretório e faz replace (os.replace
    # é atômico no mesmo filesystem). Um crash no meio da escrita não corrompe o
    # baseline existente.
    tmp = p.with_name(p.name + ".tmp")
    tmp.write_text(
        json.dumps(payload, ensure_ascii=False, indent=2), encoding="utf-8"
    )
    tmp.replace(p)
    return entrada


def comparar_com_baseline(modelo: str, taxa_atual: float,
                          caminho=None) -> dict:
    """Compara a taxa atual com a baseline de `modelo`.

    Retorna `{"modelo", "taxa_atual", "taxa_baseline", "delta", "degradado"}`.
    `degradado=True` quando `taxa_atual < taxa_baseline -
    PROBE_MARGEM_DEGRADACAO`. Baseline ausente/inválida -> `taxa_baseline=None`,
    `delta=None`, `degradado=False` (tolerante, nunca crasha). `taxa_atual`
    ausente/não numérica -> `delta=None`, `degradado=False` (sem dados).
    """
    nome = str(modelo)
    entrada = _carregar_baseline(caminho).get(nome)
    taxa_baseline = None
    if isinstance(entrada, dict):
        try:
            taxa_baseline = float(entrada.get("taxa"))
        except (TypeError, ValueError):
            taxa_baseline = None

    # `taxa_atual` inválida (None/str não numérica) NÃO crasha: sem dados para
    # comparar -> delta None e NUNCA degradado (não conflacionar com queda).
    try:
        atual = float(taxa_atual)
    except (TypeError, ValueError):
        return {
            "modelo": nome,
            "taxa_atual": taxa_atual,
            "taxa_baseline": taxa_baseline,
            "delta": None,
            "degradado": False,
        }

    if taxa_baseline is None:
        return {
            "modelo": nome,
            "taxa_atual": taxa_atual,
            "taxa_baseline": None,
            "delta": None,
            "degradado": False,
        }

    delta = atual - taxa_baseline
    return {
        "modelo": nome,
        "taxa_atual": taxa_atual,
        "taxa_baseline": taxa_baseline,
        "delta": round(delta, 4),
        # Comparação com tolerância a erro de ponto flutuante (ex.: 0.85 - 1.0
        # = -0.149999...): exatamente na margem NÃO conta como degradado.
        "degradado": round(delta, 9) < -config.PROBE_MARGEM_DEGRADACAO,
    }


# ---------------------------------------------------------------------------
# CLI


def main(argv: list[str] | None = None) -> int:
    """CLI das probes: `list` e `check <id> "<resposta>"`."""
    try:
        sys.stdout.reconfigure(encoding="utf-8", errors="replace")
    except (AttributeError, ValueError):
        pass
    parser = argparse.ArgumentParser(
        prog="python -m harness.probes",
        description="Golden probes (sensor ativo) + calibração de baseline.",
    )
    sub = parser.add_subparsers(dest="cmd")
    sub.add_parser("list", help="imprime as probes/prompts em JSON")
    p_check = sub.add_parser("check", help="checa uma resposta contra uma probe")
    p_check.add_argument("probe_id", help="id da probe (ver `list`)")
    p_check.add_argument("resposta", help="resposta fornecida pelo modelo")
    args = parser.parse_args(argv)

    if args.cmd == "list":
        print(json.dumps(PROBES, ensure_ascii=False, indent=2))
        return 0
    if args.cmd == "check":
        resultado = verificar(args.probe_id, args.resposta)
        print(json.dumps(resultado, ensure_ascii=False, indent=2))
        # Gate: exit 0 quando a resposta passa; 1 quando falha (permite uso em
        # CI/scripts). Probe inexistente -> ok=False -> exit 1.
        return 0 if resultado.get("ok") else 1
    parser.print_help()
    return 1


if __name__ == "__main__":
    sys.exit(main())
