"""Testes das golden probes (Etapa 4) — `harness/probes.py`.

Cobre:
  - SANITY: toda probe tem `resposta_esperada` que o PRÓPRIO checador aceita
    (`verificar(id, resposta_esperada) -> ok`); contagem EXATA de probes e de
    armadilhas (o teste não aceita remoção silenciosa);
  - resposta errada -> não ok (para TODAS as probes);
  - tipos validados por CÓDIGO (regex/json) executam de verdade: a verdade é
    computada por `re`/`json`, não copiada do campo esperado;
  - normalização ESTRITA de `exato` (MÉDIA 1): `[0, 1, 2]`/`[0,1,2]` casam;
    `0-1-2`/`0.1.2` NÃO; `contem` com `nao_contem` rejeita contradição;
  - `avaliar_resultados`: taxa/nível com todos corretos, com alguns errados e
    com lista vazia (nível SEM_DADOS, NÃO CRITICO);
  - `salvar_baseline`/`comparar_com_baseline` em `tempfile` (ausente -> não
    degradado; preserva outros modelos; abaixo da margem -> degradado);
    baseline UTF-8 corrompido não crasha; escrita atômica sem lixo `.tmp`;
    `taxa_atual` inválida (None/str) -> não crasha/não degradado;
  - `verificar` com probe inexistente não crasha (erro claro);
  - limiares de nível por `config.PROBE_LIMIAR_*`;
  - CLI `check`: exit 0 para resposta correta e != 0 para errada/desconhecida.

Sem rede; sem dependências externas. Rode com:
    python tests/probes_test.py
"""

from __future__ import annotations

import contextlib
import io
import json
import pathlib
import sys
import tempfile
import traceback
from unittest import mock

sys.path.insert(0, str(pathlib.Path(__file__).resolve().parent.parent))

from harness import config  # noqa: E402
from harness.probes import (  # noqa: E402
    PROBES,
    _carregar_baseline,
    avaliar_resultados,
    comparar_com_baseline,
    get_probe,
    main as probes_main,
    nivel_por_score,
    salvar_baseline,
    verificar,
)

# Armadilhas REAIS (resposta plausível-e-errada): a taxa de acerto deve ser
# dominada por estas, não por itens triviais. Contagem fixada em `_caso_01`.
_ARMADILHAS = {
    "py_slice_bug_output",
    "regex_match_digitos",
    "regex_alternativa_unica",
    "json_valido_virgula_final",
    "bug_par_impar_funcao",
    "py_mutavel_default",
    "py_sort_retorna_none",
    "py_dict_get_vs_colchete",
    "py_is_vs_igual",
    "py_precedencia_operador",
    "py_range_fim_exclusivo",
}

# Contagem EXATA esperada (falha se probe for adicionada/removida sem ajuste).
_TOTAL_PROBES = 21
_MIN_ARMADILHAS = 11

# Resposta que NÃO casa com nenhuma probe (texto arbitrário).
_RESPOSTA_ERRADA = "###RESPOSTA_INVALIDA###"

_CAMPOS_OBRIGATORIOS = {"id", "descricao", "prompt", "tipo", "formato",
                        "resposta_esperada"}


def _corretos() -> list[dict]:
    """Saída de `verificar` para a resposta de referência de cada probe."""
    return [verificar(p["id"], p["resposta_esperada"]) for p in PROBES]


def _caso_01() -> bool:
    """SANITY: estrutura das probes + a resposta de referência passa no próprio
    checador de TODAS as probes. Ids únicos. Tipos conhecidos. Contagem EXATA
    de probes e de armadilhas fixada (não aceita remoção silenciosa)."""
    tipos_validos = {
        "exato", "contem", "booleano", "regex_match",
        "regex_alternativas", "json_valido",
    }
    ids = [p["id"] for p in PROBES]
    if len(ids) != len(set(ids)):
        print("    FALHOU: ids duplicados")
        return False
    if len(PROBES) != _TOTAL_PROBES:
        print(f"    FALHOU: nº de probes != {_TOTAL_PROBES}: {len(PROBES)}")
        return False
    faltando = _ARMADILHAS - set(ids)
    if faltando:
        print(f"    FALHOU: armadilhas ausentes: {sorted(faltando)}")
        return False
    if len(_ARMADILHAS) < _MIN_ARMADILHAS:
        print(f"    FALHOU: armadilhas < {_MIN_ARMADILHAS}: {len(_ARMADILHAS)}")
        return False
    for p in PROBES:
        if not _CAMPOS_OBRIGATORIOS <= set(p):
            print(f"    FALHOU: campos faltando em {p.get('id')!r}")
            return False
        if p["tipo"] not in tipos_validos:
            print(f"    FALHOU: tipo desconhecido em {p['id']!r}: {p['tipo']}")
            return False
        r = verificar(p["id"], p["resposta_esperada"])
        if not r["ok"]:
            print(f"    FALHOU: sanity de {p['id']!r}: {r}")
            return False
    return True


def _caso_02() -> bool:
    """Resposta errada -> não ok para TODAS as probes (o checador discrimina)."""
    for p in PROBES:
        r = verificar(p["id"], _RESPOSTA_ERRADA)
        if r["ok"]:
            print(f"    FALHOU: resposta errada aceita em {p['id']!r}")
            return False
    return True


def _caso_03() -> bool:
    """Tipos validados por CÓDIGO executam de verdade (`re`/`json`): a verdade é
    computada, não copiada. `regex_match_digitos` é FALSO; `json_valido_...`
    com vírgula final é INVÁLIDO; a alternativa correta da regex é única."""
    codigos = {
        "regex_match_digitos": ("nao", "sim"),
        "regex_match_plus": ("sim", "nao"),
        "json_valido_virgula_final": ("nao", "sim"),
        "json_valido_aninhado": ("sim", "nao"),
        "regex_alternativa_unica": ("abc_123", "abc_12"),
    }
    for pid, (certo, errado) in codigos.items():
        if not verificar(pid, certo)["ok"]:
            print(f"    FALHOU: resposta correta rejeitada em {pid!r}: {certo!r}")
            return False
        if verificar(pid, errado)["ok"]:
            print(f"    FALHOU: resposta errada aceita em {pid!r}: {errado!r}")
            return False
    # Alternativas que NÃO casam com a regex (ancorada) também são rejeitadas.
    for ruim in ("abc_12", "ABC_123", "abc_1234"):
        if verificar("regex_alternativa_unica", ruim)["ok"]:
            print(f"    FALHOU: alternativa inválida aceita: {ruim!r}")
            return False
    return True


def _caso_04() -> bool:
    """`avaliar_resultados`: todos corretos -> 1.0/SAUDAVEL; três errados ->
    (n-3)/n (ATENCAO); lista vazia -> n=0, score 0.0, nivel SEM_DADOS (NÃO
    CRITICO: ausência de amostra não é degradação)."""
    n = len(PROBES)
    corretos = _corretos()
    r_all = avaliar_resultados(corretos)
    parcial = [dict(r) for r in corretos]
    parcial[0]["ok"] = False
    parcial[1]["ok"] = False
    parcial[2]["ok"] = False
    r_parc = avaliar_resultados(parcial)
    r_vazio = avaliar_resultados([])
    return (
        r_all["n"] == n
        and r_all["n_ok"] == n
        and r_all["score"] == 1.0
        and r_all["nivel"] == "SAUDAVEL"
        and r_parc["n"] == n
        and r_parc["n_ok"] == n - 3
        and r_parc["score"] == round((n - 3) / n, 4)
        and r_parc["nivel"] == "ATENCAO"
        and r_vazio == {"score": 0.0, "n": 0, "n_ok": 0, "nivel": "SEM_DADOS"}
        and nivel_por_score(1.0) != "SEM_DADOS"
    )


def _caso_05() -> bool:
    """Baseline em `tempfile` (via `config.HEALTH_BASELINE_FILE`): ausente ->
    não degradado/taxa None; grava e PRESERVA outros modelos; abaixo da margem
    -> degradado; exatamente na margem NÃO é degradado (comparação estrita)."""
    with tempfile.TemporaryDirectory() as tmp:
        caminho = pathlib.Path(tmp) / "health_baseline.json"
        with mock.patch.object(config, "HEALTH_BASELINE_FILE", caminho):
            ausente = comparar_com_baseline("m1", 0.9)
            entrada = salvar_baseline("m1", _corretos())
            salvar_baseline("m2", _corretos()[:5])
            modelos = json.loads(
                caminho.read_text(encoding="utf-8")
            )["modelos"]
            igual = comparar_com_baseline("m1", 1.0)
            degradado = comparar_com_baseline("m1", 0.80)  # delta -0.20
            no_limite = comparar_com_baseline("m1", 0.85)  # delta -0.15 (não <)
            return (
                ausente == {
                    "modelo": "m1",
                    "taxa_atual": 0.9,
                    "taxa_baseline": None,
                    "delta": None,
                    "degradado": False,
                }
                and entrada["modelo"] == "m1"
                and entrada["n"] == len(PROBES)
                and entrada["taxa"] == 1.0
                and set(modelos) == {"m1", "m2"}
                and modelos["m2"]["n"] == 5
                and igual["degradado"] is False
                and igual["delta"] == 0.0
                and degradado["degradado"] is True
                and degradado["taxa_baseline"] == 1.0
                and degradado["delta"] == -0.2
                and no_limite["degradado"] is False
            )


def _caso_06() -> bool:
    """`verificar` com probe inexistente NÃO crasha: ok=False e erro claro; a
    `resposta` recebida é ecoada. `get_probe` devolve dict/None coerente."""
    r = verificar("nao_existe", "x")
    return (
        isinstance(r, dict)
        and r["ok"] is False
        and r["id"] == "nao_existe"
        and r["recebido"] == "x"
        and "erro" in r
        and "probe desconhecida" in r["erro"]
        and get_probe("nao_existe") is None
        and get_probe(PROBES[0]["id"]) is PROBES[0]
    )


def _caso_07() -> bool:
    """Limiares de nível (`config.PROBE_LIMIAR_*`): 0.90 SAUDAVEL; 0.70 e 0.89
    ATENCAO; 0.50 e 0.69 DEGRADADO; 0.49 CRITICO."""
    return (
        nivel_por_score(1.0) == "SAUDAVEL"
        and nivel_por_score(config.PROBE_LIMIAR_SAUDAVEL) == "SAUDAVEL"
        and nivel_por_score(0.89) == "ATENCAO"
        and nivel_por_score(config.PROBE_LIMIAR_ATENCAO) == "ATENCAO"
        and nivel_por_score(0.69) == "DEGRADADO"
        and nivel_por_score(config.PROBE_LIMIAR_DEGRADADO) == "DEGRADADO"
        and nivel_por_score(0.49) == "CRITICO"
    )


def _caso_08() -> bool:
    """MÉDIA 1: normalização ESTRITA de `exato`. Para `py_lista_range`, as
    variantes de espaço (`[0, 1, 2]`/`[0,1,2]`) passam; pontuação significativa
    errada (`0-1-2`, `0.1.2`, `0 1 2`) NÃO. `aceita` declara variantes por
    probe sem afrouxar o global."""
    pid = "py_lista_range"
    if not verificar(pid, "[0, 1, 2]")["ok"]:
        print("    FALHOU: `[0, 1, 2]` deveria passar")
        return False
    if not verificar(pid, "[0,1,2]")["ok"]:
        print("    FALHOU: `[0,1,2]` deveria passar")
        return False
    for ruim in ("0-1-2", "0.1.2", "0 1 2", "(0, 1, 2)", "[0;1;2]"):
        if verificar(pid, ruim)["ok"]:
            print(f"    FALHOU: {ruim!r} aceito indevidamente")
            return False
    # `aceita` (variante explícita por probe) é honrado quando declarado.
    r = verificar("logica_primos_ate_30", "10")
    if not r["ok"]:
        print("    FALHOU: sanity de logica_primos_ate_30")
        return False
    return True


def _caso_09() -> bool:
    """BAIXA 6: `contem` com `nao_contem`. `bug_par_impar_funcao` aceita o nome
    correto (`funcao_b`), mas REJEITA frase que contradiz o enunciado (afirmar
    que a função errada está certa)."""
    pid = "bug_par_impar_funcao"
    if not verificar(pid, "funcao_b")["ok"]:
        print("    FALHOU: `funcao_b` deveria passar")
        return False
    if not verificar(pid, "A funcao_b e a incorreta")["ok"]:
        print("    FALHOU: negar a função errada deveria passar")
        return False
    for ruim in ("funcao_b esta certa", "funcao_b esta correta",
                 "funcao_b e correta", "funcao_a"):
        if verificar(pid, ruim)["ok"]:
            print(f"    FALHOU: {ruim!r} aceito indevidamente")
            return False
    return True


def _caso_10() -> bool:
    """BAIXAS 1 e 2: baseline UTF-8 corrompido não crasha (`_carregar_baseline`
    -> {}; comparação -> baseline None) e `salvar_baseline` é ATÔMICO (não deixa
    lixo `.tmp` e recupera o arquivo corrompido)."""
    with tempfile.TemporaryDirectory() as tmp:
        caminho = pathlib.Path(tmp) / "health_baseline.json"
        with mock.patch.object(config, "HEALTH_BASELINE_FILE", caminho):
            # UTF-8 inválido: deve ser tratado como ausente, sem crash.
            caminho.write_bytes(b"\xff\xfe\x00\x80 nao-e-utf8 \xff")
            if _carregar_baseline() != {}:
                print("    FALHOU: baseline corrompido não virou {}")
                return False
            comp = comparar_com_baseline("m1", 0.9)
            if comp["taxa_baseline"] is not None or comp["degradado"]:
                print(f"    FALHOU: comparação com corrompido = {comp}")
                return False
            # salvar por cima do corrompido recupera o arquivo...
            salvar_baseline("m1", _corretos())
            lixo = caminho.with_name(caminho.name + ".tmp")
            if lixo.exists():
                print("    FALHOU: sobrou arquivo temporário `.tmp`")
                return False
            dados = json.loads(caminho.read_text(encoding="utf-8"))
            if dados["modelos"]["m1"]["n"] != len(PROBES):
                print("    FALHOU: baseline recuperado incorreto")
                return False
            return True


def _caso_11() -> bool:
    """BAIXA 3: `comparar_com_baseline` com `taxa_atual` inválida (None/str) não
    crasha; `delta=None` e `degradado=False` (sem dados != degradação), mesmo
    com baseline existente."""
    with tempfile.TemporaryDirectory() as tmp:
        caminho = pathlib.Path(tmp) / "health_baseline.json"
        with mock.patch.object(config, "HEALTH_BASELINE_FILE", caminho):
            salvar_baseline("m1", _corretos())  # taxa_baseline = 1.0
            for invalida in (None, "abc", "", object()):
                r = comparar_com_baseline("m1", invalida)
                if (r["degradado"] is not False or r["delta"] is not None
                        or r["taxa_baseline"] != 1.0):
                    print(f"    FALHOU: taxa_atual={invalida!r} -> {r}")
                    return False
            return True


def _caso_12() -> bool:
    """BAIXA 5: CLI `check` serve de gate — exit 0 para resposta correta e
    != 0 para errada/desconhecida. `list` retorna 0 e o nº de probes."""
    def _run(argv):
        buf = io.StringIO()
        with contextlib.redirect_stdout(buf):
            code = probes_main(argv)
        return code, buf.getvalue()

    c_ok, out_ok = _run(["check", "py_lista_range", "[0, 1, 2]"])
    c_bad, _ = _run(["check", "py_lista_range", "0-1-2"])
    c_unknown, _ = _run(["check", "nao_existe", "x"])
    c_list, out_list = _run(["list"])
    try:
        n_list = len(json.loads(out_list))
    except (TypeError, ValueError):
        n_list = -1
    return (
        c_ok == 0
        and c_bad != 0
        and c_unknown != 0
        and c_list == 0
        and n_list == len(PROBES)
        and '"ok": true' in out_ok.lower()
    )


CASES = [
    ("sanity: resposta de referência passa em todas as probes (01)", _caso_01),
    ("resposta errada rejeitada em todas as probes (02)", _caso_02),
    ("tipos por código: regex/json computados de verdade (03)", _caso_03),
    ("avaliar_resultados: taxa/nível (todos, parcial, vazio) (04)", _caso_04),
    ("baseline: ausente/preserva/margem (05)", _caso_05),
    ("verificar: probe inexistente não crasha (06)", _caso_06),
    ("limiares de nível por config.PROBE_LIMIAR_* (07)", _caso_07),
    ("MÉDIA 1: normalização estrita de `exato` (08)", _caso_08),
    ("BAIXA 6: `contem` rejeita via `nao_contem` (09)", _caso_09),
    ("BAIXAS 1/2: baseline corrompido + escrita atômica (10)", _caso_10),
    ("BAIXA 3: `taxa_atual` inválida não crasha (11)", _caso_11),
    ("BAIXA 5: CLI check é gate (exit codes) (12)", _caso_12),
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
