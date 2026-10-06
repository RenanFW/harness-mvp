# Core — Fatos Permanentes do Ecossistema

Leitura obrigatória.

## O ecossistema

- Harness de agentes com hub orquestrador: `hub`, `brain`, `explorer`,
  `implementer`, `reviewer`, `documenter`, `pentester`. Só o implementer edita;
  o reviewer só reporta.
- Entrega exige evidências (diff, testes, lint, build); status `APROVADA`,
  `APROVADA_COM_RESSALVAS` ou `BLOQUEADA`. O hub treina os agentes locais
  (webscraper autônomo → `memory/`).

## O Brain

Aplica memória a prompts e registra fluxos novos. Modos: **padrão** (recuperar
e aplicar) e **gravação** (registrar novo); ao final, volta ao padrão.
Nunca armazena segredos, tokens ou dados sensíveis.

## Base de memória

- Fatos: `memory/core.md`; auto-modelagem: `memory/self-model.md`; padrões:
  `memory/patterns.md`; referências: `memory/references/`; episódicos:
  `memory/episodic/` (o `index.md` é gerado em runtime); avaliação:
  `memory/eval/`; sistema de agentes: `memory/agents/` — no template a pasta
  começa VAZIA: `playbook.json`/`README.md`/`resumo.md` são GERADOS por
  `python -m harness.agents compile` (orquestrador determinístico, sem LLM).

## Confiança e rastreabilidade (Trust)

Nada é gravado sem origem rastreável; origem fraca/não validada nunca é
conhecimento confirmado.

- Registros/referências: `trust: alta|media|fraca`, `origem`, `validado_por`
  (quem validou). alta = >= 2 fontes editoriais ou execução com evidências;
  media = validação parcial; fraca = sem validação; antigos sem trust →
  **fraca**.
- Anti-fabricação (webscraper): obra só com >= 2 fontes independentes (APIs
  priorizadas). `ok` → alta; `validacao_indireta` → media; `fabricacao` →
  fraca. Web shell: `validado_por: human`; humano sem trust → media.

### RAG consultivo sobre referências

`harness/rag_refs.py` (`GET /api/refs/query`): busca por relevância nas
referências validadas, filtra por trust (só alta/media) e sintetiza
deterministicamente (stdlib-only), só com referências `alta`; não inventa.

### Observabilidade do hub

`harness/observability.py` (`GET /api/observability`): panorama somente-leitura
da evolução do aprendizado (episódicos + histórico + playbook) — resumo por
status/trust/agente/data, resolução direta vs delegada, atividade diária,
re-trabalho e sugestões.

## Padrões agentic aplicados

Reflection, Chaining, Routing, Parallelization, Tool Use, Memory, Exception
Handling, HITL, RAG, Guardrails, Evaluation (`harness/eval.py`).

## HITL por nível de risco

`nivel_de_risco` (`baixo|medio|alto`; `None` → `medio`) é gate automático no
`AgentPipeline` (matriz em `harness/config.py`); inválido → `BLOQUEADA`.

| Nível | Política | Comportamento |
| ----- | -------- | ------------- |
| baixo | `auto` | Só validação do safelist `RISCO_BAIXO_AUTO_SAFELIST` (match por token; sem separadores/redirect/`\n`), não destrutivo; sem `approve`, nega. |
| medio | `hitl_por_comando` | Cada comando passa pelo callback `approve`. |
| alto | `gate_global` | Exige `approve("<RISK_GATE>")` UMA vez; sem approve → `BLOQUEADA`. |

## Grau de complexidade

Scorer determinístico `harness/complexity.py` (proxy reprodutível, sem LLM;
pesos em `docs/harness-internals.md`). `grau_complexidade` explícito faz
override; `nivel_de_risco` explícito é input (bônus). Derivações:
`tempo_maximo_seg` (600/900/1500; safety net, preserva progresso) e
`usa_sandbox` (só grau alto → container; fallback host).

- CLI do scorer: `python -m harness.complexity "<texto>" [--risco
  baixo|medio|alto]` (única exceção de shell do hub).
- `contexto_grau` (`minimo|padrao|completo`) derivado da complexidade
  (`CONTEXTO_POR_COMPLEXIDADE`): define o volume de contexto da delegação;
  RAG episódico limit 2/4/6 por nível (`RAG_LIMIT_POR_COMPLEXIDADE`).

## Gravação de registro

`AgentPipeline` grava episódico ao final por padrão (`gravar_registro=True`);
testes passam `False` (evita lixo de memória).

## Segurança e guardrails (essencial)

- **Proibido**: segredos; destrutivos (`rm -rf`, `git reset --hard`, `git
  clean -fdx`); acesso fora do projeto; instalar/executar sem aprovação.
- **check_policy** (`harness/executor.py`): destrutivos sempre bloqueiam;
  conservador bloqueia não-essenciais; essenciais (`ESSENTIAL_COMMANDS`) não
  bloqueiam.
- **Sandbox**: container OPCIONAL (`EXEC_SANDBOX_ENABLED`, default host),
  ativação por complexidade alta (`EXEC_SANDBOX_ALTO_ONLY`), fallback host em
  falha de infra; `check_policy` vale sempre. Config em
  `docs/harness-internals.md`.

## Regras permanentes de comunicação

- NUNCA use emojis em nenhum texto, resposta, relatório, comentário de código, mensagem de commit ou artefato gerado, a menos que o usuário peça explicitamente o uso de emojis.