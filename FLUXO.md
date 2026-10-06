# FLUXO.md — Como o Harness Funciona

Visão completa dos dois fluxos do projeto, dos agentes, das skills, da memória
e das políticas que conectam tudo.

## Regra central

> O agente decide **como** executar uma tarefa. O harness decide **se pode**
> executá-la. Nenhum fluxo termina sem evidências; sem evidência → `BLOQUEADA`.

## 1. O harness (código Python, `harness/`)

O runtime de controle. É o único harness "hardcoded" do projeto.

| Módulo | O que faz |
| --- | --- |
| `app.py` | Entrypoint. `python app.py` sobe o web shell na porta 8500; aceita `--port`, `--host`, `--version`. Acesso remoto confiável via HTTPS é feito por `tailscale-serve.bat` (Tailscale Serve), não por `--host 0.0.0.0`. |
| `harness/config.py` | **Guardrails (Ch18)**. Políticas centrais: `BLOCKED_PATTERNS` (`rm -rf`, `git reset --hard`...), `APPROVAL_PATTERNS`, `ALLOWED_CWD`, retry (`RETRY_MAX`, `RETRY_BACKOFF`), limites de API. |
| `harness/executor.py` | **Tool Use (Ch5) + Exception (Ch12) + Parallel (Ch3)**. Executa comandos (`shell=True`), valida `cwd` contra `ALLOWED_CWD`, bloqueia padrões destrutivos, retry com backoff (opt-in), `run_many` paralelo com grupos, histórico persistente. |
| `harness/server.py` | Web shell + API HTTP. Todos os endpoints `/api/*` exigem **HTTP Basic auth** (não há CSRF token); limites de corpo, estáticos (`web/`). |
| `harness/memory.py` | **Memory (Ch8) + RAG (Ch14)**. Lê/escreve `memory/`, busca por score de keywords. |
| `harness/eval.py` | **Evaluation (Ch19)**. Avalia saída contra critérios declarativos → `APROVADA` / `BLOQUEADA`. |
| `harness/extractor.py` | Extrai texto de PDFs/texto/código (usado pelo documenter); bloqueia segredos. |

## 2. Agentes do opencode (`.opencode/agent/`)

Configuração de agentes LLM (não é código). São 6:

| Agente | Modo | Permissões | Papel |
| --- | --- | --- | --- |
| **hub** | primary | edit: deny, bash: restrito a `python -m harness.complexity*` (única exceção), task: allow | Orquestrador. Extrai contrato, roda o scorer, aciona o brain, delega, consolida com status final. |
| **brain** | subagent | só edita `memory/` | Aplica memória (modo padrão) e grava fluxos novos (modo gravação). |
| **explorer** | subagent | só leitura | Mapeia arquivos, dependências, riscos. Nunca altera. |
| **implementer** | subagent | edit: allow, bash: ask | Único que altera arquivos; valida e entrega evidências. |
| **reviewer** | subagent | bash: ask | Crítico (Reflection). Só reporta, nunca corrige. |
| **documenter** | subagent | só `memory/references/` | Lê livros/PDFs/documentos e grava referências enxutas. |

Comandos e skills:

- `.opencode/command/hub.md` — atalho explícito do modo Hub: aciona o fluxo
  completo via `/hub <tarefa>` (no modo Hub o prefixo é opcional).
- **Delivery Protocol** — o protocolo de execução (contratos, estados, HITL,
  granularidade) está embutido em `.opencode/agent/hub.md`; a doc legível
  completa vive em `docs/protocolo-delivery.md`; a verdade de máquina nas
  constantes de `harness/agents.py` (`PipelineSpec.default`).
- **Brain** — o procedimento de memória do agente (modos padrão/gravação,
  template de registro) está embutido em `.opencode/agent/brain.md`.
- `.opencode/skills/webscraping/SKILL.md` — skill de domínio (modelo Agent
  Skills): coleta de referências web (documenter). Outras skills, quando
  criadas, seguem o mesmo schema (registro em `harness/skills.py`).

## 3. Memória (`memory/`)

| Camada | Arquivo | Conteúdo |
| --- | --- | --- |
| Semântica | `core.md` | Fatos permanentes (ecossistema, padrões). |
| Padrões | `patterns.md` | Os 21 padrões do livro e o mapeamento no projeto. |
| Episódica | `episodic/*.md` + `index.md` | Registros de execuções passadas. |
| Referências | `references/` | Livros/documentos extraídos pelo documenter. |

## 4. Dois fluxos

- **Fluxo A — opencode (orquestração inteligente):** selecione o **modo Hub** no
  opencode e digite a tarefa diretamente (o prefixo `/hub` é opcional — atalho
  explícito) → o hub delega aos agentes LLM → eles usam shell (bash via
  opencode, com permissões do `opencode.json`).
- **Fluxo B — web shell / API (runtime):** `python app.py` → POST em `/api/exec`
  → o executor aplica as mesmas políticas (`config.py`) de forma independente
  do LLM.

Os dois fluxos se conectam em 2 pontos:

1. Os agentes LLM executam comandos que passam pela mesma política (do
   `opencode.json` ou do executor).
2. Ambos leem/gravam a **mesma base `memory/`**.

### Pré-requisito do Fluxo A

Inicie o opencode **a partir deste diretório** (a raiz do projeto),
nunca do pai. A config (`.opencode/`, `opencode.json`, `AGENTS.md`) só é
carregada na inicialização e a partir do diretório de execução. Se iniciado do
diretório pai, os agentes não são registrados e o modo Hub/`/hub` falha ao
delegar (`Unknown agent type`). Valide com `opencode agent list`.

## 5. Fluxograma

```
                         ┌────────────────────────┐
                         │         USUÁRIO        │
                         └────────────┬───────────┘
                                      │
            ┌─────────────────────────┴─────────────────────────┐
            ▼                                                   ▼
┌──────────────────────────┐                      ┌──────────────────────────┐
│ FLUXO A — opencode       │                      │ FLUXO B — runtime        │
│ modo Hub (ou /hub)       │                      │ python app.py            │
└──────────────────────────┘                      └──────────────────────────┘
              │                                                 │
              ▼                                                 ▼
┌──────────────────────────┐                      ┌──────────────────────────┐
│ hub (primary, não edita) │                      │ harness/server.py        │
│ ├─ contrato entrada      │                      │ GET/POST /api/*          │
│ ├─ roda scorer           │                      │ auth + limites           │
│ ├─ aciona brain          │                      └──────────────────────────┘
│ ├─ delega explorer       │                                    │
│ ├─ delega implementer    │────────────────┐                   ▼
│ ├─ delega reviewer       │                │     ┌──────────────────────────┐
│ ├─ aciona brain          │                └────►│ harness/executor.py      │
│ └─ contrato saída        │                      │ check_policy             │
└──────────────────────────┘                      │ cwd ALLOWED_CWD          │
              │                                   │ retry/backoff (Ch12)     │
              ▼                                   │ run_many (Ch3)           │
┌──────────────────────────┐                      └──────────────────────────┘
│ base de memória          │                                    │
│ memory/core.md           │                                    ▼
│ memory/episodic/         │                      ┌──────────────────────────┐
│ memory/references/       │                      │ harness/memory.py        │
└──────────────────────────┘                      │ core / episodic / RAG    │
              │                                   └──────────────────────────┘
              ▼                                                 │
┌──────────────────────────┐                                    ▼
│ comandos executados      │                      ┌──────────────────────────┐
│ (shell) com aprovação    │                      │ harness/eval.py (Ch19)   │
│ humana                   │                      │ → APROVADA/BLOQUEADA     │
│ → políticas:             │                      └──────────────────────────┘
│ opencode.json (A)        │                                    │
│ executor.py (B)          │                                    ▼
└──────────────────────────┘                      ┌──────────────────────────┐
                                                  │ config.py guardrails     │
                                                  └──────────────────────────┘
```

## 6. Fluxo de uma tarefa (Fluxo A, passo a passo)

1. **RECEBIDA** — o hub extrai o contrato de entrada: `objetivo`, `escopo`,
   `restricoes`, `criterios_de_aceite`, `nivel_de_risco`. Campos ausentes são
   pedidos ao usuário.
2. **SCORER / contexto** — o hub roda `python -m harness.complexity
   "<objetivo> <escopo>"` → JSON com `grau_complexidade`
   (`baixo|medio|alto`) → `contexto_grau` (`minimo|padrao|completo`) via
   `CONTEXTO_POR_COMPLEXIDADE` (`harness/config.py`), definindo o volume de
   contexto de cada delegação (RAG episódico com limit 2/4/6 via
   `Memory.search` com snippet).
3. **SKILLS (on-demand)** — se a tarefa casa com uma skill de domínio
   (`.opencode/skills/`, registrada por `harness/skills.py`), o hub carrega-a
   e a aplica ao planejamento/delegação. Skills instruem; o runtime decide o
   que é permitido.
4. **MOTOR / delegação** — para tarefas repetíveis de automação o hub avalia o
   fast-path do motor determinístico (`harness/motor/`); tarefas de
   leitura/edição/análise seguem a delegação aos agentes (passos 5-8).
5. **MEMÓRIA** — o hub aciona o `brain` (modo padrão) para recuperar
   conhecimento sobre o contexto, no volume do `contexto_grau`.
6. **EM_EXPLORACAO** — delega ao `explorer` para mapear arquivos, dependências
   e riscos (omitível em tarefas triviais).
7. **EM_IMPLEMENTACAO** — delega ao `implementer` para alterar somente o escopo
   autorizado e validar.
8. **EM_REVISAO** — delega ao `reviewer` para revisão independente (não omitir
   quando houver alteração relevante de código). Achados relevantes voltam ao
   `implementer` para correção.
9. **Encerramento** — o `brain` entra em modo gravação se não havia memória
   prévia, registra o fluxo e volta ao modo padrão. Comandos executados são
   registrados em `logs/harness_history.json`. O hub consolida o contrato
   de saída com um dos status: `APROVADA`, `APROVADA_COM_RESSALVAS` ou
   `BLOQUEADA`.

## 7. Contrato de saída

```yaml
status: APROVADA | APROVADA_COM_RESSALVAS | BLOQUEADA
resumo: Resultado objetivo
arquivos_alterados:
  - path
validacoes_executadas:
  - descricao
achados_da_revisao:
  - severidade, arquivo, linha, descricao
riscos_residuais:
  - descricao
aprovacoes_solicitadas:
  - descricao
```

Além dos campos acima, o hub documenta no contrato os campos derivados do
scorer (transparência): `grau_complexidade` (baixo|medio|alto),
`contexto_grau` (minimo|padrao|completo), `tempo_maximo_seg` e `usa_sandbox`.

## 8. Estados da execução

```
RECEBIDA → CONSULTANDO_MEMORIA → EM_EXPLORACAO → EM_IMPLEMENTACAO → EM_REVISAO
   → ENCERRAMENTO → APROVADA | APROVADA_COM_RESSALVAS | BLOQUEADA
```

(`ENCERRAMENTO` é o estado terminal de consolidação do pipeline determinístico;
a saída estruturada carrega os status finais.)