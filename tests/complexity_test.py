"""Testes do scorer de complexidade (Update Final, Fase 1).

Cobre `harness/complexity.py` — `calcular_grau_complexidade`:
  (a) prompt simples de consulta -> baixo (score 0-3);
  (b) prompt de implementação moderada -> médio (score 4-8);
  (c) prompt de sistema completo + infra + produção -> alto (score 9-10);
  (d) cap em 10 (score > 10 -> 10);
  (e) bônus do nível de risco (baixo=+1, medio=+2, alto=+3) soma ao score;
  (f) override explícito pula o scorer (verificado via pipeline/_monta_contrato).

Rode com:
    python tests/complexity_test.py
"""

from __future__ import annotations

import contextlib
import io
import json
import pathlib
import subprocess
import sys

sys.path.insert(0, str(pathlib.Path(__file__).resolve().parent.parent))

from harness.complexity import (  # noqa: E402
    SCORE_MAX,
    calcular_grau_complexidade,
    main,
)


def _caso_a() -> bool:
    """(a) prompt simples de CONSULTA -> baixo (score 0-3)."""
    res = calcular_grau_complexidade(
        "consulta a lista de registros do banco e exibe o resultado "
        "sem alterar nada"
    )
    if res["grau_complexidade"] != "baixo":
        print(f"    FALHOU: consulta -> {res['grau_complexidade']} "
              f"(score {res['score']})")
        return False
    if not (0 <= res["score"] <= 3):
        print(f"    FALHOU: score da consulta {res['score']} fora de 0-3")
        return False
    # texto vazio -> baixo, score 0
    vazio = calcular_grau_complexidade("")
    if vazio["score"] != 0 or vazio["grau_complexidade"] != "baixo":
        print("    FALHOU: texto vazio")
        return False
    return True


def _caso_b() -> bool:
    """(b) prompt de implementação MODERADA -> médio (score 4-8)."""
    res = calcular_grau_complexidade(
        "criar um modulo de api que consulta o banco e implementa uma rota "
        "de integracao com testes"
    )
    if res["grau_complexidade"] != "medio":
        print(f"    FALHOU: moderada -> {res['grau_complexidade']} "
              f"(score {res['score']})")
        return False
    if not (4 <= res["score"] <= 8):
        print(f"    FALHOU: score da moderada {res['score']} fora de 4-8")
        return False
    return True


def _caso_c() -> bool:
    """(c) prompt de sistema COMPLETO + infra + produção -> alto (9-10)."""
    res = calcular_grau_complexidade(
        "implementar um sistema completo em producao com docker, container, "
        "rede, servidor, proxy, kubernetes, cloud, migracao de esquema, "
        "seguranca, auth, dados reais, breaking change, deploy, microservicos, "
        "orquestracao, enterprise, critico, testes e build"
    )
    if res["grau_complexidade"] != "alto":
        print(f"    FALHOU: sistema completo -> {res['grau_complexidade']} "
              f"(score {res['score']})")
        return False
    if not (9 <= res["score"] <= SCORE_MAX):
        print(f"    FALHOU: score do sistema {res['score']} fora de 9-10")
        return False
    return True


def _caso_d() -> bool:
    """(d) CAP em 10: score bruto > 10 -> score final 10."""
    texto = "implementar " + " ".join(
        [
            "arquivo modulo api db banco integracao producao deploy migracao "
            "esquema permissao seguranca auth docker container rede servidor "
            "proxy kubernetes cloud orquestracao pipeline grande complexo "
            "enterprise critico testes build cobertura"
        ]
        * 5
    )
    res = calcular_grau_complexidade(texto)
    if res["score"] != SCORE_MAX:
        print(f"    FALHOU: cap -> score {res['score']} (esperado {SCORE_MAX})")
        return False
    if res["grau_complexidade"] != "alto":
        print("    FALHOU: cap -> grau não-alto")
        return False
    return True


def _caso_e() -> bool:
    """(e) Bônus do nível de risco soma ao score (baixo=+1, medio=+2, alto=+3)
    e marca nivel_risco_contribuido."""
    # Mesmo texto: a base (sem risco) e com cada nível difere exatamente pelo
    # bônus. Base de "implementar api" (sem bônus) = 3.
    base = calcular_grau_complexidade("implementar api")
    if base["nivel_risco_contribuido"] is not False:
        print("    FALHOU: base não deve ter bônus de risco")
        return False
    for nivel, bonus in (("baixo", 1), ("medio", 2), ("alto", 3)):
        res = calcular_grau_complexidade("implementar api", nivel)
        if res["score"] != base["score"] + bonus:
            print(f"    FALHOU: bônus {nivel} -> score {res['score']}, "
                  f"esperado {base['score'] + bonus}")
            return False
        if res["nivel_risco_contribuido"] is not True:
            print(f"    FALHOU: {nivel} deve marcar nivel_risco_contribuido")
            return False
    # Nível desconhecido/vazio -> sem bônus (mesmo do base)
    for invalido in ("", "critico", "INVALIDO"):
        res = calcular_grau_complexidade("implementar api", invalido)
        if res["score"] != base["score"]:
            print(f"    FALHOU: nível {invalido!r} deve ter score {base['score']}")
            return False
    return True


def _caso_f() -> bool:
    """(f) OVERRIDE e INPUT de risco — semântica corrigida (Achados 1 e 2),
    verificado via `_monta_contrato` do pipeline:
      - `grau_complexidade` explícito -> OVERRIDE: pula o scorer, usa o valor;
      - `nivel_de_risco` explícito e != default -> INPUT: CHAMA o scorer com
        `nivel_risco` (aplica bônus ao score), NÃO pula;
      - `nivel_de_risco` None/vazio/"medio" (default) -> deriva SEM bônus.
    """
    from harness.pipeline import AgentPipeline

    pipe = AgentPipeline(gravar_registro=False)

    # (d) OVERRIDE de grau_complexidade: ignora o texto (que, sozinho, daria
    # baixo) e usa "alto".
    contrato_alto = pipe._monta_contrato({
        "objetivo": "consulta simples",
        "escopo": "tests/",
        "criterios_de_aceite": ["roda `python --version`"],
        "grau_complexidade": "alto",
    })
    if contrato_alto.grau_complexidade != "alto":
        print("    FALHOU: override grau_complexidade=alto não respeitado")
        return False
    if contrato_alto.usa_sandbox is not True:
        print("    FALHOU: usa_sandbox deve ser True para alto")
        return False

    # (a) nivel_de_risco explícito != default (alto) -> INPUT: scorer aplica o
    # bônus +3. O texto base "consulta simples" (sem bônus) daria baixo (score
    # 0-3); com o bônus +3 do alto o score sobe o suficiente para virar médio
    # (4-8) — prova que o scorer (com bônus) foi usado, não um override direto
    # para "alto".
    # "consulta simples ao banco" sem bônus -> baixo; com alto (+3) o score
    # 1+3=4 -> médio. Usamos esse texto mais rico para distinguir o bônus.
    contrato_sem_risco = pipe._monta_contrato({
        "objetivo": "consulta simples ao banco",
        "escopo": "tests/",
        "criterios_de_aceite": ["roda `python --version`"],
    })
    contrato_com_risco = pipe._monta_contrato({
        "objetivo": "consulta simples ao banco",
        "escopo": "tests/",
        "criterios_de_aceite": ["roda `python --version`"],
        "nivel_de_risco": "alto",
    })
    # Sem sinal explícito -> deriva do texto SEM bônus (baixo: score 1).
    if contrato_sem_risco.grau_complexidade != "baixo":
        print(f"    FALHOU: sem risco -> {contrato_sem_risco.grau_complexidade}")
        return False
    # Com risco alto (input) -> scorer aplica bônus +3 (score 1+3=4 -> médio).
    if contrato_com_risco.grau_complexidade != "medio":
        print(f"    FALHOU: risco alto input -> {contrato_com_risco.grau_complexidade}")
        return False
    if contrato_com_risco.grau_complexidade == contrato_sem_risco.grau_complexidade:
        print("    FALHOU: bônus de risco não aplicado (grau igual ao sem bônus)")
        return False

    # (b) nivel_de_risco="medio" (default) -> tratado como NÃO-informado:
    # deriva do texto SEM bônus (igual à ausência de sinal).
    contrato_default = pipe._monta_contrato({
        "objetivo": "consulta simples ao banco",
        "escopo": "tests/",
        "criterios_de_aceite": ["roda `python --version`"],
        "nivel_de_risco": "medio",
    })
    if contrato_default.grau_complexidade != contrato_sem_risco.grau_complexidade:
        print(f"    FALHOU: medio (default) deve derivar como não-informado -> "
              f"{contrato_default.grau_complexidade}")
        return False

    # (c) Sem nivel_de_risco no task -> deriva sem bônus.
    if contrato_sem_risco.usa_sandbox is not False:
        print("    FALHOU: usa_sandbox deve ser False p/ baixo")
        return False
    return True


def _caso_g() -> bool:
    """(g) Lote 1 (CLI): `main` com texto simples imprime JSON com
    `grau_complexidade` válido e retorna 0."""
    buf = io.StringIO()
    with contextlib.redirect_stdout(buf):
        rc = main(["criar api de relatorios em src"])
    if rc != 0:
        print(f"    FALHOU: main retornou {rc}")
        return False
    try:
        dados = json.loads(buf.getvalue())
    except json.JSONDecodeError:
        print(f"    FALHOU: saída não é JSON: {buf.getvalue()!r}")
        return False
    if dados.get("grau_complexidade") not in ("baixo", "medio", "alto"):
        print(f"    FALHOU: grau_complexidade inválido: {dados.get('grau_complexidade')}")
        return False
    if not isinstance(dados.get("score"), int):
        print(f"    FALHOU: score ausente/não-int: {dados.get('score')}")
        return False
    return True


def _caso_h() -> bool:
    """(h) Lote 1 (CLI): `--risco alto` aplica o bônus (score maior que o
    mesmo texto sem risco) e marca nivel_risco_contribuido."""
    def _roda(args: list[str]) -> int:
        buf = io.StringIO()
        with contextlib.redirect_stdout(buf):
            rc = main(args)
        return rc, buf.getvalue()

    rc1, saida1 = _roda(["implementar api"])
    if rc1 != 0:
        print(f"    FALHOU: main sem risco retornou {rc1}")
        return False
    rc2, saida2 = _roda(["implementar api", "--risco", "alto"])
    if rc2 != 0:
        print(f"    FALHOU: main com --risco retornou {rc2}")
        return False
    sem_risco = json.loads(saida1)
    com_risco = json.loads(saida2)
    if not (com_risco["score"] > sem_risco["score"]):
        print(f"    FALHOU: score com risco {com_risco['score']} não é maior "
              f"que sem risco {sem_risco['score']}")
        return False
    if com_risco.get("nivel_risco_contribuido") is not True:
        print("    FALHOU: nivel_risco_contribuido deve ser True com --risco")
        return False
    return True


def _caso_i() -> bool:
    """(i) Lote 1 (CLI via subprocess): `python -m harness.complexity
    "criar api de relatorios em src"` retorna JSON com `grau_complexidade`
    (valida o quoting do CLI no Windows)."""
    proc = subprocess.run(
        [sys.executable, "-m", "harness.complexity", "criar api de relatorios em src"],
        capture_output=True, text=True, encoding="utf-8", timeout=60,
    )
    if proc.returncode != 0:
        print(f"    FALHOU: returncode {proc.returncode}: {proc.stderr!r}")
        return False
    try:
        dados = json.loads(proc.stdout)
    except json.JSONDecodeError:
        print(f"    FALHOU: saída não é JSON: {proc.stdout!r}")
        return False
    if dados.get("grau_complexidade") not in ("baixo", "medio", "alto"):
        print(f"    FALHOU: campo grau_complexidade ausente/inválido: "
              f"{dados.get('grau_complexidade')}")
        return False
    return True


CASES = [
    ("(a) consulta simples -> baixo (0-3)", _caso_a),
    ("(b) implementação moderada -> médio (4-8)", _caso_b),
    ("(c) sistema completo + infra + produção -> alto (9-10)", _caso_c),
    ("(d) cap em 10", _caso_d),
    ("(e) bônus do nível de risco soma ao score", _caso_e),
    ("(f) override (grau) vs. input de risco (nivel_de_risco != default)", _caso_f),
    ("(g) Lote 1: CLI main texto simples -> JSON + grau válido + rc 0", _caso_g),
    ("(h) Lote 1: CLI --risco alto aplica bônus (score maior)", _caso_h),
    ("(i) Lote 1: CLI via subprocess (quoting Windows) -> JSON", _caso_i),
]


def main_test() -> int:
    passed = 0
    failed = 0
    for name, test in CASES:
        try:
            ok = test()
        except Exception as exc:  # noqa: BLE001
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
