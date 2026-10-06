"""Testes do RAG interno sobre referências (Item 7) — harness/rag_refs.py.

Cobre: (a) busca ranked retorna referências relevantes; (b) filtro de trust:
ref `fraca` NÃO vem por padrão; (c) `incluir_fraca=True` traz mas marca como
não-confirmada; (d) síntese estruturada com conceitos/padrões/aplicação
citando origem e alimentada SOMENTE por trust alta (media NÃO alimenta); (e)
ref sem trust -> tratada como fraca; (f) tolera diretório vazio/ausente; (g)
`consultar` com query vazia -> validação; (h) `sintetizavel` presente e correto
por item; (i) sem ref alta mas com media relevante -> síntese vazia +
advertência de "nenhuma confirmada para síntese" (não lacuna total).

Usa um diretório temporário de referências (nunca toca memory/references/).
Rode com:
    python tests/rag_refs_test.py
"""

from __future__ import annotations

import pathlib
import sys
import tempfile

sys.path.insert(0, str(pathlib.Path(__file__).resolve().parent.parent))

from harness.rag_refs import _recuperavel_de, consultar  # noqa: E402


def _escreve_ref(diretorio: pathlib.Path, nome: str, corpo: str) -> pathlib.Path:
    p = diretorio / f"{nome}.md"
    p.write_text(corpo, encoding="utf-8")
    return p


def _monta_refs(tmp: str) -> pathlib.Path:
    """Cria um diretório de referências fictícias com trusts variados."""
    d = pathlib.Path(tmp) / "references"
    d.mkdir(parents=True)

    # alta — referência relevante ao tema "padrão agentic reflection"
    _escreve_ref(d, "ref-alta", """---
id: ref-alta
tipo: livro
titulo: Padrões Agentic Aplicados
autor: Autor A
tags: [agentic, reflection, harness]
trust: alta
origem: validação editorial
validado_por: reviewer
---

# Padrões Agentic Aplicados

## Conceitos-chave
- Reflection separa Produtor e Crítico.
- Tool Use como capacidade controlada.

## Padrões acionáveis
- Nunca revisar o próprio trabalho (Produtor/Crítico separados).
- Aprovação contextual (agente + recurso + ação).

## Aplicação no harness
- implementer = produtor; reviewer = crítico.

## Pontos de atenção
- RAG é evolução natural da base.
""")

    # media — conhecimento indireto por tópicos, relevante
    _escreve_ref(d, "ref-media", """---
id: ref-media
tipo: documento
titulo: Conhecimento indireto sobre agentic
autor: Autor B
tags: [agentic, fsm, harness]
trust: media
origem: conhecimento indireto por tópicos
validado_por: motor
---

# Conhecimento indireto

## Conceitos-chave
- FSM como orquestração de agentes.

## Padrões acionáveis
- Limites de recursão para barrar loops.

## Aplicação no harness
- pipeline.py é o orquestrador determinístico.
""")

    # fraca — alerta de fabricação, NÃO deve vir por padrão
    _escreve_ref(d, "ref-fraca", """---
id: ref-fraca
tipo: documento
titulo: ALERTA de fabricação agentic
autor: Autor X
tags: [agentic, fabricacao, alerta]
trust: fraca
origem: verificação em fontes editoriais
validado_por: motor
---

# ALERTA DE FABRICAÇÃO

## Conceitos-chave
- Conteúdo fabricado não deve ser citado.
""")

    # sem trust — default fraca
    _escreve_ref(d, "ref-sem-trust", """---
id: ref-sem-trust
tipo: livro
titulo: Sem campo de trust
autor: Autor C
tags: [agentic, antigo]
---

# Sem trust

## Conceitos-chave
- Registro antigo sem validação.
""")

    # irrelevante (tags distintas) — não deve rankear para o tema agentic
    _escreve_ref(d, "ref-irrelevante", """---
id: ref-irrelevante
tipo: livro
titulo: Culinária
autor: Autor D
tags: [gastronomia, receitas]
trust: alta
---

# Culinária

## Conceitos-chave
- Receitas e temperos.
""")
    return d


def _caso_a(tmp: str) -> bool:
    """(a) busca ranked retorna as referências relevantes (alta/media) e
    pontua a mais relevante primeiro. Também verifica o campo `sintetizavel`
    por item (e): alta é sintetizável; media não."""
    d = _monta_refs(tmp)
    r = consultar("padrão agentic reflection harness", diretorio=d)
    ids = [f["id"] for f in r["fontes"]]
    por_id = {f["id"]: f for f in r["fontes"]}
    return (
        "ref-alta" in ids
        and "ref-media" in ids
        and "ref-fraca" not in ids
        and "ref-sem-trust" not in ids
        and "ref-irrelevante" not in ids
        and r["fontes"] and r["fontes"][0]["score"] >= r["fontes"][-1]["score"]
        and all(f["confirmada"] for f in r["fontes"])
        # (e) sintetizavel correto por item: alta=True, media=False
        and por_id["ref-alta"]["sintetizavel"] is True
        and por_id["ref-media"]["sintetizavel"] is False
    )


def _caso_b(tmp: str) -> bool:
    """(b) filtro de trust: ref `fraca` NÃO vem por padrão, mesmo sendo
    relevante."""
    d = _monta_refs(tmp)
    r = consultar("fabricacao agentic", diretorio=d, limit=20)
    return all(f["id"] != "ref-fraca" for f in r["fontes"])


def _caso_c(tmp: str) -> bool:
    """(c) `incluir_fraca=True` traz a fraca mas marca como não-confirmada."""
    d = _monta_refs(tmp)
    r = consultar("fabricacao agentic", diretorio=d, limit=20, incluir_fraca=True)
    fracas = [f for f in r["fontes"] if f["id"] == "ref-fraca"]
    return (
        bool(fracas)
        and fracas[0]["nao_confirmada"] is True
        and fracas[0]["confirmada"] is False
        and any("fraca" in a for a in r["advertencias"])
    )


def _caso_d(tmp: str) -> bool:
    """(d) síntese estruturada com conceitos/padrões/aplicação citando origem,
    alimentada SOMENTE por trust alta. (b) ref alta alimenta a síntese; (a) ref
    media relevante NÃO alimenta a síntese mas permanece em `fontes`; (d) ref
    fraca nunca na síntese."""
    d = _monta_refs(tmp)
    r = consultar("padrão agentic reflection harness", diretorio=d)
    s = r["sintese"]
    conceitos_ids = [c["id"] for c in s["conceitos_chave"]]
    padroes_ids = [p["id"] for p in s["padroes_acionaveis"]]
    aplicacao_ids = [a["id"] for a in s["aplicacao_no_motor"]]
    fontes_ids = [f["id"] for f in r["fontes"]]
    # (a) media relevante aparece na busca mas não alimenta a síntese
    media_na_busca = "ref-media" in fontes_ids
    return (
        media_na_busca
        and "ref-alta" in conceitos_ids
        and "ref-alta" in padroes_ids
        and "ref-alta" in aplicacao_ids
        and "ref-media" not in conceitos_ids
        and "ref-media" not in padroes_ids
        and "ref-media" not in aplicacao_ids
        and "ref-fraca" not in conceitos_ids
        and s["conceitos_chave"] and s["padroes_acionaveis"] and s["aplicacao_no_motor"]
        and "ref-alta" in r["resumo"]
        and "trust alta" in r["resumo"]
        and "trust media" in r["resumo"]
    )


def _caso_e(tmp: str) -> bool:
    """(e) ref sem trust -> tratada como fraca (default conservador)."""
    d = _monta_refs(tmp)
    r_sem = consultar("sem campo de trust agentic", diretorio=d, limit=20, incluir_fraca=True)
    sem_trust = [f for f in r_sem["fontes"] if f["id"] == "ref-sem-trust"]
    return bool(sem_trust) and sem_trust[0]["trust"] == "fraca" and sem_trust[0]["nao_confirmada"] is True


def _caso_f(tmp: str) -> bool:
    """(f) tolera diretório vazio e diretório ausente."""
    vazio = pathlib.Path(tmp) / "vazio"
    vazio.mkdir(parents=True)
    ausente = pathlib.Path(tmp) / "nao-existe"
    r_vazio = consultar("qualquer", diretorio=vazio)
    r_ausente = consultar("qualquer", diretorio=ausente)
    return r_vazio["fontes"] == [] and r_ausente["fontes"] == [] and "lacuna" in " ".join(r_vazio["advertencias"])


def _caso_g(tmp: str) -> bool:
    """(g) `consultar` com query vazia -> validação (fontes vazio + aviso)."""
    d = _monta_refs(tmp)
    r = consultar("", diretorio=d)
    r_so_espacos = consultar("   ", diretorio=d)
    return (
        r["fontes"] == []
        and any("query vazia" in a for a in r["advertencias"])
        and r_so_espacos["fontes"] == []
    )


def _caso_h(tmp: str) -> bool:
    """(h)/(c) sem ref alta mas com media relevante -> síntese vazia +
    advertência de 'nenhuma confirmada para síntese' (não lacuna total). A
    media permanece em `fontes` (consultável) mas não alimenta a síntese."""
    d = _monta_refs(tmp)
    # query com tokens exclusivos da ref-media (fsm/recursao/pipeline/loop):
    # rankeia apenas a media, não a ref-alta
    r = consultar("fsm recursao pipeline loop", diretorio=d, limit=20)
    ids = [f["id"] for f in r["fontes"]]
    s = r["sintese"]
    tem_media = "ref-media" in ids
    advertencias = " ".join(r["advertencias"])
    return (
        tem_media
        and "ref-alta" not in ids
        and s["conceitos_chave"] == []
        and s["padroes_acionaveis"] == []
        and s["aplicacao_no_motor"] == []
        and "media" in advertencias
        and "não confirmam conhecimento" in advertencias
        and "lacuna de conhecimento validado" not in advertencias
    )


def _caso_i(tmp: str) -> bool:
    """(e) campo `sintetizavel` presente e correto por item em `consultar`,
    cobrindo alta/media/fraca."""
    d = _monta_refs(tmp)
    r = consultar("padrão agentic reflection harness fabricacao", diretorio=d,
                  limit=20, incluir_fraca=True)
    por_id = {f["id"]: f for f in r["fontes"]}
    alta_ok = "ref-alta" in por_id and por_id["ref-alta"]["sintetizavel"] is True
    media_ok = "ref-media" in por_id and por_id["ref-media"]["sintetizavel"] is False
    fraca_ok = "ref-fraca" in por_id and por_id["ref-fraca"]["sintetizavel"] is False
    todos_tem_campo = all("sintetizavel" in f for f in r["fontes"])
    return alta_ok and media_ok and fraca_ok and todos_tem_campo


def _caso_j(tmp: str) -> bool:
    """(j) PENDÊNCIA 6 — matching de seções por PREFIXO: um título real com
    sufixo entre parênteses (`aplicacao no motor (semantic-cache-first)`) deve
    alimentar `aplicacao_no_motor` mesmo não sendo chave exata."""
    d = pathlib.Path(tmp) / "references"
    d.mkdir(parents=True)
    _escreve_ref(d, "ref-sufixo", """---
id: ref-sufixo
tipo: documento
titulo: Aplicação com sufixo
autor: Autor S
tags: [aplicacao, sufixo]
trust: alta
origem: validação editorial
validado_por: reviewer
---

# Aplicação com sufixo

## Aplicação no motor (semantic-cache-first)
- O motor usa cache-first com embeddings.
""")
    r = consultar("aplicacao sufixo motor", diretorio=d)
    aplicacao_ids = [a["id"] for a in r["sintese"]["aplicacao_no_motor"]]
    return (
        "ref-sufixo" in aplicacao_ids
        and r["sintese"]["aplicacao_no_motor"]
        and "cache-first" in r["sintese"]["aplicacao_no_motor"][0]["trecho"]
    )


def _caso_k(tmp: str) -> bool:
    """(k) Item 2.1 — referência `recuperavel: false` NÃO entra em `fontes` nem
    alimenta a síntese (nunca sintetizável), mesmo com trust alta e alta
    relevância. `incluir_fraca=True` NÃO a reintroduz."""
    d = pathlib.Path(tmp) / "references"
    d.mkdir(parents=True)
    _escreve_ref(d, "ref-fab", """---
id: ref-fab
tipo: documento
titulo: Fabricacao agentic patterns
autor: Autor X
tags: [agentic, fabricacao, harness]
trust: alta
recuperavel: false
origem: alerta de fabricacao
validado_por: motor
---

# ALERTA DE FABRICACAO

## Conceitos-chave
- Conteudo fabricado nao deve ser citado.
""")
    _escreve_ref(d, "ref-ok", """---
id: ref-ok
tipo: livro
titulo: Agentic patterns reais
autor: Autor Y
tags: [agentic, harness]
trust: alta
origem: validacao editorial
validado_por: reviewer
---

# Agentic patterns reais

## Conceitos-chave
- Conteudo real validado.
""")
    r = consultar("agentic harness fabricacao", diretorio=d, limit=20,
                  incluir_fraca=True)
    ids = [f["id"] for f in r["fontes"]]
    sintese_ids = [c["id"] for c in r["sintese"]["conceitos_chave"]]
    return (
        "ref-ok" in ids
        and "ref-fab" not in ids
        and "ref-fab" not in sintese_ids
        and "ref-ok" in sintese_ids
    )


def _caso_l(tmp: str) -> bool:
    """(l) Item 2.1/B2 — coerção de `_recuperavel_de`: `False`/`0` (Python) e
    strings `false`/`0`/`no`/`nao`/`não` são NÃO-recuperáveis; `None`/ausente é
    o default recuperável (`True`); `True`/`"true"` são recuperáveis."""
    _ = tmp  # caso unitário: não usa diretório de referências
    return (
        _recuperavel_de({"recuperavel": False}) is False
        and _recuperavel_de({"recuperavel": 0}) is False
        and _recuperavel_de({"recuperavel": None}) is True
        and _recuperavel_de({}) is True
        and _recuperavel_de({"recuperavel": "false"}) is False
        and _recuperavel_de({"recuperavel": "0"}) is False
        and _recuperavel_de({"recuperavel": "no"}) is False
        and _recuperavel_de({"recuperavel": "nao"}) is False
        and _recuperavel_de({"recuperavel": "não"}) is False
        and _recuperavel_de({"recuperavel": "true"}) is True
        and _recuperavel_de({"recuperavel": True}) is True
        and _recuperavel_de(None) is True
    )


def main_test() -> int:
    passed = 0
    failed = 0
    cases = [
        ("busca ranked retorna refs relevantes (a)", _caso_a),
        ("filtro de trust: fraca nao vem por padrao (b)", _caso_b),
        ("incluir_fraca=True traz mas marca nao-confirmada (c)", _caso_c),
        ("sintese so com trust alta; media nao alimenta (d)", _caso_d),
        ("ref sem trust -> tratada como fraca (e)", _caso_e),
        ("tolera diretorio vazio/ausente (f)", _caso_f),
        ("query vazia -> validacao (g)", _caso_g),
        ("sem alta mas com media -> sintese vazia + aviso (h)", _caso_h),
        ("campo sintetizavel correto por item (i)", _caso_i),
        ("matching por prefixo: secao com sufixo alimenta sintese (j) [P6]", _caso_j),
        ("recuperavel: false fora de fontes e da sintese (k) [2.1]", _caso_k),
        ("coercao _recuperavel_de: False/0/None/strings (l) [B2]", _caso_l),
    ]
    for name, test in cases:
        with tempfile.TemporaryDirectory() as tmp:
            try:
                ok = test(tmp)
            except Exception:
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
