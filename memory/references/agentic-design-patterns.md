---
id: agentic-design-patterns
tipo: livro
titulo: Agentic Design Patterns — A Hands-On Guide to Building Intelligent Systems
fonte: referência local (PDF não versionado)
data: 2026-08-15
tags: [agentic, patterns, arquitetura, agentes]
trust: alta
origem: referência local (PDF não versionado)
validado_por: motor
---

# Agentic Design Patterns

## Conceitos-chave
- 21 padrões de design para sistemas agenticos (Springer, 2026).
- Níveis de agente: 0 (núcleo de raciocínio) → 5 (autônomo completo).
- Padrões centrais: Prompt Chaining, Routing, Parallelization, Reflection,
  Tool Use, Planning, Multi-Agent, Memory, Learning, MCP, Goal Setting,
  Exception Handling, HITL, RAG, A2A, Resource-Aware, Reasoning, Guardrails,
  Evaluation, Prioritization, Exploration.

## Padrões e regras acionáveis
- **Reflection (Ch4)**: separar papéis de Produtor e Crítico; nunca revisar o
  próprio trabalho.
- **Tool Use (Ch5)**: ferramentas como capacidades controladas; agente decide
  como, harness decide se pode.
- **Memory (Ch8)**: separar contexto da tarefa (episódica) do conhecimento
  estável (semântica).
- **Guardrails (Ch18)**: políticas aplicadas no runtime, nunca só no prompt.
- **HITL (Ch13)**: aprovação contextual (agente + recurso + ação).

## Aplicação no harness
- Reflection = implementer (produtor) + reviewer (crítico).
- Chaining = delivery-protocol em estados encadeados.
- Routing = hub delega por agente.
- Memory = Brain com camadas core/episódica/referências.
- Guardrails = config.py + AGENTS.md + opencode.json.

## Pontos de atenção
- RAG (Ch14), Parallelization real (Ch3) e Evaluation (Ch19) são evoluções
  naturais quando a base crescer.
- MCP (Ch10) e A2A (Ch15) ficam fora do escopo atual.