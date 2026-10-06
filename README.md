# Harness — template genérico de orquestração de agentes opencode

harness 1.3.0 — runtime local e portátil (apenas stdlib do Python, sem dependências externas).

## Instalação

Requisito: **Python 3.12+** — o harness usa apenas a biblioteca padrão.

```bash
git clone https://github.com/RenanFW/harness-mvp.git
cd harness-mvp
python app.py          # web shell em http://127.0.0.1:8500
```

Opcional: instale o **opencode** e rode `opencode` **dentro da raiz do projeto**
para usar os agentes e o painel web.

## Uso rápido

1. Defina seu modelo/provider em `opencode.json` (o template vem **sem** modelo fixo).
2. Inicie o opencode na raiz do projeto, selecione o modo **Hub** e digite a tarefa:
   `faça X` (ou `/hub <tarefa>` como atalho).
3. O hub consulta a memória, delega exploração -> implementação -> revisão e
   entrega um contrato de saída com evidências (`APROVADA`,
   `APROVADA_COM_RESSALVAS` ou `BLOQUEADA`).

Login do web shell: `opencode` / `opencode` na primeira execução (o harness pede
uma senha personalizada). Detalhes em [Como usar](#como-usar) e
[CUSTOMIZACAO.md](CUSTOMIZACAO.md).

## Visão geral

O harness orquestra os agentes do opencode (`hub`, `brain`,
`explorer`, `implementer`, `reviewer`, `documenter`), aplica o
delivery-protocol verificável, mantém memória episódica local, controla a
execução de comandos com guardrails de runtime e aprende com cada execução
(playbook compilado + orquestrador determinístico sem LLM).

Princípio central:

> O agente decide **como** executar uma tarefa. O harness decide **se pode**
> executá-la.

Nenhuma execução termina sem evidências (diff, testes, lint ou build); sem
evidências, o status final é `BLOQUEADA`.

## Funcionalidades

- **Dois fluxos de uso**: Build direto (opencode aberto dentro do projeto) e
  Hub orquestrado (modo Hub — selecione o agente hub e digite a tarefa direto;
  o prefixo `/hub` é opcional), com contrato de entrada e saída.
- **7 agentes** com permissões granulares (editar, shell, delegar).
- **Memória do Brain**: `core.md` (fatos), `episodic/` (registros por
  execução), `references/` (documentos extraídos) e `patterns.md` (padrões).
- **Delivery-protocol verificável**: estados, roteamento por agente e contrato
  de saída estruturado.
- **Guardrails de runtime**: bloqueio de comandos destrutivos, aprovação
  humana (HITL) e "sem evidência = `BLOQUEADA`".
- **Web shell + API HTTP** com HTTP Basic auth (porta 8500).
- **Painel web do opencode** (porta 9090) com o mesmo modelo de autenticação.
- **Aprendizado contínuo**: `python -m harness.agents compile` gera o playbook
  em `memory/agents/`; `POST /api/pipeline` roda o orquestrador determinístico
  (sem LLM).
- **Motor determinístico semântico** cache-first em
  `harness/motor/` — fast-path para tarefas
  repetíveis.
- **Nomeador de projetos**: `python -m harness.namer "<objetivo>"` cria
  `sidePrjs/<nome>/` de forma determinística.
- **Pentest (auditoria de segurança web)**: agente `pentester` + skill
  `security-audit` com **3 perfis** — `osint` (passivo), `superficial` (não
  invasivo) e `completo` (ativos read-only, gated por autorização +
  `PHASE3_GATE`). Módulo SSRF-safe em `harness/security.py`; relatório em
  `docs/auditorias/<slug>/` (README + resultados + `achados.json` + `runs/`),
  com redação de segredos e anti-fabricação (nada é afirmado sem evidência).

## Como funciona

Dois fluxos conectados pela mesma base `memory/` e pelas mesmas políticas de
execução:

```
                +-------------------------------+
                |           USUARIO             |
                +---------------+---------------+
                                |
              +-----------------+-----------------+
              v                                   v
   FLUXO A — opencode (LLM)          FLUXO B — runtime Python
   modo Hub (ou /hub)                python app.py
   hub delega: brain, explorer,      harness/server.py (web shell + API)
   implementer, reviewer             harness/executor.py (políticas)
        |                                   |
        +----------------+------------------+
                         v
                 base de memoria (memory/)
                 core, episodic, references
                         |
                         v
        comandos executados (shell) com aprovacao humana
        -> politicas: opencode.json (A) OU executor.py (B)
```

- **Camada 1 — agentes opencode** (`.opencode/agent/`): configuração LLM dos 7
  agentes (papéis, permissões, regras). O `hub` (primary) orquestra; os demais
  são subagentes.
- **Camada 2 — runtime Python** (`harness/`): `app.py` sobe o web shell;
  `executor.py` executa comandos com guardrails (`config.py`); `server.py`
  expõe a API; `memory.py` lê/grava a memória; `eval.py` avalia saídas contra
  critérios.
- **Camada 3 — memória** (`memory/`): base compartilhada entre os dois fluxos;
  o Brain recupera (modo padrão) e grava (modo gravação) conhecimento.
- **Camada 4 — motor determinístico**: `harness/agents.py` compila o playbook;
  `harness/pipeline.py` implementa o delivery-protocol sem LLM, com suporte HITL
  na arquitetura (não exposto no endpoint/CLI atuais); o motor semântico em
  `harness/motor/` oferece fast-path cache-first para tarefas repetíveis.

### Granularidade de contexto (peso da tarefa)

O harness deriva **quanto contexto a LLM recebe em cada delegação** a partir do
peso da tarefa (grau de complexidade), economizando tokens (Lote 1 — níveis
conservadores; nada de conteúdo de arquivo é cortado ainda, apenas o mecanismo
de escala).

- **Scorer CLI**: o hub roda `python -m harness.complexity "<objetivo> <escopo>"`
  (com `--risco baixo|medio|alto` opcional) e recebe JSON com `score`,
  `grau_complexidade` e `fatores` — decide o volume de contexto antes de
  delegar.
- **Níveis**: `NIVEIS_CONTEXTO = (minimo, padrao, completo)`; mapeados por
  `CONTEXTO_POR_COMPLEXIDADE` (`baixo→minimo`, `medio→padrao`, `alto→completo`)
  em `harness/config.py` e expostos no contrato de saída como `contexto_grau`.
- **RAG episódico**: `Memory.search(query, limit, snippet=True)` devolve
  snippets compactos (`| Keywords | Id | Data |`); o limite de snippets por
  nível é `RAG_LIMIT_POR_COMPLEXIDADE` (minimo 2, padrao 4, completo 6).
- **Playbook**: `python -m harness.agents compile` gera `playbook.json` +
  `README.md` + `resumo.md`; o Brain lê apenas o `resumo.md` — o
  `playbook.json` nunca entra no contexto da LLM (dado de máquina do pipeline).

### Estados da execução

```
RECEBIDA -> CONSULTANDO_MEMORIA -> EM_EXPLORACAO -> EM_IMPLEMENTACAO -> EM_REVISAO
   -> APROVADA | APROVADA_COM_RESSALVAS | BLOQUEADA
```

### Contrato de saída

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

## Como usar

Requisitos:
- **Python 3.12 ou superior** (obrigatório — o harness usa apenas a biblioteca
  padrão). Confirme com `python --version`.
- **opencode** (obrigatório para os agentes e o painel web). Confirme com
  `opencode --version` e `opencode agent list` dentro do projeto (deve listar
  os 7 agentes: hub, brain, explorer, implementer, reviewer, documenter,
  pentester).
- **Docker Desktop ou Podman** (OPCIONAL — necessário apenas para o sandbox de
  execução isolada em modo público `HARNESS_PUBLIC=1`).
- **Tailscale** (OPCIONAL — necessário apenas para o deploy público com HTTPS
  real via `tailscale-serve.bat`).

> **Importante**: inicie o opencode **dentro** da raiz do projeto, nunca do
> pai. A configuração (`.opencode/`, `opencode.json`, `AGENTS.md`) é carregada
> apenas na inicialização e a partir do diretório de execução. Se iniciado do
> diretório pai, os agentes não são carregados e o modo Hub/`/hub` falha ao
> delegar (`Unknown agent type`). Valide com `opencode agent list` — deve
> listar os 7 agentes.

```bash
cd <raiz-do-projeto>
opencode                    # inicia o opencode no diretório correto
opencode agent list         # deve listar hub, brain, explorer, implementer, reviewer, documenter, pentester
```

No chat do opencode, selecione o **modo Hub** (agente `hub`) e digite a tarefa
diretamente — o prefixo `/hub` é opcional (atalho explícito):

```
faça X
```

(Ou, se preferir o atalho explícito: `/hub <tarefa>`.)

O hub extrai o contrato de entrada — `objetivo`, `escopo`, `restricoes`,
`criterios_de_aceite`, `nivel_de_risco` — aciona o brain, delega a
exploração/implementação/revisão e consolida o contrato de saída. Tarefas de
projeto novo são criadas em `sidePrjs/<nome>/` via nomeador determinístico.

### Quickstart multi-OS

Requisito único: **Python 3.12+** (o harness usa apenas a stdlib).

- **Linux/macOS**: `python app.py` na raiz do projeto (web shell em
  `http://127.0.0.1:8500`). O painel do opencode, se instalado, roda com
  `opencode web` ou via os scripts `.bat` (Windows).
- **Windows**: `python app.py` (web shell) e `start-web.bat` (painel opencode
  em `http://127.0.0.1:9090`); `run-web-public.bat` e `tailscale-serve.bat`
  cobrem exposição pública/HTTPS.

Para usar os agentes do opencode, inicie o opencode **dentro da raiz do
projeto** (a config em `.opencode/`, `opencode.json` e `AGENTS.md` é lida
apenas na inicialização).

## Como personalizar

Guia completo em [`CUSTOMIZACAO.md`](CUSTOMIZACAO.md). Resumo:

- **Modelo/provider**: defina `"model": "<provider>/<modelo>"` em
  `opencode.json` (o template vem **sem** modelo fixo). A chave de API fica no
  auth do próprio opencode, nunca no repositório.
- **Agentes**: adicione/edite arquivos em `.opencode/agent/` (7 agentes de
  exemplo: hub, brain, explorer, implementer, reviewer, documenter, pentester).
- **Skills**: adicione/edite em `.opencode/skills/<nome>/SKILL.md`.
- **Políticas de risco e execução**: `harness/config.py` (fonte única da matriz
  risco → política; não relaxe a semântica de segurança).
- **Portas e autenticação**: `--port`/`--host` no `app.py` e as variáveis
  `HARNESS_USERNAME`/`HARNESS_PASSWORD` (ou `python -m harness.auth set`).
- **Memória**: `memory/core.md`, `memory/patterns.md`, `memory/self-model.md`,
  `memory/references/` (RAG) e `memory/episodic/` (registros).
- **Publicação**: configure o remote do Git e publique. Licença MIT (ver
  `LICENSE`).

## Acesso web

### Web shell (runtime do harness)

```bash
cd <raiz-do-projeto>
python app.py                        # http://127.0.0.1:8500
python app.py --port 9000 --host 0.0.0.0   # rede local confiável apenas
python app.py --version              # harness 1.3.0
```

O web shell exige HTTP Basic auth: `HARNESS_USERNAME` (padrão `opencode`) e
senha. Fluxo real de credenciais:

- **1ª execução**: o harness inicia com as credenciais padrão
  `opencode`/`opencode`; em terminal interativo, o `app.py` **pede para definir
  uma senha** na hora (getpass) — grava `config/auth.json` (hash scrypt) e
  `config/secrets.env` (`HARNESS_USERNAME`/`HARNESS_PASSWORD`).
- Para definir/alterar depois: `python -m harness.auth set` (interativo) ou
  `python -m harness.auth set --password=<senha>` (scripts).
- Alternativa pontual: `set HARNESS_PASSWORD=minha-senha` no ambiente antes de
  subir (`HARNESS_USERNAME` para trocar o usuário).
- O web shell pede usuário/senha via `prompt()` a cada sessão — sem
  credenciais em armazenamento do navegador (B3).

### Painel web do opencode

```bash
cd <raiz-do-projeto>
start-web.bat                # http://127.0.0.1:9090
start-web.bat 9091 0.0.0.0   # rede local confiável apenas
```

O `start-web.bat` usa o mesmo modelo de autenticação: username padrão
`opencode` (`OPENCODE_SERVER_USERNAME` para trocar) e senha — da
`config/secrets.env` (definida com `python -m harness.auth set`), de
`OPENCODE_SERVER_PASSWORD` se definida no ambiente, ou, sem nenhuma, as
**credenciais padrão `opencode`/`opencode`** (fallback). O modelo e o provider
usados são os definidos pelo usuário em `opencode.json`.

### HTTPS via Tailscale (acesso remoto confiável)

Para acesso remoto, **prefira HTTPS real via Tailscale Serve** em vez de
expor em `0.0.0.0`. O TLS é terminado pelo Tailscale com certificado
Let's Encrypt automático:

```bash
cd <raiz-do-projeto>
python app.py            # web shell em 127.0.0.1:8500
start-web.bat            # painel opencode em 127.0.0.1:9090
tailscale-serve.bat      # expõe ambos no tailnet via HTTPS
```

Resultado no tailnet:

| Serviço | URL | Backend local |
| --- | --- | --- |
| Web shell do harness | `https://<maquina>.<tailnet>.ts.net/` | `127.0.0.1:8500` |
| Painel web do opencode | `https://<maquina>.<tailnet>.ts.net:8443/` | `127.0.0.1:9090` |

- `tailscale-serve.bat` (sem argumento) aplica a config do serve e imprime as
  URLs; `tailscale-serve.bat status` mostra a config; `tailscale-serve.bat
  off` remove a config.
- As credenciais de acesso são as mesmas do fluxo local (`HARNESS_*` no web
  shell, `OPENCODE_SERVER_*` no painel). A criptografia HTTPS protege as
  credenciais em trânsito.
- Em modo público (`HARNESS_PUBLIC=1`) o web shell **recusa subir com a senha
  padrão `opencode`/`opencode`** (B4 endurecido — exige senha personalizada via
  `python -m harness.auth set`; a senha não é impressa nem passa pela linha de
  comando dos processos) e execuções no host são bloqueadas: exigem sandbox
  Docker/Podman.
- Em modo público o **rate-limit de autenticação é por identidade do usuário
  Tailscale** (header `Tailscale-User-Login` ou `Tailscale-User-Name`), não
  por IP — atrás do proxy todos aparecem com o mesmo IP (F3). Em modo local
  (`HARNESS_PUBLIC=0`) a chave é o IP do cliente.
- Requisitos: Tailscale conectado com HTTPS habilitado no tailnet, e os
  serviços locais rodando (8500/9090).

### API HTTP — endpoints principais

Todas as rotas `/api/*` exigem HTTP Basic auth, **exceto** `/api/health`:

| Endpoint | Método | Uso |
| --- | --- | --- |
| `/api/health` | GET | status do servidor (público) |
| `/api/status` | GET | visão geral (jobs, memória, versão) |
| `/api/jobs` | GET | execuções ativas/realizadas |
| `/api/agents` | GET | playbook do sistema interno de agentes |
| `/api/agents/learn` | POST | recompila o playbook (compile + save) |
| `/api/sideprjs` | GET | subpastas de `sidePrjs/` (nome + data) |
| `/api/webscrape` | POST | `{"titulo":..., "autor"?, "pdf"?}` — coleta referência externa |
| `/api/exec` | POST | `{"command":"ls -la"}` — roda comando |
| `/api/exec/parallel` | POST | `{"commands":["ls","pwd"]}` — roda em paralelo |
| `/api/job?id=<id>` | GET | saída acumulada de um job |
| `/api/job/stop` | POST | encerra job |
| `/api/group?id=<g-id>` | GET | resultados consolidados de um grupo |
| `/api/memory` | GET/POST | ler memória / gravar registro episódico |
| `/api/memory/search?q=` | GET | busca RAG por relevância |
| `/api/memory/stats` | GET | relatório de saúde da memória (Ch19) |
| `/api/refs/query?q=` | GET | RAG consultivo sobre referências validadas |
| `/api/observability` | GET | panorama da evolução do aprendizado do hub |
| `/api/eval` | POST | avalia texto contra critérios |
| `/api/history` | GET/DELETE | histórico de comandos / limpa histórico |
| `/api/pipeline` | POST | orquestrador determinístico (sem LLM) |

Exemplo com `curl`:

```bash
curl -s -u "opencode:minha-senha" http://127.0.0.1:8500/api/status
```

Para definir/trocar a senha do web shell:

```bash
python -m harness.auth set            # prompt interativo (grava config/auth.json + secrets.env)
set HARNESS_PASSWORD=minha-senha      # alternativa pontual (ambiente)
python app.py
```

A senha nunca é devolvida por `/api/health`; com senha personalizada ela fica
em `config/auth.json` (hash scrypt — nunca texto puro) e `config/secrets.env`
(texto puro, consumido pelos `.bat`); no modo padrão (sem personalização) ela
não é gravada em nenhum arquivo.

## Estrutura do projeto

```
<projeto>/
|-- app.py                  # entrypoint: web shell + API (porta 8500)
|-- start-web.bat           # painel web do opencode (porta 9090)
|-- tailscale-serve.bat     # HTTPS no tailnet via Tailscale Serve (apply/status/off)
|-- AGENTS.md               # fronteira de controle (políticas do harness)
|-- opencode.json           # modelo, permissions dos agentes no opencode
|-- FLUXO.md / TUTORIAL.md  # documentação interna
|-- docs/                   # documentação detalhada (harness-internals, operacao)
|-- .opencode/
|   |-- agent/              # os 7 agentes (hub, brain, explorer, ...)
|   |-- command/            # /hub (atalho do modo Hub)
|   `-- skills/             # skills de domínio (webscraping, security-audit)
|-- harness/                # runtime Python (config, executor, server,
|   |                        #  memory, eval, extractor, agents, pipeline,
|   `                        #  skills, namer, webscraper, motor)
|-- memory/                 # core, episodic/, references/, patterns.md,
|   `                        #  agents/ (playbook + resumo.md)
|-- sidePrjs/               # projetos novos (nome gerado pelo nomeador)
|-- tests/                  # suítes de teste
|-- web/                    # front-end do web shell (index.html, app.js)
`-- logs/                   # historico de comandos
```

## Agentes

| Agente | Modo | Responsabilidade | Edita | Shell | Delega |
| --- | --- | --- | --- | --- | --- |
| hub | primary | Planejar, delegar, consolidar | Não | Sim* | Sim |
| brain | subagent | Memória: recuperar e gravar fluxos | Só `memory/` | Não | Não |
| explorer | subagent | Mapear arquivos, dependências, riscos | Não | Não | Não |
| implementer | subagent | Alterar arquivos e validar | Sim | Com aprovação | Não |
| reviewer | subagent | Revisar qualidade e riscos | Não | Com aprovação | Não |
| documenter | subagent | Ler/interpretar livros e documentos | Só `memory/references/` | Com aprovação | Não |
| pentester | subagent | Auditoria de segurança web (perfis osint/superficial/completo; nunca corrige) | Só `docs/auditorias/` | Com aprovação | Não |

(*) Exceção única de shell do hub: `python -m harness.complexity "<objetivo>
<escopo>"` — roda o scorer para derivar o grau de complexidade/contexto.

Regras comuns: apenas o implementer modifica arquivos; o implementer não
revisa o próprio trabalho (padrão Reflection); subagentes não delegam a outros
agentes.

## Aprendizado contínuo

- **Playbook**: `python -m harness.agents compile` compila o comportamento dos
  agentes opencode, o pipeline do delivery-protocol (constantes de
  `harness/agents.py`), os registros episódicos e o histórico de comandos em
  `memory/agents/playbook.json` + `README.md`. Inspecione com
  `python -m harness.agents show [nome]`.
- **Orquestrador determinístico (sem LLM)**: `POST /api/pipeline` implementa o
  delivery-protocol — estados, papéis, permissões, validações e evidência
  (doc legível em `docs/protocolo-delivery.md`). Sugere comandos de validação
  a partir do playbook e dos critérios de aceite; a arquitetura suporta
  aprovação humana (HITL) por comando via callback `approve`, mas o endpoint
  HTTP e a CLI atuais rodam sem esse callback, portanto nenhum comando é
  executado de fato — o pipeline valida o contrato, consulta a memória, explora
  o escopo e consolida o contrato de saída sem executar comandos. Também
  disponível via `python -m harness.pipeline '<json da tarefa>'`.
- **Motor determinístico semântico**: `harness/motor/` implementa cache
  semântico cache-first (embedding 384d, cosseno >= 0.92, zero-poisoning,
  sandbox, asyncio, stdlib puro).
  Tarefas semelhantes a artefatos já validados podem ser resolvidas pelo
  fast-path local — o hub pode usar esse fast-path para tarefas repetíveis em
  vez de re-resolver do zero.
- **Nomeador de projetos**: `python -m harness.namer "<objetivo>"` cria
  `sidePrjs/<nome>/` com nome único e seguro (Windows); `--dry-run` apenas
  mostra o nome sem criar pasta.
- **Histórico (F5)**: o diretório `logs/` é criado sob demanda pelo executor
  na primeira execução — o histórico de comandos persiste em
  `logs/harness_history.json` (não versionado; limite de 500 registros) e o
  access log leve do web shell em `logs/server_access.log` (F10, não
  versionado — IP/identidade, rota e status HTTP).