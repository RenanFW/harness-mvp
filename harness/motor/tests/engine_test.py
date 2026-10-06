"""Suíte autônoma de testes das Etapas 1 e 2 (padrão [PASS]/[FAIL]).

Execução:
    python tests/engine_test.py

Etapa 1 cobre: embedding (dimensão/normalização/determinismo/robustez),
cosseno, VectorStore (roundtrip, integridade, hit_count, stats, corrupção
tolerada), abstração AST (placeholders, inválido, f-strings), interpolação,
sandbox (exec, bloqueio, timeout/kill real), engine (fast-path, miss, LLM
stub, zero-poisoning, test_cases, safe_mode), ARCCache e CLI.

Etapa 2 adiciona: LLMClient (offline -> LLMError; http com servidor local
fake), schema (validate_code_response, default_validation_schema,
pydantic_validate), engine com LLMClient real (fast-path na 2ª chamada),
zero-poisoning de código destrutivo vindo do LLM, engine.stats, ARC
integrado ao engine (put + ensure_disk_limit) e benchmark (3/3 OK).

Isolamento: cada caso usa TemporaryDirectory para o banco e o workdir do
sandbox (monkeypatch de config.DB_PATH/DATA_DIR apenas no caso CLI) — o
data/ real do projeto NUNCA é tocado.
"""

from __future__ import annotations

import asyncio
import contextlib
import http.server
import importlib.util
import io
import json
import pathlib
import sqlite3
import struct
import sys
import tempfile
import threading
import time
from dataclasses import asdict
from unittest import mock

# ------------------------------------------------------------------ pacote
# O motor agora é um subpackage do harness (harness.motor) — importável por
# nome Python válido (sem o hífen da antiga pasta sidePrjs).
ROOT = pathlib.Path(__file__).resolve().parent.parent.parent.parent
if str(ROOT) not in sys.path:
    sys.path.insert(0, str(ROOT))

from harness.motor import (  # noqa: E402
    ARCCache,
    AgenticHarnessEngine,
    LocalEmbedder,
    SandboxRunner,
    VectorStore,
    abstract_to_template,
    config,
    interpolate_template,
)
from harness.motor import engine as engine_mod  # noqa: E402
from harness.motor.llm_client import (  # noqa: E402
    LLMClient,
    LLMError,
)
from harness.motor.models import (  # noqa: E402
    ArtifactRecord,
    ExecutionResult,
)
from harness.motor.schema import (  # noqa: E402
    default_validation_schema,
    pydantic_validate,
    validate_code_response,
)

DIM = config.EMBEDDING_DIM
_FMT = f"<{DIM}f"


# ------------------------------------------------------------------ helpers
def rodar(coro) -> ExecutionResult:
    return asyncio.run(coro)


class _Ambiente:
    """Ambiente isolado: banco e sandbox em TemporaryDirectory."""

    def __init__(self, threshold: float = 0.92, llm=None):
        self.tmp = tempfile.TemporaryDirectory(prefix="ahs_case_")
        self.db = pathlib.Path(self.tmp.name) / "cache.db"
        self.embedder = LocalEmbedder()
        self.store = VectorStore(self.db)
        self.sandbox = SandboxRunner(workdir=self.tmp.name, force_mode="exec")
        self.engine = AgenticHarnessEngine(
            self.embedder, self.store, self.sandbox,
            llm_client=llm, threshold=threshold,
        )

    def __enter__(self):
        return self

    def __exit__(self, *exc):
        self.tmp.cleanup()


class _StubLLM:
    """Stub da interface LLM (Etapa 2): generate(task, domain, schema) -> código."""

    def __init__(self, codigo: str = "print()"):
        self.codigo = codigo

    def generate(self, task: str, domain: str | None = None, schema: dict | None = None) -> str:
        return self.codigo


class _FakeLLMClient:
    """Fake de LLMClient (mode "http") para o engine — sem rede real."""

    mode = "http"

    def __init__(self, codigo: str = "print()"):
        self.codigo = codigo

    def generate(self, task: str, domain: str | None = None, validation_schema: dict | None = None) -> str:
        return self.codigo


class _SandboxModoFake:
    """Delega a execução ao runner real, mas reporta um `mode` arbitrário.

    Usado nos testes do gate MOTOR_REQUER_CONTAINER (Item 7.1) para simular um
    modo de container (docker/podman) sem depender de docker/podman reais: a
    execução de fato continua acontecendo no runner `exec`."""

    def __init__(self, real, modo: str):
        self._real = real
        self._mode = modo

    @property
    def mode(self) -> str:
        return self._mode

    def run(self, code, timeout=None):
        return self._real.run(code, timeout)


class _ContadorHandler(http.server.BaseHTTPRequestHandler):
    """Servidor fake: responde {"code": ...} e conta POSTs (zero HTTP extra)."""

    chamadas = 0
    RESPOSTA = {"code": "print('fake http ok')"}

    def do_POST(self):  # noqa: N802 — assinatura do BaseHTTPRequestHandler
        _ContadorHandler.chamadas += 1
        try:
            tamanho = int(self.headers.get("Content-Length", 0) or 0)
            if tamanho:
                self.rfile.read(tamanho)
        except (ValueError, OSError):
            pass
        corpo = json.dumps(self.RESPOSTA).encode("utf-8")
        self.send_response(200)
        self.send_header("Content-Type", "application/json")
        self.send_header("Content-Length", str(len(corpo)))
        self.end_headers()
        self.wfile.write(corpo)

    def log_message(self, *args):  # noqa: ARG002 — silencioso
        pass


@contextlib.contextmanager
def _servidor_fake():
    """Servidor HTTP local em thread; yield a porta escolhida pelo SO."""
    servidor = http.server.ThreadingHTTPServer(("127.0.0.1", 0), _ContadorHandler)
    porta = servidor.server_address[1]
    thread = threading.Thread(target=servidor.serve_forever, daemon=True)
    thread.start()
    try:
        yield porta
    finally:
        servidor.shutdown()
        servidor.server_close()


def _salvar(amb: _Ambiente, rid: str, texto: str, created_at: str = "") -> str:
    """Salva um artefato simples (payload sem placeholders) e retorna o id."""
    vetor = amb.embedder.generate_embedding(texto)
    rec = ArtifactRecord(
        id=rid,
        intent_vector=vetor,
        domain="code_gen",
        execution_payload={
            "template": "print('ok')",
            "placeholders": {},
            "ast_valid": True,
        },
        validation_schema={},
        created_at=created_at or time.strftime("%Y-%m-%dT%H:%M:%S+00:00"),
    )
    return amb.store.save(rec)


# ------------------------------------------------------------------ casos
CASES: list[tuple[str, object]] = []


def caso(nome: str):
    def deco(fn):
        CASES.append((nome, fn))
        return fn

    return deco


@caso("embedding_dimensao_e_normalizacao")
def _():
    amb = _Ambiente()
    vetor = amb.embedder.generate_embedding("criar script de backup")
    assert len(vetor) == DIM == 384, f"len={len(vetor)}"
    norma = sum(x * x for x in vetor) ** 0.5
    assert abs(norma - 1.0) < 1e-6, f"norma={norma}"
    amb.__exit__(None, None, None)


@caso("embedding_deterministico")
def _():
    amb = _Ambiente()
    v1 = amb.embedder.generate_embedding("mesmo texto de exemplo")
    v2 = amb.embedder.generate_embedding("mesmo texto de exemplo")
    assert v1 == v2, "mesmo texto deve gerar o mesmo vetor (cache determinístico)"
    amb.__exit__(None, None, None)


@caso("embedding_vazio_none_nao_crasha")
def _():
    amb = _Ambiente()
    vazio = amb.embedder.generate_embedding("")
    assert len(vazio) == DIM and all(x == 0.0 for x in vazio), "vazio -> zeros"
    nulo = amb.embedder.generate_embedding(None)  # type: ignore[arg-type]
    assert len(nulo) == DIM and all(x == 0.0 for x in nulo), "None -> zeros"
    amb.__exit__(None, None, None)


@caso("cosine_basico")
def _():
    amb = _Ambiente()
    v = amb.embedder.generate_embedding("texto identico a si mesmo")
    assert abs(amb.embedder.cosine(v, v) - 1.0) < 1e-9, "cosseno de si mesmo = 1"
    outro = amb.embedder.generate_embedding("invasao firewall rede exfiltracao")
    sim = amb.embedder.cosine(v, outro)
    assert 0.0 <= sim < 1.0, f"textos distintos: 0 <= {sim} < 1"
    assert amb.embedder.cosine([1.0, 2.0], [1.0]) == 0.0, "dims diferentes -> 0"
    assert amb.embedder.cosine([], []) == 0.0, "vazios -> 0"
    amb.__exit__(None, None, None)


@caso("embedder_mode_valido")
def _():
    amb = _Ambiente()
    assert amb.embedder.mode in {"onnx", "fallback", "safe"}, amb.embedder.mode
    amb.__exit__(None, None, None)


@caso("store_save_retorna_id")
def _():
    amb = _Ambiente()
    rid = _salvar(amb, "", "tarefa qualquer")
    assert isinstance(rid, str) and rid, "save deve retornar id str"
    assert amb.store.count() == 1
    amb.__exit__(None, None, None)


@caso("store_roundtrip_find_nearest")
def _():
    amb = _Ambiente()
    rid = _salvar(amb, "a1", "gerar funcao de soma numerica")
    vetor = amb.embedder.generate_embedding("gerar funcao de soma numerica")
    rec = amb.store.find_nearest(vetor, threshold=0.1)
    assert rec is not None, "threshold baixo deve achar"
    assert rec.id == rid, "roundtrip deve retornar o mesmo registro"
    assert rec.execution_payload["template"] == "print('ok')"
    amb.__exit__(None, None, None)


@caso("store_threshold_alto_none")
def _():
    amb = _Ambiente()
    _salvar(amb, "a1", "soma de numeros inteiros")
    vetor_outro = amb.embedder.generate_embedding("invasao firewall rede exfiltracao")
    rec = amb.store.find_nearest(vetor_outro, threshold=0.9999)
    assert rec is None, "threshold alto com vetor distinto -> None"
    amb.__exit__(None, None, None)


@caso("store_integrity_check_true")
def _():
    amb = _Ambiente()
    _salvar(amb, "a1", "qualquer tarefa")
    assert amb.store.integrity_check() is True
    amb.__exit__(None, None, None)


@caso("store_increment_hit_count_e_stats")
def _():
    amb = _Ambiente()
    _salvar(amb, "a1", "tarefa de patching de seguranca")
    assert amb.store.increment_hit_count("a1") is True
    assert amb.store.increment_hit_count("inexistente") is False
    vetor = amb.embedder.generate_embedding("tarefa de patching de seguranca")
    rec = amb.store.find_nearest(vetor, 0.1)
    assert rec is not None and rec.hit_count == 1
    st = amb.store.stats()
    assert st["total"] == 1
    assert st["por_domain"].get("code_gen") == 1
    assert st["hits_totais"] == 1
    assert st["db_bytes"] > 0
    amb.__exit__(None, None, None)


@caso("store_registro_corrompido_nao_derruba")
def _():
    amb = _Ambiente()
    rid = _salvar(amb, "bom", "texto comum de exemplo")
    vetor = amb.embedder.generate_embedding("texto comum de exemplo")
    # registro corrompido (payload não-JSON) inserido direto no SQLite
    conn = sqlite3.connect(amb.db)
    try:
        conn.execute(
            "INSERT INTO artifacts (id, intent_vector, domain, execution_payload,"
            " validation_schema, hit_count, created_at) VALUES (?, ?, ?, ?, ?, 0, ?)",
            ("ruim", struct.pack(_FMT, *vetor), "code_gen",
             "{{{ json quebrado", "{}", "2026-01-01T00:00:00+00:00"),
        )
        conn.commit()
    finally:
        conn.close()
    rec = amb.store.find_nearest(vetor, 0.1)
    assert rec is not None and rec.id == rid, "corrompido é ignorado (log e segue)"
    amb.__exit__(None, None, None)


@caso("ast_literais_placeholders")
def _():
    r = abstract_to_template("x = 1\ny = 'oi'\nz = 2.5\nb = True\nn = None")
    assert r["ast_valid"] is True
    assert "{{int_1}}" in r["template"]
    assert r["placeholders"]["int_1"] == "int"
    assert r["placeholders"]["str_1"] == "str"
    assert r["placeholders"]["float_1"] == "float"
    assert r["placeholders"]["bool_1"] == "bool"
    assert r["placeholders"]["none_1"] == "none"


@caso("ast_invalido_ast_valid_false")
def _():
    r = abstract_to_template("def f(:")
    assert r["ast_valid"] is False
    assert r["template"] == "def f(:"
    assert r["placeholders"] == {}


@caso("ast_fstring_nao_quebrada")
def _():
    r = abstract_to_template("print(f'x={1}')")
    assert r["ast_valid"] is True
    assert r["placeholders"] == {}, "literais dentro de f-string não são abstraídos"
    assert r["template"] == "print(f'x={1}')"


@caso("interpolate_com_valores")
def _():
    t = abstract_to_template("print('oi')\nx = 5")
    codigo = interpolate_template(t, {"str_1": "tchau", "int_1": 7})
    assert "print('tchau')" in codigo
    assert "x = 7" in codigo
    # template ast_valid False retorna o original intacto
    inv = abstract_to_template("def f(:")
    assert interpolate_template(inv, {}) == "def f(:"


@caso("interpolate_variavel_ausente_valueerror")
def _():
    t = abstract_to_template("print('oi')\nx = 5")
    try:
        interpolate_template(t, {"str_1": "x"})
        raise AssertionError("deveria levantar ValueError")
    except ValueError:
        pass


@caso("sandbox_exec_codigo_ok_exit0")
def _():
    amb = _Ambiente()
    r = amb.sandbox.run("print('hello sandbox')")
    assert r.exit_code == 0, f"exit={r.exit_code}"
    assert r.success is True
    assert "hello sandbox" in r.output
    assert r.executed_locally is True
    amb.__exit__(None, None, None)


@caso("sandbox_bloqueio_rm_rf")
def _():
    amb = _Ambiente()
    r = amb.sandbox.run("import os\nos.system('rm -rf /tmp/alvo')")
    assert r.exit_code == 1, "bloqueado -> exit 1 (convenção)"
    assert r.success is False
    assert "bloqueado" in (r.error or "").lower()
    amb.__exit__(None, None, None)


@caso("sandbox_timeout_kill_exit_nao_zero")
def _():
    amb = _Ambiente()
    inicio = time.perf_counter()
    r = amb.sandbox.run("import time\ntime.sleep(60)", timeout=1.5)
    decorrido = time.perf_counter() - inicio
    assert r.exit_code != 0, "timeout deve matar o processo (exit != 0)"
    assert r.success is False
    assert decorrido < 15, f"kill real: levou {decorrido:.1f}s"
    amb.__exit__(None, None, None)


@caso("engine_fast_path_hit")
def _():
    amb = _Ambiente(threshold=0.1)
    vetor = amb.embedder.generate_embedding("gerar funcao de soma")
    amb.store.save(ArtifactRecord(
        id="fast1", intent_vector=vetor, domain="code_gen",
        execution_payload={
            "template": "print('ok')", "placeholders": {}, "ast_valid": True,
        },
        validation_schema={},
    ))
    r = rodar(amb.engine.execute_task("gerar funcao de soma", "code_gen"))
    assert r.success is True
    assert r.executed_locally is True, "cache hit deve rodar local"
    assert r.exit_code == 0
    rec = amb.store.find_nearest(vetor, 0.1)
    assert rec is not None and rec.hit_count == 1, "hit_count incrementado"
    amb.__exit__(None, None, None)


@caso("engine_replay_falho_zera_hit_count_e_marca_revalidacao")
def _():
    # achado A1: replay no fast-path com exit != 0 -> invalidate() ANTES de
    # retornar (hit_count zerado + needs_revalidation=1), nada persiste.
    amb = _Ambiente(threshold=0.1)
    vetor = amb.embedder.generate_embedding("tarefa com replay quebrado")
    amb.store.save(ArtifactRecord(
        id="replay1", intent_vector=vetor, domain="code_gen",
        execution_payload={
            "template": "raise RuntimeError('boom replay')",
            "placeholders": {}, "ast_valid": True,
        },
        validation_schema={},
    ))
    # artefato quente antes do replay falho (contador > 0)
    assert amb.store.increment_hit_count("replay1") is True
    r = rodar(amb.engine.execute_task("tarefa com replay quebrado", "code_gen"))
    assert r.success is False
    assert r.executed_locally is True
    assert r.artifact_id == "replay1"
    assert r.error == "artefato local falhou; revalidacao pendente"
    # nada é persistido/removido: o MESMO artefato continua no banco
    assert amb.store.count() == 1
    assert amb.store.pending_revalidation() == ["replay1"]
    conn = sqlite3.connect(amb.db)
    try:
        linha = conn.execute(
            "SELECT hit_count, needs_revalidation FROM artifacts "
            "WHERE id = 'replay1'"
        ).fetchone()
    finally:
        conn.close()
    assert linha is not None, "artefato permanece no banco"
    assert linha[0] == 0, f"hit_count zerado, veio {linha[0]}"
    assert linha[1] == 1, f"needs_revalidation=1, veio {linha[1]}"
    amb.__exit__(None, None, None)


@caso("store_find_nearest_ignora_revalidado")
def _():
    # achado A1: find_nearest filtra needs_revalidation=1; um registro
    # saudável continua encontrável normalmente.
    amb = _Ambiente()
    vetor = amb.embedder.generate_embedding("tarefa de teste revalidacao")
    _salvar(amb, "bom1", "tarefa de teste revalidacao")
    assert amb.store.invalidate("bom1") is True
    assert amb.store.invalidate("inexistente") is False
    assert amb.store.pending_revalidation() == ["bom1"]
    rec = amb.store.find_nearest(vetor, 0.1)
    assert rec is None, "registro invalidado deve ser IGNORADO pelo fast-path"
    _salvar(amb, "bom2", "tarefa de teste revalidacao")
    rec2 = amb.store.find_nearest(vetor, 0.1)
    assert rec2 is not None and rec2.id == "bom2", "registro saudável encontrado"
    amb.__exit__(None, None, None)


@caso("engine_revalidar_pendentes_fecha_dead_end")
def _():
    # A1: revalidação fecha o dead-end — artefato que passa volta ao
    # fast-path (clear_revalidation); que falha é apagado (delete).
    amb = _Ambiente()
    _salvar(amb, "ok1", "tarefa revalidacao a")
    _salvar(amb, "quebrado", "tarefa revalidacao b")
    # quebra o artefato "quebrado" (payload passa a falhar no sandbox) ANTES
    # de invalidar (save() reseta needs_revalidation -> ordem importa)
    rec = amb.store.get("quebrado")
    rec.execution_payload = {
        "template": "raise RuntimeError('boom')",
        "placeholders": {},
        "ast_valid": True,
    }
    amb.store.save(rec)
    assert amb.store.invalidate("ok1") is True
    assert amb.store.invalidate("quebrado") is True
    res = amb.engine.revalidar_pendentes(domain="code_gen")
    assert res["revalidados"] == 1, res
    assert res["apagados"] == 1, res
    assert amb.store.get("ok1") is not None, "ok1 voltou ao fast-path"
    assert amb.store.get("quebrado") is None, "quebrado apagado"
    assert amb.store.pending_revalidation() == []
    amb.__exit__(None, None, None)


@caso("engine_threshold_efetivo_por_fidelidade")
def _():
    # cache-first com embedder de baixa fidelidade (safe/fallback) exige
    # limiar MAIS ALTO (conservador anti false-hit); onnx usa o configurado.
    amb = _Ambiente(threshold=0.92)
    assert amb.embedder.mode in ("onnx", "fallback", "safe")
    if amb.embedder.mode in ("safe", "fallback"):
        assert amb.engine._threshold_efetivo() == 0.98, amb.engine._threshold_efetivo()
    else:
        assert amb.engine._threshold_efetivo() == 0.92
    amb.__exit__(None, None, None)


@caso("engine_miss_sem_llm")
def _():
    amb = _Ambiente()
    r = rodar(amb.engine.execute_task("tarefa totalmente nova", "code_gen"))
    assert r.success is False
    assert r.executed_locally is False
    assert r.error == "cache-miss sem LLM"
    assert amb.store.count() == 0
    amb.__exit__(None, None, None)


@caso("engine_llm_stub_persiste_segundo_call_fast_path")
def _():
    amb = _Ambiente(threshold=0.1, llm=_StubLLM("print()"))
    r1 = rodar(amb.engine.execute_task("tarefa unica com llm", "code_gen"))
    assert r1.success is True
    assert r1.executed_locally is False, "primeira chamada é miss (LLM)"
    assert r1.artifact_id is not None
    assert amb.store.count() == 1, "artefato persistido"
    # segunda chamada: mesmo embedding -> fast-path
    r2 = rodar(amb.engine.execute_task("tarefa unica com llm", "code_gen"))
    assert r2.success is True
    assert r2.executed_locally is True, "segunda chamada deve ser fast-path"
    assert r2.exit_code == 0
    amb.__exit__(None, None, None)


@caso("engine_zero_poisoning_nao_persiste")
def _():
    amb = _Ambiente(threshold=0.1, llm=_StubLLM("raise RuntimeError('boom')"))
    r = rodar(amb.engine.execute_task("tarefa com codigo ruim", "code_gen"))
    assert r.success is False
    assert "nada persistido" in (r.error or "")
    assert amb.store.count() == 0, "exit != 0 nunca é persistido (zero-poisoning)"
    amb.__exit__(None, None, None)


@caso("engine_test_cases_validacao")
def _():
    # test_case que falha -> nada é persistido
    amb = _Ambiente(threshold=0.1, llm=_StubLLM("print()"))
    r = rodar(amb.engine.execute_task(
        "t1", "code_gen", test_cases=[{"code": "raise ValueError('x')"}],
    ))
    assert r.success is False
    assert amb.store.count() == 0
    amb.__exit__(None, None, None)
    # test_case ok -> persiste
    amb2 = _Ambiente(threshold=0.1, llm=_StubLLM("print()"))
    r2 = rodar(amb2.engine.execute_task(
        "t2", "code_gen", test_cases=[{"code": "print('ok')"}],
    ))
    assert r2.success is True
    assert amb2.store.count() == 1
    amb2.__exit__(None, None, None)


@caso("engine_safe_mode")
def _():
    amb = _Ambiente()
    assert isinstance(amb.engine.safe_mode, bool)
    # sandbox forçado "exec" -> safe_mode True sempre neste ambiente
    assert amb.engine.safe_mode is True
    amb.__exit__(None, None, None)


@caso("arc_get_hit_miss_warm_e_evict")
def _():
    amb = _Ambiente()
    arc = ARCCache(amb.store, cap=4)
    assert arc.get("fantasma") is False, "miss não registra hit"
    rid = _salvar(amb, "a1", "tarefa arc de exemplo")
    arc.put(rid)
    assert arc.get(rid) is True, "hit promove T1 -> T2"
    assert arc.get(rid) is True, "hit em T2 mantém frequência"
    vetor = amb.embedder.generate_embedding("tarefa arc de exemplo")
    rec = amb.store.find_nearest(vetor, 0.1)
    assert rec is not None and rec.hit_count == 2, "frequência refletida no banco"
    assert arc.warm() >= 1, "warm reidrata ids do banco em T1"
    amb.__exit__(None, None, None)

    # eviction por disco: limite pequeno -> remove entradas + VACUUM
    amb2 = _Ambiente()
    arc2 = ARCCache(amb2.store)
    for i in range(3):
        _salvar(amb2, f"e{i}", f"tarefa evict {i}",
                created_at=f"2026-01-0{i + 1}T00:00:00+00:00")
    removidos = arc2.evict_if_over_disk(limit_bytes=200)
    assert removidos > 0, "entradas devem ser removidas acima do limite"
    assert amb2.store.count() < 3
    assert amb2.store.integrity_check() is True, "VACUUM preserva integridade"
    amb2.__exit__(None, None, None)


@caso("cli_main_dry_run_e_miss")
def _():
    with tempfile.TemporaryDirectory(prefix="ahs_cli_") as tmp:
        orig_db = config.DB_PATH
        orig_data = config.DATA_DIR
        config.DB_PATH = pathlib.Path(tmp) / "cache.db"
        config.DATA_DIR = pathlib.Path(tmp) / "data"
        try:
            buf = io.StringIO()
            with contextlib.redirect_stdout(buf):
                rc = engine_mod.main(
                    ["criar script de backup", "--domain", "code_gen", "--dry-run"]
                )
            saida = buf.getvalue()
            assert rc == 0
            dados = json.loads(saida)
            assert dados["dry_run"] is True
            assert dados["embedding_dim"] == 384
            # chave CLI UNIFICADA: `sandbox_modo` (não mais `sandbox_mode`)
            assert "sandbox_modo" in dados, "dry-run deve usar sandbox_modo"
            assert "sandbox_mode" not in dados, "chave antiga sandbox_mode removida"
            # execução normal sem LLM: miss -> JSON com success False
            buf2 = io.StringIO()
            with contextlib.redirect_stdout(buf2):
                rc2 = engine_mod.main(["outra tarefa nova", "--domain", "code_gen"])
            assert rc2 == 0
            saida2 = buf2.getvalue()
            assert "cache-miss sem LLM" in saida2
            # asdict() inclui sandbox_modo (agora campo REAL da dataclass)
            dados2 = json.loads(saida2)
            assert "sandbox_modo" in dados2, "CLI normal deve incluir sandbox_modo"
            assert "sandbox_mode" not in dados2
        finally:
            config.DB_PATH = orig_db
            config.DATA_DIR = orig_data


# ---------------------------------------------------------- Etapa 2 (novos)
@caso("llmclient_offline_generate_levanta_llmerror")
def _():
    cliente = LLMClient(endpoint=None)
    assert cliente.mode == "offline"
    try:
        cliente.generate("tarefa", "code_gen")
        raise AssertionError("modo offline deve levantar LLMError")
    except LLMError as exc:
        assert "offline" in str(exc).lower()


@caso("validate_code_response_valido")
def _():
    codigo = validate_code_response(
        {"code": "print('ok')", "explicacao": "exemplo"}
    )
    assert codigo == "print('ok')"
    assert validate_code_response({"code": "print(1)"}) == "print(1)"
    # pydantic_validate: sem schema_cls -> fallback dict (não altera os dados)
    dados = {"code": "print(1)"}
    assert pydantic_validate(dados, None) is dados


@caso("validate_code_response_invalido")
def _():
    invalidos = [
        "não é dict",
        {},
        {"code": ""},
        {"code": "   "},
        {"code": 42},
        {"code": None},
        {"code": "x", "explicacao": 42},
        {"code": "x", "explicacao": None},
    ]
    for dados in invalidos:
        try:
            validate_code_response(dados)
            raise AssertionError(f"deveria levantar ValueError: {dados!r}")
        except ValueError:
            pass


@caso("default_validation_schema_por_dominio")
def _():
    s = default_validation_schema("code_gen")
    assert "code" in s["required"]
    assert s["properties"]["code"]["type"] == "string"
    p = default_validation_schema("pentest")
    assert "target" in p["required"], "pentest exige target (placeholder)"
    assert default_validation_schema("patching")["required"] == ["code"]
    assert default_validation_schema("devsecops")["required"] == ["code"]
    try:
        default_validation_schema("dominio_inexistente")
        raise AssertionError("domínio desconhecido deve levantar ValueError")
    except ValueError:
        pass


@caso("llmclient_http_servidor_local_fake")
def _():
    _ContadorHandler.chamadas = 0
    with _servidor_fake() as porta:
        cliente = LLMClient(endpoint=f"http://127.0.0.1:{porta}/gerar", timeout=10.0)
        assert cliente.mode == "http"
        codigo = cliente.generate("tarefa de teste", "code_gen")
        assert codigo == "print('fake http ok')"
        assert _ContadorHandler.chamadas == 1, "uma única chamada HTTP"
        # o código retornado é executável no sandbox (usado de verdade)
        amb = _Ambiente()
        r = amb.sandbox.run(codigo)
        assert r.exit_code == 0, r.error
        assert "fake http ok" in r.output
        amb.__exit__(None, None, None)


@caso("engine_llmclient_fake_fast_path_segunda_chamada")
def _():
    # código sem literais: o template persistido não tem placeholders e o
    # replay (2ª chamada) interpola sem vars (contrato da Etapa 1)
    amb = _Ambiente(threshold=0.1, llm=_FakeLLMClient("print()"))
    r1 = rodar(amb.engine.execute_task("tarefa com llmclient fake", "code_gen"))
    assert r1.success is True, r1.error
    assert r1.executed_locally is False, "1ª chamada: miss -> LLM"
    assert amb.store.count() == 1, "artefato persistido"
    r2 = rodar(amb.engine.execute_task("tarefa com llmclient fake", "code_gen"))
    assert r2.success is True
    assert r2.executed_locally is True, "2ª chamada: fast-path local (>= 0.92)"
    assert r2.artifact_id == r1.artifact_id, "mesmo artefato reusado"
    amb.__exit__(None, None, None)


@caso("engine_zero_poisoning_rm_rf_nao_persiste")
def _():
    amb = _Ambiente(
        threshold=0.1,
        llm=_FakeLLMClient("import os\nos.system('rm -rf /tmp/alvo')"),
    )
    r = rodar(amb.engine.execute_task("tarefa com codigo destrutivo", "code_gen"))
    assert r.success is False
    assert "nada persistido" in (r.error or "")
    assert amb.store.count() == 0, "código bloqueado nunca é persistido"
    amb.__exit__(None, None, None)


@caso("engine_stats_hits_misses_hit_rate")
def _():
    amb = _Ambiente(threshold=0.1, llm=_FakeLLMClient("print()"))
    # 1 miss (LLM persiste) + 1 hit (fast-path local)
    rodar(amb.engine.execute_task("tarefa stats unica", "code_gen"))
    rodar(amb.engine.execute_task("tarefa stats unica", "code_gen"))
    st = amb.engine.stats()
    assert st["hits"] == 1, st
    assert st["misses"] == 1, st
    assert st["execucoes"] == 2
    assert abs(st["hit_rate"] - 0.5) < 1e-9
    assert st["avg_time_ms"] > 0.0
    # engine sem execução: zeros seguros
    amb2 = _Ambiente(threshold=0.1, llm=_FakeLLMClient("print()"))
    st0 = amb2.engine.stats()
    assert st0["hits"] == 0 and st0["misses"] == 0
    assert st0["execucoes"] == 0
    assert st0["hit_rate"] == 0.0 and st0["avg_time_ms"] == 0.0
    amb.__exit__(None, None, None)
    amb2.__exit__(None, None, None)


@caso("engine_cache_arc_put_apos_persistir")
def _():
    amb = _Ambiente(threshold=0.1, llm=_FakeLLMClient("print()"))
    arc = ARCCache(amb.store, cap=8)
    amb.engine.cache = arc
    r = rodar(amb.engine.execute_task("tarefa com arc no engine", "code_gen"))
    assert r.success is True and r.artifact_id is not None
    assert arc.get(r.artifact_id) is True, "artefato deve estar no ARC após save"
    assert amb.store.integrity_check() is True, "ensure_disk_limit preserva integridade"
    amb.__exit__(None, None, None)


@caso("engine_requer_container_nao_persiste_fora_de_container")
def _():
    # Item 7.1: com MOTOR_REQUER_CONTAINER=True e sandbox fora de container
    # ("exec"), o artefato validado NÃO é persistido e o resultado é explícito.
    amb = _Ambiente(threshold=0.1, llm=_StubLLM("print('ok')"))
    assert amb.sandbox.mode == "exec"
    with mock.patch.object(config, "MOTOR_REQUER_CONTAINER", True):
        r = rodar(amb.engine.execute_task("tarefa isolamento container", "code_gen"))
    assert r.success is False, r
    erro = r.error or ""
    assert "isolamento de container exigido" in erro, erro
    assert "nada persistido" in erro, erro
    assert getattr(r, "sandbox_modo", None) == "exec", getattr(r, "sandbox_modo", None)
    assert amb.store.count() == 0, "nada deve ser persistido fora de container"
    amb.__exit__(None, None, None)


@caso("engine_requer_container_persiste_com_modo_container")
def _():
    # Item 7.1: com MOTOR_REQUER_CONTAINER=True e modo de container simulado
    # (docker), o artefato validado É persistido normalmente.
    amb = _Ambiente(threshold=0.1, llm=_StubLLM("print('ok')"))
    amb.engine.sandbox = _SandboxModoFake(amb.sandbox, "docker")
    with mock.patch.object(config, "MOTOR_REQUER_CONTAINER", True):
        r = rodar(amb.engine.execute_task("tarefa container ok", "code_gen"))
    assert r.success is True, r.error
    assert getattr(r, "sandbox_modo", None) == "docker"
    assert amb.store.count() == 1, "container deve persistir"
    amb.__exit__(None, None, None)


@caso("engine_requer_container_default_persiste")
def _():
    # Item 7.1: default (False) preserva o comportamento — persiste normalmente
    # mesmo com sandbox "exec", expondo o modo no resultado.
    amb = _Ambiente(threshold=0.1, llm=_StubLLM("print('ok')"))
    with mock.patch.object(config, "MOTOR_REQUER_CONTAINER", False):
        r = rodar(amb.engine.execute_task("tarefa default persiste", "code_gen"))
    assert r.success is True, r.error
    assert getattr(r, "sandbox_modo", None) == "exec"
    assert amb.store.count() == 1, "default deve persistir"
    amb.__exit__(None, None, None)


@caso("engine_requer_container_cache_hit_exec_nao_serve")
def _():
    # R1 (achado MÉDIA): o gate MOTOR_REQUER_CONTAINER vale TAMBÉM no
    # fast-path (cache-hit). Com flag True e sandbox "exec", o artefato NÃO é
    # executado no host, NÃO incrementa hit_count e NÃO é invalidado — o store
    # fica intacto (sem mutação) e o resultado é explícito.
    amb = _Ambiente(threshold=0.1)
    _salvar(amb, "hitct1", "tarefa cache hit container exec")
    # contador quente ANTES: se o gate falhar, seria somado um hit indevido
    assert amb.store.increment_hit_count("hitct1") is True
    with mock.patch.object(config, "MOTOR_REQUER_CONTAINER", True):
        r = rodar(amb.engine.execute_task(
            "tarefa cache hit container exec", "code_gen",
        ))
    assert r.success is False, r
    assert r.executed_locally is True, "cache-hit foi encontrado, mas não servido"
    assert r.artifact_id == "hitct1"
    erro = r.error or ""
    assert "isolamento de container exigido" in erro, erro
    assert "cache-hit não servido" in erro, erro
    assert getattr(r, "sandbox_modo", None) == "exec"
    # store intacto: hit_count NÃO incrementado, nada invalidado/persistido
    rec = amb.store.get("hitct1")
    assert rec is not None and rec.hit_count == 1, "hit_count não deve mudar"
    assert amb.store.pending_revalidation() == [], "não deve invalidar"
    assert amb.store.count() == 1, "nada persistido/removido"
    amb.__exit__(None, None, None)


@caso("engine_requer_container_cache_hit_modo_container_serve")
def _():
    # R1: com MOTOR_REQUER_CONTAINER=True e modo de container (docker), o
    # cache-hit É servido normalmente e incrementa hit_count.
    amb = _Ambiente(threshold=0.1)
    _salvar(amb, "hitct2", "tarefa cache hit container docker")
    amb.engine.sandbox = _SandboxModoFake(amb.sandbox, "docker")
    with mock.patch.object(config, "MOTOR_REQUER_CONTAINER", True):
        r = rodar(amb.engine.execute_task(
            "tarefa cache hit container docker", "code_gen",
        ))
    assert r.success is True, r.error
    assert r.executed_locally is True
    assert r.artifact_id == "hitct2"
    assert getattr(r, "sandbox_modo", None) == "docker"
    rec = amb.store.get("hitct2")
    assert rec is not None and rec.hit_count == 1, "container serve o cache-hit"
    amb.__exit__(None, None, None)


@caso("execution_result_asdict_inclui_sandbox_modo")
def _():
    # Campo REAL da dataclass (aditivo, default "") -> asdict() inclui a chave.
    r = ExecutionResult(success=True)
    assert asdict(r)["sandbox_modo"] == "", "default vazio preserva compat"
    r.sandbox_modo = "docker"
    assert asdict(r)["sandbox_modo"] == "docker", "asdict deve refletir o modo"


@caso("benchmark_roda_e_passa_3_3")
def _():
    motor_dir = ROOT / "harness/motor"
    sys.path.insert(0, str(motor_dir))
    spec = importlib.util.spec_from_file_location(
        "ahs_benchmark", motor_dir / "benchmark.py"
    )
    mod = importlib.util.module_from_spec(spec)
    assert spec.loader is not None
    spec.loader.exec_module(mod)
    buf = io.StringIO()
    with contextlib.redirect_stdout(buf):
        rc = mod.main([])
    saida = buf.getvalue()
    assert rc == 0, f"exit {rc}\n{saida}"
    assert "CRITERIOS: 3/3 OK" in saida, saida


# ------------------------------------------------------------------ runner
def main(argv: list[str] | None = None) -> int:
    """Roda a suíte e imprime [PASS]/[FAIL] + RESULTADO."""
    passaram = 0
    falharam = 0
    for nome, fn in CASES:
        try:
            fn()
            print(f"[PASS] {nome}")
            passaram += 1
        except Exception as exc:  # noqa: BLE001 — reporta e segue
            print(f"[FAIL] {nome}: {type(exc).__name__}: {exc}")
            falharam += 1
    print(f"\nRESULTADO: {passaram} passaram, {falharam} falharam")
    return 1 if falharam else 0


if __name__ == "__main__":
    sys.exit(main())