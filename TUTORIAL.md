# Tutorial do Harness (template genérico)

## 1. Iniciar

```bash
cd <raiz-do-projeto>
python app.py            # web shell em http://127.0.0.1:8500
python app.py --port 9000 --host 0.0.0.0   # rede local confiável apenas
python app.py --version  # harness 1.3.0
```

> **Importante:** para usar os agentes, o modo Hub e o comando `/hub` no
> opencode, inicie o
> opencode **a partir deste diretório**, nunca do pai:
>
> ```bash
> cd <raiz-do-projeto>
> opencode
> ```
>
> A config (`.opencode/`, `opencode.json`, `AGENTS.md`) é carregada apenas na
> inicialização e a partir do diretório de onde o opencode é executado. Se
> iniciado do diretório pai, os agentes não são carregados e o modo
> Hub/`/hub` não
> delega. Confira com `opencode agent list` — deve listar os 7 agentes.

## 1b. Painel web do opencode

Para usar o opencode no navegador (mesmos agentes e modelo do terminal):

```bash
cd <raiz-do-projeto>
start-web.bat               # http://127.0.0.1:9090
start-web.bat 9091 0.0.0.0  # rede local confiável apenas
```

> O projeto raiz é sempre o diretório de onde o script roda — por isso
> `start-web.bat` deve ser executado a partir da raiz do projeto. O modelo e o
> provider são os definidos pelo usuário em `opencode.json`.

### Autenticação do painel web (igual ao web shell)

O `start-web.bat` exige HTTP Basic auth no painel web. A senha segue o
**mesmo modelo do harness** (não é gerada aleatoriamente por execução):

- Se a senha personalizada já foi configurada (`python -m harness.auth set` →
  `config/auth.json` + `config/secrets.env`), o script usa essa senha
  (`python -m harness.auth password`).
- Senão, se `OPENCODE_SERVER_PASSWORD` estiver definida no ambiente, usa ela.
- Senão, usa as **credenciais padrão `opencode/opencode`** (1ª execução) e
  exibe um aviso recomendando definir uma senha.
- Username padrão: `opencode` (troque com `OPENCODE_SERVER_USERNAME`).

Ao abrir `http://127.0.0.1:9090` no navegador, use o **Basic auth**:
- usuário: `opencode`
- senha: a personalizada configurada, ou `opencode` se ainda não configurou.

Para fixar uma senha:

```bash
python -m harness.auth set        # prompt: define a senha (scrypt em config/auth.json)
start-web.bat 9091 0.0.0.0        # rede local confiável apenas
```

(Alternativa pontual: definir `OPENCODE_SERVER_PASSWORD` no ambiente antes de
rodar o `start-web.bat`.)

### HTTPS via Tailscale (acesso remoto confiável)

Para acessar de fora da máquina com **HTTPS real** (certificado Let's
Encrypt), use o `tailscale-serve.bat` em vez de expor em `0.0.0.0`:

```bash
cd <raiz-do-projeto>
python app.py            # web shell em 127.0.0.1:8500
start-web.bat            # painel opencode em 127.0.0.1:9090
tailscale-serve.bat      # expõe ambos no tailnet via HTTPS
```

URLs no tailnet:

| Serviço | URL |
| --- | --- |
| Web shell do harness | `https://<maquina>.<tailnet>.ts.net/` |
| Painel web do opencode | `https://<maquina>.<tailnet>.ts.net:8443/` |

- `tailscale-serve.bat status` mostra a config atual; `tailscale-serve.bat
  off` remove a config.
- As credenciais são as mesmas do fluxo local (`HARNESS_*` / `OPENCODE_SERVER_*`);
  o HTTPS criptografa as credenciais em trânsito.
- Em modo público (`HARNESS_PUBLIC=1`) o web shell **recusa subir com a senha
  padrão `opencode`/`opencode`** (B4 endurecido): exige senha personalizada
  (`python -m harness.auth set`). Execuções no host são bloqueadas: exigem
  sandbox Docker/Podman.
- **Rate-limit por identidade (F3)**: em modo público o rate-limit de
  autenticação usa a **identidade do usuário Tailscale** (header
  `Tailscale-User-Login` ou `Tailscale-User-Name`) em vez do IP — atrás do
  proxy todos os clientes aparecem com o mesmo IP, e rate-limit por IP
  causaria lockout global. Em modo local (`HARNESS_PUBLIC=0`) a chave é o IP
  (headers Tailscale nunca são confiados em modo local).
- O TLS é terminado pelo Tailscale; os serviços locais continuam apenas em
  `127.0.0.1` (não é preciso `--host 0.0.0.0`).

## 2. Usar o web shell

- Abra `http://127.0.0.1:8500`, digite comandos na linha `$` e Enter.
- Bloqueios do `config.py` valem aqui também (`rm -rf`, `git reset --hard`,
  etc. são negados).
- **Parar** interrompe jobs rodando; **Atualizar** recarrega a memória do
  Brain; **Limpar tela** zera o terminal.
- A coluna da direita mostra `memory/core.md` e os registros episódicos.

## 3. API HTTP (sem navegador)

| Endpoint | Método | Uso |
| --- | --- | --- |
| `/api/health` | GET | status do servidor (público) |
| `/api/status` | GET | visão geral (jobs, memória, versão) |
| `/api/jobs` | GET | execuções ativas/realizadas |
| `/api/agents` | GET | playbook do sistema interno de agentes |
| `/api/agents/learn` | POST | recompila o playbook (compile + save) |
| `/api/sideprjs` | GET | subpastas de `sidePrjs/` (nome + data) |
| `/api/webscrape` | POST | `{"titulo":..., "autor"?, "pdf"?}` — coleta referência externa |
| `/api/pipeline` | POST | orquestrador determinístico (sem LLM) |
| `/api/exec` | POST | `{"command":"ls -la"}` — roda comando |
| `/api/exec/parallel` | POST | `{"commands":["ls","pwd"]}` — roda em paralelo (Ch3) |
| `/api/job?id=<id>` | GET | saída acumulada de um job |
| `/api/group?id=<g-id>` | GET | resultados consolidados do grupo |
| `/api/job/stop` | POST | encerra job |
| `/api/memory` | GET/POST | ler memória / gravar registro episódico |
| `/api/memory/search?q=` | GET | busca RAG por relevância (Ch14) |
| `/api/memory/stats` | GET | relatório de saúde da memória (Ch19); `?save=1` grava Markdown |
| `/api/refs/query` | GET | RAG consultivo sobre referências validadas: `?q=&limit=&incluir_fraca=` |
| `/api/observability` | GET | panorama da evolução do aprendizado do hub |
| `/api/eval` | POST | avalia texto contra critérios (Ch19) |
| `/api/history` | GET | histórico de comandos |
| `/api/history` | DELETE | limpa o histórico |

> **Autenticação:** todas as rotas `/api/*` — **exceto** `/api/health` —
> exigem **HTTP Basic auth** (usuário/senha). Sem credenciais ou com
> credenciais inválidas a resposta é `401`. Veja a subseção abaixo.

### Autenticação (HTTP Basic auth)

Não existe token de `/api/*` — todas as rotas (exceto `/api/health`) exigem
**HTTP Basic auth** (usuário/senha). O fluxo real de credenciais:

- **1ª execução**: o harness inicia com as credenciais padrão
  `opencode`/`opencode`; em terminal interativo, o `app.py` **pede para definir
  uma senha** na hora (getpass). Isso grava `config/auth.json` (hash scrypt) e
  `config/secrets.env` (`HARNESS_USERNAME`/`HARNESS_PASSWORD`).
- Para definir/alterar depois: `python -m harness.auth set` (interativo) ou
  `python -m harness.auth set --password=<senha>` (scripts).
- Alternativa pontual: definir `HARNESS_PASSWORD` no ambiente antes de subir
  (`set HARNESS_PASSWORD=...` no cmd / `export HARNESS_PASSWORD=...` no Git
  Bash), mantendo o usuário `opencode` (`HARNESS_USERNAME` para trocar).

- O **web shell** pede usuário e senha via `prompt()` no navegador a cada
  sessão; as credenciais **não** são persistidas em armazenamento do navegador
  (B3: sem credenciais em armazenamento).
- Em chamadas via `curl`, use o **Basic auth**:

```bash
curl -s -u "opencode:minha-senha" http://127.0.0.1:8500/api/status
```

- A senha **nunca** é devolvida por `/api/health` (endpoint público de
  status). Com senha personalizada ela fica em `config/auth.json` (hash
  scrypt — nunca texto puro) e em `config/secrets.env` (texto puro, consumido
  pelos `.bat` do painel opencode); no modo padrão (sem personalização) ela
  não é gravada em nenhum arquivo.

Exemplo de avaliação:

```bash
curl -s -X POST http://127.0.0.1:8500/api/eval -H "Content-Type: application/json" \
  -u "opencode:minha-senha" \
  -d '{"text":"build ok","exit_code":0,"criteria":[{"type":"contains","value":"ok"},{"type":"exit_zero","value":true}]}'
# -> {"status":"APROVADA","passed":2,"total":2,...}
```

## 4. Execução de tarefas pelos agentes

0. No chat do opencode, selecione o **modo Hub** e digite a tarefa diretamente
   (o prefixo `/hub` é opcional — atalho explícito).
1. O hub (modo Hub selecionado ou via `/hub`) extrai objetivo, escopo,
   restrições e critérios de aceite.
2. O hub roda o scorer para decidir o volume de contexto da execução:
   `python -m harness.complexity "<objetivo> <escopo>"` → JSON com
   `grau_complexidade` (`baixo|medio|alto`) — mapeado para `contexto_grau`
   (`minimo|padrao|completo`) em `harness/config.py`
   (`CONTEXTO_POR_COMPLEXIDADE`). Cada delegação envia à LLM o contexto do
   nível derivado (RAG episódico com limit 2/4/6 via `Memory.search` com
   snippet).
3. O hub aciona o **brain** (memória em `memory/` — volume conforme o
   `contexto_grau`), **explorer** mapeia, **implementer** altera arquivos,
   **reviewer** revisa.
4. Status final: `APROVADA`, `APROVADA_COM_RESSALVAS` ou `BLOQUEADA` (sem
   evidências = bloqueada).

## 5. Memória do Brain

- `memory/core.md` — fatos permanentes.
- `memory/episodic/` — registros por execução (index automático).
- `memory/references/` — livros/documentos extraídos (via
  `python -m harness.extractor arquivo.pdf --pages 1-50`).
- `memory/patterns.md` — os 21 padrões agentic e onde estão aplicados.
- `memory/agents/resumo.md` — resumo do playbook (~2KB) gerado pelo compile;
  é a **única fonte** do playbook para o Brain (o `playbook.json` é dado de
  máquina do pipeline determinístico e nunca entra no contexto da LLM).

## 5b. Auditoria de segurança web (pentest)

O harness tem um agente `pentester` dedicado (skill `security-audit`), com 3
perfis de teste que **o agente pergunta antes de agir**:

| Perfil | O que faz | Toca o alvo? |
| ------ | --------- | ------------ |
| `osint` | RDAP/whois, DNS (DoH), SPF/DMARC, CT logs — só fontes públicas | Não |
| `superficial` | OSINT + headers (HSTS/CSP/XFO), cookies, TLS, redirects, erros, well-known | Sim, GET/HEAD |
| `completo` | Tudo + ativos read-only (métodos, CORS, reflexão, GraphQL, paths, portas, segredos JS redigidos) | Sim, com autorização |

Modo Hub: peça uma auditoria informando a URL — o hub delega ao `pentester`,
que pergunta o perfil e, para `completo`, exige autorização explícita
(`--autorizado` + gate `PHASE3_GATE`). Execução direta:

```bash
python -m harness.security https://site.com --perfil superficial --relatorio
python -m harness.security https://site.com --perfil completo --autorizado \
  --operador "<proprietário>" --relatorio
python -m harness.security --self-test   # valida o módulo num servidor local
```

O relatório fica em `docs/auditorias/<slug>/` (`README.md`, `resultados.md`,
`achados.json`, `runs/` com diff). O agente **nunca corrige** — só testa e
reporta; POST em formulários reais é opt-in (`--permitir-post-forms`).

## 6. Aprendizado determinístico (sem LLM)

Além dos agentes opencode, o harness oferece ferramentas determinísticas que
implementam o delivery-protocol (doc legível: `docs/protocolo-delivery.md`) e
o aprendizado contínuo:

- **Playbook**: `python -m harness.agents compile` compila o comportamento dos
  agentes opencode, o pipeline do delivery-protocol (constantes de
  `harness/agents.py`), os registros episódicos e o histórico de comandos em
  `memory/agents/playbook.json` + `README.md` + `resumo.md`.
  O Brain lê apenas o `resumo.md`; o `playbook.json` é dado de máquina do
  pipeline. Inspecione com `python -m harness.agents show [nome]`.
- **Orquestrador determinístico**: `POST /api/pipeline` (ou
  `python -m harness.pipeline '<json da tarefa>'`) implementa o
  delivery-protocol — estados, papéis, permissões, validações e evidência. A
  arquitetura suporta aprovação humana (HITL) por comando via callback
  `approve`, mas o endpoint HTTP e a CLI atuais rodam **sem** esse callback,
  portanto **não executam comandos**: validam o contrato, consultam a memória,
  exploram o escopo e consolidam o contrato de saída sem executar comandos.
- **Motor determinístico semântico**: em
  `harness/motor/` — cache semântico
  cache-first (embedding 384d, cosseno >= 0.92, zero-poisoning, sandbox) para
  tarefas repetíveis, resolvendo pelo fast-path local em vez de re-resolver do
  zero.
- **Nomeador de projetos**: `python -m harness.namer "<objetivo>"` cria
  `sidePrjs/<nome>/` com nome único e seguro (Windows); `--dry-run` apenas
  mostra o nome sem criar pasta.