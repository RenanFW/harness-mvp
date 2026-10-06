"""Configurações do harness: políticas de execução e caminhos."""

import os
import pathlib

# Raiz do projeto (diretório do package)
ROOT = pathlib.Path(__file__).resolve().parent.parent

# Diretório de memória compartilhado com o Brain (agente opencode)
MEMORY_DIR = ROOT / "memory"
EPISODIC_DIR = MEMORY_DIR / "episodic"
INDEX_FILE = EPISODIC_DIR / "index.md"

# Relatório de avaliação da memória (Ch19 aplicado à memória episódica):
# saída de `harness/eval_memory.py` (gerado, não versionado como fonte).
EVAL_DIR = MEMORY_DIR / "eval"
# Top-N de EXIBIÇÃO (tags / consultas com miss) no relatório de avaliação da
# memória. A detecção de reuso/órfão NÃO usa este limite (varre a memória
# inteira — ver harness/eval_memory.py).
MAX_EVAL_TOP = 10

# Observabilidade do hub (Item 6): top-N de EXIBIÇÃO das listas do panorama
# (`harness/observability.py`) — top agentes/dias e a curva por agente do
# playbook. As DISTRIBUIÇÕES completas (`resumo_geral.por_*`) nunca são
# truncadas — só as listas de topo/curva levam o corte.
MAX_OBS_LIMIT = 10

# Playbook do sistema interno de agentes (aprendido do opencode)
AGENTS_DIR = MEMORY_DIR / "agents"
PLAYBOOK_FILE = AGENTS_DIR / "playbook.json"

# Fonte de verdade: configuração dos agentes opencode (somente leitura).
# Caminhos ABSOLUTOS ancorados em config.ROOT; caminhos RELATIVOS usados
# pelos parsers de `harness/agents.py` que aceitam `root` customizado (testes
# com árvore temporária mockam config.ROOT — o layout é definido num só lugar
# e o parser nunca aponta para o projeto real quando `root` é customizado).
OPCODE_DIR = ROOT / ".opencode"
OPCODE_AGENT_DIR = OPCODE_DIR / "agent"
OPCODE_REL_DIR = pathlib.Path(".opencode")
OPCODE_REL_AGENT_DIR = OPCODE_REL_DIR / "agent"
OPCODE_REL_SKILLS_DIR = OPCODE_REL_DIR / "skills"

# Schema obrigatório do frontmatter de skills de domínio (.opencode/skills/*/
# SKILL.md, modelo Agent Skills): `origem` rastreável + `trust` seguem o mesmo
# princípio anti-fabricação das referências (ver TRUST abaixo e memory/core.md).
# `description` é o gatilho de carga on-demand (o agente carrega a skill quando
# a tarefa casa com a descrição). Validação em harness/skills.py.
SKILLS_CAMPOS_OBRIGATORIOS = (
    "name",
    "description",
    "tipo",
    "dominio",
    "origem",
    "trust",
    "validado_por",
    "data",
    "tags",
)

# Diretório web (terminal no navegador)
WEB_DIR = ROOT / "web"

# Referência padrão para projetos novos criados via /hub ou agentes locais.
# Cada projeto vira uma subpasta sidePrjs/<nome>/ (nome gerado pelo nomeador).
SIDE_PRJS_DIR = ROOT / "sidePrjs"

# Modelo de confiança (Trust) — ver memory/core.md: nenhuma memória é gravada
# sem origem rastreável. Níveis normalizados usados no frontmatter de registros
# episódicos (memory.py) e referências do webscraper (webscraper.py).
#   alta  — validado por evidência externa/editorial (>= 2 fontes editoriais)
#           ou execução com evidências (tests/build).
#   media — validado parcialmente (conhecimento indireto por tópicos com
#           fontes públicas; contrato de saída com ressalvas).
#   fraca — sem validação ou origem não rastreável (alerta de fabricação,
#           conteúdo não confirmado, ausência de evidência).
TRUST = {
    "alta": "alta",
    "media": "media",
    "fraca": "fraca",
}
# Ordem do mais conservador ao mais confiável (para agregar pelo pior trust).
TRUST_ORDEM = ("fraca", "media", "alta")
# Default conservador: registros antigos ou sem trust NÃO são tratados como
# conhecimento confirmado (memória não validada vale fraca).
TRUST_DEFAULT = "fraca"

# Recuperabilidade (Item 2.1 — modelo de confiança): campo booleano
# `recuperavel` no frontmatter de registros/referências. Default `True`
# quando AUSENTE (conservador para não perder memória antiga). SOMENTE o
# alerta de FABRICAÇÃO (status `fabricacao` / `<slug>-fabricacao.md`) é gravado
# com `recuperavel: false` — fabricação explícita é tóxica e NUNCA deve ser
# recuperada por `Memory.search`/RAG. `nao_confirmado` NÃO é não-recuperável:
# é evidência insuficiente (trust fraca normal, apenas rotulada), portanto
# continua `recuperavel: true`.
RECUPERAVEL_DEFAULT = True

# Trust mínimo recuperado no RAG episódico do PIPELINE (Item 2.2 — rotular ->
# filtrar). `Memory.search` aceita `min_trust` (default None = sem filtro,
# retrocompatível); o pipeline passa este nível em CONSULTANDO_MEMORIA para que
# memória `fraca` (e não-recuperável) NÃO alimente o pipeline como conhecimento
# confirmado. `media` mantém disponível o conhecimento parcial validado e
# bloqueia o que é apenas fraco.
RAG_MIN_TRUST = "media"

# Self-awareness (Etapa 3): detector DETERMINÍSTICO de degradação do modelo
# (`harness/health.py`). NÃO usa LLM como juiz (um modelo degradado se
# auto-avalia como "ótimo"): cada fator é um PROXY documentado, derivado de
# artefatos que o harness já produz (playbook, episódicos, histórico de
# comandos). Somente leitura — nenhuma fonte é alterada.
# Limiares de nível de saúde (score 0..1):
#   score >= SAUDE_LIMIAR_SAUDAVEL (0.75) -> SAUDAVEL
#   score >= SAUDE_LIMIAR_ATENCAO  (0.55) -> ATENCAO
#   score >= SAUDE_LIMIAR_DEGRADADO(0.35) -> DEGRADADO
#   score <  SAUDE_LIMIAR_DEGRADADO       -> CRITICO
SAUDE_LIMIAR_SAUDAVEL = 0.75
SAUDE_LIMIAR_ATENCAO = 0.55
SAUDE_LIMIAR_DEGRADADO = 0.35
# Pesos da média ponderada dos fatores (fonte única do score). A soma é 1.0;
# cada chave casa com um fator de `harness/health.py::gerar_saude`.
SAUDE_PESOS = {
    "licoes_confiaveis": 0.25,
    "evidencia_substantiva": 0.25,
    "contrato_completo": 0.15,
    "historico_saudavel": 0.15,
    "achados_reviewer": 0.20,
}

# Ladder de resposta adaptativa (Etapa 5): transforma a DETECÇÃO de saúde
# (Etapa 3) em AÇÃO sobre o pipeline. Os níveis reusam `SAUDE_LIMIAR_*`:
#   SAUDAVEL -> comportamento normal;
#   ATENCAO  -> informativo (não muda gates; apenas expõe `saude`/etapa);
#   DEGRADADO-> endurece: força risco "alto" (gate_global -> exige RISCO_GATE),
#               força `usa_sandbox=True` e mantém o anti-envenomamento da
#               gravação de memória;
#   CRITICO  -> o mesmo endurecimento + RECUSA a execução até aprovação humana
#               explícita via `approve(SAUDE_GATE_CRITICO)`. Marcador passado ao
#               callback `approve` UMA vez; sem aprovação/negado -> BLOQUEADA
#               antes de qualquer comando.
SAUDE_GATE_CRITICO = "<SAUDE_CRITICA>"

# Golden probes (Etapa 4) — sensor ATIVO de qualidade do modelo
# (`harness/probes.py`). Diferente da saúde passiva (Etapa 3), as probes medem
# a taxa de acerto do modelo em tarefas de resposta conhecida, com checador
# 100% determinístico (nunca outro LLM). Limiares do `nivel` da taxa (score
# 0..1):
#   score >= PROBE_LIMIAR_SAUDAVEL (0.90) -> SAUDAVEL
#   score >= PROBE_LIMIAR_ATENCAO  (0.70) -> ATENCAO
#   score >= PROBE_LIMIAR_DEGRADADO(0.50) -> DEGRADADO
#   score <  PROBE_LIMIAR_DEGRADADO       -> CRITICO
# CALIBRAÇÃO: a baseline por modelo deve ser medida em ALTO esforço antes de
# fixar estes limiares (medir em esforço baixo gera falsos degradados).
PROBE_LIMIAR_SAUDAVEL = 0.90
PROBE_LIMIAR_ATENCAO = 0.70
PROBE_LIMIAR_DEGRADADO = 0.50
# Margem de degradação em relação à baseline do modelo: `degradado` quando a
# taxa atual cai MAIS que esta margem abaixo da baseline.
PROBE_MARGEM_DEGRADACAO = 0.15
# Arquivo de baseline de saúde por modelo (gravado por
# `harness.probes.salvar_baseline`). Fica em `logs/` (runtime, não versionado).
HEALTH_BASELINE_FILE = ROOT / "logs" / "health_baseline.json"

# Motor determinístico semântico (subpackage harness.motor) é um PACOTE
# AUTÔNOMO, importável sob demanda (`import harness.motor`); o runtime do
# harness (pipeline/executor/servidor) NÃO o importa no boot.

# Porta padrão do servidor web
DEFAULT_PORT = 8500

# Diretórios permitidos para execução de comandos
ALLOWED_CWD = {ROOT}

# Comandos sempre bloqueados (destrutivos ou perigosos). Estes padrões são
# EXPLICITAMENTE DESTRUTIVOS: o `check_policy` (executor.py) os aplica SEMPRE,
# e a safelist de comandos essenciais (`ESSENTIAL_COMMANDS`) NUNCA prevalece
# aqui. Ex.: `rm -rf`, `git reset --hard` e `shutdown` bloqueiam mesmo que o
# comando comece com um token essencial (`git`).
#
# COBERTURA DE `rm` (F1): os padrões de string abaixo (`rm -rf`, `rm -fr`)
# cobrem as formas coladas comuns. As formas ESPAÇADAS / em qualquer ordem /
# caixa mista (`rm -r -f x`, `rm -f -R x`, `rm -rF`, `rm -Rf`, `rm -r --force`)
# são cobertas por FLAGS pelo helper `executor._rm_destrutivo`, chamado ANTES
# deste loop no `check_policy` — não duplicar aqui (manteria a lista inchada e
# não cobriria todas as permutações).
BLOCKED_PATTERNS = [
    "rm -rf",
    "rm -fr",
    "git reset --hard",
    "git clean -fdx",
    "git clean -fd",
    "shutdown",
    "reboot",
    "format c:",
    "mkfs",
    "dd if=",
    "> /dev/sda",
    "rmdir /s",
    "rd /s",
    "rd /q",
    "del /f /s /q",
    "erase /s",
    "erase /q",
    "rd /s /q",
]

# Padrões de REFORÇO CONSERVADOR (Follow-up do Item 2 — HITL): cobrem casos que
# o BLOCKED_PATTERNS clássico NÃO pegava (`del <arquivo>`, `curl ... | sh`,
# `wget ... | bash`, pipe para interpretador). Diferente dos destrutivos, estes
# podem ser RELAXADOS pela safelist de comandos essenciais: se o comando for
# essencial (`_is_essential` em executor.py), NÃO bloqueia; se não for essencial
# e casar um destes padrões, BLOQUEIA. O match é por substring (lowercase),
# igual ao BLOCKED_PATTERNS.
CONSERVATIVE_PIPE_TO_SHELL = (
    "| sh",
    "| bash",
    "| zsh",
    "| python",  # pipe para interpretador (ex.: curl url | python)
)
CONSERVATIVE_BLOCK_PATTERNS = (
    "del ",  # `del <arquivo>` (sem -r): destrutivo pontual de arquivo
    "erase ",  # alias cmd.exe de `del` (`erase <arquivo>`): mesmo tratamento
) + CONSERVATIVE_PIPE_TO_SHELL + (
    "2>/dev/null &&",  # suprime erros e encadeia (ofusca falha)
)

# Safelist de comandos ESSENCIAIS do dia a dia que o harness precisa executar.
# Prevalece sobre os padrões de REFORÇO CONSERVADOR (CONSERVATIVE_BLOCK_PATTERNS),
# mas NUNCA sobre os padrões explicitamente destrutivos (BLOCKED_PATTERNS).
# O match em executor.py é por TOKEN inicial (shlex), com suporte a prefixos de
# vários tokens (ex.: `python -m`, `git commit`). Comandos que fazem PIPE para
# um interpretador (`| sh`, `| bash`, `| zsh`, `| python`) nunca são tratados
# como essenciais — ex.: `curl url` (sozinho) é essencial, mas `curl url | sh`
# NÃO é (é bloqueado).
ESSENTIAL_COMMANDS = (
    # linguagens / ferramentas de validação
    "python", "python3", "pip", "pip3", "pytest",
    "node", "npm", "npx", "gcc", "g++", "make", "cmake",
    # git (operações essenciais; destrutivas como `reset --hard` bloqueiam antes)
    "git", "git status", "git diff", "git log", "git add",
    "git commit", "git checkout", "git branch", "git pull", "git push",
    # navegação / inspeção / arquivos
    "cd", "dir", "ls", "pwd", "type", "copy", "move", "mkdir",
    "find", "where", "cat", "touch",
    # rede essencial (sozinho); `curl|sh`/`wget|sh` são bloqueados pelo pipe
    "curl", "wget",
    # prefixos compostos comuns
    "python -m", "python tests/",
)

# Sandbox de execução do Executor (Item 4, Fase 1) — isolamento em container
# Docker/Podman OPCIONAL. Quando `EXEC_SANDBOX_ENABLED=True`, o Executor tenta
# rodar o comando dentro de um container (fronteira física) montando o working
# tree do projeto; se o container não estiver disponível ou falhar, cai para o
# modo host seguro atual (com `check_policy` mantido). Default `False` =
# comportamento atual preservado (host). `check_policy` continua valendo em
# ambos os modos (bloqueio destrutivo nunca é relaxado pelo sandbox).
#
# CONFIGURÁVEL POR VARIÁVEL DE AMBIENTE (Pendência B1): a variável
# `EXEC_SANDBOX_ENABLED` pode ligar/desligar o sandbox sem editar código. Os
# valores são interpretados como booleano:
#   "1" / "true" / "yes"   -> True (sandbox habilitado)
#   "0" / "false" / "no"   -> False (sandbox desabilitado)
# Ausente ou valor INVÁLIDO -> False (default seguro, comportamento atual
# preservado). O default segue `False` mesmo sem a variável no ambiente.
# ATENÇÃO: a env é lida NO IMPORT deste módulo (linha `EXEC_SANDBOX_ENABLED =
# _env_bool(...)` abaixo) — precisa estar setada ANTES de iniciar o processo
# (ex.: `EXEC_SANDBOX_ENABLED=1 python app.py`, ou
# `$env:EXEC_SANDBOX_ENABLED="1"` no PowerShell antes de subir o harness).
# Mudar a variável em runtime NÃO tem efeito: a constante já foi resolvida no
# import.
def _env_bool(name: str, default: bool = False) -> bool:
    """Lê `name` de os.environ e interpreta como booleano.

    Aceita "1"/"true"/"yes" -> True; "0"/"false"/"no" -> False; ausente ou
    valor inválido -> `default` (False por padrão). Case-insensitive."""
    val = os.environ.get(name)
    if val is None:
        return default
    v = val.strip().lower()
    if v in ("1", "true", "yes", "on"):
        return True
    if v in ("0", "false", "no", "off"):
        return False
    return default


def _env_int(name: str, default: int) -> int:
    """Lê `name` de os.environ e interpreta como inteiro >= 0; ausente ou
    valor inválido -> `default`."""
    val = os.environ.get(name)
    if val is None:
        return default
    try:
        parsed = int(val.strip())
    except (TypeError, ValueError):
        return default
    return max(0, parsed)


def _env_int_pos(name: str, default: int) -> int:
    """Lê `name` de os.environ como inteiro ESTRITAMENTE POSITIVO.

    Reusa `_env_int` (aceita inteiro, `max(0, ...)`) e GARANTE o contrato
    > 0: ausente, valor inválido OU <= 0 -> `default` (default seguro). Usado
    por timeouts cujo zero/negativo desativaria a proteção por engano.
    """
    valor = _env_int(name, default)
    return valor if valor > 0 else default


EXEC_SANDBOX_ENABLED = _env_bool("EXEC_SANDBOX_ENABLED", False)
# Ativação CONDICIONAL do sandbox por complexidade (Update Final, Fase 1):
# quando `EXEC_SANDBOX_ALTO_ONLY=True`, o Executor só tenta o container para
# tarefas de complexidade ALTA (usa_sandbox=True vindo do pipeline); baixo/
# médio continuam no host. Default `False` (não ativa por padrão global — a
# ativação é por complexidade alta). `EXEC_SANDBOX_ENABLED=1` continua sendo o
# override explícito que FORÇA o sandbox para qualquer comando; a prioridade é:
# override explícito (env/task) > sandbox condicional por complexidade. Lida no
# import, igual ao EXEC_SANDBOX_ENABLED.
EXEC_SANDBOX_ALTO_ONLY = _env_bool("EXEC_SANDBOX_ALTO_ONLY", False)
# Imagem base do container; pode ser ajustada para incluir git/compiladores.
# Fase 2 (hardening): a IMAGEM é o mecanismo recomendado para ferramentas — use
# uma imagem customizada com git/compiladores/utilitários já embutidos (ex.:
# `python:3-slim` + Dockerfile com `RUN apt-get install git gcc`), porque o
# container roda com `--network none` por padrão (isolamento de rede) e
# instalações no runtime (`pip install`) NÃO teriam rede disponível.
EXEC_SANDBOX_IMAGE = "python:3-slim"
# Timeout do container, em segundos.
EXEC_SANDBOX_TIMEOUT = 60
# Diretório do projeto a montar no container (caminho fixo dentro do container:
# /workspace). O cwd host do comando é traduzido para o subcaminho no mount.
EXEC_SANDBOX_MOUNT_ROOT = ROOT
# Backend de resolução: "auto" | "docker" | "podman". "auto" detecta
# docker CLI/SDK -> podman CLI -> host. "docker"/"podman" forçam o backend.
EXEC_SANDBOX_BACKEND = "auto"
# Caminho fixo (container) onde EXEC_SANDBOX_MOUNT_ROOT é montado.
EXEC_SANDBOX_MOUNT_PATH = "/workspace"

# Hardening do sandbox (Fase 2 — limites de recurso e privilégios). Defaults
# seguros que NÃO quebram a escrita no mount do projeto: `--read-only` só
# congela o rootfs do container (o mount /workspace permanece gravável);
# `--memory/--cpus/--pids-limit` limitam uso de recurso (anti-fork-bomb);
# `--cap-drop ALL` + `--security-opt no-new-privileges` reduzem o kernel
# surface. `EXEC_SANDBOX_USER` vazio = comportamento atual (root); defina
# (ex.: "nobody") para rodar como não-root — ATENÇÃO: com `--user`, a escrita
# em arquivos do mount pertencentes a outro uid pode falhar com permissão.
EXEC_SANDBOX_USER = os.environ.get("EXEC_SANDBOX_USER", "")
EXEC_SANDBOX_READ_ONLY = _env_bool("EXEC_SANDBOX_READ_ONLY", True)
EXEC_SANDBOX_MEMORY = os.environ.get("EXEC_SANDBOX_MEMORY", "512m")
EXEC_SANDBOX_CPUS = os.environ.get("EXEC_SANDBOX_CPUS", "1.0")
EXEC_SANDBOX_PIDS_LIMIT = _env_int("EXEC_SANDBOX_PIDS_LIMIT", 256)
EXEC_SANDBOX_CAP_DROP = _env_bool("EXEC_SANDBOX_CAP_DROP", True)
EXEC_SANDBOX_NO_NEW_PRIVS = _env_bool("EXEC_SANDBOX_NO_NEW_PRIVS", True)

# Segurança de deploy (B1): quando HARNESS_PUBLIC=1, comandos no HOST são
# BLOQUEADOS — a execução exige sandbox Docker/Podman (isolamento físico).
# Ver harness/executor.py (run) e harness/server.py (B4).
PUBLIC_REQUIRE_SANDBOX = True

# Detecção do CLI docker fora do PATH (Docker Desktop). O executável do Docker
# Desktop pode NÃO estar no PATH (ex.: instalado em AppData Local), então o
# `_docker_disponivel` do executor também procura por caminhos conhecidos.
# `EXEC_SANDBOX_DOCKER_CLI` é um caminho opcional explícito (vazio = detectar
# automaticamente). Se não definido, `_docker_cli_path` (executor.py) varre
# `EXEC_SANDBOX_DOCKER_CLI_CANDIDATES` e o `PATH` via `shutil.which("docker")`.
EXEC_SANDBOX_DOCKER_CLI = ""  # ex.: r"C:\Program Files\Docker\Docker\resources\bin\docker.exe"
# Timeout (segundos) para a verificação de ATIVIDADE do daemon (Pendência B2) em
# `_docker_disponivel` (executor.py): `docker info` pode ser lento quando o
# daemon está caído (tenta conectar e espera), então o subprocess usa este
# timeout curto para não travar a resolução do sandbox. Falha/timeout -> o
# daemon é tratado como inativo e o sandbox cai para host (seguro).
EXEC_SANDBOX_DAEMON_TIMEOUT = 3
# Caminhos comuns do CLI docker do Docker Desktop, na ordem de preferência.
# `%ProgramFiles%` e `%LOCALAPPDATA%` são expandidos na detecção (executor.py).
EXEC_SANDBOX_DOCKER_CLI_CANDIDATES = (
    r"%ProgramFiles%\Docker\Docker\resources\bin\docker.exe",  # Docker Desktop padrão
    r"%LOCALAPPDATA%\Programs\DockerDesktop\resources\bin\docker.exe",  # AppData Local
)

# Comandos que exigem confirmação explícita no web shell
APPROVAL_PATTERNS = [
    "pip install",
    "pipenv install",
    "npm install",
    "poetry add",
]

# HITL por níveis de risco (Item 2): o `nivel_de_risco` da tarefa é um GATE
# automático que define a política de aprovação HITL de forma consistente.
# Níveis válidos: baixo | medio | alto (None -> "medio", retrocompatível).
NIVEIS_RISCO = ("baixo", "medio", "alto")
NIVEL_RISCO_DEFAULT = "medio"

# Matriz risco -> política (fonte de verdade da decisão HITL no pipeline):
#   baixo -> "auto"            comandos de VALIDAÇÃO (que passam check_policy e
#                              NÃO disparam APPROVAL_PATTERNS) são aprovados
#                              automaticamente QUANDO houver `approve`; sem
#                              `approve`, mantém o padrão seguro (nega). Nunca
#                              burla o bloqueio destrutivo de check_policy.
#   medio -> "hitl_por_comando" comportamento atual: cada comando passa pelo
#                              callback `approve` (HITL por comando).
#   alto  -> "gate_global"     REQUER aprovação humana explícita ANTES de
#                              qualquer execução: um "ok global" UMA vez no
#                              início; sem `approve` ou sem aprovação do gate,
#                              o pipeline BLOQUEIA antes de rodar qualquer
#                              comando (não roda parcialmente).
RISCO_POLITICA = {
    "baixo": "auto",
    "medio": "hitl_por_comando",
    "alto": "gate_global",
}
# Marcador do gate global de risco alto passado ao callback `approve` UMA vez.
RISCO_GATE_ALTO = "<RISK_GATE>"

# Graus de complexidade válidos (Update Final, Fase 1): derivados do prompt
# pelo scorer `harness/complexity.py`, OU fornecidos explicitamente (override).
NIVEIS_COMPLEXIDADE = ("baixo", "medio", "alto")

# Timeout de EXECUÇÃO por grau de complexidade (safety net, Exception Ch12):
#   baixo  -> 600s  (10 min)
#   medio  -> 900s  (15 min)
#   alto   -> 1500s (25 min)
# O `tempo_maximo_seg` derivado é o limite TOTAL do run_task (entre etapas). Se
# o task fornecer `tempo_maximo_seg`, esse valor explícito é respeitado
# (configurável). É um SAFETY NET: detecta travamento e encerra com ressalva
# preservando o progresso — NÃO corta execução legítima no meio.
TEMPO_MAXIMO_POR_COMPLEXIDADE = {
    "baixo": 600,
    "medio": 900,
    "alto": 1500,
}
# Default de tempo máximo quando a complexidade é desconhecida (não deve
# ocorrer em execução normal — cai no medio).
TEMPO_MAXIMO_DEFAULT = 900

# Timeout/poll de comando no pipeline (Lote: números mágicos em config).
# `_executa_comando` aguarda cada job com este limite e faz polling curto.
COMANDO_TIMEOUT_SEG = 30.0
COMANDO_POLL_SEG = 0.1

# Timeout POR ETAPA do pipeline (Item 7.2). Cada etapa pesada/sujeita a travar
# (consulta de memória, exploração, revisão) roda numa thread daemon com
# `join(timeout)`. O timeout é COOPERATIVO, NÃO preemptivo: se a etapa estourar,
# a thread NÃO é interrompida (limitação do Python: não há kill/preempção de
# thread) — o pipeline encerra com APROVADA_COM_RESSALVAS + achado de timeout de
# etapa, PRESERVANDO o progresso já registrado (`progresso_preservado=True`), em
# vez de travar para sempre. Etapas com EFEITOS COLATERAIS (implementação/
# execução de comandos) NÃO são envolvidas: a thread sobrevivente continuaria
# agindo. Quando a etapa termina dentro do tempo, o comportamento é inalterado.
#
# CONFIGURÁVEL POR VARIÁVEL DE AMBIENTE (estilo `_env_int`): a env
# `PIPELINE_ETAPA_TIMEOUT_SEG` sobrescreve o default (300s). O valor é lido no
# IMPORT e GARANTIDO > 0 (se <= 0 ou inválido, volta ao default seguro 300) —
# um timeout zero/negativo desativaria a proteção por engano.
PIPELINE_ETAPA_TIMEOUT_SEG = _env_int_pos("PIPELINE_ETAPA_TIMEOUT_SEG", 300)

# Cap de arquivos enumerados na etapa de EXPLORAÇÃO (Item 7.2): evita glob
# patológico (ex.: `**/*` sobre árvore gigante) enumerar sem limite. Ao atingir
# o cap, a exploração PARA e a etapa registra `truncado=True`. Valores <= 0
# desativam o cap (sem limite).
PIPELINE_EXPLORACAO_MAX_ARQUIVOS = 2000

# Timeout de cada arquivo de teste no selfcheck.
SELFCHECK_TIMEOUT_SEG = 600

# Limites de trechos de contexto (RAG) — enxugamento de tokens.
SNIPPET_LIMITE = 300  # chars do trecho de `Memory.search(snippet=True)`
TRECHO_LIMITE = 220  # chars do trecho de seção de `rag_refs`

# Granularidade de contexto (Lote 1 — otimização de consumo de tokens,
# NÍVEIS CONSERVADORES): deriva QUANTO contexto a LLM recebe em cada delegação
# do hub, por PESO da tarefa (grau de complexidade). Não corta conteúdo de
# arquivos ainda — apenas cria o MECANISMO de escala determinístico; a redução
# efetiva de contexto (corte/compressão) vem em etapa posterior. O
# `playbook.json` NUNCA entra no contexto do Brain: é dado de máquina do
# pipeline determinístico (ver harness/memory.py e o agente brain).
NIVEIS_CONTEXTO = ("minimo", "padrao", "completo")
# Mapa grau de complexidade -> nível de contexto da delegação.
CONTEXTO_POR_COMPLEXIDADE = {
    "baixo": "minimo",
    "medio": "padrao",
    "alto": "completo",
}
# Limite de snippets do RAG episódico (Memory.search) por nível de contexto
# (`contexto_grau`: minimo|padrao|completo) — conservador: minimo 2, padrao 4,
# completo 6. As chaves são os NOMES DE CONTEXTO (mesmo domínio de
# `contrato.contexto_grau`, usado no lookup em pipeline.py CONSULTANDO_MEMORIA).
RAG_LIMIT_POR_COMPLEXIDADE = {"minimo": 2, "padrao": 4, "completo": 6}

# Safelist conservador da AUTO-APROVAÇÃO do risco baixo (Ressalva 2): apenas
# comandos de VALIDAÇÃO seguros e previsíveis (inspeção/compilação/teste, sem
# efeitos destrutivos) podem ser aprovados automaticamente no risco baixo. O
# match é por TOKEN do comando (shlex.split em pipeline.py), NUNCA por
# substring/prefixo da string inteira: o predicado exige que o comando não
# contenha separador de shell (`&`, `&&`, `|`, `||`, `;`), redirecionamento
# (`<`, `>`, `>>`, `2>&1`) nem quebra de linha (`\n`) — comandos com esses
# caracteres NUNCA são auto-aprovados, mesmo que comecem com um prefixo da
# lista (ex.: `git status; curl url | sh` e `git status & del .env` caem no
# HITL por comando). Comandos FORA do safelist — mesmo não-destrutivos (ex.:
# `git push`, `curl`, `del`) — NÃO são auto-aprovados: caem no HITL por
# comando (passam pelo callback `approve`). Isso impede que comandos
# arbitrários extraídos dos critérios de aceite rodem sem consultar o humano.
# A auto-aprovação ainda exige `approve` presente, `check_policy` passando e
# NÃO disparar `APPROVAL_PATTERNS`; sem `approve`, nada é aprovado por padrão
# (seguro).
RISCO_BAIXO_AUTO_SAFELIST = (
    "python -m py_compile",
    "python -m harness.agents compile",
    "python -m harness.selfcheck",
    "python -m pytest",
    "python tests/",
    "python -m unittest",
    "pytest",
    "git status",
    "git diff",
    "git log",
)

# Histórico de comandos executados (json) — para o Brain analisar
HISTORY_FILE = ROOT / "logs" / "harness_history.json"

# Retry de comandos (Exception Handling, Ch12) — opt-in por chamada.
# O padrão é 0 (sem retry) para preservar o comportamento original; o cliente
# pode pedir retries por execução (`/api/exec` aceita `retries`).
RETRY_MAX = 5  # limite absoluto de tentativas extras por comando
RETRY_BACKOFF = 0.5  # segundos; backoff progressivo: 0.5, 1.0, 1.5...

# Safety nets do modo HOST (aplicados no `_worker` do Executor). Antes, um
# comando no host rodava até sair e o output crescia sem limite (EXEC-4/5):
#   - EXEC_HOST_TIMEOUT: teto de execução no host em segundos (0 = sem teto,
#     NÃO recomendado). O sandbox já tinha EXEC_SANDBOX_TIMEOUT; o host não.
#   - EXEC_MAX_OUTPUT_BYTES: teto de saída acumulada por job (bytes). Ao
#     atingir, o reader para e o processo é encerrado pelo timeout.
# Configuráveis por variável de ambiente (lidas no import, como os demais).
EXEC_HOST_TIMEOUT = _env_int("EXEC_HOST_TIMEOUT", 3600)  # 1h default
EXEC_MAX_OUTPUT_BYTES = _env_int("EXEC_MAX_OUTPUT_BYTES", 1024 * 1024)  # 1 MiB

# Limites de segurança para a API
MAX_PARALLEL = 16  # máximo de comandos em /api/exec/parallel
MAX_SEARCH_LIMIT = 20  # máximo de resultados de /api/memory/search
MAX_REF_QUERY_LIMIT = 20  # máximo de fontes retornadas por /api/refs/query (RAG sobre referências)
MAX_BODY_BYTES = 512 * 1024  # 512 KB máximos por corpo de requisição

# Robustez do servidor HTTP (anti-DoS): teto de REQUISIÇÕES SIMULTÂNEAS
# (threads) e rate-limit de EXECUÇÃO por identidade (anti thread/process
# exhaustion). O socket timeout por conexão é definido no Handler (setup).
MAX_CONCURRENT_REQUESTS = 32  # teto de threads de requisição ativas
# Posts de /api/exec e /api/exec/parallel por identidade, por janela.
MAX_EXEC_PER_WINDOW = 60
EXEC_RATE_WINDOW = 60.0  # segundos

# Auditoria de segurança web (agente pentester / skill security-audit)
# Alvos INTERNOS (loopback/privado) só são testáveis caso a caso: host listado
# em ALLOWED_AUDIT_TARGETS E AUDIT_ALLOW_INTERNAL ativa. Sem allowlist, o
# anti-SSRF bloqueia (default seguro). Configurado por override de código/CLI,
# nunca por toggle automático.
ALLOWED_AUDIT_TARGETS: set[str] = set()
AUDIT_ALLOW_INTERNAL = False
# Gate da Fase 3 (testes ativos): aprovação humana explícita, além do gate de
# risco alto da tarefa. Marcador passado ao callback `approve` UMA vez.
PHASE3_GATE = "<PHASE3_GATE>"
# Diretório dos relatórios de auditoria (docs/auditorias/<slug>/)
AUDIT_REPORT_DIR = ROOT / "docs" / "auditorias"
# Rate-limit e teto das sondas ativas (Fase 3)
AUDIT_RATE_LIMIT_SEG = 0.5
AUDIT_MAX_PROBES = 10
# Payloads DESTRUTIVOS jamais enviados pela Fase 3 (marcadores case-insensitive)
AUDIT_PAYLOADS_DESTRUTIVOS = (
    "drop table", "delete from", "truncate", "rm -rf", "os.system",
    "subprocess", "fs.writefile", "eval(", "exec(",
)

# Autenticação HTTP Basic do harness (mesmo modelo do painel web do opencode):
# TODAS as rotas /api/* (exceto /api/health) exigem `Authorization: Basic`.
#   Usuário : HARNESS_USERNAME (default "opencode").
#   Senha   : HARNESS_PASSWORD (override) — se não definida, usa a PADRÃO
#             opencode/opencode (credenciais de fábrica). Na primeira execução
#             o app.py pede para definir uma senha personalizada (prompt no
#             terminal), que grava config/auth.json (modo "hash"); até trocar,
#             o harness funciona com opencode/opencode.
# A senha nunca é devolvida por /api/health.
AUTH_DEFAULT_USERNAME = "opencode"
AUTH_DEFAULT_PASSWORD = "opencode"

_harn_user = os.environ.get("HARNESS_USERNAME")
_harn_user = _harn_user.strip() if _harn_user else ""
AUTH_USERNAME = _harn_user if _harn_user else AUTH_DEFAULT_USERNAME

_harn_pass = os.environ.get("HARNESS_PASSWORD")
_harn_pass = _harn_pass.strip() if _harn_pass else ""
AUTH_PASSWORD = _harn_pass if _harn_pass else AUTH_DEFAULT_PASSWORD

# Modo público (HARNESS_PUBLIC=1): deploy exposto via Tailscale Serve. Exige
# HTTPS (host 127.0.0.1 + tailscale-serve.bat) e senha fixa; execuções no
# HOST são bloqueadas (exigem sandbox Docker/Podman — ver executor.py B1 e
# server.py B4).
HARNESS_PUBLIC = _env_bool("HARNESS_PUBLIC", False)

# Senha personalizada por hash (F0/F1): `python -m harness.auth set` grava
# config/auth.json (scrypt) e config/secrets.env (texto puro p/ os .bat).
# Quando o arquivo hash existe, a autenticação do web shell valida contra o
# hash (auth.validate) em vez das constantes AUTH_* abaixo.
AUTH_HASH_FILE = ROOT / "config" / "auth.json"
# Modo de autenticação resolvido no import:
#   "hash"   -> config/auth.json existe (senha personalizada)
#   "env"    -> HARNESS_PASSWORD definida no ambiente
#   "padrao" -> credenciais padrão opencode/opencode (fábrica)
AUTH_MODE = "hash" if AUTH_HASH_FILE.exists() else ("env" if _harn_pass else "padrao")

# Realm exibido pelo navegador no prompt de autenticação
AUTH_REALM = "harness"