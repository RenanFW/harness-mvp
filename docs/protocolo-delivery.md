# Delivery Protocol — Documento de Referência

Protocolo obrigatório para toda execução iniciada pelo hub. Ele transforma uma
solicitação em um processo verificável, com entrada definida, estados rastreados
e saída estruturada.

**Este documento é a referência LEGÍVEL do protocolo.** A verdade de máquina
(estados, roteamento, contrato de saída, regras de bloqueio) vive nas
constantes de `harness/agents.py` (`PipelineSpec.default`) e é executada pelo
orquestrador determinístico `harness/pipeline.py`. Se este documento divergir
do código, o **runtime prevalece**.

## Contrato de entrada

Toda execução começa extraindo estes campos da solicitação do usuário:

| Campo | Descrição |
| ----- | --------- |
| objetivo | O que precisa ser entregue |
| escopo | Arquivos ou área que podem ser alterados |
| restricoes | Ações proibidas ou limites |
| criterios_de_aceite | Como comprovar que terminou |
| nivel_de_risco | `baixo`, `medio` ou `alto` |

Exemplo:

```yaml
objetivo: Adicionar autenticação ao endpoint de relatórios
escopo: src/reports e tests/reports
restricoes:
  - Não alterar o formato da resposta
  - Não instalar dependências
criterios_de_aceite:
  - Usuário não autenticado recebe 401
  - Testes existentes continuam passando
nivel_de_risco: medio
```

Se algum campo obrigatório estiver ausente, peça ao usuário antes de começar.

> **Projetos novos**: pedidos que criam um projeto (e não alteram o harness)
> vivem em `sidePrjs/<nome>/`. O `<nome>` é gerado pelo **nomeador do harness**
> (`python -m harness.namer "<objetivo>"` ou import de `harness.namer`) e o
> escopo da execução passa a ser `sidePrjs/<nome>/`.

## Estados da execução

```
RECEBIDA → CONSULTANDO_MEMORIA → EM_EXPLORACAO → EM_IMPLEMENTACAO → EM_REVISAO
   → ENCERRAMENTO → APROVADA | APROVADA_COM_RESSALVAS | BLOQUEADA
```

1. `RECEBIDA` — entrada extraída e validada.
2. `CONSULTANDO_MEMORIA` — Brain recupera conhecimento sobre tags/keywords.
3. `EM_EXPLORACAO` — mapeamento do impacto (quando aplicável).
4. `EM_IMPLEMENTACAO` — alteração dentro do escopo autorizado.
5. `EM_REVISAO` — revisão independente com evidências.
6. `ENCERRAMENTO` — estado terminal de consolidação do pipeline (o último item
   da máquina de estados; a saída estruturada carrega os status finais).
7. `APROVADA` | `APROVADA_COM_RESSALVAS` | `BLOQUEADA` — encerramento.

## Roteamento (etapa → agente)

| Etapa | Agente |
| ----- | ------ |
| MEMORIA | brain |
| EXPLORACAO | explorer |
| IMPLEMENTACAO | implementer |
| REVISAO | reviewer |
| GRAVACAO | brain |

## Granularidade de contexto por complexidade

Otimização de consumo de tokens (Lote 1, níveis CONSERVADORES — nenhum
conteúdo de arquivo é cortado ainda; apenas o mecanismo de escala). Cada
delegação envia à LLM o contexto do nível derivado pelo scorer
(`contexto_grau`: `minimo` | `padrao` | `completo`, mapeado via
`CONTEXTO_POR_COMPLEXIDADE` em `harness/config.py`).

| Recurso | minimo | padrao | completo |
| ------- | ------ | ------ | -------- |
| Prompt de delegação | só contrato | contrato + memória curta | contrato + memória + referências |
| Brain lê | core.md | core.md + index.md | + resumo.md do playbook |
| RAG episódico (snippets) | limit 2 | limit 4 | limit 6 |
| RAG de referências (livros) | nunca | só se pedido | sim |
| Explorer | omitido | leve | completo |
| Reviewer | curto | padrão | profundo |

Regra: `memory/agents/playbook.json` NUNCA é lido pelo Brain — é dado de
máquina do pipeline determinístico (o `resumo.md`, quando existir, é a única
fonte do playbook para o Brain).

A delegação ao **implementer** inclui, como contexto curto, as **lições
confiáveis** do playbook (trust `alta`/`media`) — o fecho do loop de
aprendizado. A fonte ACESSÍVEL ao hub é a leitura de
`memory/agents/resumo.md` (seção "Lições confiáveis por agente"); o CLI
`python -m harness.agents lessons <agente>` é para uso humano/ferramentas (o
hub NÃO tem permissão de shell para ele). As lições seguem para o prompt de
delegação (~1 linha por lição, sem as de trust `fraca` e sem meta-ruído
autorreferente ao playbook). No nível de contexto **completo** elas podem ser
incluídas junto do restante do contexto; o `playbook.json` cru continua
proibido em qualquer nível.

## HITL por nível de risco

O `nivel_de_risco` é um gate automático no pipeline (`baixo|medio|alto`;
ausente → `medio`, retrocompatível; inválido → `BLOQUEADA`). Política aplicada
(matriz em `harness/config.py`):

| Nível | Política | Comportamento |
| ----- | -------- | ------------- |
| baixo | `auto` | Comandos de validação do safelist `RISCO_BAIXO_AUTO_SAFELIST` (match por token; sem separadores/redirect/`\n`), não destrutivos, aprovados automaticamente quando há `approve`; sem `approve`, nega por padrão. |
| medio | `hitl_por_comando` | Cada comando passa pelo `approve` (padrão). |
| alto | `gate_global` | Exige `approve` explícito ANTES de qualquer execução; sem `approve`/negado → `BLOQUEADA` antes de rodar. |

## Skills de domínio (on-demand)

Skills em `.opencode/skills/<nome>/SKILL.md` são capacidades de tarefa
(conhecimento + procedimento) carregadas sob demanda quando a tarefa casa com a
`description`. O hub seleciona e cita as skills relevantes na delegação;
`harness/skills.py` registra/valida (origem + trust). **Skills instruem o
*como*; o runtime decide o *se pode*** — nenhuma skill autoriza o que o harness
proíbe.

## Contrato de saída

Toda execução entrega exatamente este formato:

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

Além dos campos do pipeline, o hub documenta `grau_complexidade`,
`contexto_grau`, `tempo_maximo_seg` e `usa_sandbox` (transparência do scorer).

## Regras de encerramento

- `APROVADA` exige evidências: diff, testes, lint ou build.
- `BLOQUEADA` quando: há achado de segurança, falta de evidência, escopo
  violado ou aprovação humana negada.
- Achados relevantes do reviewer voltam ao implementer para correção antes de
  encerrar.
- **Regras do harness prevalecem**: agentes e skills instruem; o runtime
  (`config.py`, `AGENTS.md`, `executor.py`) decide o que é permitido.

## Atribuições

- **hub**: extrai a entrada, aciona o brain, seleciona skills, delega,
  consolida a saída.
- **brain**: recupera memória e grava fluxos novos.
- **explorer**: só lê; relata arquivos, dependências e riscos.
- **implementer**: único que edita; entrega evidências. Recebe na delegação as
  lições confiáveis do playbook (realimentação do aprendizado).
- **pentester**: auditoria de segurança web — executa a skill `security-audit`
  com perfil `osint|superficial|completo` (pergunta o perfil; `completo` exige
  autorização de escopo + gate `PHASE3_GATE`). Nunca corrige; edita somente
  `docs/auditorias/**` e entrega o relatório gerado pelo módulo
  (`harness/security.py`).
- **reviewer**: só reporta; nunca corrige.
- **documenter**: lê/interpreta documentos; grava referências.

## Verificação de consistência

As constantes do pipeline (`harness/agents.py`) e este documento devem concordar
sobre estados, roteamento, contrato de saída e regras de bloqueio. O teste
`PipelineSpec.default` (`tests/agents_test.py`) valida as constantes; mantê-las
em sincronia com este documento é responsabilidade do ciclo de revisão.