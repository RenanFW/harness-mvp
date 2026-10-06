"""Extração de texto de livros, documentos e arquivos.

Usado pelo agente documenter (via web shell/executor) para transformar
PDFs e arquivos em texto que o modelo consegue interpretar.

Uso:
    python -m harness.extractor <caminho> [--pages 1-50]
"""

from __future__ import annotations

import argparse
import pathlib
import sys

# Arquivos que nunca devem ser extraídos (segredos, credenciais)
SECRET_NAMES = {
    ".env", ".env.local", ".env.production",
    "id_rsa", "id_ed25519",
    "credentials", ".npmrc", ".netrc",
}
SECRET_SUFFIXES = {".pem", ".key", ".p12", ".pfx", ".ovpn"}


def _is_secret(path: pathlib.Path) -> bool:
    name = path.name.lower()
    if name in SECRET_NAMES or name in {f".env.{e}" for e in ("local", "production", "dev", "prod")}:
        return True
    if path.suffix.lower() in SECRET_SUFFIXES:
        return True
    return any(s in name for s in ("secret", "token", "credential"))


def extract(path: str | pathlib.Path, pages: str | None = None) -> str:
    """Extrai texto legível de um arquivo. PDF usa pypdf (se instalado)."""
    path = pathlib.Path(path)
    if _is_secret(path):
        raise PermissionError(f"arquivo sensível bloqueado: {path.name}")
    suffix = path.suffix.lower()

    if suffix == ".pdf":
        return _extract_pdf(path, pages)
    if suffix in {".txt", ".md", ".py", ".json", ".yaml", ".yml", ".csv",
                  ".html", ".htm", ".js", ".ts", ".java", ".c", ".cpp",
                  ".go", ".rs", ".sh", ".toml", ".ini", ".cfg"}:
        return path.read_text(encoding="utf-8", errors="replace")
    # binário ou extensão desconhecida: tenta ler como texto
    try:
        return path.read_text(encoding="utf-8", errors="replace")
    except OSError:
        raise ValueError(f"não foi possível ler {path}")


def _extract_pdf(path: pathlib.Path, pages: str | None = None) -> str:
    try:
        from pypdf import PdfReader
    except ImportError:
        raise RuntimeError(
            "pypdf não instalado. Rode: pip install pypdf"
        )

    reader = PdfReader(str(path))
    total = len(reader.pages)
    start, end = _parse_pages(pages, total)
    chunks = []
    for i in range(start - 1, end):
        text = reader.pages[i].extract_text() or ""
        chunks.append(f"--- página {i + 1}/{total} ---\n{text}")
    return "\n\n".join(chunks)


def _parse_pages(spec: str | None, total: int) -> tuple[int, int]:
    if not spec:
        return 1, total
    try:
        if "-" in spec:
            a, b = spec.split("-", 1)
            return max(1, int(a)), min(total, int(b))
        return max(1, int(spec)), max(1, int(spec))
    except ValueError:
        return 1, total


def main(argv: list[str] | None = None) -> int:
    # Console Windows (cp1252) não imprime todos os caracteres UTF-8 do texto
    # extraído; usa UTF-8 com substituição (mesma disciplina das outras CLIs).
    try:
        sys.stdout.reconfigure(encoding="utf-8", errors="replace")
    except (AttributeError, ValueError):
        pass
    parser = argparse.ArgumentParser(prog="harness.extractor")
    parser.add_argument("path", help="caminho do arquivo (PDF, texto, código)")
    parser.add_argument("--pages", help="faixa de páginas para PDF, ex.: 1-50")
    args = parser.parse_args(argv)

    try:
        print(extract(args.path, args.pages))
    except (OSError, ValueError, RuntimeError, PermissionError) as exc:
        print(f"erro: {exc}", file=sys.stderr)
        return 1
    return 0


if __name__ == "__main__":
    sys.exit(main())