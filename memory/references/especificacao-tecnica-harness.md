---
id: especificacao-tecnica-harness
tipo: documento
titulo: Especificação Técnica — Aprendizado Determinístico e Cache Semântico (Local Harness)
fonte: fonte externa (PDF de especificação não versionado neste template)
data: 2026-08-16
tags: [especificacao, semantic-cache, embeddings, onnx, hnsw, sandbox, arc, sqlite]
trust: alta
origem: fonte externa (PDF de especificação não versionado neste template)
validado_por: motor
---

# Especificação Técnica — Cache Semântico e Motor Determinístico

Especificação v1.0.0-RELEASE (4 páginas), status
"Aprovado para Implementação", agosto/2026. Módulo "Core Execution & Knowledge
Retention" para o Agentic Harness Local (Python); provedor remoto: REST LLM API.
Documento de especificação técnica do motor (integrado como harness.motor).

## Conceitos-chave
- Padrão central: **Semantic-Cache First Execution Loop**. Para cada requisição:
  busca vetorial local para reutilizar artefatos executáveis estáticos (scripts,
  mutações AST ou receitas de automação) aprendidos em iterações anteriores. Em
  cache miss: consome API de LLM online, valida o resultado em ambiente isolado
  (sandbox), extrai a estrutura executável e salva o artefato no armazenamento
  vetorial/relacional local.
- Pipeline closed-loop em 6 etapas: (1) ROUTER & EMBEDDING GENERATOR (ONNX/
  FastEmbed + busca HNSW no Vector Store) → similaridade cosseno ≥ 0.92? →
  SIM: (2) KNOWLEDGE RETRIEVAL local (payload parametrizado em disco + injeção
  de variáveis do contexto) | NÃO: (3) EXTERNAL LLM DISPATCH (API remota com
  prompt estruturado JSON/Pydantic) → (4) SANDBOX VALIDATION (Docker/EXEC:
  compila/executa, valida exit code 0 e asserções) → aprovado: (5) PERSISTENCE
  (grava vetor + AST) | reprovado: REJEIÇÃO/RETRY COM API → (6) DYNAMIC
  EXECUTION ENGINE (executa a ação isolada, incrementa contadores de frequência
  — Hot Knowledge — e atualiza estado).
- Embeddings: cálculo estritamente local (sem requisições HTTP), runtime ONNX;
  modelo all-MiniLM-L6-v2 quantizado INT8 (~30MB em disco); interface
  `generate_embedding(text: str) -> List[float]` (dimensão 384); métrica:
  similaridade de cosseno cos(θ) = (A·B)/(||A|| ||B||).
- Schema SQLite (extensão vetorial ou Qdrant embarcado): `id` TEXT UUIDv4 (PK);
  `intent_vector` FLOAT[384] (embedding normalizado); `domain` VARCHAR(50)
  (devsecops, pentest, patching, code_gen); `execution_payload` TEXT JSON
  (script Python, AST serializada ou comando shell com marcadores
  `{{target_ip}}`); `validation_schema` TEXT JSON (asserções estáticas);
  `hit_count` INTEGER (reutilizações sem API externa); `created_at` TIMESTAMP.
- REGRA CRÍTICA DE PERSISTÊNCIA: nunca salvar respostas em texto puro ou
  diários de conversa; apenas scripts parametrizados, blocos AST válidos ou
  comandos isolados que passaram por validação.
- Algoritmo (pseudo-código Python no PDF): dataclass `ExecutionResult(success,
  output, executed_locally, execution_time_ms)`; classe `AgenticHarnessEngine`
  com `execute_task` async: embedding → `find_nearest(threshold=0.92)` →
  fast-path local (interpola variáveis, `sandbox.run(payload)`, exit 0 →
  `increment_hit_count`) | fallback LLM → `run_validation(code, test_cases)` →
  se não passou, `raise RuntimeError` (nunca persiste artefato não validado) →
  `_abstract_to_template` (AST: substitui literais por placeholders) →
  `save_artifact` → retorna resultado verificado.
- Resiliência: (a) isolamento de sandbox — nenhuma instrução da LLM online ou
  do banco local roda no host; containers efêmeros Docker/Podman via SDK ou
  ambientes restritos por seccomp e cgroups; (b) prevenção de corrupção do
  cache — artefato local que falha (mudança no ambiente host) tem `hit_count`
  zerado e é sinalizado para revalidação via API online; (c) política de
  invalidação de memória ARC (Adaptive Replacement Cache) com limite de 2GB em
  disco, avaliando recência e frequência de reuso.
- Critérios de aceite: (1) cache hit < 50ms; (2) após aprendizado inicial de
  tarefa pentest/DevSecOps, 100% das chamadas equivalentes atendidas sem HTTP
  externo; (3) nenhum código da API externa com exit != 0 gravado na base.

## Padrões e regras acionáveis
- Threshold de cosseno 0.92 para reuso de artefato.
- Fast-path < 50ms (métrica `execution_time_ms` no contrato).
- Artefato só persiste após validação em sandbox (exit 0 + asserções).
- Parametrização via AST (literais → placeholders) antes de persistir.
- hit_count = Hot Knowledge; falha local zera contador e revalida via API.
- ARC com teto de 2GB para a base local.
- Execução inteira assíncrona (asyncio): embedding, busca, sandbox, LLM.

## Aplicação no harness
- É a especificação-mãe do motor integrado (harness/motor).
  pipeline.py (6 estados) já espelha o closed-loop; faltam: router de embeddings
  ONNX, HNSW/cosseno 0.92, cache SQLite (schema acima), dispatch LLM com schema
  Pydantic, sandbox Docker/Podman (executor.py hoje roda no host com
  BLOCKED_PATTERNS — zero-poisoning exige container), módulo AST de abstração,
  política ARC e contadores hit_count.
- memory.py (RAG por interseção de tokens, peso keyword/título/corpo) é
  candidato a ser complementado pelo cache semântico vetorial (intent_vector
  384d) — camadas distintas (episódica vs. artefatos validados).
- eval.py (exit_code + regras) alinha com a validação de sandbox (exit 0 +
  asserções).
- config.py: adicionar threshold (0.92), limite ARC (2GB), caminho do SQLite e
  tempo-alvo (50ms).
- Os 3 critérios de aceite da seção 6 podem virar regras no eval/pipeline.

## Pontos de atenção
- Documento de especificação interna do projeto.
- Provedor remoto REST LLM é dependência externa; consumo apenas em cache miss.
- "Nunca salve respostas em texto puro" não conflita com a memória episódica
  (registros de fluxo) — o cache semântico guarda artefatos validados, não
  conversas.
- Embedding INT8 (~30MB) é leve para CPU; não exige GPU (ver guia-agentes).
