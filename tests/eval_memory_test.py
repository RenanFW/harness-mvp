"""Testes do fecho de avaliação da memória (Evaluation Ch19 aplicado à
memória episódica) — `harness/eval_memory.py`.

Cobre: (a) `gerar_relatorio` retorna dict com as chaves esperadas; (b)
órfãos = registros fracos sem reuso (candidato a poda); (c) é somente
leitura (não modifica registros); (d) `salvar_relatorio` grava Markdown e
retorna o caminho; (e) tolera ausência/arquivo inválido de histórico;
(f) top_miss conta misses de consultas do histórico (só status de execução
real — A4); (g) memória vazia; (h) regressão do A1 — memória maior que o
top-N: órfãos estáveis em limit=1/10/0; (i) A5 — comandos sem tokens não
viram miss falsa; (j) A7 — keywords com aspas normalizadas.

Rode com:
    python tests/eval_memory_test.py
"""

from __future__ import annotations

import contextlib
import datetime
import json
import pathlib
import sys
import tempfile
from unittest import mock

sys.path.insert(0, str(pathlib.Path(__file__).resolve().parent.parent))

from harness import config  # noqa: E402
from harness.eval_memory import gerar_relatorio, salvar_relatorio, _hits_por_consulta  # noqa: E402
from harness.memory import Memory, trust_de  # noqa: E402


@contextlib.contextmanager
def _memoria_tmp(tmp: str):
    """Memory isolada em diretório temp, com os mocks de EPISODIC_DIR/
    INDEX_FILE/MEMORY_DIR ATIVOS até o fim do bloco (nunca toca memory/ real)."""
    ep = pathlib.Path(tmp) / "episodic"
    idx = ep / "index.md"
    mem = pathlib.Path(tmp) / "memory"
    with mock.patch.object(config, "EPISODIC_DIR", ep), \
         mock.patch.object(config, "INDEX_FILE", idx), \
         mock.patch.object(config, "MEMORY_DIR", mem):
        yield Memory()


def _hist_inexistente(tmp: str) -> pathlib.Path:
    return pathlib.Path(tmp) / "harness_history_inexistente.json"


def _popula(m: Memory) -> None:
    """Três registros: um forte recuperado por tag de amostra, um médio
    recuperado por tag alheia, um fraco totalmente isolado (órfão). O fraco
    é escrito diretamente (sem boilerplate das seções vazias de record())
    para ser GENUINAMENTE isolado em vocabulário — assim o teste valida a
    heurística `isolada_vocabularmente` sem falso positivo."""
    m.record(
        keywords=["python"], agente="implementer", tema="fluxo validado com testes",
        entrada="", fluxo="", resultado="", contexto="",
        trust="alta", origem="testes com evidências", validado_por="reviewer",
    )
    m.record(
        keywords=["testes"], agente="hub", tema="validacao com python",
        entrada="", fluxo="", resultado="", contexto="",
        trust="media", origem="validação parcial", validado_por="motor",
    )
    config.EPISODIC_DIR.mkdir(parents=True, exist_ok=True)
    (config.EPISODIC_DIR / "reg-isolada-2026-08-19.md").write_text(
        "---\nid: reg-isolada-2026-08-19\n"
        "keywords: [zzz-unica]\ndata: 2026-08-19\n"
        "agente: web\nstatus: completed\n---\n\n"
        "# quirkaleja\n\nconteudo quilombera estranha e unica\n",
        encoding="utf-8",
    )


def _caso_a() -> bool:
    """(a) gerar_relatorio retorna dict com resumo/trust_dist/reuso_por_tag/
    orfaos/top_miss/sugestoes e resumo com total/com_reuso/orfaos/taxa_reuso."""
    with tempfile.TemporaryDirectory() as tmp:
        with _memoria_tmp(tmp) as m:
            _popula(m)
            r = gerar_relatorio(m, history_file=_hist_inexistente(tmp))
            chaves = {"resumo", "trust_dist", "reuso_por_tag", "orfaos",
                      "top_miss", "sugestoes"}
            res = r["resumo"]
            return (
                chaves <= set(r)
                and {"total_registros", "com_reuso", "sem_reuso", "orfaos",
                     "taxa_reuso"} <= set(res)
                and res["total_registros"] == 3
                and isinstance(r["reuso_por_tag"], list)
                and isinstance(r["orfaos"], list)
                and isinstance(r["top_miss"], list)
                and isinstance(r["sugestoes"], list)
                and bool(r["metrica"])  # heurística documentada no relatório
            )


def _caso_b() -> bool:
    """(b) órfãos = registros fracos sem reuso; fortes/médios nunca órfãos;
    marcados candidato_a_poda."""
    with tempfile.TemporaryDirectory() as tmp:
        with _memoria_tmp(tmp) as m:
            _popula(m)
            r = gerar_relatorio(m, history_file=_hist_inexistente(tmp))
            ids_orfaos = {o["id"] for o in r["orfaos"]}
            fracos = {rec["id"] for rec in m.list_records()
                      if trust_de(rec["meta"]) == "fraca"}
            nao_fracos = {rec["id"] for rec in m.list_records()
                          if trust_de(rec["meta"]) != "fraca"}
            return (
                ids_orfaos == fracos  # exatamente os fracos sem reuso
                and bool(ids_orfaos)  # há ao menos um órfão no cenário
                and all(o["candidato_a_poda"] is True for o in r["orfaos"])
                and all(o["trust"] == "fraca" for o in r["orfaos"])
                and not (ids_orfaos & nao_fracos)
                # o fraco é também isolado vocabularmente (nunca recuperável)
                and all(o["isolada_vocabularmente"] is True for o in r["orfaos"])
            )


def _caso_c() -> bool:
    """(c) somente leitura: gerar_relatorio + salvar_relatorio não modificam
    registros (mesma contagem e mesmo conteúdo de arquivos)."""
    with tempfile.TemporaryDirectory() as tmp:
        with _memoria_tmp(tmp) as m:
            _popula(m)
            antes = {rec["id"]: rec["file"] for rec in m.list_records()}
            conteudo_antes = {
                pathlib.Path(f).read_bytes() for f in antes.values()
            }
            r = gerar_relatorio(m, history_file=_hist_inexistente(tmp))
            destino = pathlib.Path(tmp) / "eval"
            salvar_relatorio(m, destino)
            depois = m.list_records()
            conteudo_depois = {
                pathlib.Path(rec["file"]).read_bytes() for rec in depois
            }
            return (
                len(depois) == len(antes)
                and {rec["id"] for rec in depois} == set(antes)
                and conteudo_antes == conteudo_depois  # bytes idênticos
                and r["resumo"]["total_registros"] == len(antes)
            )


def _caso_d() -> bool:
    """(d) salvar_relatorio grava Markdown em <destino>/<data>-memoria.md com
    as seções esperadas e retorna o caminho."""
    with tempfile.TemporaryDirectory() as tmp:
        with _memoria_tmp(tmp) as m:
            _popula(m)
            destino = pathlib.Path(tmp) / "eval"
            caminho = salvar_relatorio(m, destino)
            texto = caminho.read_text(encoding="utf-8")
            esperado = ["Resumo", "Métrica", "Distribuição por trust",
                        "Reuso por tag", "Órfãos candidatos a poda",
                        "Top consultas com miss", "Sugestões"]
            return (
                caminho.parent == destino
                and caminho.name == f"{datetime.date.today().isoformat()}-memoria.md"
                and caminho.is_file()
                and all(sec in texto for sec in esperado)
            )


def _caso_e() -> bool:
    """(e) tolera histórico ausente e histórico inválido (não quebra)."""
    with tempfile.TemporaryDirectory() as tmp:
        with _memoria_tmp(tmp) as m:
            _popula(m)
            ausente = gerar_relatorio(m, history_file=_hist_inexistente(tmp))
            invalido = pathlib.Path(tmp) / "history_invalido.json"
            invalido.write_text("{json quebrado", encoding="utf-8")
            quebrado = gerar_relatorio(m, history_file=invalido)
            nao_lista = pathlib.Path(tmp) / "history_nao_lista.json"
            nao_lista.write_text('{"command": "x"}', encoding="utf-8")
            sem_lista = gerar_relatorio(m, history_file=nao_lista)
            return (
                "resumo" in ausente and "resumo" in quebrado and "resumo" in sem_lista
                # sem histórico, restam só as keywords de amostra (3)
                and ausente["consultas_avaliadas"] == 3
                and quebrado["consultas_avaliadas"] == ausente["consultas_avaliadas"]
                and sem_lista["consultas_avaliadas"] == ausente["consultas_avaliadas"]
            )


def _caso_f() -> bool:
    """(f) top_miss: consultas do histórico com 0 resultados viram miss,
    contando repetições (n_misses) — mas apenas comandos com status de
    execução real (A4): `blocked`/`started` não contam como miss de memória."""
    with tempfile.TemporaryDirectory() as tmp:
        with _memoria_tmp(tmp) as m:
            _popula(m)
            hist = pathlib.Path(tmp) / "history.json"
            hist.write_text(json.dumps([
                {"command": "pip install frobnicate-quantum", "status": "finished"},
                {"command": "pip install frobnicate-quantum", "status": "finished"},
                {"command": "pip install frobnicate-quantum", "status": "blocked"},
                {"command": "pip install frobnicate-quantum", "status": "started"},
            ]), encoding="utf-8")
            r = gerar_relatorio(m, history_file=hist)
            alvo = [x for x in r["top_miss"]
                    if "frobnicate" in x["consulta"]]
            return (
                len(alvo) == 1
                and alvo[0]["n_misses"] == 2  # só os 2 "finished" contam (A4)
                and alvo[0]["origem"] == "historico"
            )


def _caso_g() -> bool:
    """(g) memória vazia não quebra: total 0, taxa 0, sugestão clara."""
    with tempfile.TemporaryDirectory() as tmp:
        with _memoria_tmp(tmp) as m:
            r = gerar_relatorio(m, history_file=_hist_inexistente(tmp))
            return (
                r["resumo"]["total_registros"] == 0
                and r["resumo"]["taxa_reuso"] == 0.0
                and r["resumo"]["orfaos"] == 0
                and any("vazia" in s for s in r["sugestoes"])
            )


def _caso_h() -> bool:
    """(h) REGRESSÃO DO A1 — memória MAIOR que o top-N de exibição: o
    veredito de órfãos/reuso NÃO pode variar com o limit (10, 1 ou 0). Na
    implementação antiga, `search(limit=10)` truncava resultados e o registro
    fraco B (recuperado apenas via título, rank baixo) virava órfão com
    limit=1 mas não com limit=10 — falso órfão por truncamento."""
    with tempfile.TemporaryDirectory() as tmp:
        with _memoria_tmp(tmp) as m:
            _popula(m)
            # B é fraco e só é recuperado pela consulta "python" via TÍTULO
            # (score 2 < 3 da kw do A) — exatamente o caso que o top-N
            # truncado perdia. B fica fraco de propósito (status sem trust).
            config.EPISODIC_DIR.mkdir(parents=True, exist_ok=True)
            (config.EPISODIC_DIR / "reg-b-fraco-2026-08-19.md").write_text(
                "---\nid: reg-b-fraco-2026-08-19\n"
                "keywords: [testes]\ndata: 2026-08-19\n"
                "agente: hub\nstatus: completed\n---\n\n"
                "# validacao com python\n\nconteudo\n",
                encoding="utf-8",
            )
            # Amplia para 13 registros (> MAX_EVAL_TOP=10) compartilhando
            # vocabulário — garante que o top-N de exibição é realmente
            # menor que a memória.
            for i in range(9):
                m.record(keywords=[f"extra{i:02d}"], agente="hub",
                         tema=f"fluxo extra {i} com python", entrada="",
                         fluxo="", resultado="", contexto="", trust="media",
                         origem="caso h", validado_por="motor")
            r1 = gerar_relatorio(m, history_file=_hist_inexistente(tmp), limit=1)
            r10 = gerar_relatorio(m, history_file=_hist_inexistente(tmp), limit=10)
            r0 = gerar_relatorio(m, history_file=_hist_inexistente(tmp), limit=0)
            orfaos = lambda r: {o["id"] for o in r["orfaos"]}
            return (
                orfaos(r1) == orfaos(r10) == orfaos(r0)  # A1: estável
                and r1["resumo"] == r10["resumo"] == r0["resumo"]
                and r0["resumo"]["total_registros"] == 13
                and bool(r0["reuso_por_tag"])  # A8: limit=0 = sem topo (lista cheia)
            )


def _caso_i() -> bool:
    """(i) A5: comandos SEM tokens (cd, ls) NÃO viram miss falsa no top_miss;
    e comandos bloqueados NÃO contam como miss (A4, ver _caso_f)."""
    with tempfile.TemporaryDirectory() as tmp:
        with _memoria_tmp(tmp) as m:
            _popula(m)
            hist = pathlib.Path(tmp) / "history.json"
            hist.write_text(json.dumps([
                {"command": "cd", "status": "finished"},
                {"command": "ls", "status": "finished"},
                {"command": "x", "status": "blocked"},
                {"command": "comando totalmente estranho xyzzy", "status": "finished"},
                {"command": "comando totalmente estranho xyzzy", "status": "blocked"},
            ]), encoding="utf-8")
            r = gerar_relatorio(m, history_file=hist)
            alvo = [x for x in r["top_miss"] if "xyzzy" in x["consulta"]]
            return (
                len(alvo) == 1 and alvo[0]["n_misses"] == 1  # só o finished
                and all("cd" not in x["consulta"] and "ls" not in x["consulta"]
                        for x in r["top_miss"])  # sem-token nunca vira miss
            )


def _caso_j() -> bool:
    """(j) A7: keywords com aspas literais no frontmatter são normalizadas
    (sem `"`/`'` nas tags do relatório e nas consultas de amostra)."""
    with tempfile.TemporaryDirectory() as tmp:
        with _memoria_tmp(tmp) as m:
            config.EPISODIC_DIR.mkdir(parents=True, exist_ok=True)
            (config.EPISODIC_DIR / "reg-aspas-2026-08-19.md").write_text(
                '---\nid: reg-aspas-2026-08-19\n'
                'keywords: ["seguranca", \'validacao\']\ndata: 2026-08-19\n'
                "agente: web\nstatus: completed\n---\n\n"
                "# tema aspas\n\ncorpo\n",
                encoding="utf-8",
            )
            r = gerar_relatorio(m, history_file=_hist_inexistente(tmp))
            tags = {t["tag"] for t in r["reuso_por_tag"]}
            return (
                "seguranca" in tags and "validacao" in tags
                and not any('"' in t or "'" in t for t in tags)
            )


def _caso_k() -> bool:
    """(k) P7 — PARIDADE: `_hits_por_consulta` (a varredura de scoring do
    eval_memory) deve coincidir com `Memory.search()` para a MESMA consulta e
    memória: o conjunto de hits (score > 0) é idêntico. Se memory.py mudar o
    scoring, o teste detecta a divergência entre as duas implementações."""
    with tempfile.TemporaryDirectory() as tmp:
        with _memoria_tmp(tmp) as m:
            _popula(m)
            records = m.list_records()
            consultas = [
                ("python", "amostra", 1),
                ("testes", "amostra", 1),
                ("fluxo", "amostra", 1),
                ("zzz-unica", "amostra", 1),
                ("quilombera", "amostra", 1),
            ]
            hits = _hits_por_consulta(records, consultas)
            for q, _o, _n in consultas:
                res = m.search(q, limit=max(len(records), 1))
                ids_search = {r["id"] for r in res}
                if ids_search != hits[q]:
                    return False
            return True


def _caso_l() -> bool:
    """(l) P7 — `limit` NEGATIVO (ex.: -1) é ILIMITADO: não trunca errado
    (`ranked[:-1]`/`top_miss[:-1]`). O relatório com limit=-1 deve ser igual
    ao de limit=0 (sem topo), nunca descartar o último item da lista."""
    with tempfile.TemporaryDirectory() as tmp:
        with _memoria_tmp(tmp) as m:
            _popula(m)
            r_neg = gerar_relatorio(m, history_file=_hist_inexistente(tmp), limit=-1)
            r_zero = gerar_relatorio(m, history_file=_hist_inexistente(tmp), limit=0)
            return (
                r_neg["reuso_por_tag"] == r_zero["reuso_por_tag"]
                and r_neg["top_miss"] == r_zero["top_miss"]
                and r_neg["resumo"] == r_zero["resumo"]
                and bool(r_neg["reuso_por_tag"])  # lista completa preservada
            )


def _caso_m() -> bool:
    """(m) M2 — PARIDADE com `Memory.search`: registro `recuperavel: false`
    (alerta de fabricação) NÃO pode contar como reutilizado. A varredura
    `_hits_por_consulta` deve excluí-lo, como `Memory.search` sempre faz. Um
    gêmeo recuperável com as mesmas keywords PROVA que a consulta casaria os
    dois sem o filtro — assim o teste detecta a regressão."""
    with tempfile.TemporaryDirectory() as tmp:
        with _memoria_tmp(tmp) as m:
            config.EPISODIC_DIR.mkdir(parents=True, exist_ok=True)
            (config.EPISODIC_DIR / "reg-fab-2026-09-11.md").write_text(
                "---\nid: reg-fab-2026-09-11\nkeywords: [fabricacao, toxica]\n"
                "data: 2026-09-11\nagente: webscraper\nstatus: fabricacao\n"
                "trust: fraca\nrecuperavel: false\n---\n\n"
                "# Fabricacao toxica\n\nconteudo\n",
                encoding="utf-8",
            )
            (config.EPISODIC_DIR / "reg-ok-2026-09-11.md").write_text(
                "---\nid: reg-ok-2026-09-11\nkeywords: [fabricacao]\n"
                "data: 2026-09-11\nagente: webscraper\nstatus: completed\n"
                "trust: media\n---\n\n# Tema ok\n\nconteudo\n",
                encoding="utf-8",
            )
            hist = pathlib.Path(tmp) / "history.json"
            hist.write_text(json.dumps([
                {"command": "fabricacao toxica", "status": "finished"},
            ]), encoding="utf-8")
            consulta = "fabricacao toxica"
            hits = _hits_por_consulta(m.list_records(), [(consulta, "historico", 1)])
            r = gerar_relatorio(m, history_file=hist)
            return (
                "reg-fab-2026-09-11" not in hits[consulta]  # M2: excluído
                and "reg-ok-2026-09-11" in hits[consulta]  # gêmeo é hit
                and r["resumo"]["total_registros"] == 2
                and r["resumo"]["com_reuso"] == 1  # só o recuperável
            )


CASES = [
    ("gerar_relatorio: chaves esperadas + resumo completo (a)", _caso_a),
    ("órfãos = registros fracos sem reuso, candidato_a_poda (b)", _caso_b),
    ("somente leitura: registros intactos após relatório (c)", _caso_c),
    ("salvar_relatorio grava Markdown e retorna caminho (d)", _caso_d),
    ("tolerância a histórico ausente/inválido/não-lista (e)", _caso_e),
    ("top_miss: consulta do histórico com 0 resultados (f)", _caso_f),
    ("memória vazia não quebra (g)", _caso_g),
    ("memória > limit: órfãos estáveis em limit=1/10/0 (h) [A1/A8]", _caso_h),
    ("comandos sem tokens/bloqueados não viram miss (i) [A5/A4]", _caso_i),
    ("keywords com aspas normalizadas no relatório (j) [A7]", _caso_j),
    ("paridade: _hits_por_consulta == Memory.search (k) [P7]", _caso_k),
    ("limit negativo = ilimitado, igual a limit=0 (l) [P7]", _caso_l),
    ("recuperavel: false nao conta como reutilizado (m) [M2]", _caso_m),
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
