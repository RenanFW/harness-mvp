# Skills do Harness — modelo Agent Skills

Este diretório contém as **skills de domínio** do harness: capacidades de
tarefa (conhecimento + procedimento) carregadas **on-demand** pela LLM quando
a tarefa casa com a `description` de uma skill (gatilho nativo do opencode).

## Princípios

- **Skills instruem o *como*; o harness decide o *se pode*.** Skills ensinam a
  executar um tipo de tarefa; permissões e decisões finais seguem o runtime
  (`harness/config.py`, `AGENTS.md`, `harness/executor.py`). Nenhuma instrução
  de skill autoriza o que o harness proíbe.
- **Origem e trust obrigatórios** (anti-fabricação): todo `SKILL.md` exige
  `origem` rastreável (referência em `memory/references/` ou módulo do
  harness) e `trust` (alta|media|fraca), no mesmo espírito das referências.
- **Registro**: `harness/skills.py` lista/valida as skills
  (`python -m harness.skills validate`; `GET /api/skills` no web shell).

## Schema (frontmatter obrigatório)

```yaml
---
name: <skill-id>            # mesmo nome da pasta
description: <gatilho de carga — descreve QUANDO usar>   # obrigatório p/ on-demand
tipo: skill
dominio: <ex.: security, testing, devops>
origem: <caminho de ref OU módulo do harness — separado por vírgula>  # obrigatório
trust: alta | media | fraca
validado_por: motor | human
data: YYYY-MM-DD
tags: [<tag1>, <tag2>]
---
```

`origem` deve apontar para um arquivo que **existe** em `memory/references/`
(ex.: `memory/references/<ref-id>.md`) ou um módulo real do harness
(ex.: `harness/webscraper.py`). A validação (`harness/skills.py`) confere o
arquivo em disco — sem substring/glob: origem fabricada ou inexistente reprova
a skill. `trust` ausente/inválido → `fraca` (conservador, não reprova).

Corpo recomendado: **Conceitos**, **Padrões acionáveis**, **Procedimento
(passos)**, **Armadilhas**, **Limites** (quando a skill NÃO se aplica) e a
seção **"Regras do harness prevalecem"**.

## Skills registradas

| Skill | Domínio | Trust | Uso |
| --- | --- | --- | --- |
| `webscraping` | coleta-de-conteudo | alta | Coleta legítima de livros/PDFs/docs (agente documenter) |
| `security-audit` | security | alta | Auditoria de segurança web autorizada (agente pentester) |

Registre novas skills no índice da tabela acima e valide com
`python -m harness.skills validate`.