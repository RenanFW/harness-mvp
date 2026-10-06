# Auditoria — Ameaça-modelo e riscos residuais

Status da auditoria: **APROVADA_COM_RESSALVAS** (2026-08-23).

Este documento registra o que é **garantido** pelo harness após a auditoria e o
que é **risco residual assumido por design**. Nenhum engenheiro honesto atesta
"zero falhas" — este arquivo é a fronteira transparente entre o que o harness
promete e o que ele não promete.

## O que foi corrigido (resumo)

### Segurança (executor, servidor, auth, webscraper)
- **Bypass do guardrail `rm`** via interpretadores/runner fechado
  (`sh -c 'rm -rf'`, `cmd /c`, `wsl`, `git bash -c`, `sudo`, `xargs`,
  `find -exec/-execdir`, `env -S`) — antes, `rm` fora de "posição de comando"
  executava; agora strings de comando de interpretadores são expandidas
  (`_flatten_shell`) e o scan posicional cobre runners com ARGUMENTOS
  (`sudo -u root rm -rf x`, `env -i rm -rf x`, `timeout 5 rm -rf x`,
  `su -c "..."`, `find . -exec rm -rf {} +`), sufixo `.exe` (`cmd.exe`,
  `powershell.exe`, `wsl.exe`) e preserva o falso-positivo do `echo`
  (`wsl echo rm -rf x`, `find . -exec echo rm -rf {}` não bloqueiam).
- **Modo público (`HARNESS_PUBLIC=1`) recusa subir** com credenciais padrão
  ou senha de fábrica (antes só avisava) — `server.py` B4 endurecido.
- **Senha fora da linha de comando e do console** — `run-web-public.bat` e
  `start-web.bat` resolvem a senha internamente; `tailscale-serve.bat` não
  imprime nem embute senhas no processo.
- **Anti-SSRF no webscraper** — allowlist http/https, rejeição de
  loopback/privado/link-local/CGNAT (resolução de DNS + validação), teto de
  redirects com revalidação por hop, e `_host_editorial` por SUFIXO de domínio
  pleno (sem bypass de substring/prefixo: `amazon.com.evil.example` e
  `books.google.com.evil.example` não contam; `books.google` foi expandido
  para os domínios concretos `books.google.com/.com.br/.co.uk`).
- **Safety nets do host** — timeout de execução (`EXEC_HOST_TIMEOUT`, 1h
  default) e teto de saída (`EXEC_MAX_OUTPUT_BYTES`, 1 MiB) no `_worker`, com
  contador INCREMENTAL de bytes (reader linear, sem regressão O(n²) para
  comandos verbosos legítimos). Comando encerrado por cap conta como EVIDÊNCIA
  de execução no contrato do pipeline (ressalva, não "não executou").
- **Hardening do sandbox** — `--read-only`, `--memory/--cpus/--pids-limit`,
  `--cap-drop ALL`, `--security-opt no-new-privileges` (configuráveis;
  `--user` opcional).
- **Robustez HTTP** — socket timeout por conexão (anti-slowloris), teto de
  requisições simultâneas (503 sob carga), rate-limit de `/api/exec` por
  identidade.

### Lógica (pipeline, memória, frontmatter)
- **Contrato de saída não mente mais**: timeout e bloqueio no meio da
  implementação preservam `validacoes_executadas`/`aprovacoes_solicitadas`
  reais; timeout COM execução grava memória episódica.
- **Parser de frontmatter unificado** (`harness/frontmatter.py`) — tolerante a
  CRLF (Windows), com listas em bloco e valores com `:`; eliminado o drift
  entre `memory`, `rag_refs` e `webscraper`.
- **`_perm_nao_deny` correto** — mapa inline `{"**": "deny"}` não conta como
  permissão.
- **`..` travado no safelist de auto-aprovação** (`python tests/x.py
  ../../secret.py` não é auto-aprovado).

### Motor determinístico (`harness/motor`)
- Tornado **pacote autônomo** (removido o import eager do boot do harness).
- **Failover do sandbox** com daemon Docker inativo (antes: 100% de falha).
- **Race `remove=True` + `logs()`** corrigida no docker.
- **Dead-end da revalidação fechado** — `revalidar_pendentes()` reexecuta
  artefatos pendentes: passou → volta ao fast-path; falhou de verdade (exit
  != 0 no sandbox) → apagado. Interpolação sem valores (placeholders) e
  falhas de infra NÃO destroem artefatos — mantêm pendente.
- **False-hit do cache-first mitigado** — threshold efetivo mais alto em
  modos de baixa fidelidade do embedder (safe/fallback).

### Consistência / amadorismo
- `hilt` → `hitl` (typo) em config, pipeline, docs, testes.
- Números mágicos movidos para `config.py`.
- `stdout.reconfigure` padronizado nas CLIs.
- Frontend: `try/catch`, `btoa` unicode-safe, guarda de erro 401 (sem job
  fantasma), estado morto removido.
- Docs realinhados (documenter `memory/references/`, `gravar_registro`).

## Riscos residuais ASSUMIDOS (por design, não escondidos)

1. **Deny-list de comandos é defesa em profundidade, NÃO fronteira de
   segurança.** O `rm` destrutivo por flags (inclusive via runners,
   interpretadores, `find -exec`, `env -S` e `.exe`) está fechado, mas
   **interpreter-injection** segue fora do alcance de padrões de string:
   `python -c "import os; os.remove(...)"`, `node -e ...`,
   `curl -o x && sh x`, `awk 'system(...)'`, `perl -e 'system(...)'` não são
   detectados. Quem tem credenciais válidas localmente (`/api/exec`) roda
   código arbitrário. É a ameaça-modelo assumida. Mitigação real: sandbox
   **sempre ativo** (hoje é opcional, `EXEC_SANDBOX_ENABLED`), obrigatório no
   modo público.

2. **Sandbox Docker é fronteira física, não isolamento endurecido.** Mesmo
   com `--read-only`/`--cap-drop ALL`/`--user`, o container compartilha o
   kernel do host; um comprometimento do container no modo público ainda é
   risco. Para exposição a usuários NÃO confiáveis: sandbox rootless +
   sempre-ativo, autenticação 2FA/token, revisão manual de `memory/`.

3. **Auth Basic sobre HTTP em modo local.** `python app.py --host 0.0.0.0`
   sem HTTPS expõe credenciais em base64 no wire. Seguro apenas no
   tailnet/HTTPS do Tailscale. Em modo público o bind 0.0.0.0 é bloqueado.

4. **Cache semântico do motor é best-effort.** O threshold efetivo reduz,
   mas não elimina, o false-hit do FastVectorizer (bag-of-words): um artefato
   com exit 0 no sandbox não garante adequação SEMÂNTICA à tarefa atual. O
   artefato é revalidado no sandbox; a adequação semântica é do chamador.

5. **Validação anti-fabricação do webscraper é heurística, não prova.**
   Hosts editoriais e fontes são verificados, mas um atacante com controle de
   DNS pode tentar rebinding (TOCTOU na validação SSRF) — mitigação padrão,
   não fronteira absoluta.

6. **`python -m harness.auth password` imprime a senha no stdout** — é o
   mecanismo usado pelos `.bat` para ler `config/secrets.env` (local). Não
   aparece na linha de comando de processos; console local é o limite.

## Recomendação de deploy honesta

- **Tailnet confiável (Tailscale) + uso local/CLI**: adequado após esta
  auditoria (com senha personalizada definida).
- **HTTPS público com usuários não confiáveis**: NÃO recomendado sem os itens
  1-2 acima resolvidos.

## Garantias que se mantêm

- Nenhuma execução termina sem evidências (diff, testes, lint/build) —
  status `BLOQUEADA` caso contrário.
- Nenhuma memória é gravada sem origem rastreável (trust `fraca` por default).
- Zero-poisoning do motor: nada com exit != 0 é persistido.
- Suíte completa (`python -m harness.selfcheck`): 13 arquivos, todas as
  regressões verdes.