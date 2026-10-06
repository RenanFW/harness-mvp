"""Nomeador determinístico de projetos do harness (stdlib only).

Gera um nome único e seguro para pasta de projeto a partir de um contexto
(objetivo/descrição/keywords) e cria a subpasta em `sidePrjs/<nome>/`.

Padrão Routing (Ch2): o hub aciona este módulo ao receber um pedido de projeto
novo via `/hub`, em vez de escolher o nome manualmente — o nomeador garante
reprodutibilidade (mesmo contexto -> mesmo nome base), segurança para pastas no
Windows (sem acentos, sem caracteres reservados, sem espaço/ponto final) e
unicidade (case-insensitive, com sufixo numérico em colisões).

Uso:
    python -m harness.namer "Criação de API REST para pedidos"
    python -m harness.namer "Criação de API" --dry-run
    python -m harness.namer "Meu projeto" --base c:/tmp/projetos

Import programático:
    from harness.namer import criar_projeto, nome_projeto, slugify
"""

from __future__ import annotations

import argparse
import pathlib
import re
import sys
import unicodedata

from . import config

# Tamanho máximo do nome gerado (chars)
MAX_NOME = 40

# Stopwords em pt (normalizadas: minúsculas, sem acentos). Palavras nesta lista
# nunca entram no nome do projeto — o nome deve carregar apenas o que identifica
# o projeto (Chaining: extrair o essencial do contexto).
STOPWORDS = {
    "a", "ao", "aos", "as", "com", "como", "da", "das", "de", "do", "dos",
    "e", "em", "entre", "essa", "esse", "esta", "este", "isso", "isto",
    "mais", "mas", "meu", "minha", "na", "nas", "no", "nos", "o", "os",
    "ou", "para", "pela", "pelo", "pelas", "pelos", "por", "que", "se",
    "sem", "seu", "sua", "seus", "suas", "um", "uma", "uns", "umas", "uso",
    "usando", "projeto", "projetos", "aplicacao", "aplicacoes", "sistema",
    "sistemas",
}

# Nomes reservados do Windows (case-insensitive) — não podem ser nome de pasta
# nem de arquivo; `slugify` os recusa com ValueError.
_RESERVADOS = (
    ("CON", "PRN", "AUX", "NUL")
    + tuple(f"COM{i}" for i in range(1, 10))
    + tuple(f"LPT{i}" for i in range(1, 10))
)


def slugify(texto: str) -> str:
    """Normaliza texto para um nome de pasta seguro.

    - lowercase;
    - remove acentos via unicodedata (NFKD + remoção de combining marks)
      (ex.: "Criação de API" -> "criacao-de-api");
    - troca qualquer caractere não-alfanumérico por "-";
    - colapsa múltiplos "-" e remove "-" nas pontas.

    Nomes inválidos levantam ValueError:
    - reservados do Windows (CON, PRN, AUX, NUL, COM1-9, LPT1-9);
    - texto terminando em "." ou espaço (inválido em pastas do Windows);
    - texto que não produz nenhum caractere alfanumérico (slug vazio).
    """
    if texto.endswith((" ", ".")):
        raise ValueError(f"nome inválido: termina com '.' ou espaço: {texto!r}")
    normal = unicodedata.normalize("NFKD", texto.lower())
    sem_acentos = "".join(c for c in normal if not unicodedata.combining(c))
    slug = re.sub(r"[^a-z0-9]+", "-", sem_acentos).strip("-")
    if not slug:
        raise ValueError(f"nome inválido: nenhum caractere utilizável em {texto!r}")
    if slug.upper() in _RESERVADOS:
        raise ValueError(f"nome inválido: reservado do Windows: {slug!r}")
    return slug


def _palavras_significativas(contexto: str) -> list[str]:
    """Extrai do contexto as palavras que podem entrar no nome: normalizadas
    (minúsculas, sem acentos), com >= 3 letras e fora das stopwords.

    Palavras que `slugify` recusa (ex.: reservadas do Windows) são ignoradas —
    o nomeador nunca quebra por causa de uma palavra isolada.
    """
    normal = unicodedata.normalize("NFKD", contexto.lower())
    sem_acentos = "".join(c for c in normal if not unicodedata.combining(c))
    palavras: list[str] = []
    for token in re.split(r"[^a-z0-9]+", sem_acentos):
        if not token or len(token) < 3 or token in STOPWORDS:
            continue
        try:
            palavra = slugify(token)
        except ValueError:
            continue
        if palavra and palavra not in palavras:
            palavras.append(palavra)
    return palavras


def _monta_nome(palavras: list[str]) -> str:
    """Junta as palavras com "-" sem estourar MAX_NOME.

    Na JUNÇÃO de múltiplas palavras, monta palavra por palavra (respeitando o
    limite) e para antes de cortar a próxima palavra no meio. Palavra ÚNICA
    que excede MAX_NOME é um caso à parte: é truncada (limite duro), pois não
    há o que descartar sem perder o identificador. Se não houver palavras,
    retorna o fallback "projeto".
    """
    if not palavras:
        return "projeto"
    nome = palavras[0]
    if len(nome) > MAX_NOME:
        nome = nome[:MAX_NOME].rstrip("-")
    for palavra in palavras[1:]:
        if len(nome) + 1 + len(palavra) > MAX_NOME:
            break
        nome += "-" + palavra
    return nome


def _usados_em(base: pathlib.Path) -> set[str]:
    """Nomes ocupados dentro de `base` (minúsculas): pastas NÃO vazias e
    arquivos. Pastas vazias podem ser reutilizadas (mkdir exist_ok) — nada é
    sobrescrito, apenas reaproveitado."""
    usados: set[str] = set()
    if not base.is_dir():
        return usados
    for entrada in base.iterdir():
        if entrada.is_dir():
            if any(entrada.iterdir()):  # pasta não vazia: colide
                usados.add(entrada.name.lower())
        else:
            usados.add(entrada.name.lower())  # arquivo ocupa o nome
    return usados


def nome_projeto(contexto: str, existentes: list[str] | None = None) -> str:
    """Gera um nome de projeto determinístico a partir do contexto.

    Extrai as palavras significativas (>= 3 letras, fora das stopwords), junta
    com "-" respeitando MAX_NOME e garante UNICIDADE (case-insensitive —
    importante no Windows): se o nome colidir com `existentes` (ou, quando
    `existentes` é None, com as subpastas reais de `sidePrjs/`), sufixa "-2",
    "-3", ... até achar um nome livre. O nome final — inclusive com sufixo de
    unicidade — nunca excede MAX_NOME: na junção de múltiplas palavras, novas
    palavras só entram inteiras (sem corte no meio); uma palavra única maior
    que MAX_NOME é truncada (limite duro). Contexto sem palavras significativas
    (ou só stopwords) vira o fallback "projeto".
    """
    palavras = _palavras_significativas(contexto)
    base_nome = _monta_nome(palavras)
    usados = {str(e).lower() for e in (existentes or [])}
    if existentes is None and config.SIDE_PRJS_DIR.is_dir():
        usados |= {p.name.lower() for p in config.SIDE_PRJS_DIR.iterdir() if p.is_dir()}
    candidato = base_nome
    sufixo = 2
    while candidato.lower() in usados:
        sufixo_str = f"-{sufixo}"
        # O sufixo de unicidade conta no limite: trunca a base para o nome
        # final nunca exceder MAX_NOME (ex.: base de 40 chars + "-2" -> base
        # vira 38 chars e o nome final fica em 40).
        limite = MAX_NOME - len(sufixo_str)
        base_curta = base_nome[:limite].rstrip("-")
        candidato = f"{base_curta}{sufixo_str}"
        sufixo += 1
    return candidato


def criar_projeto(contexto: str, base: pathlib.Path | None = None) -> pathlib.Path:
    """Cria a subpasta do projeto em `base or config.SIDE_PRJS_DIR` e retorna
    o Path criado.

    - mkdir(parents=True, exist_ok=True);
    - se o nome colidir com pasta existente NÃO vazia (ou com um arquivo), gera
      sufixo numérico — nada é sobrescrito; pastas vazias são reutilizadas.

    Nota: passar `base` fora de `config.ROOT` (ex.: `--base c:/tmp` na CLI) é
    uso intencional do OPERADOR HUMANO, fora das políticas de execução de
    agentes — o harness bloqueia caminhos externos para agentes; este módulo
    não aplica esse bloqueio por ser chamado também diretamente pela CLI.
    """
    base = pathlib.Path(base) if base is not None else config.SIDE_PRJS_DIR
    usados = _usados_em(base)
    nome = nome_projeto(contexto, existentes=sorted(usados))
    destino = base / nome
    destino.mkdir(parents=True, exist_ok=True)
    return destino


def main(argv: list[str] | None = None) -> int:
    """CLI do nomeador.

    `python -m harness.namer "<contexto>"` imprime o caminho criado
    (ex.: `sidePrjs/criacao-de-api`); `--dry-run` só gera o nome sem criar
    pasta; `--base <dir>` troca o diretório base.

    `--base` fora de `config.ROOT` é uso intencional do operador humano, fora
    das políticas de execução de agentes — o harness bloqueia caminhos
    externos para agentes; a CLI não aplica esse bloqueio.
    """
    # Console Windows (cp1252) não imprime acentos UTF-8; usa UTF-8 com
    # substituição para nunca quebrar a saída (mesmo padrão do pipeline.py).
    try:
        sys.stdout.reconfigure(encoding="utf-8", errors="replace")
    except (AttributeError, ValueError):
        pass
    parser = argparse.ArgumentParser(
        prog="python -m harness.namer",
        description="Nomeador determinístico de projetos do harness (sidePrjs/).",
    )
    parser.add_argument(
        "contexto",
        help="objetivo/descrição/keywords do projeto (entre aspas)",
    )
    parser.add_argument(
        "--dry-run",
        action="store_true",
        help="apenas gera o nome único, sem criar pasta",
    )
    parser.add_argument(
        "--base",
        default=None,
        help=(
            "diretório base alternativo (padrão: sidePrjs/); fora de "
            "config.ROOT é uso intencional do operador humano, fora das "
            "políticas de execução de agentes"
        ),
    )
    args = parser.parse_args(argv)

    base = pathlib.Path(args.base) if args.base else config.SIDE_PRJS_DIR
    usados = _usados_em(base)
    if args.dry_run:
        print(nome_projeto(args.contexto, existentes=sorted(usados)))
        return 0
    destino = criar_projeto(args.contexto, base)
    if base == config.SIDE_PRJS_DIR:
        # base padrão: imprime caminho relativo à raiz (ex.: sidePrjs/nome)
        print(destino.relative_to(config.ROOT).as_posix())
    else:
        print(str(destino))
    return 0


if __name__ == "__main__":
    sys.exit(main())