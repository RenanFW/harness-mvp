# Operação — Como rodar e usar o harness

Conteúdo operacional movido de `AGENTS.md` (Lote 2 — enxugamento de contexto).
Conteúdo integral preservado; o `AGENTS.md` mantém apenas a fronteira de
controle.

## Runtime local

O harness roda de forma local e portátil, sem dependências externas:

- `python app.py` inicia o web shell em `http://127.0.0.1:8500` (terminal no
  navegador + API de comandos). Para acesso remoto via Tailscale, prefira o
  **HTTPS real** do `tailscale-serve.bat` em vez de `--host 0.0.0.0`:
  `tailscale-serve.bat` expõe no tailnet com certificado Let's Encrypt
  (`https://<maquina>.<tailnet>.ts.net/` → 8500 e `:8443/` → painel opencode).
  Só use `python app.py --host 0.0.0.0 --port 8500` em redes confiáveis.
- `start-web.bat` inicia o opencode web (painel do opencode) a partir deste
  diretório: `start-web.bat [porta] [host]` (padrão `9090`/`127.0.0.1`). Ele
  exige HTTP Basic auth com as **credenciais do harness**: usa a senha
  personalizada de `config/secrets.env` se configurada (via
  `python -m harness.auth set`), senão `OPENCODE_SERVER_PASSWORD` do ambiente,
  senão o padrão `opencode/opencode` (username padrão `opencode`). No primeiro
  start interativo, o `app.py` pede para definir a senha.
- `tailscale-serve.bat [apply|status|off]` configura o acesso HTTPS no tailnet:
  porta 443 → web shell (8500) e porta 8443 → painel opencode (9090). O TLS é
  terminado pelo Tailscale (Let's Encrypt automático); os serviços locais
  continuam em `127.0.0.1`. Em modo público (`HARNESS_PUBLIC=1`) o web shell
  **recusa subir com a senha padrão `opencode`/`opencode`** (B4 endurecido —
  exige senha personalizada via `python -m harness.auth set`) e execuções no
  host são bloqueadas: exigem sandbox Docker/Podman.
- **Rate-limit por identidade (F3)**: em modo público (`HARNESS_PUBLIC=1`, via
  Tailscale Serve), o rate-limit de autenticação usa a IDENTIDADE do usuário
  Tailscale — header `Tailscale-User-Login` (login, ex.: alice@example.com) ou
  `Tailscale-User-Name` (nome de exibição) — em vez do IP. Atrás do proxy
  TODOS os clientes aparecem como o mesmo IP; rate-limit por IP causaria
  lockout global. Em modo local (`HARNESS_PUBLIC=0`), a chave é o IP do
  cliente (headers Tailscale NUNCA são confiados em modo local — um cliente
  direto poderia forjá-los). O Tailscale Serve remove esses headers de
  requisições forjadas (anti-spoofing) e o harness escuta apenas em
  `127.0.0.1` em público.
- A execução de comandos passa por políticas em `harness/config.py`
  (comandos destrutivos são bloqueados).
- A memória do Brain é compartilhada em `memory/` (mesma base do agente Brain).

## Dois fluxos de uso

- **Fluxo Build (direto)**: `opencode` aberto DENTRO deste diretório — chat e
  tarefas diretos, sem camadas extras. Usa o modelo configurado pelo usuário em
  `opencode.json`.
- **Fluxo Hub (orquestrado)**: selecione o **modo Hub** no opencode e digite a
  tarefa diretamente no chat (ex.: "faça X") — o prefixo `/hub` é opcional
  (atalho explícito). O hub aplica o
  `delivery-protocol` (embutido em `.opencode/agent/hub.md`; doc em
  `docs/protocolo-delivery.md`), consulta a memória via brain, delega a
  exploração/implementação/revisão e consolida o contrato de saída. Quando a
  solução já existe na memória, o hub executa direto; senão, aprende com os
  agentes e grava o fluxo novo em `memory/episodic/`.
- O modelo e o provider são definidos pelo usuário em `opencode.json` (nunca a
  chave de API — ela fica no auth do próprio opencode).

> **Exceção única de bash do hub (scorer)**: o hub tem UMA única exceção de
> shell — rodar `python -m harness.complexity "<objetivo> <escopo>"` (com
> `--risco baixo|medio|alto` opcional) para derivar o grau de complexidade e o
> `contexto_grau` da execução antes de delegar. O `contexto_grau`
> (`minimo|padrao|completo`, mapeado por `CONTEXTO_POR_COMPLEXIDADE` em
> `harness/config.py`) define o volume de contexto que cada agente recebe:
> Brain lê `core.md` (minimo), + `episodic/index.md` (padrao), + 
> `memory/agents/resumo.md` (completo), e o RAG episódico usa limit 2/4/6
> (`RAG_LIMIT_POR_COMPLEXIDADE`). Operacionalmente: quem opera o harness vê o
> scorer rodar no início de cada tarefa via `/hub`, e o `grau_complexidade` +
> `contexto_grau` aparecem no contrato de saída.

## Como iniciar o opencode

**Sempre inicie o opencode DENTRO deste diretório** (a raiz do projeto),
nunca do diretório pai:

```bash
cd <raiz-do-projeto>
opencode
```

O opencode carrega `.opencode/`, `opencode.json` e `AGENTS.md` apenas a partir
do diretório de inicialização. Se iniciado do diretório pai, os agentes
(`hub`, `brain`, `explorer`, `implementer`, `reviewer`, `documenter`,
`pentester`) — logo o modo Hub e o comando `/hub` — e as skills **não são
carregados**, e a delegação via ferramenta `task` falha com
`Unknown agent type`.

Verifique se está no diretório certo:

```bash
cd <raiz-do-projeto>
opencode agent list   # deve listar os 7 agentes do harness
```

Se os agentes não aparecerem, feche e reinicie o opencode a partir da raiz do
projeto (a config é lida apenas na inicialização).

## Aprendizado de agentes locais

O harness lê os agentes do opencode (`.opencode/agent/*.md`), os registros
episódicos (`memory/episodic/`) e o histórico de comandos (`logs/`), e compila
um playbook estruturado em `memory/agents/` (modelo de papéis, permissões,
pipeline delivery-protocol, validações e lições):

- `python -m harness.agents compile` — compila e salva o playbook
  (`memory/agents/playbook.json` + `README.md` + `resumo.md`).
- `python -m harness.agents show [nome]` — inspeciona o playbook.
- `POST /api/pipeline` — orquestrador determinístico (sem LLM) que aplica o
  playbook: estados, papéis, permissões, validações e evidência.
- Os `.opencode/agent/*.md` continuam fonte de verdade — nunca são editados
  pelo harness.

## Padrões de design agentic

Este projeto aplica os padrões de *Agentic Design Patterns* (Springer 2026).
Referência completa em `memory/patterns.md`. Padrões centrais:

- **Reflection (Ch4)**: implementer (produtor) + reviewer (crítico).
- **Chaining (Ch1)**: tarefa decomposta em etapas encadeadas.
- **Routing (Ch2)**: hub roteia por agente.
- **Parallelization (Ch3)**: comandos em paralelo via `run_many`/grupos.
- **Tool Use (Ch5)**: ferramentas controladas por políticas.
- **Memory (Ch8/9)**: Brain recupera e grava conhecimento.
- **Exception Handling (Ch12)**: falhas com retry/backoff e status claro.
- **HITL (Ch13)**: aprovações contextuais.
- **RAG (Ch14)**: busca por relevância nos registros episódicos.
- **Guardrails (Ch18)**: políticas no runtime, não no prompt.
- **Evaluation (Ch19)**: saídas validadas contra critérios antes de aprovar.