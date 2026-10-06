"""Parser de frontmatter compartilhado do harness.

Unifica os antigos parsers duplicados e divergentes de `agents.py`,
`memory.py`, `rag_refs.py` e `webscraper.py` em UM módulo com comportamento
consistente e tolerante:

- `split_frontmatter`: separa o bloco `--- ... ---` do corpo, tolerante a
  CRLF (Windows) e a ausência do fechamento `---`;
- `parse_frontmatter`: parseia o bloco em um dict plano `chave -> valor`,
  com valores que contêm `:`, aspas removidas e linhas de continuação
  (indentadas / itens de lista em bloco) concatenadas ao valor anterior;
- `parse`: combinação das duas (conveniência).

Nenhum módulo precisa mais reimplementar a regex/detecção — se um bug de
parsing aparecer, corrige-se em um único lugar.
"""

from __future__ import annotations

import re

# Bloco entre `---` no início do documento. Tolerante a espaços após `---` e a
# CRLF (`\r\n`). EXIGE o fechamento `---`: um arquivo que abre `---` e não
# fecha é tratado como SEM frontmatter (meta vazio + corpo completo) — antes,
# a leniência `|\Z` fazia o corpo INTEIRO virar meta e o `body` ficava vazio
# (regressão silenciosa em arquivos malformados).
_FRONTMATTER_RE = re.compile(
    r"^---[ \t]*\r?\n"
    r"(.*?)"
    r"\r?\n---[ \t]*(?:\r?\n|\Z)",
    re.S,
)

# Chave de frontmatter: `nome:` no início da linha (sem `: ` interno duplo).
_KV_RE = re.compile(r"^([A-Za-z_][A-Za-z0-9_-]*):[ \t]*(.*)$")


def split_frontmatter(text: str) -> tuple[str, str]:
    """Separa o bloco `--- ... ---` do corpo do documento.

    Retorna `(bloco_frontmatter, corpo)`. Sem frontmatter no início -> bloco
    vazio e o texto original como corpo. Normaliza CRLF para LF na detecção.
    """
    if not text:
        return "", text
    m = _FRONTMATTER_RE.match(text)
    if not m:
        return "", text
    return m.group(1), text[m.end():]


def parse_frontmatter(bloco: str) -> dict[str, str]:
    """Parseia o CONTEÚDO de um bloco frontmatter em um dict plano.

    Trata:
      - chaves planas `chave: valor`;
      - valores com `:` interno (ex.: títulos) — preservados;
      - aspas simples/duplas removidas do valor;
      - linhas de continuação (indentadas, ou itens `- ...` de lista em
        bloco) concatenadas ao valor da chave anterior (com espaço);
      - linhas em branco ignoradas.
    """
    campos: dict[str, str] = {}
    chave_atual: str | None = None
    for linha in bloco.splitlines():
        if not linha.strip():
            continue
        kv = _KV_RE.match(linha)
        if kv:
            chave_atual = kv.group(1).strip()
            valor = kv.group(2).strip().strip('"').strip("'")
            campos[chave_atual] = valor
        elif chave_atual is not None:
            # continuação do valor anterior (item de lista em bloco, linha
            # indentada multi-linha) — nunca vira chave falsa.
            extra = linha.strip()
            atual = campos[chave_atual]
            campos[chave_atual] = (atual + " " + extra) if atual else extra
    return campos


def parse(text: str) -> tuple[dict[str, str], str]:
    """Conveniência: separa e parseia em uma chamada.

    Retorna `(campos, corpo)`. Sem frontmatter -> `({}, texto)` (mesmo
    contrato do antigo `Memory._parse`).
    """
    bloco, corpo = split_frontmatter(text)
    if not bloco:
        return {}, corpo
    return parse_frontmatter(bloco), corpo