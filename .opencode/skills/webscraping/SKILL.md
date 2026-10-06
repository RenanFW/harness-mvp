---
name: webscraping
description: "Pesquisa e coleta legítima de conteúdo na web para fundamentar o harness. Use ao buscar livros, PDFs, documentação ou referências externas para embasar projetos — sempre com validação anti-fabricação (>= 2 fontes editoriais) e anti-pirataria."
tipo: skill
dominio: coleta-de-conteudo
origem: "memory/references/index.md, memory/references/*.md, harness/webscraper.py"
trust: alta
validado_por: motor
data: 2026-08-30
tags: [webscraping, coleta, anti-fabricacao, anti-pirataria, referencias]
---

# Webscraping — Protocolo de pesquisa e coleta legítima

Protocolo empírico (49 referências indexadas) para o agente
**documenter**. NUNCA piratear; NUNCA inventar. Detalhes operacionais
(método de busca, downloads, fallback indireto A–F, tabela de saída,
armadilhas) na docstring de `harness/webscraper.py`.

## Regras do harness prevalecem

Esta skill instrui o *como* coletar. O *se pode* é decidido pelo runtime
(`harness/config.py` deny-list anti-pirataria, `AGENTS.md`) — nenhuma instrução
aqui autoriza material pirata ou conteúdo não validado.

## 0) Módulo autônomo (execução automática)

A coleta mecânica (busca, validação anti-fabricação, PDF legítimo, download,
gravação) está **automatizada** em `harness/webscraper.py`. Prefira executá-lo:

```bash
python -m harness.webscraper "<título>" --autor "<autor>"            # valida + grava
python -m harness.webscraper "<título>" --autor "<autor>" --pdf      # + baixa PDF oficial
python -m harness.webscraper "<título>" --autor "<autor>" --busca-apenas  # só inspeção
```

API: `POST /api/webscrape` com `{"titulo", "autor", "pdf"}` (status
`ok` | `fabricacao`, `id`, `path`, `pdf`, `fallback_indireto`). O módulo
implementa sozinho o protocolo: multi-backend (DuckDuckGo HTML → Bing HTML em
failover), APIs de livros sem chave (Google Books, Open Library, Internet
Archive), anti-fabricação (>= 2 fontes editoriais, senão alerta
`<slug>-fabricacao.md` + fallback indireto), anti-pirataria (só z-lib/zlib) e
validação `Content-Type: application/pdf`. Depois, o documenter **completa** a
referência lendo o conteúdo baixado.

## 1) Validação anti-fabricação (SEMPRE primeiro)

~30% dos títulos pedidos **não existem** (fabricados por LLM):

1. Confirme a existência em **>= 2 fontes editoriais independentes** (site do
   autor, editora, Google Books, Amazon, Goodreads).
2. Se não existir: grave alerta em `memory/references/<slug>-fabricacao.md`;
   **NUNCA invente** sumário/conceitos/citações; registre o substituto real
   mais próximo com ressalva.
3. Registre na tabela de saída para auditoria.

## 2) Fontes preferenciais legítimas

1. **Site oficial do autor** (ex.: `multiagentbook.com`, `fluentpython.com`).
2. **Repositórios GitHub oficiais** (livros de código aberto).
3. **Versões gratuitas oficiais** (nlp.stanford.edu/IR-book, cosmicpython.com,
   obeythetestinggoat.com, cs.au.dk/~amoeller/spa/, capítulos-amostra).
4. Google Books (preview) e Amazon (página oficial) para metadados.
5. PDFs de universidades/institutos (domínio aberto).

**Evitar**: O'Reilly/Manning **direto** (403 de bots) — use site do autor,
Google Books ou GitHub oficial.

## 5) Entrega (uma referência por livro)

Crie `memory/references/<slug>.md` com frontmatter
(`id`, `tipo`, `titulo`, `autor`, `fonte`, `data`, `tags`, `trust`, `origem`,
`validado_por`) e as seções obrigatórias: **Conceitos-chave**, **Padrões
acionáveis**, **Aplicação no motor**, **Pontos de atenção**. Atualize
`memory/references/index.md`.

## Armadilha essencial

Console cp1252 quebra acentos: rode com `PYTHONIOENCODING=utf-8` e ASCII em
prints (demais na docstring de `harness/webscraper.py`).