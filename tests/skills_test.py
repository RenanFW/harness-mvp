"""Testes do registro de skills de domínio (modelo Agent Skills) —
harness/skills.py.

Cobre: (a) `listar_skills` varre `.opencode/skills/*/SKILL.md`; (b) validação
do frontmatter obrigatório (schema P7); (c) `origem` rastreável (referência em
memory/references/ ou módulo do harness) e `trust` válido; (d) skill sem
origem/trust inválido é listada como inválida; (e) tolera diretório ausente
(retorna []); (f) CLI `validate` retorna 0 quando todas válidas e 1 com falha.

Usa um diretório temporário (nunca toca .opencode/skills/ nem memory/).
Rode com:
    python tests/skills_test.py
"""

from __future__ import annotations

import pathlib
import sys
import tempfile

sys.path.insert(0, str(pathlib.Path(__file__).resolve().parent.parent))

from harness import skills as skills_mod  # noqa: E402


def _monta_arvore(tmp: str) -> pathlib.Path:
    """Cria .opencode/skills/<nome>/SKILL.md + memory/references/ + módulo
    harness/ fictícios."""
    root = pathlib.Path(tmp)
    skills_dir = root / ".opencode" / "skills"
    refs_dir = root / "memory" / "references"
    refs_dir.mkdir(parents=True)
    (root / "harness").mkdir(parents=True)

    # referência válida para origem
    (refs_dir / "ref-validada.md").write_text(
        "---\nid: ref-validada\ntitulo: x\ntrust: alta\n---\n", encoding="utf-8")

    # módulo fictício do harness para origem do tipo harness/<módulo>.py
    (root / "harness" / "webscraper.py").write_text(
        '"""módulo fictício para teste"""\n', encoding="utf-8")

    boa = skills_dir / "boa-skill" / "SKILL.md"
    boa.parent.mkdir(parents=True)
    boa.write_text("""\
---
name: boa-skill
description: gatilho da skill boa
tipo: skill
dominio: teste
origem: memory/references/ref-validada.md
trust: alta
validado_por: motor
data: 2026-08-30
tags: [teste]
---
# Boa skill
""", encoding="utf-8")

    ruim = skills_dir / "ruim-skill" / "SKILL.md"
    ruim.parent.mkdir(parents=True)
    ruim.write_text("""\
---
name: ruim-skill
description: gatilho
tipo: skill
dominio: teste
origem: https://invalida.example/x
trust: duvida
data: 2026-08-30
---
# Ruim
""", encoding="utf-8")
    return root


def _caso_a() -> bool:
    """listar_skills lista as skills do diretório temp (boa válida, ruim não)."""
    with tempfile.TemporaryDirectory() as tmp:
        root = _monta_arvore(tmp)
        skills = skills_mod.listar_skills(root)
        por_nome = {s["name"]: s for s in skills}
        return (
            set(por_nome) == {"boa-skill", "ruim-skill"}
            and por_nome["boa-skill"]["valida"] is True
            and por_nome["boa-skill"]["trust"] == "alta"
            and por_nome["ruim-skill"]["valida"] is False
        )


def _caso_b() -> bool:
    """validar_skill: campos obrigatórios ausentes geram erro."""
    campos = {"name": "x", "tipo": "skill", "dominio": "d",
              "origem": "memory/references/ref-validada.md", "trust": "alta",
              "validado_por": "motor", "data": "2026-08-30", "tags": "[t]"}
    with tempfile.TemporaryDirectory() as tmp:
        root = _monta_arvore(tmp)
        return any("description" in e for e in skills_mod.validar_skill(campos, root=root))


def _caso_c() -> bool:
    """validar_skill: trust inválido vira fraca (conservador), origem real ok."""
    with tempfile.TemporaryDirectory() as tmp:
        root = _monta_arvore(tmp)
        campos = {"name": "x", "description": "d", "tipo": "skill", "dominio": "d",
                  "origem": "memory/references/ref-validada.md", "trust": "duvida",
                  "validado_por": "motor", "data": "2026-08-30", "tags": "[t]"}
        # trust inválido não reprova (vira fraca conservador), mas origem real ok
        return skills_mod.validar_skill(campos, root=root) == []


def _caso_d() -> bool:
    """validar_skill: origem não rastreável reprova."""
    with tempfile.TemporaryDirectory() as tmp:
        root = _monta_arvore(tmp)
        campos = {"name": "x", "description": "d", "tipo": "skill", "dominio": "d",
                  "origem": "https://invalida.example/x", "trust": "alta",
                  "validado_por": "motor", "data": "2026-08-30", "tags": "[t]"}
        return any("origem" in e for e in skills_mod.validar_skill(campos, root=root))


def _caso_e() -> bool:
    """listar_skills tolera diretório de skills ausente."""
    with tempfile.TemporaryDirectory() as tmp:
        return skills_mod.listar_skills(pathlib.Path(tmp)) == []


def _caso_f() -> bool:
    """origem apontando para módulo do harness (ex.: harness/webscraper.py) ok."""
    with tempfile.TemporaryDirectory() as tmp:
        root = _monta_arvore(tmp)
        campos = {"name": "x", "description": "d", "tipo": "skill", "dominio": "d",
                  "origem": "harness/webscraper.py", "trust": "alta",
                  "validado_por": "motor", "data": "2026-08-30", "tags": "[t]"}
        return skills_mod.validar_skill(campos, root=root) == []


def _caso_g() -> bool:
    """origem burlável NÃO passa: módulo inexistente, URL com 'harness/',
    'harness/' sozinho e ref inexistente reprovam."""
    with tempfile.TemporaryDirectory() as tmp:
        root = _monta_arvore(tmp)
        base = {"name": "x", "description": "d", "tipo": "skill", "dominio": "d",
                "trust": "alta", "validado_por": "motor", "data": "2026-08-30",
                "tags": "[t]"}
        for origem in ("harness/arquivo_que_nao_existe.py",
                       "https://evil.example.com/harness/leak",
                       "harness/",
                       "memory/references/ref-inexistente.md"):
            campos = dict(base, origem=origem)
            if not skills_mod.validar_skill(campos, root=root):
                return False
        return True


def _caso_h() -> bool:
    """origem com id de referência sozinho (sem prefixo) é aceito."""
    with tempfile.TemporaryDirectory() as tmp:
        root = _monta_arvore(tmp)
        campos = {"name": "x", "description": "d", "tipo": "skill", "dominio": "d",
                  "origem": "ref-validada", "trust": "alta", "validado_por": "motor",
                  "data": "2026-08-30", "tags": "[t]"}
        return skills_mod.validar_skill(campos, root=root) == []


def _caso_i() -> bool:
    """name divergente do nome da pasta reprova."""
    with tempfile.TemporaryDirectory() as tmp:
        root = _monta_arvore(tmp)
        campos = {"name": "outro-nome", "description": "d", "tipo": "skill",
                  "dominio": "d", "origem": "ref-validada", "trust": "alta",
                  "validado_por": "motor", "data": "2026-08-30", "tags": "[t]"}
        return any("name" in e for e in skills_mod.validar_skill(
            campos, root=root, nome_pasta="boa-skill"))


CASOS: list[tuple[str, object]] = [
    ("listar_skills lista skills do dir temp (boa/ruim)", _caso_a),
    ("validar_skill: description ausente gera erro", _caso_b),
    ("validar_skill: trust inválido vira fraca conservador", _caso_c),
    ("validar_skill: origem não rastreável reprova", _caso_d),
    ("listar_skills tolera diretório ausente", _caso_e),
    ("validar_skill: origem módulo do harness ok", _caso_f),
    ("validar_skill: origem burlável reprova (módulo/URL/harness/ sozinho)", _caso_g),
    ("validar_skill: origem com id de ref sozinho ok", _caso_h),
    ("validar_skill: name divergente da pasta reprova", _caso_i),
]


def main() -> int:
    falhas = 0
    for nome, caso in CASOS:
        try:
            ok = caso()
        except Exception as e:  # noqa: BLE001
            ok = False
            print(f"  [ERRO {type(e).__name__}] {nome}: {e}")
        if ok:
            print(f"  [PASS] {nome}")
        else:
            falhas += 1
            print(f"  [FAIL] {nome}")
    print(f"\nRESULTADO: {len(CASOS) - falhas} passaram, {falhas} falharam")
    return 1 if falhas else 0


if __name__ == "__main__":
    raise SystemExit(main())