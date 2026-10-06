# Agentic Harness Local Determinístico — Semantic Cache-First

Motor determinístico closed-loop **cache-first** para execução de tarefas de
agentes locais, 100% stdlib (SQLite puro, sem extensões; ONNX/docker/podman são
opcionais com failover automático para safe mode).

> **Pacote AUTÔNOMO**: este motor é o subpackage `harness.motor` (antes vivia
> em um sidePrj removido). Ele NÃO é carregado no boot do harness — o runtime
> (pipeline/executor/servidor) não o importa; use via `import harness.motor`,
> CLI própria (`python -m harness.motor.engine`) ou futura integração.

**Ideia central**: tarefas semelhantes não precisam ser re-resolvidas. O
embedding local (384d) da tarefa é comparado por cosseno com o cache semântico
persistido; acima de `THRESHOLD` (0.92) o artefato é re-executado localmente
(fast-path); abaixo, cai no fallback LLM externo — e o resultado validado é
abstraído em template e persistido para as próximas chamadas.

> Regra de ouro **zero-poisoning**: nada com `exit_code != 0` é persistido.
> Um replay de cache que falha **invalida** o artefato (hit_count zerado +
> `needs_revalidation=1`) em vez de contaminar o cache.

---

## Visão geral — o closed-loop

```
 tarefa ──► embedding local (384d, L2)
              │
              ▼
       find_nearest (cosseno >= 0.92?)
              │                        │
           SIM (hit)                 NÃO (miss)
              │                        │
   interpolate template ──► sandbox    │
              │                        ▼
        exit 0? ── SIM ──► fast-path  LLM externo (opcional) ──► código
              │                        │
            NÃO │                      ▼
              ▼                  sandbox (exit 0?) ── NÃO ──► descartado
   invalidate(id) ──► NÃO reusado      │
   (hit_count=0,                       SIM
    needs_revalidation=1)              ▼
                              abstract_to_template (AST)
                              store.save + ARCCache.put
                              ensure_disk_limit (MAX_DISK_GB=2)
```

O motor é **assíncrono** (`asyncio`), nunca crasha (exceções viram
`ExecutionResult(success=False, error=...)`) e mantém estatísticas
(hits/misses/avg_time_ms/hit_rate).

## Arquitetura — as 6 etapas da especificação

| Etapa | Componente | Responsabilidade |
| --- | --- | --- |
| 1. Embedding local | `embedder.py` | Vetor 384d normalizado L2; ONNX opcional, fallback determinístico (`FastVectorizer`) |
| 2. Busca semântica | `vector_store.py` | SQLite puro, scan linear com cosseno; `needs_revalidation` exclui registros invalidados |
| 3. Abstração AST | `ast_abstraction.py` | Código → template com placeholders tipados (int/str/float/bool/none); f-strings preservadas |
| 4. Cache adaptativo | `cache.py` | ARCCache em memória (T1/B1/T2/B2) + eviction de disco (recência/frequência) com teto 2 GB |
| 5. Sandbox | `sandbox.py` | Execução isolada com timeout real e kill de árvore; modos docker/podman/exec |
| 6. Motor + LLM | `engine.py`, `llm_client.py`, `schema.py` | Orquestração asyncio, provedor REST opcional, validação estrita do contrato |

## Estrutura de arquivos (após integração)

```
harness/motor/
├── __init__.py          # exports públicos (ARCCache, AgenticHarnessEngine, ...)
├── config.py            # constantes: THRESHOLD, DOMAINS, MAX_DISK_GB, TIMEOUT, ...
├── models.py            # ExecutionResult, ArtifactRecord (dataclasses validadas)
├── embedder.py          # LocalEmbedder (onnx/fallback/safe) + FastVectorizer + cosine
├── vector_store.py      # VectorStore (SQLite + cosseno + invalidate + revalidação)
├── ast_abstraction.py   # abstract_to_template / interpolate_template
├── cache.py             # ARCCache (ARC em memória + eviction de disco)
├── sandbox.py           # SandboxRunner (docker/podman/exec) + políticas de bloqueio
├── engine.py            # AgenticHarnessEngine (asyncio) + CLI
├── llm_client.py        # LLMClient (offline/http) + LLMError
├── schema.py            # validate_code_response, default_validation_schema
├── benchmark.py         # benchmark dos 3 critérios de aceite (tabela)
├── tests/engine_test.py # suíte autônoma [PASS]/[FAIL]
└── data/                # cache.db + sandbox (criados sob demanda; .gitignore)
```

## Como rodar (a partir da raiz do harness)

```bash
cd <raiz-do-projeto>

# CLI do motor (JSON com o resultado)
python -m harness.motor.engine "gerar funcao de validacao de cnpj em python" --domain code_gen

# dry-run (só embedding + busca; não executa sandbox nem persiste)
python -m harness.motor.engine "criar script de backup" --domain devsecops --dry-run

# suíte de testes (autônoma, isolada em TemporaryDirectory)
python harness/motor/tests/engine_test.py

# benchmark dos critérios de aceite (exit 0 = 3/3 OK)
python harness/motor/benchmark.py
```

> No Windows use `PYTHONIOENCODING=utf-8` se o console quebrar acentos
> (cp1252). No Linux/macOS não é necessário.

## Modos de operação

### Embedder (`LocalEmbedder.mode`)

| Modo | Quando | Fidelidade |
| --- | --- | --- |
| `onnx` | `onnxruntime` instalado E modelo INT8 em `models/all-MiniLM-L6-v2-int8.onnx` (ou `EMBEDDER_MODEL_PATH`) | Alta (com tokenizer real em etapa futura) |
| `fallback` | onnxruntime presente mas modelo ausente/falho | Reduzida |
| `safe` | onnxruntime indisponível → `FastVectorizer` puro-Python | Reduzida (determinístico, sem dependências) |

Qualquer falha de inferência ONNX cai no `FastVectorizer` — nunca crasha.

### Sandbox (`SandboxRunner.mode`)

| Modo | Resolução | Isolamento |
| --- | --- | --- |
| `docker` | SDK `docker` disponível (imagem `python:3-slim`) | **Sim — conformidade com a spec** |
| `podman` | CLI `podman` disponível (mesma imagem) | **Sim — conformidade com a spec** |
| `exec` | safe mode: subprocess com `shell=False`, Python `-I` (isolated), env mínimo (só PATH), cwd em `data/sandbox/`, timeout com `taskkill /T /F` antes do `terminate` (Windows) | **NÃO — sem isolamento de host** (risco residual, ver abaixo) |

Isolamento de REDE (F2): tanto o caminho docker (SDK) quanto o podman (CLI)
rodam o container com `--network none` — o código no sandbox NÃO acessa a rede
do host. O modo `exec` continua sem isolamento de host (limitação declarada).

Políticas de bloqueio (portadas de `harness/config.py`): `rm -rf`,
`git reset --hard`, `git clean -fdx`, `shutdown`, `mkfs`, `dd if=`, etc.
Código bloqueado não é executado (exit 1). A paridade da lista com
`config.BLOCKED_PATTERNS` é verificada por teste (F13).

### Failover e safe mode

- Embedder e sandbox resolvem o melhor modo disponível em tempo de
  construção; `engine.safe_mode` é `True` se `embedder.mode == "safe"` **ou**
  `sandbox.mode == "exec"`.
- LLMClient: sem endpoint → modo `offline` (nenhuma chamada HTTP;
  `generate()` levanta `LLMError`, o engine captura e nunca crasha).
- Todo código gerado pelo LLM passa pelo sandbox com `exit_code` REAL antes
  de persistir (zero-poisoning); `test_cases` opcionais rodam antes da
  persistência como validação estrita adicional.

## Schema do banco

```sql
CREATE TABLE artifacts (
    id                   TEXT PRIMARY KEY,
    intent_vector        BLOB NOT NULL,          -- struct.pack('<384f')
    domain               TEXT NOT NULL,          -- code_gen | devsecops | pentest | patching
    execution_payload    TEXT NOT NULL,          -- JSON (template AST + placeholders)
    validation_schema    TEXT NOT NULL,          -- JSON
    hit_count            INTEGER NOT NULL DEFAULT 0,
    needs_revalidation   INTEGER NOT NULL DEFAULT 0,  -- 1 = replay falhou (A1)
    created_at           TEXT NOT NULL
);
```
(scan linear O(n) — HNSW é opcional futuro; sem índices por domínio, pois
nenhuma query filtra por domain hoje.)

- `find_nearest` **ignora** `needs_revalidation=1` (registros invalidados não
  são reusados pelo fast-path).
- `invalidate(id)` zera `hit_count` e marca `needs_revalidation=1`; nada é
  apagado (diagnóstico preservado).
- `pending_revalidation()` lista os ids pendentes.
- Bancos antigos (sem a coluna) são migrados automaticamente na abertura
  (`PRAGMA table_info` → `ALTER TABLE ... ADD COLUMN`).

## Critérios de aceite (benchmark 3/3 OK)

| Critério | Medição | Resultado |
| --- | --- | --- |
| (a) Resolução do cache hit < 50 ms | embedding + find_nearest + interpolação (100x) | média ≈ 5 ms — **OK** |
| (b) Reuso ≥ 0.92 sem HTTP extra | 1ª chamada miss (LLM) + 2ª chamada local; contador HTTP permanece em 1 | **OK** |
| (c) 0% corrupção | `integrity_check()` + 100x roundtrip serialize/deserialize | **OK** |

## Riscos residuais (aceitos e declarados)

1. **Modo `exec` do sandbox NÃO oferece isolamento de host.** É o safe mode
   do harness (Python `-I`, env mínimo, bloqueio de padrões destrutivos),
   mas não atende o isolamento da spec. **Pré-requisito operacional** para
   conformidade: `docker` ou `podman` disponíveis na máquina.
2. **Embedding fallback de fidelidade reduzida** (`FastVectorizer`): suficiente
   para o cache determinístico, menos semântico que o ONNX real.
3. **LLMClient depende de ambiente**: sem variável de chave configurada, o
   modo http prossegue sem `Authorization` (com warning) ou usa o modo
   `offline` (miss sem LLM).

## Como integrar uma LLM externa

Sem segredos no código — apenas **nomes** de variáveis de ambiente:

```python
from harness.motor.llm_client import LLMClient

cliente = LLMClient(
    endpoint="https://api.provedor.com/v1/generate",   # modo "http"
    api_key_env="LLM_API_KEY",                          # NOME da env var, nunca a chave
    model="seu-modelo",
    timeout=30.0,
)
```

- A resposta deve ser JSON com a chave `code` (str com o código Python);
  `explicacao` é opcional. Qualquer falha (rede, HTTP, JSON, validação) vira
  `LLMError` — o motor nunca crasha.
- Se `LLM_API_KEY` não existir no ambiente, o cliente emite um warning claro
  (`warnings.warn`) e prossegue sem o header `Authorization`.
- `schema_cls` (Pydantic) é opcional; sem ele, vale a validação nativa de
  `schema.validate_code_response`.
- Modo `offline` (`endpoint=None`) para testes e operação sem provedor.
