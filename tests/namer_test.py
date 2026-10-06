"""Testes do nomeador determinístico de projetos (zero dependências).
Rode com:
    python tests/namer_test.py
"""

from __future__ import annotations

import pathlib
import subprocess
import sys
import tempfile
import traceback

sys.path.insert(0, str(pathlib.Path(__file__).resolve().parent.parent))

from harness import config  # noqa: E402
from harness.namer import (  # noqa: E402
    MAX_NOME,
    criar_projeto,
    nome_projeto,
    slugify,
)


def _caso_1() -> bool:
    """slugify: "Criação de API" -> "criacao-de-api"; remove acentos."""
    return (
        slugify("Criação de API") == "criacao-de-api"
        and slugify("Água boa") == "agua-boa"
        and slugify("café") == "cafe"
    )


def _caso_2() -> bool:
    """slugify: colapsa separadores e remove "-" nas pontas."""
    return (
        slugify("Olá   Mundo!!") == "ola-mundo"
        and slugify("a--b__c") == "a-b-c"
        and slugify("--x--") == "x"
    )


def _caso_3() -> bool:
    """slugify: recusa nomes reservados do Windows (CON, LPT1, ...)."""
    for nome in ("CON", "con", "PrN", "LPT1", "com3", "AUX", "NUL"):
        try:
            slugify(nome)
        except ValueError:
            continue
        return False
    return True


def _caso_4() -> bool:
    """slugify: recusa nomes terminados em '.' ou espaço e slug vazio."""
    for nome in ("projeto.", "projeto ", "  ", "..."):
        try:
            slugify(nome)
        except ValueError:
            continue
        return False
    return True


def _caso_5() -> bool:
    """nome_projeto: gera nome curto e limpo a partir do contexto."""
    return (
        nome_projeto("Criação de API REST para pedidos", existentes=[])
        == "criacao-api-rest-pedidos"
        and nome_projeto("Criação de API", existentes=[]) == "criacao-api"
    )


def _caso_6() -> bool:
    """nome_projeto: unicidade case-insensitive com sufixo -2, -3..."""
    return (
        nome_projeto("Criação de API", existentes=["criacao-api"])
        == "criacao-api-2"
        and nome_projeto("Criação de API", existentes=["CRIACAO-API"])
        == "criacao-api-2"
        and nome_projeto(
            "Criação de API",
            existentes=["criacao-api", "criacao-api-2"],
        ) == "criacao-api-3"
    )


def _caso_7() -> bool:
    """nome_projeto: contexto só de stopwords (ou vazio) vira "projeto"."""
    return (
        nome_projeto("de e para com o a um", existentes=[])
        == "projeto"
        and nome_projeto("", existentes=[]) == "projeto"
    )


def _caso_8() -> bool:
    """nome_projeto: respeita MAX_NOME (nunca estoura e não corta palavra)."""
    contexto = (
        "plataforma de gestão de estoque e inventário para pequenas empresas "
        "de varejo online com relatórios avançados"
    )
    nome = nome_projeto(contexto, existentes=[])
    return 0 < len(nome) <= MAX_NOME and not nome.endswith("-")


def _caso_9() -> bool:
    """criar_projeto: cria pasta em base temporária e retorna Path existente."""
    with tempfile.TemporaryDirectory() as tmp:
        base = pathlib.Path(tmp)
        destino = criar_projeto("Criação de API REST para pedidos", base)
        return (
            destino.is_dir()
            and destino.parent == base
            and destino.name == "criacao-api-rest-pedidos"
        )


def _caso_10() -> bool:
    """criar_projeto: colisão com pasta NÃO vazia gera sufixo (-2)."""
    with tempfile.TemporaryDirectory() as tmp:
        base = pathlib.Path(tmp)
        (base / "criacao-api").mkdir()
        (base / "criacao-api" / "arquivo.txt").write_text("x", encoding="utf-8")
        destino = criar_projeto("Criação de API", base)
        return destino.name == "criacao-api-2" and destino.is_dir()


def _caso_11() -> bool:
    """criar_projeto: NÃO cria pasta fora da base (base temporária)."""
    with tempfile.TemporaryDirectory() as tmp:
        base = pathlib.Path(tmp)
        destino = criar_projeto("Ferramenta de backup em Python", base)
        return (
            base in destino.parents
            and destino.name == "ferramenta-backup-python"
            and destino.is_dir()
        )


def _caso_12() -> bool:
    """CLI --dry-run: imprime o nome sem criar pasta."""
    with tempfile.TemporaryDirectory() as tmp:
        out = subprocess.run(
            [sys.executable, "-m", "harness.namer",
             "Criação de API REST", "--dry-run", "--base", tmp],
            capture_output=True, text=True, encoding="utf-8",
            cwd=str(config.ROOT), timeout=30,
        )
        nome = out.stdout.strip()
        return (
            out.returncode == 0
            and nome == "criacao-api-rest"
            and not list(pathlib.Path(tmp).iterdir())
        )


def _caso_13() -> bool:
    """CLI sem flag: cria a pasta de fato na base informada."""
    with tempfile.TemporaryDirectory() as tmp:
        out = subprocess.run(
            [sys.executable, "-m", "harness.namer",
             "Criação de API REST", "--base", tmp],
            capture_output=True, text=True, encoding="utf-8",
            cwd=str(config.ROOT), timeout=30,
        )
        caminho = pathlib.Path(out.stdout.strip())
        return out.returncode == 0 and caminho.is_dir() and caminho.name == "criacao-api-rest"


def _caso_14() -> bool:
    """Integração: criar_projeto() cria de fato a subpasta em base temporária
    (nome determinístico + pasta existente). Antes, este caso só re-derivava o
    literal de config (tautológico)."""
    with tempfile.TemporaryDirectory() as tmp:
        base = pathlib.Path(tmp) / "sidePrjs"
        destino = criar_projeto("API de pagamento", base=base)
        if not destino.is_dir() or destino.parent != base:
            return False
        # colisão com pasta NÃO vazia -> sufixo numérico (nada sobrescrito)
        (destino / "arquivo.txt").write_text("x", encoding="utf-8")
        destino2 = criar_projeto("API de pagamento", base=base)
        return destino2.is_dir() and destino2 != destino


def _caso_15() -> bool:
    """nome_projeto sem `existentes`: consulta sidePrjs/ real e não quebra."""
    nome = nome_projeto("Criação de API")
    return isinstance(nome, str) and 0 < len(nome) <= MAX_NOME


def _caso_16() -> bool:
    """nome_projeto: palavra única > MAX_NOME é truncada (limite duro)."""
    palavra = "pneumoultramicroscopicossilicovulcanoconiotico"  # 46 letras
    nome = nome_projeto(palavra, existentes=[])
    return (
        len(palavra) > MAX_NOME  # pré-condição: palavra realmente estoura
        and len(nome) == MAX_NOME
        and nome == palavra[:MAX_NOME]
    )


def _caso_17() -> bool:
    """nome_projeto: colisão com base em MAX_NOME gera sufixo sem estourar."""
    palavra = "pneumoultramicroscopicossilicovulcanoconiotico"  # 46 letras
    base_ocupado = palavra[:MAX_NOME]  # 40 chars já usados
    nome = nome_projeto(palavra, existentes=[base_ocupado])
    return (
        len(base_ocupado) == MAX_NOME  # pré-condição: base no limite
        and len(nome) <= MAX_NOME
        and nome.endswith("-2")
        and len(nome) == MAX_NOME  # base truncada (38) + "-2" (2) = 40
    )


def _caso_18() -> bool:
    """slugify: reservado com acento ("CÓN") também é recusado."""
    try:
        slugify("CÓN")
    except ValueError:
        return True
    return False


CASES = [
    ("slugify basico (acentos removidos)", _caso_1),
    ("slugify colapsa separadores", _caso_2),
    ("slugify recusa reservados do Windows", _caso_3),
    ("slugify recusa '.'/' ' final e vazio", _caso_4),
    ("nome_projeto gera nome limpo", _caso_5),
    ("nome_projeto unicidade case-insensitive (-2, -3)", _caso_6),
    ("nome_projeto fallback 'projeto' (stopwords/vazio)", _caso_7),
    ("nome_projeto respeita MAX_NOME", _caso_8),
    ("criar_projeto cria pasta na base (tempfile)", _caso_9),
    ("criar_projeto colisao gera sufixo", _caso_10),
    ("criar_projeto nao cria fora da base", _caso_11),
    ("CLI --dry-run imprime nome sem criar", _caso_12),
    ("CLI sem flag cria pasta", _caso_13),
    ("Integration: SIDE_PRJS_DIR = ROOT/sidePrjs", _caso_14),
    ("nome_projeto sem existentes consulta sidePrjs/", _caso_15),
    ("nome_projeto palavra unica > MAX_NOME truncada", _caso_16),
    ("nome_projeto colisao com base em MAX_NOME nao estoura", _caso_17),
    ("slugify recusa reservado com acento (CON)", _caso_18),
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