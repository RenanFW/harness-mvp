# Como personalizar este template

Este é um template genérico do harness: um orquestrador de agentes opencode com
memória local e execução controlada. Ele não fixa provider nem modelo — você
escolhe os seus. Este guia cobre os pontos de customização.

## 1. Modelo e provider

O template vem **sem** `"model"` em `opencode.json` (modelo agnóstico). Defina o
seu provedor/modelo no arquivo:

```json
{
  "$schema": "https://opencode.ai/config.json",
  "model": "<provider>/<modelo>",
  "share": "disabled"
}
```

A chave de API nunca vai no repositório: ela fica no auth do próprio opencode.
Os docs (`README.md`, `TUTORIAL.md`, `docs/operacao.md`) referenciam o modelo
apenas como "o definido pelo usuário em `opencode.json`".

## 2. Agentes

Os agentes ficam em `.opencode/agent/` (um arquivo Markdown por agente). O
template traz 7:

| Agente | Papel |
| --- | --- |
| `hub` | primário: planeja, delega e consolida |
| `brain` | memória: recupera e grava fluxos |
| `explorer` | mapeia arquivos, dependências e riscos |
| `implementer` | altera arquivos e valida (único que edita) |
| `reviewer` | revisa qualidade e riscos (não corrige) |
| `documenter` | lê/interpreta documentos para `memory/references/` |
| `pentester` | auditoria de segurança web (perfis osint/superficial/completo) |

Para adicionar/editar um agente, crie ou ajuste o `.md` correspondente seguindo
o formato dos existentes (frontmatter com modo, ferramentas e permissões).
Reinicie o opencode após mudanças; valide com `opencode agent list`.

## 3. Skills

Skills de domínio ficam em `.opencode/skills/<nome>/SKILL.md`. O template inclui
`webscraping` (coleta legítima de referências) e `security-audit` (auditoria de
segurança web autorizada). Para criar uma nova skill, adicione
`.opencode/skills/<nome>/SKILL.md` com frontmatter (`name`, `description`) e o
fluxo de trabalho.

## 4. Políticas de risco e execução

A fonte única das políticas de risco/execução é `harness/config.py`:

- matriz risco -> política (`baixo`/`medio`/`alto`);
- `BLOCKED_PATTERNS` / `APPROVAL_PATTERNS` de comandos;
- parâmetros de sandbox (`EXEC_SANDBOX_*`) e limites de complexidade.

Ajuste com cuidado: não relaxe a semântica de segurança (destrutivos sempre
bloqueados, `check_policy` sempre ativo). Mudanças de permissão do opencode
ficam em `opencode.json` (bloco `permission`).

## 5. Portas e autenticação

- Web shell: `python app.py`, com `--port` (padrão 8500) e `--host`.
- Painel opencode: `start-web.bat [porta] [host]` (padrão 9090/127.0.0.1).
- Usuário/senha: `HARNESS_USERNAME`/`HARNESS_PASSWORD` no ambiente, ou
  `python -m harness.auth set` (grava `config/auth.json` + `config/secrets.env`,
  ambos ignorados pelo git).
- Exposição remota: prefira HTTPS real via Tailscale (`tailscale-serve.bat`) a
  `--host 0.0.0.0`. Em modo público (`HARNESS_PUBLIC=1`) a senha padrão é
  recusada.

## 6. Memória

- `memory/core.md` — fatos permanentes;
- `memory/patterns.md` — padrões de design agentic aplicados;
- `memory/self-model.md` — auto-modelagem (alvo);
- `memory/references/` — RAG consultivo sobre referências (trust alta/media);
- `memory/episodic/` — registros por execução (recomeça vazio no template);
- `memory/agents/` — playbook compilado (`python -m harness.agents compile`).

O template reinicia a memória: episódicos, playbook e avaliações começam
vazios; permanecem apenas core/patterns/self-model e as 5 referências internas.

## 7. Publicação

1. Ajuste nome/descrição e remova o que não usar.
2. `git init` (ou use o repositório já inicializado) e configure o remote.
3. Publique no GitHub. A licença é MIT (ver `LICENSE`), com titular
   "Harness contributors".

Antes de publicar, rode a suíte de testes e uma varredura por dados pessoais
para garantir que nada sensível foi adicionado.
