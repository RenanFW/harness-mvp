"""Auto-checagem do harness (M1) — stdlib only.

Descobre e roda todos os testes executáveis:
    tests/*_test.py  e  harness/motor/tests/*_test.py

Cada arquivo tem `if __name__ == "__main__":` e contador próprio
([PASS]/[FAIL]). Executa cada um via subprocess com o cwd do projeto e
agrega: exit code 0 -> PASS, senão FAIL (mostra as últimas linhas de saída).

Resumo final: "X arquivos, Y passaram, Z falharam". Exit code != 0 se
qualquer arquivo falhar.

Uso:
    python -m harness.selfcheck
"""

from __future__ import annotations

import pathlib
import subprocess
import sys

from . import config

_TEST_GLOBS = (
    "tests/*_test.py",
    "harness/motor/tests/*_test.py",
)
_TIMEOUT_SEG = config.SELFCHECK_TIMEOUT_SEG


def _descobrir_testes() -> list[pathlib.Path]:
    """Lista os arquivos de teste (deduplicados, em ordem alfabética)."""
    vistos: set[pathlib.Path] = set()
    arquivos: list[pathlib.Path] = []
    for glob in _TEST_GLOBS:
        for p in sorted(config.ROOT.glob(glob)):
            if p not in vistos:
                vistos.add(p)
                arquivos.append(p)
    return arquivos


def _rodar_um(path: pathlib.Path) -> tuple[bool, str]:
    """Roda um arquivo de teste; retorna (passou, cauda da saída)."""
    try:
        proc = subprocess.run(
            [sys.executable, str(path)],
            cwd=str(config.ROOT),
            capture_output=True,
            text=True,
            encoding="utf-8",
            errors="replace",
            timeout=_TIMEOUT_SEG,
        )
    except subprocess.TimeoutExpired:
        return False, "TIMEOUT (mais de 600s)"
    ok = proc.returncode == 0
    cauda = "\n".join((proc.stdout or "").splitlines()[-8:])
    return ok, cauda


def main(argv: list[str] | None = None) -> int:
    # Console Windows (cp1252): os nomes dos testes têm acentos; usa UTF-8 com
    # substituição (mesma disciplina das outras CLIs).
    try:
        sys.stdout.reconfigure(encoding="utf-8", errors="replace")
    except (AttributeError, ValueError):
        pass
    _ = argv  # sem argumentos nesta versão
    testes = _descobrir_testes()
    if not testes:
        print("[selfcheck] nenhum teste encontrado")
        return 1
    falhas: list[tuple[pathlib.Path, str]] = []
    for path in testes:
        nome = path.relative_to(config.ROOT).as_posix()
        ok, cauda = _rodar_um(path)
        if ok:
            print(f"[PASS] {nome}")
        else:
            print(f"[FAIL] {nome}")
            falhas.append((path, cauda))
    total = len(testes)
    passou = total - len(falhas)
    print(f"\n[selfcheck] {total} arquivos, {passou} passaram, {len(falhas)} falharam")
    for path, cauda in falhas:
        print(f"\n--- saída de {path.relative_to(config.ROOT).as_posix()} (últimas linhas) ---")
        print(cauda)
    return 1 if falhas else 0


if __name__ == "__main__":
    sys.exit(main())