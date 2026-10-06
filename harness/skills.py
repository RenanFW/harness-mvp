"""Registro e validação de skills de domínio do harness (modelo Agent Skills).

As skills vivem em `.opencode/skills/<nome>/SKILL.md` e são **capacidades de
tarefa** (conhecimento + procedimento) carregadas on-demand pela LLM quando a
descrição casa com o problema. Este módulo dá ao RUNTIME consciência delas
(observabilidade e validação), mantendo a separação de princípios:

- skills INSTRUEM o *como* executar um tipo de tarefa;
- o harness (runtime) decide o *se pode* (`config.py`, `AGENTS.md`).

Trust e origem: o frontmatter de uma skill exige `origem` rastreável e `trust`
(alta/media/fraca), seguindo o mesmo princípio anti-fabricação das referências
(`memory/references/`). Uma skill sem origem válida não é listada como válida.

Uso:
    python -m harness.skills list       # lista skills com trust e status
    python -m harness.skills validate   # valida todas as skills
"""

from __future__ import annotations

import pathlib
import re
import sys

from . import config
from . import frontmatter

# Campos obrigatórios do frontmatter de uma skill (schema P7). Fonte única em
# config.SKILLS_CAMPOS_OBRIGATORIOS.
_CAMPOS_OBRIGATORIOS = tuple(config.SKILLS_CAMPOS_OBRIGATORIOS)

# Prefixo dos módulos do harness aceitos como origem de uma skill (validação
# confere que o arquivo existe de fato — ver `_origem_rastreavel`).
_HARNESS_PREFIX = "harness/"


def _skill_dir(root: pathlib.Path | None = None) -> pathlib.Path:
    return (root or config.ROOT) / config.OPCODE_REL_SKILLS_DIR


def _trust_de(campos: dict) -> str:
    """Trust do frontmatter; sem o campo -> fraca (conservador)."""
    t = str(campos.get("trust") or "").strip().lower()
    return t if t in config.TRUST else config.TRUST_DEFAULT


def listar_skills(root: pathlib.Path | None = None) -> list[dict]:
    """Varre `.opencode/skills/*/SKILL.md` e retorna o registro de cada skill
    (nome, frontmatter, corpo e status de validação). Tolerante a diretório
    ausente/vazio (retorna [])."""
    diretorio = _skill_dir(root)
    skills: list[dict] = []
    if not diretorio.is_dir():
        return skills
    for arquivo in sorted(diretorio.glob("*")):
        if not arquivo.is_dir():
            continue
        skill_file = arquivo / "SKILL.md"
        if not skill_file.exists():
            continue
        try:
            texto = skill_file.read_text(encoding="utf-8", errors="replace")
        except OSError:
            continue
        campos, corpo = frontmatter.parse(texto)
        erros = validar_skill(campos, root=root, nome_pasta=arquivo.name)
        skills.append({
            "name": campos.get("name") or arquivo.name,
            "file": str(skill_file),
            "meta": campos,
            "trust": _trust_de(campos),
            "descricao": campos.get("description", ""),
            "origem": campos.get("origem", ""),
            "valida": not erros,
            "erros": erros,
        })
    return skills


def validar_skill(campos: dict, root: pathlib.Path | None = None,
                  nome_pasta: str | None = None) -> list[str]:
    """Valida o frontmatter de uma skill contra o schema P7. Retorna lista de
    erros (vazia = válida). Regras:
    - todos os campos obrigatórios presentes e não-vazios;
    - `name` bate com o nome da pasta (quando informado);
    - `trust` ausente/inválido → `fraca` (conservador, NÃO reprova — mesmo
      comportamento das referências; ver `_trust_de`);
    - `origem` referencia um arquivo que EXISTE em `memory/references/` ou um
      módulo `harness/<módulo>.py` existente (origem rastreável e verificada)."""
    erros: list[str] = []

    for campo in _CAMPOS_OBRIGATORIOS:
        valor = str(campos.get(campo) or "").strip()
        if not valor:
            erros.append(f"campo obrigatório ausente: {campo}")

    if nome_pasta and campos.get("name") != nome_pasta:
        erros.append(f"name ({campos.get('name')!r}) difere do nome da pasta "
                     f"({nome_pasta!r})")

    # trust ausente/inválido -> fraca (conservador), NÃO reprova a skill:
    # consistente com a política de trust das referências (memory/core.md).
    _trust_de(campos)

    origem = str(campos.get("origem") or "").strip()
    if origem:
        if not _origem_rastreavel(origem, root):
            erros.append("origem não rastreável: não referencia um arquivo "
                         "existente em memory/references/ nem um módulo "
                         "existente do harness")
    else:
        erros.append("origem ausente (anti-fabricação)")

    return erros


def _origem_rastreavel(origem: str, root: pathlib.Path | None = None) -> bool:
    """True se PELO MENOS um token de `origem` aponta para um arquivo que
    existe em `memory/references/` (id do arquivo) ou para um módulo real
    `harness/<módulo>.py` (verificado em disco — sem substring)."""
    raiz = root or config.ROOT
    refs_dir = raiz / "memory" / "references"

    # tokens: suporta lista inline `[a, b]` do schema (remove colchetes) e
    # separadores vírgula/espaço.
    tokens = re.split(r"[\s,]+", origem.strip().strip("[]"))
    for token in tokens:
        token = token.strip()
        if not token:
            continue
        # referência em memory/references/<id>.md — confere o ARQUIVO no disco
        if token.startswith("memory/references/"):
            caminho = raiz / token
            if caminho.is_file() and caminho.suffix == ".md":
                return True
            continue
        # módulo do harness — confere que o arquivo existe de fato
        if token.startswith(_HARNESS_PREFIX):
            caminho = raiz / token
            if caminho.is_file() and caminho.suffix == ".py":
                return True
            continue
        # id de referência sozinho (schema tolerante: sem prefixo)
        if refs_dir.is_dir():
            if token.endswith(".md"):
                token = token[:-3]
            if (refs_dir / f"{token}.md").is_file():
                return True
    return False


def resumo_sintetico(root: pathlib.Path | None = None) -> dict:
    """Panorama para o endpoint GET /api/skills: total, válidas/inválidas e
    distribuição por trust.

    NÃO expõe caminho absoluto dos arquivos (convenção F11 do server) — o
    caminho fica visível apenas no CLI `list`, fora do escopo HTTP."""
    skills = listar_skills(root)
    validas = [s for s in skills if s["valida"]]
    por_trust: dict[str, int] = {}
    for s in skills:
        por_trust[s["trust"]] = por_trust.get(s["trust"], 0) + 1
    # payload público: sem `file` (caminho absoluto) — só nome/descrição/trust
    publico = [
        {k: v for k, v in s.items() if k != "file"}
        for s in skills
    ]
    return {
        "total": len(skills),
        "validas": len(validas),
        "invalidas": len(skills) - len(validas),
        "por_trust": por_trust,
        "skills": publico,
    }


def main(argv: list[str] | None = None) -> int:
    """CLI: `list` lista skills; `validate` valida todas."""
    try:
        sys.stdout.reconfigure(encoding="utf-8", errors="replace")
    except (AttributeError, ValueError):
        pass
    args = list(argv) if argv is not None else sys.argv[1:]
    comando = args[0] if args else "list"

    if comando == "list":
        for s in listar_skills():
            flag = "OK " if s["valida"] else "XX "
            print(f"{flag}{s['name']} [{s['trust']}] {s['descricao'] or ''}")
        return 0

    if comando == "validate":
        skills = listar_skills()
        falhas = 0
        for s in skills:
            if s["valida"]:
                print(f"[OK] {s['name']} (trust {s['trust']})")
            else:
                falhas += 1
                print(f"[FAIL] {s['name']}")
                for e in s["erros"]:
                    print(f"  - {e}")
        print(f"\n{len(skills) - falhas}/{len(skills)} skills válidas.")
        return 1 if falhas else 0

    print(f"uso: python -m harness.skills {{list|validate}} (comando inválido: {comando})")
    return 2


if __name__ == "__main__":
    raise SystemExit(main())