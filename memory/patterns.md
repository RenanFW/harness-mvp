# Padrões de Design Agentic — Aplicados neste Projeto

Referência derivada de *Agentic Design Patterns: A Hands-On Guide to Building
Intelligent Systems* (Springer, 2026). Cada padrão indica onde já está
aplicado no harness e o que pode evoluir.

## Catálogo (21 capítulos)

| # | Padrão | Status | Onde |
| - | ------ | ------ | ---- |
| 1 | Prompt Chaining | aplicado | `delivery-protocol` encadeia entrada → estados → saída |
| 2 | Routing | aplicado | `hub` roteia por agentes (explorer/implementer/reviewer) |
| 3 | Parallelization | aplicado | `run_many` executa comandos em paralelo; `/api/exec/parallel` + `/api/group` |
| 4 | Reflection (Generator-Critic) | aplicado | `implementer` (produtor) + `reviewer` (crítico) |
| 5 | Tool Use | aplicado | `harness/executor.py` com políticas |
| 6 | Planning | aplicado | `hub` transforma solicitação em plano com estados |
| 7 | Multi-Agent Collaboration | aplicado | hub delega handoffs sequenciais entre agentes |
| 8 | Memory Management | aplicado | `memory/` (core + episodic) do Brain |
| 9 | Learning and Adaptation | aplicado | Brain grava fluxos novos (modo gravação) |
| 10 | Model Context Protocol (MCP) | não | fora do escopo atual |
| 11 | Goal Setting and Monitoring | aplicado | contrato de entrada define objetivo e critérios |
| 12 | Exception Handling and Recovery | aplicado | executor com retries/backoff (Ch12) e status BLOQUEADA |
| 13 | Human-in-the-Loop | aplicado | aprovações para shell e escrita sensível |
| 14 | Knowledge Retrieval (RAG) | aplicado | busca por score de keywords em `Memory.search` + `/api/memory/search` |
| 15 | Inter-Agent Communication (A2A) | não | hub→subagentes cobre o necessário |
| 16 | Resource-Aware Optimization | aplicado | limites de tempo por complexidade (`tempo_maximo_seg` no `TaskContract`, derivado do grau de complexidade via `harness/complexity.py`) |
| 17 | Reasoning Techniques | aplicado | planner → explorar → implementar → revisar |
| 18 | Guardrails/Safety | aplicado | `config.py`, `AGENTS.md`, `opencode.json` |
| 19 | Evaluation and Monitoring | aplicado | `harness/eval.py` (critérios + status) + histórico |
| 20 | Prioritization | aplicado | classificação de risco no hub |
| 21 | Exploration and Experimentation | parcial | `explorer` mapeia antes de alterar |

## Mapeamento arquitetural

```
[Ch13 HITL] Aprovações
      |
[Ch6 Planning] [Ch1 Chaining] [Ch2 Routing]
      |                |            |
      v                v            v
     hub ------------> brain (Ch8 Memória, Ch9 Aprendizado)
      |
      +--> explorer (Ch21 Exploração)
      +--> implementer (Ch5 Tool Use, Ch12 Recovery)  --- produtor
      +--> reviewer (Ch4 Reflection)                  --- crítico
      |
      v
   [Ch19 Evaluation] [Ch18 Guardrails] [Ch20 Priorização]
```

## Regras derivadas dos padrões

1. **Reflection (Ch4)**: produtor e crítico são papéis separados. O
   implementer nunca revisa o próprio trabalho.
2. **Chaining (Ch1)**: uma tarefa complexa é decomposta em etapas simples;
   cada etapa alimenta a próxima.
3. **Tool Use (Ch5)**: ferramentas são capacidades controladas; o agente
   decide como usar, o harness decide se pode.
4. **Memory (Ch8)**: separar contexto da tarefa (episódica) de conhecimento
   estável (semântica). Recuperar antes, consolidar depois.
5. **Exception (Ch12)**: toda falha registra motivo, permite retry com backoff
   e encerra com status claro (`BLOQUEADA` em vez de "deu errado"). O timeout
   do pipeline (`tempo_maximo_seg`, derivado do grau de complexidade) é um
   SAFETY NET: detecta travamento e encerra com `APROVADA_COM_RESSALVAS`
   preservando o progresso — não corta execução legítima no meio.
6. **Guardrails (Ch18)**: políticas são aplicadas no runtime, nunca apenas
   no prompt.
7. **HITL (Ch13)**: aprovação contextual (agente + recurso + ação), não
   genérica.
8. **Evaluation (Ch19)**: toda entrega é validada contra critérios declarativos
   antes do status final; sem critérios explícitos, aplica-se `default_rules()`.

## Auto-modelagem

O harness se enxerga como um **loop de controle determinístico em torno de
LLMs probabilísticas** — consolidação de como é, o que já existe e as
fronteiras em `memory/self-model.md`. Ao implementar um padrão novo, comparar
com o self-model (alvo) antes de escrever código.

## Coleta externa (Tool Use + Memory aplicados à web)

O módulo `harness/webscraper.py` encadeia validação anti-fabricação (>= 2
fontes editoriais), achado de PDF legítimo (deny-list anti-pirataria) e
gravação da referência — o padrão de **Routing/Chaining** levado à coleta de
conhecimento que alimenta `memory/`. Na Fase 3, a validação **prioriza as APIs
de livros** (fontes estáveis/JSON) e trata a **busca web como bônus
best-effort** (falha de markup/anti-bot nunca derruba); evidência parcial vira
`nao_confirmado` (sem falso alerta), e a anti-fabricação (>= 2 fontes
independentes) permanece. Toda referência gravada carrega `trust` preenchido e
o RAG consultivo (Ch14/Ch19) produz síntese determinística a partir das
referências de trust alta.

## Evolução natural (por prioridade)

- RAG com vetores para a memória (Ch14) — quando a base crescer.
- Evaluation por schema JSON (Ch19) — regras aninhadas/condicionais.
- Resource-aware (Ch16) — limites de tokens/custo por execução.

## Grau de complexidade (Ch16/Ch12, Update Final Fase 1)

A tarefa recebe um **grau de complexidade** (baixo/medio/alto) derivado por
heurística determinística (`harness/complexity.py`) — proxy honesto e
reprodutível, sem LLM — que **deriva recursos**: `tempo_maximo_seg`
(baixo→600, medio→900, alto→1500, safety net de timeout) e `usa_sandbox`
(True apenas para alto → container Docker com fallback host). Override
explícito (usuário fornece `grau_complexidade`/`nivel_de_risco`) pula o scorer
(tratado no pipeline `_monta_contrato`). É o padrão **Resource-Aware (Ch16)** +
**Exception/Recovery (Ch12)** aplicados de forma determinística.

## Padrão reutilizável — Auditoria de código gerado por LLM

Princípios genéricos aplicáveis a qualquer projeto que chegue "pronto" ao
harness com origem em LLM. Complementa Reflection (Ch4), Guardrails (Ch18),
Evaluation (Ch19) e HITL (Ch13).

- Conferir promessa vs implementação: validar o código REAL sobre amostras, não
  apenas ler a documentação.
- Testar a diagonal: amostra válida PASSA e amostra com erro ACUSA — sem isso a
  promessa de validação não existe.
- Exigir decisão server-side em regras sensíveis (billing, autorização); nunca
  confiar em decisão tomada no cliente.
- Redigir segredos SEMPRE em relatórios (referência genérica, nunca o valor).
- Sem testes automatizados e sem lint é red flag: qualquer "validação" é
  suspeita até prova contrária.
- Revisão independente final, com achados re-confirmados; o que sobra vira
  ressalva documentada.