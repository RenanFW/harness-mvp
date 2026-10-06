"""Validação completa de ponta a ponta do harness (todos os endpoints e casos).

Rode do diretório raiz do projeto:
    python tests/validation.py

Suíte usa a porta padrão 8520 e é auto-limpante: remove os registros de
memória e jobs criados durante os testes. Exit 0 = tudo passou.
"""

import base64
import contextlib
import json
import os
import pathlib
import sys
import tempfile
import time
import urllib.error
import urllib.request

ROOT = pathlib.Path(__file__).resolve().parent.parent
sys.path.insert(0, str(ROOT))

from harness.server import HarnessServer  # noqa: E402
from harness import auth, config  # noqa: E402

PORT = int(os.environ.get("HARNESS_TEST_PORT", "8520"))
BASE = f"http://127.0.0.1:{PORT}"
# Credenciais da config importada (mesmo processo do servidor):
#   - AUTH_MODE=="hash": o servidor valida contra o hash (config/auth.json) e
#     a senha real está em config/secrets.env (via auth.get_password()). Usar
#     config.AUTH_PASSWORD aqui daria 401 (senha padrão opencode/opencode).
#   - AUTH_MODE=="env"/"padrao": config.AUTH_PASSWORD é a senha efetiva
#     (variável de ambiente ou padrão opencode/opencode).
# Se AUTH_MODE=="hash" e secrets.env não existir, o teste não consegue
# autenticar (não há como conhecer a senha do hash) — o erro será um 401
# claro em vez de credencial errada silenciosa.
AUTH_USER = config.AUTH_USERNAME
AUTH_PASS = auth.get_password() or config.AUTH_PASSWORD
AUTH = base64.b64encode(f"{AUTH_USER}:{AUTH_PASS}".encode()).decode()
PASS = 0
FAIL = 0


def check(name, cond, extra=""):
    global PASS, FAIL
    if cond:
        PASS += 1
        print(f"  [PASS] {name}")
    else:
        FAIL += 1
        print(f"  [FAIL] {name} {extra}")


def _auth_header(auth=True):
    """Header Authorization Basic. `auth=True` usa as credenciais corretas;
    `auth` como string usa esse valor; `auth=False` omite o header."""
    if auth is True:
        return {"Authorization": "Basic " + AUTH}
    if auth:
        return {"Authorization": "Basic " + auth}
    return {}


def get(path, auth=True):
    req = urllib.request.Request(BASE + path, headers=_auth_header(auth))
    try:
        with urllib.request.urlopen(req, timeout=8) as r:
            return r.status, json.loads(r.read().decode())
    except urllib.error.HTTPError as e:
        return e.code, json.loads(e.read().decode())


def post(path, data, auth=True):
    headers = {"Content-Type": "application/json"}
    headers.update(_auth_header(auth))
    req = urllib.request.Request(BASE + path, data=json.dumps(data).encode(), headers=headers)
    try:
        with urllib.request.urlopen(req, timeout=15) as r:
            return r.status, json.loads(r.read().decode())
    except urllib.error.HTTPError as e:
        return e.code, json.loads(e.read().decode())


def delete(path, auth=True):
    req = urllib.request.Request(BASE + path, method="DELETE", headers=_auth_header(auth))
    try:
        with urllib.request.urlopen(req, timeout=8) as r:
            return r.status, json.loads(r.read().decode())
    except urllib.error.HTTPError as e:
        return e.code, json.loads(e.read().decode())


def wait_job(job_id, tries=50):
    for _ in range(tries):
        st, snap = get(f"/api/job?id={job_id}")
        if st == 200 and snap["status"] not in ("running",):
            return snap
        time.sleep(0.2)
    return None


def _limpa_rate_limit(srv: HarnessServer) -> None:
    """B2/F3: zera o estado do rate-limit da classe Handler entre blocos de
    sondas sem auth.

    A suíte faz 14+ tentativas inválidas de autenticação; com MAX_FALHAS=5
    (janela de 60s) o IP local seria bloqueado após a 5ª falha e as demais
    devolveriam 429 em cascata (quebrando o 401 esperado). O rate-limit em si
    é coberto por tests/security_test.py — aqui apenas impedimos que a suíte
    e2e se auto-bloqueie (mesmo processo, mesmo IP 127.0.0.1). Cada bloco de
    sondas chamado após uma limpeza fica com no máximo MAX_FALHAS falhas."""
    cls = srv._httpd.RequestHandlerClass
    with cls._lock_rate:
        cls._falhas.clear()
        cls._bloqueios.clear()


def cleanup():
    """Remove registros de memória e histórico criados pela suíte."""
    st, idx = get("/api/memory")
    if st == 200:
        for rec in idx.get("records", []):
            if rec["id"].startswith("validacao-final-"):
                (ROOT / "memory" / "episodic" / f"{rec['id']}.md").unlink(missing_ok=True)
        from harness.memory import Memory
        Memory()._refresh_index()
    delete("/api/history")


@contextlib.contextmanager
def _index_tmp(tmp: str):
    """Isola o `config.INDEX_FILE` num diretório temporário (Finding 2):
    `Memory()._refresh_index()` (chamado ao gravar/limpar registros durante a
    suíte) escreve o índice episódico no TEMP, NUNCA no `memory/episodic/index.md`
    rastreado. Mesmo padrão do `config.EVAL_DIR` já usado abaixo (o handler lê
    `config.INDEX_FILE` no momento do acesso, então o patch vale também para a
    thread do servidor). `config.EPISODIC_DIR` continua sendo o real — a suíte
    lê e apaga os registros `validacao-final-*` que ela cria no diretório real."""
    from unittest import mock
    idx = pathlib.Path(tmp) / "index.md"
    original = config.INDEX_FILE
    with mock.patch.object(config, "INDEX_FILE", idx):
        yield
    config.INDEX_FILE = original


def main() -> int:
    global PASS, FAIL
    print("== isolando INDEX_FILE + HISTORY_FILE (Finding 2) ==")
    with tempfile.TemporaryDirectory() as tmp:
        with _index_tmp(tmp):
            # Isola o HISTÓRICO de comandos num arquivo temporário: o
            # `delete("/api/history")` do cleanup() NÃO pode apagar o
            # logs/harness_history.json real (era destrutivo). O Executor lê
            # config.HISTORY_FILE no __init__ e grava no acesso — o patch
            # antes do `_main()` vale para a thread do servidor.
            hist = pathlib.Path(tmp) / "history.json"
            original = config.HISTORY_FILE
            config.HISTORY_FILE = hist
            try:
                return _main()
            finally:
                config.HISTORY_FILE = original


def _main() -> int:
    global PASS, FAIL
    print("== iniciando servidor ==")
    srv = HarnessServer(port=PORT)
    srv.serve_forever()
    time.sleep(0.5)

    print("== fixture de memoria (suite auto-contida; copia limpa sem registros pre-existentes) ==")
    st, rec = post("/api/memory", {"tema": "validacao-final-fixture",
                                   "keywords": ["validacao", "teste", "final"],
                                   "resultado": "fixture auto-contida da suite"})
    check("fixture memory POST 200 + id", st == 200 and rec.get("id"))
    FIXTURE_ID = rec.get("id")

    print("== GET endpoints ==")
    st, h = get("/api/health")
    check("health 200", st == 200)
    # A1: health é o ÚNICO endpoint público e expõe APENAS {ok, name} — não
    # vaza root, versão de runtime, histórico nem credenciais. (Antes o teste
    # só checava a ausência de um campo que nunca existiu no schema.)
    check("health schema fechado (apenas ok+name)",
          set(h.keys()) == {"ok", "name"} and h.get("ok") is True
          and h.get("name") == "harness")

    st, s = get("/api/status")
    check("status 200", st == 200 and s.get("ok") is True)
    check("status tem contadores", all(k in s for k in ("jobs_running", "jobs_total", "history", "memory_records")))

    st, mem = get("/api/memory")
    check("memory GET 200", st == 200 and "core" in mem and "records" in mem)

    st, _ = get(f"/api/memory?id={FIXTURE_ID}")
    check("memory id 200 (fixture)", st == 200)
    st, _ = get("/api/memory?id=inexistente")
    check("memory id inexistente 404", st == 404)

    st, res = get("/api/memory?id=../../harness/config.py")
    check("memory path traversal bloqueado", st == 404)

    st, res = get("/api/memory/search?q=validacao")
    check("memory search 200 + item", st == 200 and res["items"])
    st, res = get("/api/memory/search?q=zzz")
    check("memory search sem resultado", st == 200 and not res["items"])
    st, res = get("/api/memory/search?q=validacao&limit=50")
    check("memory search limit clamp 20", st == 200 and len(res["items"]) <= 20)
    st, res = get("/api/memory/search?q=validacao&limit=-3")
    check("memory search limit negativo -> 1..20", st == 200)

    print("== refs/query (RAG consultivo sobre referências, Item 7) ==")
    st, res = get("/api/refs/query?q=agentic+design+harness")
    check("refs/query 200 + estrutura", st == 200
          and "query" in res and "fontes" in res
          and "sintese" in res and "resumo" in res and "advertencias" in res)
    st, res = get("/api/refs/query?q=agentic&limit=50")
    check("refs/query limit clamp MAX_REF_QUERY_LIMIT", st == 200
          and len(res["fontes"]) <= config.MAX_REF_QUERY_LIMIT)
    st, res = get("/api/refs/query?q=qualquercoisairrelevante")
    check("refs/query sem resultado ainda 200 com lacuna", st == 200
          and not res["fontes"])
    st, res = get("/api/refs/query?q=")
    check("refs/query query vazia -> validação (fontes vazio)", st == 200
          and not res["fontes"] and any("query vazia" in a for a in res["advertencias"]))
    st, res = get("/api/refs/query?q=agentic&incluir_fraca=1")
    check("refs/query incluir_fraca ok", st == 200 and "advertencias" in res)
    st, _ = get("/api/refs/query?q=agentic", auth=False)
    check("refs/query sem auth 401", st == 401)

    st, res = get("/api/memory/stats")
    check("memory stats 200 + estrutura", st == 200
          and "resumo" in res and "orfaos" in res
          and "reuso_por_tag" in res and "sugestoes" in res)
    # A2: EVAL_DIR monkeypatchado para diretório temporário (padrão de
    # eval_memory_test._memoria_tmp) — o save=1 NUNCA toca memory/eval/ real.
    # O handler lê config.EVAL_DIR no momento do request, então o patch vale
    # para a thread do servidor; o temp é limpo ao final do bloco.
    with tempfile.TemporaryDirectory() as tmp_eval:
        eval_original = config.EVAL_DIR
        config.EVAL_DIR = pathlib.Path(tmp_eval) / "eval"
        try:
            st, res = get("/api/memory/stats?save=1")
            check("memory stats save=1 grava relatório markdown", st == 200
                  and res.get("arquivo")
                  and pathlib.Path(res["arquivo"]).exists()
                  and pathlib.Path(res["arquivo"]).parent == config.EVAL_DIR)
        finally:
            config.EVAL_DIR = eval_original  # suite auto-limpante

    print("== observability (Item 6) ==")
    st, res = get("/api/observability")
    check("observability 200 + estrutura", st == 200
          and "resumo_geral" in res and "resolucao" in res
          and "tempo" in res and "retrabalho" in res
          and "playbook" in res and "sugestoes" in res)
    st, _ = get("/api/observability", auth=False)
    check("observability sem auth 401", st == 401)

    st, res = get("/api/history")
    check("history GET 200", st == 200 and "items" in res)
    st, res = get("/api/jobs")
    check("jobs 200", st == 200)

    print("== rotas inexistentes ==")
    st, _ = get("/api/nao-existe")
    check("rota inexistente 404", st == 404)
    st, _ = get("/nao-existe.html")
    check("static inexistente 404", st == 404)

    print("== exec ==")
    st, res = post("/api/exec", {"command": "python --version"})
    check("exec 200", st == 200 and res.get("id"))
    snap = wait_job(res["id"])
    check("exec finaliza com exit 0", snap and snap["exit_code"] == 0)

    st, res = post("/api/exec", {"command": "  "})
    check("comando vazio -> error", st == 200 and res["status"] == "error")

    st, res = post("/api/exec", {"command": "rm -rf alguma coisa"})
    check("comando bloqueado (rm -rf)", res["status"] == "blocked")

    st, res = post("/api/exec", {"command": "dir", "cwd": "C:/Windows"})
    check("cwd fora do projeto bloqueado", res["status"] == "blocked")

    st, res = post("/api/exec", {"command": "python -c \"import sys; sys.exit(1)\"", "retries": "abc"})
    snap = wait_job(res["id"])
    check("retries invalido -> sem retry", snap and snap["attempts"] == 1)

    print("== exec/parallel ==")
    st, res = post("/api/exec/parallel", {"commands": ["echo a", "echo b"]})
    check("parallel 200 + group", st == 200 and res.get("group") and len(res["items"]) == 2)
    gid = res["group"]
    time.sleep(1.0)
    st, res = get(f"/api/group?id={gid}")
    check("group consolidado done", st == 200 and res.get("done") is True and res.get("failed") is False)

    st, res = post("/api/exec/parallel", {"commands": ["echo x"] * 20})
    check("parallel acima do limite 400", st == 400)

    st, res = post("/api/exec/parallel", {"commands": "notalist"})
    check("parallel commands nao-lista 400", st == 400)

    st, _ = get("/api/group?id=inexistente")
    check("group inexistente 404", st == 404)

    print("== eval ==")
    st, res = post("/api/eval", {"text": "build ok", "exit_code": 0})
    check("eval default rules APROVADA", res.get("status") == "APROVADA")
    st, res = post("/api/eval", {"text": "falhou", "exit_code": 1})
    check("eval default rules BLOQUEADA", res.get("status") == "BLOQUEADA")
    st, res = post("/api/eval", {"text": '{"a":1}', "criteria": [{"type": "json_valid"}, {"type": "min_length", "value": 5}]})
    check("eval json_valid + min_length APROVADA", res.get("status") == "APROVADA")
    st, res = post("/api/eval", {"text": "oi", "criteria": [{"type": "not_contains", "value": "erro"}]})
    check("eval not_contains", res.get("status") == "APROVADA")
    st, res = post("/api/eval", {"text": "abc", "criteria": [{"type": "regex", "pattern": "^[a-z]+$"}]})
    check("eval regex", res.get("status") == "APROVADA")
    st, res = post("/api/eval", {"text": "x", "criteria": "notalist"})
    check("eval criteria nao-lista 400", st == 400)

    print("== memory POST ==")
    st, rec = post("/api/memory", {"tema": "validacao-final", "keywords": ["final", "teste"], "resultado": "ok"})
    check("memory POST 200 + id", st == 200 and rec.get("id"))
    rec_id = rec.get("id")
    st, fetched = get(f"/api/memory?id={rec_id}")
    check("registro gravado recuperavel", st == 200 and fetched["id"] == rec_id)
    st, idx = get("/api/memory")
    check("index atualizado (registro novo presente)", any(r["id"] == rec_id for r in idx["records"]))

    # Pendência 4: POST sem validado_por (default human via web shell)
    st, rec_h = post("/api/memory", {"tema": "validacao-final", "resultado": "default human"})
    check("memory POST sem validado_por 200 + id", st == 200 and rec_h.get("id"))
    st, fetched_h = get(f"/api/memory?id={rec_h['id']}")
    check("POST sem validado_por grava validado_por=human no frontmatter",
          st == 200 and fetched_h.get("meta", {}).get("validado_por") == "human")
    # Pendência 3: validado_por=human SEM trust no corpo -> trust media
    # (validação humana explícita não é memória sem validação = fraca)
    check("POST sem trust + validado_por=human (default) grava trust=media",
          st == 200 and fetched_h.get("meta", {}).get("trust") == "media")
    # Pendência 4: POST COM validado_por explícito é respeitado
    st, rec_m = post("/api/memory", {"tema": "validacao-final", "resultado": "motor",
                                     "validado_por": "motor"})
    check("memory POST com validado_por=motor 200 + id", st == 200 and rec_m.get("id"))
    st, fetched_m = get(f"/api/memory?id={rec_m['id']}")
    check("POST com validado_por=motor respeita o valor enviado",
          st == 200 and fetched_m.get("meta", {}).get("validado_por") == "motor")
    # Pendência 3: validado_por=motor SEM trust no corpo -> trust default (fraca)
    # (não é validação humana explícita; mantém o default conservador)
    check("POST validado_por=motor sem trust grava trust=fraca (default)",
          st == 200 and fetched_m.get("meta", {}).get("trust") == "fraca")
    # Pendência 3: validado_por=human COM trust explícito -> respeita o corpo
    st, rec_a = post("/api/memory", {"tema": "validacao-final", "resultado": "human alta",
                                     "validado_por": "human", "trust": "alta"})
    check("memory POST validado_por=human + trust=alta explícito 200 + id",
          st == 200 and rec_a.get("id"))
    st, fetched_a = get(f"/api/memory?id={rec_a['id']}")
    check("POST validado_por=human + trust explícito respeita alta",
          st == 200 and fetched_a.get("meta", {}).get("trust") == "alta"
          and fetched_a.get("meta", {}).get("validado_por") == "human")
    # Pendência 4: origem opcional; ausente -> "_não informada_" (default do record)
    st, fetched_o = get(f"/api/memory?id={rec_h['id']}")
    check("POST sem origem grava origem=_não informada_ (default)",
          st == 200 and fetched_o.get("meta", {}).get("origem") == "_não informada_")

    print("== autenticação (HTTP Basic) ==")
    # B2/F3: zera falhas acumuladas das sondas sem auth acima (refs/query e
    # observability) — cada grupo abaixo fica com no máximo MAX_FALHAS falhas.
    _limpa_rate_limit(srv)
    st, _ = post("/api/exec", {"command": "echo hi"}, auth=False)
    check("POST sem auth 401", st == 401)
    st, _ = delete("/api/history", auth=False)
    check("DELETE sem auth 401", st == 401)
    st, _ = post("/api/eval", {"text": "x"}, auth=False)
    check("POST eval sem auth 401", st == 401)

    _limpa_rate_limit(srv)  # novo grupo de sondas (<= MAX_FALHAS sem 429)
    st, _ = get("/api/status", auth=False)
    check("GET status sem auth 401", st == 401)
    st, _ = get("/api/history", auth=False)
    check("GET history sem auth 401", st == 401)
    st, _ = get("/api/jobs", auth=False)
    check("GET jobs sem auth 401", st == 401)
    st, _ = get("/api/memory", auth=False)
    check("GET memory sem auth 401", st == 401)
    st, _ = get("/api/memory/stats", auth=False)
    check("GET memory stats sem auth 401", st == 401)
    _limpa_rate_limit(srv)  # 5 falhas no grupo acima; libera o próximo
    st, _ = get("/api/agents", auth=False)
    check("GET agents sem auth 401", st == 401)
    st, _ = get("/api/sideprjs", auth=False)
    check("GET sideprjs sem auth 401", st == 401)

    _limpa_rate_limit(srv)
    bad_auth = base64.b64encode(b"opencode:senha-errada").decode()
    st, _ = get("/api/status", auth=bad_auth)
    check("GET credencial inválida 401", st == 401)
    st, _ = post("/api/exec", {"command": "echo hi"}, auth=bad_auth)
    check("POST credencial inválida 401", st == 401)

    _limpa_rate_limit(srv)  # credencial válida não pode herdar 429 residual
    st, res = post("/api/exec", {"command": "echo auth-ok"})
    check("POST exec credencial válida 200", st == 200 and res.get("id"))

    print("== DELETE history ==")
    st, _ = delete("/api/history")
    check("DELETE history 200", st == 200)
    st, res = get("/api/history")
    check("history limpo", st == 200 and res["items"] == [])

    print("== corpo grande ==")
    big = '{"command":"echo x","pad":"' + "a" * (600 * 1024) + '"}'
    req = urllib.request.Request(BASE + "/api/exec", data=big.encode(), headers={
        "Content-Type": "application/json", "Authorization": "Basic " + AUTH})
    try:
        with urllib.request.urlopen(req, timeout=8) as r:
            code = r.status
    except urllib.error.HTTPError as e:
        code = e.code
    check("corpo > 512KB -> 413", code == 413)

    print("== stop job ==")
    st, res = post("/api/exec", {"command": "python -c \"import time; time.sleep(30)\""})
    st2, res2 = post("/api/job/stop", {"id": res["id"]})
    check("stop job 200", st2 == 200 and res2.get("stopped") is True)

    print("== pipeline (HITL por nível de risco) ==")
    def _pipeline_task(risco):
        return {
            "objetivo": "validacao pipeline risco",
            "escopo": "tests/",
            "restricoes": [],
            "criterios_de_aceite": ["roda `python --version` com sucesso"],
            "nivel_de_risco": risco,
        }
    # /api/pipeline roda com gravar_registro=False (F4 — dry-run via web: sem
    # approve nenhum comando é aprovado; gravar registros só geraria lixo).
    # A suíte exercita a MESMA lógica via a CLASSE direto com
    # `gravar_registro=False` — testa os mesmos comportamentos (gate de
    # risco alto, nível inválido, propagação de risco/politica, retrocompat)
    # SEM gerar lixo de memória. O endpoint continua sendo coberto por sua
    # existência; a validação da propagação usa o mesmo contrato da classe.
    from harness.pipeline import AgentPipeline  # noqa: E402
    # /api/pipeline roda com approve=None -> risco alto deve BLOQUEAR com motivo
    res = AgentPipeline(approve=None, gravar_registro=False).run_task(_pipeline_task("alto"))
    check("pipeline risco alto + approve=None -> BLOQUEADA",
          res.get("status") == "BLOQUEADA"
          and "risco alto exige aprovação humana" in res.get("resumo", ""))
    # risco inválido -> BLOQUEADA por contrato
    res = AgentPipeline(approve=None, gravar_registro=False).run_task(_pipeline_task("critico"))
    check("pipeline nivel_de_risco inválido -> BLOQUEADA",
          res.get("status") == "BLOQUEADA"
          and "nivel_de_risco inválido" in res.get("resumo", ""))
    # nível chega ao contrato: saida registra risco/politica (medio default)
    res = AgentPipeline(approve=None, gravar_registro=False).run_task(_pipeline_task("medio"))
    check("pipeline propaga nivel_de_risco e registra politica",
          res.get("risco") == "medio"
          and res.get("politica") == "hitl_por_comando")
    # retrocompatibilidade: tarefa sem nivel_de_risco vira 'medio'
    task_sem_risco = _pipeline_task("medio")
    del task_sem_risco["nivel_de_risco"]
    res = AgentPipeline(approve=None, gravar_registro=False).run_task(task_sem_risco)
    check("pipeline sem nivel_de_risco -> medio (retrocompatível)",
          res.get("risco") == "medio")

    # Update Final (Fase 1): o pipeline deriva grau de complexidade e timeout.
    # Tarefa de consulta simples (sem override) -> baixo/600; sistema completo
    # + infra + produção -> alto/1500; usa_sandbox True só para alto.
    pipe = AgentPipeline(approve=None, gravar_registro=False)
    c_baixo = pipe._monta_contrato({
        "objetivo": "consulta ao banco e exibe o resultado",
        "escopo": "tests/",
        "criterios_de_aceite": ["roda `python --version` com sucesso"],
    })
    check("pipeline deriva grau baixo + tempo 600",
          c_baixo.grau_complexidade == "baixo"
          and c_baixo.tempo_maximo_seg == 600
          and c_baixo.usa_sandbox is False)
    c_alto = pipe._monta_contrato({
        "objetivo": "implementar um sistema completo em producao com docker, "
                    "container, rede, kubernetes, cloud, migracao de esquema, "
                    "seguranca, auth, dados reais, deploy, microservicos, "
                    "orquestracao, enterprise, critico, testes e build",
        "escopo": "tests/",
        "criterios_de_aceite": ["roda `python --version` com sucesso"],
    })
    check("pipeline deriva grau alto + tempo 1500 + usa_sandbox",
          c_alto.grau_complexidade == "alto"
          and c_alto.tempo_maximo_seg == 1500
          and c_alto.usa_sandbox is True)
    # override explícito de tempo respeitado
    c_override = pipe._monta_contrato({
        "objetivo": "consulta ao banco e exibe o resultado",
        "escopo": "tests/",
        "criterios_de_aceite": ["roda `python --version` com sucesso"],
        "tempo_maximo_seg": 77,
    })
    check("pipeline override tempo_maximo_seg explícito",
          c_override.tempo_maximo_seg == 77)

    print("== shutdown ==")
    cleanup()
    srv.shutdown()

    print(f"\nRESULTADO: {PASS} passaram, {FAIL} falharam")
    return 1 if FAIL else 0


if __name__ == "__main__":
    sys.exit(main())