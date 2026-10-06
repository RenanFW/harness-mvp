# Harness Internals — histórico técnico detalhado

Detalhes técnicos e histórico de implementação movidos de `memory/core.md`
(Lote 2 — enxugamento de contexto). Conteúdo integral preservado; o `core.md`
mantém apenas os fatos permanentes resumidos.

## HITL por nível de risco (detalhes)

O `nivel_de_risco` da tarefa (`baixo|medio|alto`; `None` → `medio`,
retrocompatível) é um **gate automático** no `AgentPipeline`
(`harness/pipeline.py`, fonte da matriz em `harness/config.py`):
`nivel_de_risco` inválido → contrato inválido → `BLOQUEADA` em `RECEBIDA`.

| Nível | Política | Comportamento |
| ----- | -------- | ------------- |
| baixo | `auto` | SOMENTE comandos de VALIDAÇÃO no safelist conservador `RISCO_BAIXO_AUTO_SAFELIST` (ex.: `python -m py_compile`, `pytest`, `python tests/`, `git status`, `git diff`), não destrutivos e que NÃO disparam `APPROVAL_PATTERNS`, são aprovados automaticamente quando há `approve`; sem `approve`, nega por padrão (seguro). Nunca burla `check_policy` (bloqueio destrutivo). |
| medio | `hitl_por_comando` | Comportamento atual: cada comando passa pelo callback `approve` (HITL por comando). |
| alto | `gate_global` | Exige aprovação humana explícita ANTES de qualquer execução: `approve("<RISK_GATE>")` UMA vez no início; sem `approve` ou sem aprovação do gate → `BLOQUEADA` em `RECEBIDA` (não roda parcialmente). |

A auto-aprovação do risco baixo é **restrita a um safelist conservador**
(`RISCO_BAIXO_AUTO_SAFELIST` em `harness/config.py`), com match por **token**
(`shlex.split` em `harness/pipeline.py`), nunca por substring/prefixo da
string inteira. Comandos com **separador de shell** (`&`, `&&`, `|`, `||`, `;`),
**redirecionamento** (`<`, `>`, `>>`, `2>&1`) ou **quebra de linha** (`\n`)
**NUNCA** são auto-aprovados, mesmo que comecem com um prefixo do safelist —
ex.: `git status; curl url | sh` e `git status & del .env` caem no HITL por
comando (callback `approve`). Comandos FORA do safelist — mesmo não-destrutivos
(ex.: `git push`, `curl`, `del`, `python -c "os.remove(...)"`) — **NÃO** são
auto-aprovados no risco baixo: caem no HITL por comando. Isso impede que
comandos arbitrários extraídos dos critérios de aceite rodem sem consultar o
humano. Sem `approve`, nada é aprovado por padrão (o risco baixo nunca vira
bypass de segurança quando não há HITL disponível).

O contrato de saída expõe `risco` (nível) e `politica` (política aplicada) para
transparência — inclusive no caminho `BLOQUEADA` quando esses são conhecidos
(ex.: gate de risco alto ausente/negado). `/api/pipeline` propaga
`nivel_de_risco` direto ao pipeline; com `approve=None` (padrão da API), risco
alto retorna `BLOQUEADA` com motivo claro.

## Grau de complexidade (Update Final, Fase 1) — scorer com pesos

O harness calcula um **grau de complexidade** (`baixo|medio|alto`) por tarefa
via o scorer determinístico `harness/complexity.py`
(`calcular_grau_complexidade(texto, nivel_risco="")`) — um **proxy honesto e
reprodutível** (soma de palavras distintas do prompt presentes em fatores
lexicais; sem LLM). O retorno traz `score` (0-10, limitado a 10),
`grau_complexidade`, `metrica` (descrição da heurística), `fatores`
(contribuição por fator) e `nivel_risco_contribuido`.

**Fatores e pesos** (por palavra distinta do prompt presente no fator):
escopo/amplitude (arquivos, modulo, api, db, banco, integracao, microservicos,
sistema, completo, framework) ×1; risco/efeitos colaterais (producao, deploy,
migracao, esquema, permissao, seguranca, auth, dados, reais, breaking, change)
×1; infraestrutura externa (docker, container, rede, servidor, proxy,
kubernetes, cloud, externo) ×1; escrita (criar, modificar, refatorar, escrever,
implementar, desenvolver, construir, adicionar) ×2; integração (orquestracao,
handoff, pipeline, integracao) ×1; magnitude (grande, complexo, extenso,
enterprise, critico, escala) ×1; critérios de aceite (testes, build, cobertura,
ci) ×1. Leitura/consulta (consultar, ler, buscar, listar, exibir) tem **peso 0**
(contrapeso — tarefas só de leitura são menos complexas). **Bônus do
`nivel_risco`** (quando fornecido ao scorer): baixo=+1, medio=+2, alto=+3.
**Classificação**: 0-3=baixo, 4-8=médio, 9-10=alto. O score é **limitado a 10**.

**Override vs. input de risco** (tratado no pipeline `_monta_contrato`, não no
scorer):
- `grau_complexidade` **explícito** no task → **override**: pula o scorer e usa
  o valor fornecido (o cliente diz o resultado final desejado).
- `nivel_de_risco` **explícito e diferente do default** (`"baixo"`/`"alto"`) →
  é um **input para o scorer**: `_monta_contrato` chama
  `calcular_grau_complexidade(texto, nivel_risco=<risco_explícito>)`, aplicando
  o bônus (baixo+1/medio+2/alto+3) ao score lexical. NÃO pula o scorer — o
  nível de risco alimenta a derivação, não é o resultado.
- `nivel_de_risco` `None`/vazio/igual ao default (`"medio"`) → **não** é sinal
  explícito: o texto do prompt (objetivo+escopo+restrições+critérios) é passado
  ao scorer **SEM bônus de risco** (o default "medio" não soma bônus ao score
  automático).

**Derivações do grau**:
- `tempo_maximo_seg` (timeout SAFETY NET, Exception Ch12): baixo→600,
  medio→900, alto→1500; se o task fornecer `tempo_maximo_seg`, esse valor
  explícito é respeitado. No `run_task`, o decorrido é checado ENTRE etapas; ao
  ultrapassar, o pipeline encerra com `APROVADA_COM_RESSALVAS` + achado de
  timeout (`timeout_safety_net: True`), **preservando o progresso já
  registrado** (não é watchdog que mata no meio).
- `usa_sandbox`: True **apenas para grau alto** → o Executor tenta o container
  Docker (o executor decide se há Docker; senão fallback host). Baixo/médio
  ficam no **host** mesmo com Docker disponível.

O contrato de saída (`_consolida`) e o caminho de timeout expõem
`grau_complexidade`, `tempo_maximo_seg` e `usa_sandbox` para transparência. O
hub (`.opencode/agent/hub.md`) usa o scorer e respeita o override.

## Granularidade de contexto (Lote 1) — peso da tarefa

Otimização de consumo de tokens (Lote 1): o harness deriva QUANTO contexto a
LLM recebe em cada delegação do hub, por PESO da tarefa (grau de
complexidade). Níveis CONSERVADORES (`harness/config.py`) — nada de conteúdo
de arquivo é cortado ainda; apenas o mecanismo de escala determinístico (a
redução efetiva de contexto/compressão vem em etapa posterior).

- `NIVEIS_CONTEXTO = ("minimo", "padrao", "completo")`. O mapeamento
  `CONTEXTO_POR_COMPLEXIDADE = {baixo: minimo, medio: padrao, alto: completo}`
  deriva o `contexto_grau` a partir do grau de complexidade (`_monta_contrato`
  em `harness/pipeline.py`), exposto no `TaskContract` e no contrato de saída
  (`contexto_grau`).
- `RAG_LIMIT_POR_COMPLEXIDADE = {"minimo": 2, "padrao": 4, "completo": 6}`
  limita os snippets do RAG episódico na etapa `CONSULTANDO_MEMORIA`:
  `Memory.search(query, limit, snippet=True)` (Lote 1) devolve o corpo
  compactado (`_snippet_body`, ~300 chars) no índice episódico compacto
  `| Keywords | Id | Data |` — economiza tokens sem perder a localização do
  registro.
- **CLI do scorer**: `python -m harness.complexity "<texto>" [--risco
  baixo|medio|alto]` → JSON (`score`, `grau_complexidade`, `fatores`,
  `nivel_risco_contribuido`). O hub roda o scorer na `RECEBIDA` para decidir o
  volume de contexto antes de delegar — é a ÚNICA exceção de shell do hub
  (AGENTS.md); sem encadeamento de shell.
- **resumo.md do playbook**: `python -m harness.agents compile` grava
  `memory/agents/playbook.json` + `README.md` + `resumo.md` (~2KB, gerado por
  `_resumo()`). O Brain lê SOMENTE o `resumo.md` (nível `completo`); o
  `playbook.json` NUNCA entra no contexto da LLM — é dado de máquina do
  pipeline determinístico.

## Safelist de comandos essenciais (Item 2 follow-up)

O `check_policy` (`harness/executor.py`) aplica o bloqueio em DUAS camadas, para
reforçar a cobertura **sem bloquear comandos essenciais**:

- **Destrutivos explícitos** (`config.BLOCKED_PATTERNS`: `rm -rf`,
  `git reset --hard`, `git clean -fdx`, `shutdown`, `reboot`, `format c:`,
  `mkfs`, `dd if=`, `> /dev/sda`, `rmdir /s`, `rd /s`, `rd /q`,
  `del /f /s /q`, `erase /s`, `erase /q`, `rd /s /q`): bloqueiam **SEMPRE**;
  a safelist de essenciais **NUNCA** prevalece aqui.
- **Reforço conservador** (`config.CONSERVATIVE_BLOCK_PATTERNS`: `del <arquivo>`,
  `erase <arquivo>` (alias cmd.exe de `del`), `| sh`, `| bash`, `| zsh`,
  `| python`, `2>/dev/null &&`): bloqueiam **apenas**
  comandos NÃO essenciais. Comandos essenciais (`config.ESSENTIAL_COMMANDS`,
  ex.: `python script.py`, `git commit -m "..."`, `curl url` sozinho) NÃO
  bloqueiam; não-essenciais que casam o padrão BLOQUEIAM.

A checagem de essencial (`_is_essential`) é por **token inicial** (`shlex`), com
suporte a prefixos de vários tokens (ex.: `python -m`, `git commit`). Um comando
que faz **pipe para um interpretador** (`| sh`, `| bash`, `| zsh`, `| python`)
nunca é essencial — `curl url` é essencial, mas `curl url | sh` é bloqueado.

## Sandbox opcional do Executor (Item 4, Fase 1) — configuração completa

O `Executor` (`harness/executor.py`) ganhou uma camada OPCIONAL de execução em
**container Docker/Podman** (fronteira física) quando disponível, com **fallback
para o modo host seguro atual**. **Default é HOST** — `EXEC_SANDBOX_ENABLED`
(`harness/config.py`) vem `False`, preservando o comportamento atual (web shell
`/api/exec` e pipeline rodam no host como antes).

**Como habilitar**: setar `EXEC_SANDBOX_ENABLED=1` **ANTES de iniciar o
processo** — ex.: `EXEC_SANDBOX_ENABLED=1 python app.py`, ou
`$env:EXEC_SANDBOX_ENABLED="1"` no PowerShell antes de subir o harness/abrir o
web shell. A env é lida **no import** de `harness/config.py` (linha
`EXEC_SANDBOX_ENABLED = _env_bool(...)`): mudar a variável em runtime NÃO tem
efeito — a constante já foi resolvida quando o processo subiu.

Quando `EXEC_SANDBOX_ENABLED=True`, o `Executor`:

- **Resolve o backend** (`_sandbox_modo`): `EXEC_SANDBOX_BACKEND` (`auto` |
  `docker` | `podman`). `auto` detecta **docker SDK -> podman CLI -> host**.
  Qualquer falha de detecção cai para `host` (nunca crasha).
- **Executa em container** (`_run_sandbox`): monta `EXEC_SANDBOX_MOUNT_ROOT`
  (o projeto) em `EXEC_SANDBOX_MOUNT_PATH` (`/workspace`) e roda
  `docker run --rm --network none -v <root>:/workspace -w <cwd-no-mount>
  <imagem> sh -c "<comando>"` (ou podman equivalente). O **cwd host é
  traduzido** para o subcaminho dentro do mount (`_traduz_cwd`:
  `<projeto>\sub` -> `/workspace/sub`; fora do mount -> fallback
  `/workspace`). Captura stdout/stderr e **exit_code reais**. Timeout via
  thread + `_matar_arvore` (padrão do motor).
  **Fase 2 (hardening)**: o container roda com `--network none` por padrão
  (isolamento de rede); a imagem é customizável via `EXEC_SANDBOX_IMAGE`
  (mecanismo recomendado para incluir git/compiladores — instalação no
  runtime não tem rede por causa do `--network none`); limites de recurso e
  privilégios configuráveis (`EXEC_SANDBOX_READ_ONLY`, `EXEC_SANDBOX_MEMORY`,
  `EXEC_SANDBOX_CPUS`, `EXEC_SANDBOX_PIDS_LIMIT`, `--cap-drop ALL`,
  `no-new-privileges` e `--user` opcional). O Popen
  do CLI é registrado no job e `stop()` mata o `docker run` (taskkill/
  terminate) de forma análoga ao host.
- **Cai para host** se o container falhar (Docker indisponível, mount falha,
  erro) — nunca bloqueia o usuário por falta de sandbox. O snapshot do job
  expõe `sandbox: True/False` para transparência (rodou em container ou host).
  Em especial, quando o CLI docker existe mas o **daemon está inativo** (erros
  de CONECTIVIDADE do daemon), o `_run_sandbox` reconhece a falha de
  INFRAESTRUTURA via `_eh_falha_infra_daemon` e retorna `None` -> o job cai
  para o **modo host seguro** em vez de reportar erro do comando. As
  assinaturas de conectividade cobrem o Docker (`cannot connect to the docker
  daemon`, `dockerdesktoplinuxengine`, `is the docker daemon running`) E o
  Podman (`podman.sock`, `cannot connect`/`connection refused` COM contexto de
  daemon), além de `error during connect`, `internal server error` e HTTP
  `500`. **A1 (re-revisão Fase 2)**: as assinaturas genéricas `cannot connect`
  e `connection refused` SÓ valem acompanhadas de contexto de daemon
  (`docker`/`podman`/`daemon`/`containerd`) — sem contexto são saída LEGÍTIMA
  de um comando que rodou no container (ex.: `curl: connection refused` numa
  porta fechada) e NÃO podem ser classificadas como falha de infra (senão o
  comando cairia para o host, re-executando isolado no host). Um `exit != 0`
  de um comando que RODOU no container (isolamento funcionou) continua sendo
  **erro do comando** — NÃO cai para host, preservando o isolamento (não
  re-executa no host um comando que falhou isolado).
- **`check_policy` continua valendo SEMPRE, em qualquer modo**: o bloqueio
  destrutivo (`rm -rf`, `git reset --hard`, etc.) acontece ANTES de rodar no
  container, e o `cwd` continua validado contra `ALLOWED_CWD` (mesma disciplina
  de `_resolve_cwd`).

Configuração: `EXEC_SANDBOX_ENABLED`, `EXEC_SANDBOX_IMAGE` (`python:3-slim`;
  Fase 2 — customizável para imagem com git/compiladores),
`EXEC_SANDBOX_TIMEOUT` (60s), `EXEC_SANDBOX_MOUNT_ROOT`,
`EXEC_SANDBOX_BACKEND`, `EXEC_SANDBOX_MOUNT_PATH` (`/workspace`) e o hardening
da Fase 2 (`EXEC_SANDBOX_USER`, `EXEC_SANDBOX_READ_ONLY`,
`EXEC_SANDBOX_MEMORY`, `EXEC_SANDBOX_CPUS`, `EXEC_SANDBOX_PIDS_LIMIT`,
`EXEC_SANDBOX_CAP_DROP`, `EXEC_SANDBOX_NO_NEW_PRIVS`). O sandbox do
Executor é
independente do `SandboxRunner` do motor (`harness/motor/sandbox.py` — que roda
`python -c` para validação de código LLM); o Executor roda **comandos
arbitrários de shell**, reutilizando o conceito de resolução de modo/constantes,
mas sem importar o runner do motor.

**Ativação condicional por complexidade (Update Final, Fase 1)**:
`EXEC_SANDBOX_ALTO_ONLY` (`harness/config.py`, default `False`) liga o sandbox
apenas para tarefas de complexidade ALTA (via `usa_sandbox=True` vindo do
pipeline); baixo/médio ficam no **host**. Prioridade: override explícito
(`EXEC_SANDBOX_ENABLED=1` força para qualquer comando; `usa_sandbox` explícito
no `Executor.run(...)` — `False` força host, `True` tenta container) >
sandbox condicional por complexidade. O default de ambos é `False` — **a
ativação NÃO é global por padrão** (é por complexidade alta). O `Executor.run`
aceita `usa_sandbox` e o pipeline repassa `contrato.usa_sandbox`.

### Motor determinístico — zero-poisoning e isolamento (Item 7.1)

No motor (`harness/motor`), a promessa de **zero-poisoning** significa
**não persistir código que falhou**: o `AgenticHarnessEngine` só grava no cache
um artefato cujo código retornou `exit 0` no sandbox (e cujos `test_cases`,
quando fornecidos, também passaram). Nada com `exit != 0` é persistido.

O **ISOLAMENTO**, porém, depende do modo resolvido pelo `SandboxRunner`
(`harness/motor/sandbox.py`), na ordem **`docker` -> `podman` -> `exec`**:

- `docker` (SDK com daemon ativo) ou `podman` (CLI) -> o código roda em
  **container**, com `--network none` (sem rede do host);
- sem container disponível -> **safe mode `exec`**: `subprocess` com
  `shell=False`, Python `-I`, `env` mínimo e timeout com kill real, rodando
  **no HOST**. O `exec` NÃO oferece isolamento de host (é apenas a execução
  segura de fallback).

Para transparência, o modo de validação é exposto no resultado como
`sandbox_modo` (`docker` | `podman` | `exec`): campo declarado da dataclass
`ExecutionResult` (default `""`), portanto incluído em `dataclasses.asdict()`,
tanto via API quanto no JSON da CLI do motor.

**Endurecimento opt-in**: com `MOTOR_REQUER_CONTAINER=True` (variável de
ambiente `MOTOR_REQUER_CONTAINER`, lida no **import** de
`harness/motor/config.py`; default `False`), o motor exige container em dois
pontos. No **miss**, NÃO persiste artefato validado fora de container: quando o
modo não é `docker`/`podman`, retorna `ExecutionResult(success=False,
error="... isolamento de container exigido; nada persistido ...")` e não grava
nada. No **cache-hit** (fast-path), NÃO serve o artefato cacheado: com o modo
fora de `docker`/`podman`, retorna `ExecutionResult(success=False,
error="... cache-hit não servido ...")` sem incrementar `hit_count` e sem
invalidar o registro (só um replay que rodou de fato conta como hit). O default
`False` preserva o comportamento anterior (persiste e serve o cache-hit em
qualquer modo).

### Detecção do CLI docker fora do PATH (Docker Desktop)

O `docker` CLI do Docker Desktop pode NÃO estar no PATH (ex.: instalado em
`%LOCALAPPDATA%\Programs\DockerDesktop`), e o SDK docker (`import docker`) é
opcional e pode não estar instalado. Para não depender de PATH/SDK, a detecção
(`_docker_disponivel`/`_docker_cli_path` em `harness/executor.py`) procura o CLI
na ordem: (1) `config.EXEC_SANDBOX_DOCKER_CLI` (caminho explícito opcional);
(2) `shutil.which("docker")` (PATH); (3) candidatos comuns do Docker Desktop
(`config.EXEC_SANDBOX_DOCKER_CLI_CANDIDATES`, com `%ProgramFiles%`/
`%LOCALAPPDATA%` expandidos): `%ProgramFiles%\Docker\Docker\resources\bin\docker.exe`
e `%LOCALAPPDATA%\Programs\DockerDesktop\resources\bin\docker.exe`. Quando o
caminho é encontrado, `_run_sandbox` invoca o CLI pelo **caminho absoluto**
(`docker run ...`) em vez de depender de `docker` no PATH. O SDK docker continua
sendo fallback se o CLI não for localizado. A detecção confirma apenas a
**presença do CLI**, não a atividade do daemon.

## Gravação de registro no pipeline (`gravar_registro`)

O `AgentPipeline` (`harness/pipeline.py`) recebe o parâmetro
`gravar_registro: bool = True` (default True, retrocompatível: `AgentPipeline()`
continua gravando). Quando `False`, o pipeline **não grava** registro episódico
ao final (`_grava_registro` é pulado). Execuções de **teste/validação** devem
passar `gravar_registro=False` para não gerar **lixo de memória** (ex.: registros
`validacao-pipeline-risco-*` criados a cada execução da suíte). O endpoint
`/api/pipeline` (`harness/server.py`) roda com `gravar_registro=False` (F4 —
dry-run via web: nenhum comando é aprovado sem `approve`, então gravar
registros só geraria lixo); a suíte `tests/validation.py` exercita a mesma
lógica via a classe com `gravar_registro=False`.

## Conhecimento de segurança (LOTE 5)

Síntese das 10 referências de segurança coletadas em 2026-08-16 (detalhe em
`memory/references/`; índice 49×49 em `memory/references/index.md`).

- **Metodologia de pentest** (Weidman, Kim, Stuttard/Pinto): processo
  estruturado — escopo/autorização -> recon (OSINT/scan) -> enumeração/
  mapeamento -> exploração -> pós-exploração (pivoting, privilege escalation)
  -> relatório reproduzível com evidência. Ordem web: mapear -> authn -> sessão
  -> acesso -> entrada -> lógica -> infra. Red team: assumed breach, Living off
  the Land, credenciais como alvo central.
- **Segurança web** (Stuttard/Pinto, Zalewski, Li): classes OWASP (broken
  authn, IDOR, SQL/NoSQL injection, XSS refletido/armazenado/DOM, CSRF,
  clickjacking, XXE, SSRF, template injection, deserialização). Regras: todo
  input do cliente é hostil; validar no servidor; sanitizar por contexto de
  saída (HTML/atributo/JS/CSS/URL); same-origin policy é a base; Content-Type
  correto + nosniff; cookies HttpOnly/Secure/SameSite; encodings permissivos
  criam bypass entre browser e servidor.
- **Hardening/sandbox** (Erickson, Anderson, Kohnfelder): fail-safe defaults
  (negar por padrão), defesa em profundidade, menor privilégio, superfície de
  ataque mínima, separação de privilégios, validar tamanhos/tipos em parsers
  (nunca confiar no remetente). Namespaces/seccomp: ver
  `container-security-liz-rice.md`.
- **DevSecOps / secure design** (Kohnfelder, Anderson): ameaçar o design antes
  do código — mapear ativos, attack surface e trust boundaries; STRIDE
  (Spoofing, Tampering, Repudiation, Info disclosure, DoS, Elevation of
  privilege); mitigação em camadas; revisão de design sem julgamento; preferir
  bibliotecas consagradas a cripto caseira; modelar o adversário real
  (economia, incentivos, usabilidade).
- **Cyber ops / blue team** (Bejtlich, Forshaw): NSM pressupõe a invasão
  ("quando", não "se"); 3 tipos de dado (full content, session, transaction);
  ciclo coleção -> análise -> escalação -> resolução com evidência; protocolos
  são código — fuzzing, validação de limites, captura/replay.
- **Aplicação ao harness**: pentest do web shell (app.py:8500) e da API;
  headers de segurança (CSP/nosniff/frame-ancestors) e escape de saída contra
  XSS; `logs/harness_history.json` como trilha NSM; STRIDE sobre trust boundaries
  hub/agentes/shell; guardrails de `config.py` como fail-safe defaults.

### Compliance de PDFs

- A política anti-pirataria do harness (deny-list) permanece intacta para
  execuções automáticas; PDFs ficam sob `sidePrjs/<projeto>/docs/livros/`.
- Lição: PDFs de segurança raramente são gratuitos de forma legítima — preferir
  metadados editoriais (2+ fontes) + amostras oficiais a cópias de terceiros.

## Apêndice — texto integral das seções resumidas no core.md (Lote 2)

As seções abaixo foram RESUMIDAS no `memory/core.md` por instrução do Lote 2
(Trust em resumo curto; RAG consultivo e Observabilidade em 1 parágrafo). O
texto integral original é preservado aqui, sem perda de informação.

### Base de memória (bullet do motor, original)

- O **motor determinístico semântico** (`harness/motor/`, subpackage
  `harness.motor`) é o cache-first: embedding local 384d + cosseno >= 0.92 →
  fast-path < 50ms; miss → fallback LLM opcional; sandbox de validação com
  zero-poisoning. É um **pacote AUTÔNOMO** (não carregado no boot do harness;
  importável sob demanda ou via CLI própria) — antes vivia em sidePrjs/.

### Confiança e rastreabilidade (Trust) — texto integral original

Princípio: **nenhuma memória é gravada sem origem rastreável**; registros com
origem fraca ou não validada ficam marcados para que o harness (RAG/brain) não
os trate como conhecimento confirmado. Previne a "fraude de agente" — o harness
nunca se auto-alimenta com conteúdo fabricado sem evidência.

- Todo registro episódico (`memory/episodic/`) e referência
  (`memory/references/`) carrega no frontmatter: `trust: alta|media|fraca`,
  `origem` (texto rastreável) e `validado_por` (quem validou: reviewer,
  implementer, hub, motor, human).
  - **alta** — validado por evidência externa/editorial (ex.: referência de
    livro confirmada em >= 2 fontes editoriais) ou execução com evidências
    (tests/build).
  - **media** — validado parcialmente (ex.: conhecimento indireto por tópicos
    com >= 2 fontes públicas; contrato de saída com ressalvas).
  - **fraca** — sem validação ou origem não rastreável (ex.: alerta de
    fabricação, conteúdo não confirmado, ausência de evidência).
- Registros **antigos sem `trust`** assumem **fraca** na leitura (conservador):
  memória não validada nunca é tratada como conhecimento confirmado
  (`trust_de()` em `harness/memory.py`; `parse_episode` em `harness/agents.py`).
- O RAG/brain **não trata memória com trust fraca como conhecimento confirmado**;
  a curva de aprendizado do playbook (`memory/agents/playbook.json`) marca cada
  lição/achado com o trust do episódio de origem e expõe `n_licoes_fracas` /
  `n_licoes_confiaveis` por agente.
- As lições da curva são agregadas **por parágrafo** (não por linha física):
  `harness/agents.py` concatena as linhas de continuação de um mesmo parágrafo
  (`_agrupa_paragrafos`) antes do filtro de qualidade (`_e_licao_util`) e da
  agregação, evitando que um parágrafo quebrado em várias linhas vire várias
  lições fragmentadas (contagem mais honesta).
- Mapeamento do webscraper (`harness/webscraper.py`): `ok` (>= 2 fontes
  editoriais) → **alta**; `validacao_indireta` → **media**; `fabricacao`
  (alerta) → **fraca**.
- **Validação anti-fabricação (Fase 3)**: o webscraper **prioriza as APIs de
  livros** (Google Books/Open Library/Internet Archive — JSON estável) e trata
  a **busca web como bônus best-effort** (nunca derruba a validação). Existe =
  >= 2 fontes independentes: 2+ APIs distintas, ou 1 API + 1 resultado web
  editorial **relevante** (domínio editorial que cita a obra —
  `_resultado_relevante`; homepage/landing genérica NÃO conta). Quando há
  evidência **parcial** (>= 1 API real confirma a obra, mas abaixo do limiar),
  `validate_fabrication` retorna `existe: False` + `indeterminado: True` → o
  `coletar` grava `<slug>-nao-confirmado.md` (status `nao_confirmado`,
  evidência insuficiente, trust **fraca**) em vez de um alerta duro de
  fabricação (evita falso alerta quando a API confirma). Sem nenhuma evidência
  real de API → fabricação (alerta `<slug>-fabricacao.md`, trust fraca).
  Falha de requisição de API (rede/429/JSON inválido) é exposta em `erros_api`
  no retorno de `validate_fabrication` (não tratada como "0 resultados"
  silencioso). A anti-fabricação permanece: nunca inventa, nunca aceita obra
  sem >= 2 fontes independentes.
- Registros gravados via **web shell** (`POST /api/memory`) são inseridos por
  um humano → ficam marcados `validado_por: human` no frontmatter
  (rastreabilidade). Quando `validado_por` é `human` (validação humana
  explícita) e o corpo NÃO fornece `trust`, o trust sobe para **media** (HITL —
  validação humana explícita não é memória sem validação = fraca); se o cliente
  enviar `validado_por` (ex.: `motor`) ou `trust` explicitamente no corpo, o
  valor enviado é respeitado (validado_por=motor sem trust mantém o default
  `fraca`).

### RAG consultivo sobre referências (Item 7) — texto integral original

Capacidade consultiva (`harness/rag_refs.py`, endpoint `GET /api/refs/query`)
que busca e sintetiza conhecimento das referências validadas em
`memory/references/`:

- **Busca por relevância**: reutiliza a heurística do `Memory.search`
  (tokens; 3×tags + 2×título + 1×corpo/seções), adaptada às referências,
  retornando-as ranked por score.
- **Filtro de trust**: por padrão retorna apenas referências `alta`/`media`
  na **busca** (`fontes`). `incluir_fraca=True` (parâmetro de depuração)
  inclui `fraca` mas marcadas como **não-confirmadas** — nunca apresentadas
  como conhecimento confirmado. Referências sem o campo `trust` valem
  **fraca** (default conservador).
- **Síntese determinística**: `consultar(query, limit, incluir_fraca)` retorna
  `query`, `fontes` (com score/id/titulo/autor/trust/sintetizavel/trechos),
  `sintese` estruturada (`conceitos_chave`, `padroes_acionaveis`,
  `aplicacao_no_motor`, `pontos_de_atencao`) com citação da origem (`id`),
  `resumo` (parágrafo objetivo montado das seções reais) e `advertencias`.
  **A síntese e o resumo são alimentados SOMENTE por referências de trust
  `alta`** (`sintetizavel=True`); referências `media` permanecem disponíveis
  na busca (`fontes`) e são consultáveis, mas **não confirmam conhecimento**
  nem alimentam a síntese. Quando há `media` mas nenhuma `alta`, a síntese
  fica vazia com advertência específica (não é lacuna total de busca).
  **Não inventa conteúdo** — stdlib-only, sem LLM obrigatório.

### Observabilidade do hub (Item 6) — texto integral original

Capacidade de observação da EVOLUÇÃO do aprendizado (`harness/observability.py`,
endpoint `GET /api/observability`), que agrega três fontes somente-leitura —
registros episódicos (`Memory().list_records()`), histórico de comandos
(`logs/harness_history.json`) e playbook (`load_playbook()`) — num panorama
JSON (`gerar_panorama(memory, playbook, history_file)`):

- **Resumo geral**: total de registros e distribuições por `status`, `trust`
  (alta/media/fraca — default conservador), `agente` (multi-tag: cada menção
  no rótulo conta para o agente) e `data` (atividade por dia).
- **Resolução de tarefas**: direta via memória vs. delegada — heurística
  documentada no retorno (`metrica`/`resolucao.heuristica`), proxy declarado:
  delegado = rótulo de agente com implementer/reviewer OU corpo/keywords com
  `EM_IMPLEMENTACAO`/`EM_REVISAO`/implementer/reviewer.
- **Tempo**: proxy de atividade diária (média de registros por dia ativo) —
  sem timestamps de início/fim persistidos, não se inventa métrica de duração.
- **Re-trabalho**: registros com a sub-seção `achados_da_revisao` no Resultado
  (com/sem achados, taxa, total de bullets) + curva por agente do playbook
  (`n_licoes`/`n_licoes_fracas`/`n_achados`) quando disponível. A chave
  `retrabalho.fonte_curva` indica a fonte da curva por agente — `"playbook"`
  (`n_achados` = achados distintos agregados) ou `"episodios"` (`n_achados` =
  nº de registros do agente com a seção `achados_da_revisao`); a semântica é
  documentada em `retrabalho.heuristica`.
- **Evolução do playbook**: `schema_version`, data, nº de agentes e o resumo
  de `learned.por_agente` (indica se o playbook existe ou não).
- **Sugestões** objetivas (ex.: registros com trust fraca candidatos a
  revisão; agente com mais lições fracas a avaliar).
- Top-N de exibição limitado por `MAX_OBS_LIMIT` (`config.py`); as
  distribuições completas (`resumo_geral.por_*`) nunca são truncadas.
- Complementa `harness/eval_memory.py` (saúde da memória: reuso/órfãos): o
  Item 6 mede a evolução do HUB/aprendizado, não a saúde da memória.

### Padrões agentic aplicados — texto integral original (Evaluation)

- Evaluation: saídas validadas contra critérios (`harness/eval.py`); fecho de
  avaliação da própria memória em `harness/eval_memory.py` (relatório em
  `memory/eval/<data>-memoria.md`, endpoint `GET /api/memory/stats`) — mede
  reuso e órfãos da memória episódica; somente leitura, poda é sugestão.
- Guardrails: políticas no runtime, não no prompt. 
