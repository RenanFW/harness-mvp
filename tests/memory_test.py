"""Testes da memória episódica com Trust (confiança/rastreabilidade).

Cobre: (a) record() grava frontmatter com trust/origem/validado_por;
(b) defaults seguros (trust fraca quando não passado); (c) _parse lê os
campos; (d) registros antigos (sem trust) são lidos com default fraca;
(e) valores com `:` interno não quebram o parse; (f) search() anexa trust;
(g) search(snippet=True) trunca o body preservando meta/id/file/score/trust;
(h) search() default (snippet=False) devolve o body completo (retrocompatível).

Rode com:
    python tests/memory_test.py
"""

from __future__ import annotations

import contextlib
import pathlib
import sys
import tempfile
from unittest import mock

sys.path.insert(0, str(pathlib.Path(__file__).resolve().parent.parent))

from harness import config  # noqa: E402
from harness.memory import Memory, recuperavel_de, trust_de  # noqa: E402


@contextlib.contextmanager
def _memoria_tmp(tmp: str):
    """Context manager: Memory isolada em diretório temp, com os mocks de
    EPISODIC_DIR/INDEX_FILE/MEMORY_DIR ATIVOS até o fim do bloco (nunca toca
    memory/ real — o mock não pode ser restaurado antes do uso)."""
    ep = pathlib.Path(tmp) / "episodic"
    idx = ep / "index.md"
    mem = pathlib.Path(tmp) / "memory"
    with mock.patch.object(config, "EPISODIC_DIR", ep), \
         mock.patch.object(config, "INDEX_FILE", idx), \
         mock.patch.object(config, "MEMORY_DIR", mem):
        yield Memory()


def _caso_a() -> bool:
    """(a) record() grava frontmatter com trust/origem/validado_por."""
    with tempfile.TemporaryDirectory() as tmp:
        with _memoria_tmp(tmp) as m:
            rec = m.record(
                keywords=["trust", "rastreabilidade"],
                agente="implementer",
                tema="trust na memoria",
                entrada="x",
                fluxo="y",
                resultado="z",
                contexto="w",
                trust="alta",
                origem="execução via /hub, delivery-protocol; evidências: tests 26/26",
                validado_por="reviewer",
            )
            meta = rec["meta"]
            return (
                meta.get("trust") == "alta"
                and meta.get("origem") == "execução via /hub, delivery-protocol; evidências: tests 26/26"
                and meta.get("validado_por") == "reviewer"
            )


def _caso_b() -> bool:
    """(b) defaults seguros: sem trust -> fraca; sem origem/validado_por ->
    marcados como ausentes (nunca conhecimento confirmado)."""
    with tempfile.TemporaryDirectory() as tmp:
        with _memoria_tmp(tmp) as m:
            rec = m.record(
                keywords=["k"], agente="web", tema="sem trust",
                entrada="", fluxo="", resultado="", contexto="",
            )
            meta = rec["meta"]
            return (
                meta.get("trust") == config.TRUST_DEFAULT
                and meta.get("origem") == "_não informada_"
                and meta.get("validado_por") == "_não informado_"
            )


def _caso_c() -> bool:
    """(c) _parse (via get/list_records) lê os campos gravados."""
    with tempfile.TemporaryDirectory() as tmp:
        with _memoria_tmp(tmp) as m:
            rec = m.record(
                keywords=["k"], agente="hub", tema="parse trust",
                entrada="e", fluxo="f", resultado="r", contexto="c",
                trust="media", origem="origem rastreável do caso c",
                validado_por="hub",
            )
            lido = m.get(rec["id"])
            meta = lido["meta"] if lido else {}
            nos_records = any(
                r["meta"].get("trust") == "media" for r in m.list_records()
            )
            return (
                lido is not None
                and meta.get("trust") == "media"
                and meta.get("origem") == "origem rastreável do caso c"
                and meta.get("validado_por") == "hub"
                and nos_records
            )


def _caso_d() -> bool:
    """(d) registros antigos (sem trust) são lidos com default fraca."""
    with tempfile.TemporaryDirectory() as tmp:
        ep = pathlib.Path(tmp) / "episodic"
        ep.mkdir(parents=True)
        (ep / "reg-antigo.md").write_text(
            "---\nid: reg-antigo\nkeywords: [a]\ndata: 2026-08-15\n"
            "agente: hub\nstatus: completed\n---\n\n# Tema antigo\n\ncorpo\n",
            encoding="utf-8",
        )
        with mock.patch.object(config, "EPISODIC_DIR", ep), \
             mock.patch.object(config, "MEMORY_DIR", pathlib.Path(tmp) / "memory"):
            m = Memory()
            rec = m.get("reg-antigo")
            return (
                rec is not None
                and "trust" not in rec["meta"]  # arquivo real não tem a chave
                and trust_de(rec["meta"]) == "fraca"
            )


def _caso_e() -> bool:
    """(e) valores com `:` interno (ex.: URLs) não quebram o parse."""
    with tempfile.TemporaryDirectory() as tmp:
        with _memoria_tmp(tmp) as m:
            origem = "webscraping indireto; fontes: https://a.com:8080/x e https://b.com/y"
            rec = m.record(
                keywords=["k"], agente="web", tema="dois pontos",
                entrada="", fluxo="", resultado="", contexto="",
                trust="media", origem=origem, validado_por="motor",
            )
            meta = m.get(rec["id"])["meta"]
            return (
                meta.get("origem") == origem  # ":" interno preservado por partition()
                and meta.get("trust") == "media"
            )


def _caso_f() -> bool:
    """(f) search() anexa trust a cada resultado (RAG vê confiança)."""
    with tempfile.TemporaryDirectory() as tmp:
        with _memoria_tmp(tmp) as m:
            m.record(
                keywords=["confianca", "busca"], agente="hub", tema="busca trust",
                entrada="", fluxo="", resultado="", contexto="",
                trust="alta", origem="evidências externas", validado_por="reviewer",
            )
            itens = m.search("confianca busca")
            return (
                bool(itens)
                and all("trust" in item for item in itens)
                and itens[0]["trust"] == "alta"
            )


def _corpo_longo() -> str:
    """Corpo de teste com > 300 chars (para forçar o truncamento do snippet)."""
    return " ".join(f"palavra-{i:02d}" for i in range(40))  # ~ 470 chars


def _caso_g() -> bool:
    """(g) Lote 1 — search(snippet=True): body truncado (<= ~300 chars) com
    título `# ` preservado; meta/id/file/score/trust mantidos."""
    with tempfile.TemporaryDirectory() as tmp:
        with _memoria_tmp(tmp) as m:
            corpo = _corpo_longo()
            rec = m.record(
                keywords=["snippet", "truncado"], agente="hub", tema="teste snippet",
                entrada=corpo, fluxo=corpo, resultado=corpo, contexto=corpo,
                trust="alta", origem="evidências", validado_por="reviewer",
            )
            itens = m.search("snippet truncado", snippet=True)
            if not itens:
                return False
            item = itens[0]
            # meta/id/file/score/trust preservados
            if not (
                item["id"] == rec["id"]
                and item["file"] == rec["file"]
                and item["meta"] == rec["meta"]
                and item.get("trust") == "alta"
                and isinstance(item.get("score"), int)
            ):
                return False
            # body truncado: título + "\n" + resto(+ "…") => máx. 302 chars
            if len(item["body"]) > 302:
                return False
            if item["body"] == rec["body"]:  # precisou truncar de fato
                return False
            # título `# ` preservado no snippet
            if not item["body"].startswith("# "):
                return False
            return True
    return False


def _caso_h() -> bool:
    """(h) Lote 1 — search() default (snippet=False): body completo
    (retrocompatível), com meta/id/trust presentes."""
    with tempfile.TemporaryDirectory() as tmp:
        with _memoria_tmp(tmp) as m:
            corpo = _corpo_longo()
            rec = m.record(
                keywords=["snippet", "integral"], agente="hub", tema="teste body",
                entrada=corpo, fluxo=corpo, resultado=corpo, contexto=corpo,
                trust="media", origem="evidências", validado_por="reviewer",
            )
            itens = m.search("snippet integral")  # default snippet=False
            if not itens:
                return False
            item = itens[0]
            return (
                item["body"] == rec["body"]  # body completo, sem truncar
                and item["id"] == rec["id"]
                and item.get("trust") == "media"
            )
    return False


def _caso_i() -> bool:
    """(i) Item 2.2 — search(min_trust): default None mantém o comportamento
    antigo (inclui fraca); min_trust='media' exclui a fraca e mantém
    media/alta. O campo `trust` continua por item."""
    with tempfile.TemporaryDirectory() as tmp:
        with _memoria_tmp(tmp) as m:
            for trust in ("alta", "media", "fraca"):
                m.record(
                    keywords=["nivel", "trust"], agente="hub",
                    tema=f"nivel {trust}", entrada="", fluxo="",
                    resultado="", contexto="", trust=trust,
                    origem="evidência", validado_por="reviewer",
                )
            sem_filtro = m.search("nivel trust", limit=20)  # default None
            com_filtro = m.search("nivel trust", limit=20, min_trust="media")
            trusts_sem = {i["trust"] for i in sem_filtro}
            trusts_com = {i["trust"] for i in com_filtro}
            return (
                "fraca" in trusts_sem
                and trusts_sem == {"alta", "media", "fraca"}
                and "fraca" not in trusts_com
                and trusts_com == {"alta", "media"}
                and all("trust" in i for i in com_filtro)
            )
    return False


def _caso_j() -> bool:
    """(j) Item 2.1 — registro NÃO-recuperável (recuperavel: false) NUNCA é
    devolvido por search(), mesmo com trust alta e alta relevância. O default
    da ausência do campo é recuperável (`True`)."""
    with tempfile.TemporaryDirectory() as tmp:
        ep = pathlib.Path(tmp) / "episodic"
        ep.mkdir(parents=True)
        (ep / "fab.md").write_text(
            "---\nid: fab\nkeywords: [fabricacao, toxica]\ndata: 2026-09-11\n"
            "agente: webscraper\nstatus: fabricacao\ntrust: alta\n"
            "recuperavel: false\n---\n\n# Fabricacao toxica\n\ncorpo\n",
            encoding="utf-8",
        )
        (ep / "ok.md").write_text(
            "---\nid: ok\nkeywords: [fabricacao, toxica]\ndata: 2026-09-11\n"
            "agente: webscraper\nstatus: completed\ntrust: alta\n---\n\n"
            "# Fabricacao ok\n\ncorpo\n",
            encoding="utf-8",
        )
        with mock.patch.object(config, "EPISODIC_DIR", ep), \
             mock.patch.object(config, "MEMORY_DIR", pathlib.Path(tmp) / "memory"):
            m = Memory()
            itens = m.search("fabricacao toxica", limit=20)
            ids = [i["id"] for i in itens]
            fab = m.get("fab")
            ok = m.get("ok")
            return (
                "ok" in ids
                and "fab" not in ids
                and fab is not None and recuperavel_de(fab["meta"]) is False
                and ok is not None and recuperavel_de(ok["meta"]) is True
            )
    return False


def _caso_k() -> bool:
    """(k) Item 2.1/B2 — coerção de `recuperavel_de`: `False`/`0` (Python) e
    strings `false`/`0`/`no`/`nao`/`não` são NÃO-recuperáveis; `None`/ausente
    é o default recuperável (`True`); `True`/`"true"` são recuperáveis. O
    idioma `valor or ""` mascarava `False`/`0` do Python (bug B2)."""
    return (
        recuperavel_de({"recuperavel": False}) is False
        and recuperavel_de({"recuperavel": 0}) is False
        and recuperavel_de({"recuperavel": 0.0}) is False
        and recuperavel_de({"recuperavel": None}) is True
        and recuperavel_de({}) is True
        and recuperavel_de({"recuperavel": "false"}) is False
        and recuperavel_de({"recuperavel": "0"}) is False
        and recuperavel_de({"recuperavel": "no"}) is False
        and recuperavel_de({"recuperavel": "nao"}) is False
        and recuperavel_de({"recuperavel": "não"}) is False
        and recuperavel_de({"recuperavel": "true"}) is True
        and recuperavel_de({"recuperavel": True}) is True
        and recuperavel_de(None) is True
    )


CASES = [
    ("record() grava frontmatter com trust/origem/validado_por (a)", _caso_a),
    ("defaults seguros: trust fraca + origem/validado ausentes marcados (b)", _caso_b),
    ("_parse lê os campos gravados (c)", _caso_c),
    ("registros antigos sem trust -> default fraca (d)", _caso_d),
    ("valores com ':' interno não quebram o parse (e)", _caso_e),
    ("search() anexa trust aos resultados (f)", _caso_f),
    ("search(snippet=True) trunca body preservando meta/id/file/score/trust (g)", _caso_g),
    ("search() default devolve body completo — retrocompatível (h)", _caso_h),
    ("search(min_trust='media') exclui fraca; default None mantém (i)", _caso_i),
    ("search() nunca devolve registro recuperavel: false (j)", _caso_j),
    ("coercao recuperavel_de: False/0/None/strings (k) [B2]", _caso_k),
]


def main_test() -> int:
    passed = 0
    failed = 0
    for name, test in CASES:
        try:
            ok = test()
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