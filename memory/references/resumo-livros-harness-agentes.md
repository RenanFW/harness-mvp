---
id: resumo-livros-harness-agentes
tipo: documento
titulo: Resumo de Referências — Harness para Agentes de IA
fonte: fonte externa (PDF resumo não versionado neste template)
data: 2026-08-16
tags: [harness, sandbox, avaliacao, fsm, referencia]
trust: alta
origem: fonte externa (PDF resumo não versionado neste template)
validado_por: motor
---

# Resumo de Referências: Harness para Agentes de IA

Documento sintético de consulta (2 páginas) sobre obras técnicas de controle,
isolamento, avaliação e execução segura de agentes autônomos. Não há autor
individual: material gerado pelo próprio usuário ("Documento gerado como
material sintético de consulta").

## Conceitos-chave
- **Building Systems with Large Language Models** (Julian Bright, O'Reilly):
  camada de orquestração, persistência de memória, gerenciamento de contexto e
  integração de ferramentas (tool calling). Foco no harness: loops de controle
  e mecanismos de decisão determinísticos em torno do comportamento
  probabilístico das LLMs.
- **Designing Autonomous Agents** (Siddharth Sharma, O'Reilly): resiliência,
  prevenção de falhas sistêmicas, controle de recursos e recuperação graciosa.
  Foco no harness: modelagem como Máquina de Estados Finitos (FSM) para impor
  limites de recursão e barrar loops infinitos de chamadas.
- **Generative AI Systems** (Tom Taulli): ciclo de vida ponta a ponta de IA
  generativa; Evaluation Harnesses, testes contra benchmarks reais e validação
  estruturada de respostas. Foco: ambientes de simulação (synthetic
  environments) para medir taxa de sucesso de tarefas e validar segurança
  antes do deploy.
- **Container Security** (Liz Rice, O'Reilly): segurança de contêineres e
  isolamento em nível de sistema operacional. Foco: isolamento em nível de
  kernel (namespaces, cgroups, gVisor, Firecracker) para evitar escape de
  código gerado pelo agente.
- Frameworks de mercado: SWE-bench (avaliação de agentes em repositórios GitHub
  reais dentro de contêineres isolados); LangGraph/AutoGen (controle de estado,
  persistência em banco, checkpointing e Human-in-the-loop); OpenAI Evals
  (padronização de métricas de desempenho e testes de regressão para saídas de
  LLMs).

## Padrões e regras acionáveis
- Loop de controle determinístico em torno da LLM probabilística.
- FSM com limites de recursão para barrar loops infinitos.
- Código gerado por agente só executa isolado (kernel/container) antes de tocar
  o host.
- Medir taxa de sucesso de tarefas em ambiente sintético antes do deploy.
- Checkpointing e HITL como requisitos do ciclo de vida de agentes.

## Aplicação no harness
- pipeline.py já é o orquestrador determinístico sem LLM; o resumo valida a
  direção e sugere limites de recursão/iteração por tarefa.
- eval.py + critérios de aceite do pipeline = mini Evaluation Harness (Ch19).
- executor.py roda no host com BLOCKED_PATTERNS; evolução natural é sandbox em
  container (Docker/Podman) para zero-poisoning.
- memory/episodic + snapshot de etapas do pipeline ≈ checkpointing (LangGraph).

## Pontos de atenção
- Material sintético; autores/obras não verificados contra fontes originais
  (sem ISBNs).
- Referências de arquitetura apenas — nenhum segredo.
