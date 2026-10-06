# Self-Model — Como o Harness Se Enxerga

Memória semântica de auto-modelagem: consolida os tópicos-chave dos livros de
referência com o estado REAL do harness. Serve de alvo (target model) para
auto-correção: ao implementar algo, compare com o que está descrito aqui e com
o que já existe no código.

Consolidado em 2026-08-16 a partir de `memory/references/` (resumo de
referências e obra de *Agentic Design Patterns*, Springer 2026).

## 1. Modelo mental do ecossistema

- O harness é um **loop de controle determinístico em torno de um núcleo
  probabilístico** (LLM). As LLMs são subagentes; o orquestrador
  (`harness/pipeline.py`) e o motor determinístico (cache semântico) decidem
  sem LLM.
- **FSM por design**: toda execução tem estados finitos (`RECEBIDA` →
  `CONSULTANDO_MEMORIA` → `EM_EXPLORACAO` → `EM_IMPLEMENTACAO` → `EM_REVISAO`
  → encerramento). Limites de recursão barram loops infinitos.
- **Guardrails no runtime, não no prompt**: políticas em `harness/config.py`
  (`BLOCKED_PATTERNS`, `APPROVAL_PATTERNS`) e `AGENTS.md` — o harness
  decide se o agente pode executar.
- **Evaluation primeiro**: nada é aprovado sem evidências
  (diff, testes, lint, build) — `harness/eval.py` + critérios de aceite.
- **HITL contextual**: aprovação por (agente + recurso + ação), não genérica.

## 2. Arquitetura real (o que já existe)

| Peça | Caminho | Papel |
| ---- | ------- | ----- |
| Orquestrador determinístico | `harness/pipeline.py` | pipeline sem LLM: estados, papéis, validações; deriva grau de complexidade + timeout + sandbox (`TaskContract` com `grau_complexidade`, `tempo_maximo_seg`, `usa_sandbox`) |
| Motor cache semântico | `harness/motor/` | fast-path determinístico: embedding local + cosseno >= 0.92, fallback à LLM em cache-miss, sandbox de validação |
| Execução de comandos | `harness/executor.py` | políticas, retry/backoff, grupos paralelos, sandbox opcional em container Docker/Podman (default host; ativação condicional por complexidade via `usa_sandbox`/`EXEC_SANDBOX_ALTO_ONLY`, com fallback) |
| Scorer de complexidade | `harness/complexity.py` | grau baixo/medio/alto por heurística determinística (proxy honesto): soma de palavras do prompt em fatores lexicais + bônus de risco, score limitado a 10; CLI `python -m harness.complexity "<texto>" [--risco baixo|medio|alto]` (única exceção de shell do hub); deriva `contexto_grau` (minimo/padrao/completo) para o volume de contexto das delegações |
| Memória | `harness/memory.py` | camadas semântica (core/patterns) + episódica (episodic/) |
| Webscraper autônomo | `harness/webscraper.py` | coleta externa: valida anti-fabricação, anti-pirataria, baixa PDF, grava referência |
| Nomeador determinístico | `harness/namer.py` | `sidePrjs/<nome>/` único e seguro |
| RAG consultivo sobre referências | `harness/rag_refs.py` | `consultar(query)` busca/sintetiza `memory/references/`; filtro de trust (alta/media na busca por padrão); síntese determinística alimentada SOMENTE por trust alta (`sintetizavel`), media só consultável; endpoint `GET /api/refs/query` |
| Avaliação | `harness/eval.py` | critérios declarativos + `default_rules()` |
| Aprendizado de agentes | `harness/agents.py` | compila `.opencode/agent/*.md` em `memory/agents/playbook.json` + `README.md` + `resumo.md` (~2KB); o Brain lê apenas o `resumo.md` (playbook.json nunca entra no contexto da LLM) |
| Observabilidade do hub | `harness/observability.py` | panorama da EVOLUÇÃO do aprendizado (Item 6): agrega episódicos + histórico de comandos + playbook em `gerar_panorama()` (resumo/distribuições, direto vs. delegado, tempo proxy, re-trabalho com `fonte_curva` playbook/episodios, evolução do playbook, sugestões); endpoint `GET /api/observability`; somente leitura |
| Agentes opencode | `.opencode/agent/*.md` | hub, brain, explorer, implementer, reviewer, documenter, pentester (fonte de verdade) |
| Web shell + API | `harness/server.py` | terminal no navegador + endpoints (HTTP Basic auth) |

- **Detecção do Docker no sandbox do Executor** (`harness/executor.py`): o CLI
  do Docker Desktop pode não estar no PATH (instalado em um diretório local do
  usuário, fora do PATH) e o SDK `import docker` é opcional. A detecção procura
  o CLI por caminho explícito (`EXEC_SANDBOX_DOCKER_CLI`), PATH
  (`shutil.which`) e candidatos comuns (`EXEC_SANDBOX_DOCKER_CLI_CANDIDATES` em
  `harness/config.py`), e usa o caminho absoluto no `docker run`. Confirma
  apenas a presença do CLI, não a atividade do daemon (falhas de conectividade
  do daemon — Docker E Podman, via `_eh_falha_infra_daemon` — caem para o modo
  host seguro; desde o A1 da re-revisão da Fase 2, as assinaturas genéricas
  `cannot connect`/`connection refused` só valem COM contexto de daemon, para
  não re-executar no host um comando que rodou no container e imprimiu saída
  legítima com essas frases).
  Fase 2:
  container roda com `--network none` (rede isolada), imagem customizável
  (`EXEC_SANDBOX_IMAGE`) com hardening (`EXEC_SANDBOX_READ_ONLY`,
  `EXEC_SANDBOX_MEMORY`/`CPUS`/`PIDS_LIMIT`, `--cap-drop ALL`,
  `no-new-privileges`, `--user` opcional) e `stop()` mata o `docker run`
  (registrado no job) de forma análoga ao host.
- **Trust e rastreabilidade** (anti-fraude de agente): nenhuma memória é
  gravada sem `origem` rastreável; todo registro episódico e referência carrega
  `trust` (alta/media/fraca) e `validado_por` no frontmatter. Registros antigos
  sem `trust` valem **fraca** (conservador) — o RAG/brain não trata memória
  fraca como conhecimento confirmado. O playbook expõe `n_licoes_fracas` /
  `n_licoes_confiaveis` por agente (lições marcadas com o trust da origem).
  Mapeamento webscraper: `ok` → alta, `validacao_indireta` → media,
  `fabricacao` → fraca. Na Fase 3, a validação prioriza as APIs de livros
  (estáveis) e trata a busca web como bônus best-effort; evidência parcial
  (ex.: 1 API confirma, abaixo do limiar) vira `nao_confirmado` (trust fraca,
  sem falso alerta de fabricação); sem evidência → `fabricacao`. No `POST
  /api/memory`, `validado_por: human` (sem
  `trust` no corpo) eleva o trust para **media** (HITL — validação humana
  explícita); `validado_por: motor` mantém o default `fraca`; `trust` explícito
  no corpo é sempre respeitado. **Base de referências**:
  `memory/references/` armazena referências com `trust` preenchido
  (alta/media/fraca); o RAG consultivo sobre referências produz síntese apenas
  com referências de trust alta.

## 3. Lições dos livros (o que aplicar)

- **Loop determinístico** (Bright/Sharma): persistir a direção do pipeline e do
  cache semântico; reforçar limites de recursão por tarefa.
- **Isolamento de código gerado** (Liz Rice): evolução natural é executar o
  código dos agentes em container/sandbox (kernel/namespaces) antes de tocar o
  host — hoje roda com `BLOCKED_PATTERNS`.
- **Evaluation Harness** (Taulli): medir taxa de sucesso em ambiente sintético
  antes do deploy; `eval.py` + benchmark do motor são o embrião.
- **Checkpointing/HITL** (LangGraph/AutoGen): snapshot de etapas do pipeline ≈
  `memory/episodic`; aprovações contextuais já existem.
- **FSM como disciplina** (Sharma): cada estado do pipeline tem entrada/saída
  e ação permitida; nenhum estado pode encadear a si mesmo sem limite.

## 4. Fronteiras (o que o harness NÃO é)

- Não lê segredos (`.env`, tokens, chaves) — proibido em `AGENTS.md`.
- Não executa comandos destrutivos (`rm -rf`, `git reset --hard`, etc.).
- Não acessa diretórios fora do projeto.
- Não inventa conteúdo: referências inexistentes viram alerta
  `-fabricacao.md`, nunca ficção.
- Não baixa material pirata (deny-list `DENY_PIRATARIA`: família z-lib/zlib).

## 5. Métricas de auto-observação

- Testes: cada arquivo de `tests/*.py` é uma suíte autônoma que imprime
  `[PASS]`/`[FAIL]`; o motor tem suíte própria em
  `harness/motor/tests/engine_test.py`.
- Benchmark do motor: cache-hit < 5 ms, reuso sem HTTP, integridade OK.
- Histórico de comandos: `logs/harness_history.json` (runtime, não versionado).
- Observabilidade do hub (`harness/observability.py`, `GET /api/observability`):
  panorama da evolução do aprendizado — total/distribuições dos episódicos,
  direto vs. delegado (heurística documentada como proxy), atividade diária,
  re-trabalho por achados de revisão e evolução do playbook
  (`schema_version`/`learned.por_agente`), com sugestões objetivas.
- Playbook aprendido: `memory/agents/playbook.json` — inclui a curva de
  aprendizado por agente em `learned.por_agente` (lições/validações/achados
  agregados por agente, com ocorrências e origens rastreáveis) e o resumo de
  confiança por agente (`n_licoes_fracas`/`n_licoes_confiaveis`).
