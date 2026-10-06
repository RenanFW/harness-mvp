# sidePrjs — Referência padrão de projetos

Esta pasta é a **referência padrão** do harness para criar qualquer projeto
solicitado via `/hub` ou agentes locais.

## Convenção

- Todo projeto novo vira uma subpasta `sidePrjs/<nome>/`.
- O `<nome>` é gerado pelo **nomeador determinístico** do harness
  (`harness/namer.py`) — nunca digitado à mão.
- Nomes são slug seguros: minúsculas, hífens, sem acentos, únicos
  (case-insensitive — importante no Windows) e com no máximo
  `MAX_NOME = 40` caracteres — inclusive quando há sufixo de unicidade.

## Como nomear (CLI)

```bash
# cria sidePrjs/<nome>/ e imprime o caminho
python -m harness.namer "Criação de API REST para pedidos"
# -> sidePrjs/criacao-api-rest-pedidos

# só mostra o nome, sem criar pasta
python -m harness.namer "Criação de API REST para pedidos" --dry-run

# base alternativa
python -m harness.namer "Meu projeto" --base C:/tmp/projetos
```

> `--base` fora de `config.ROOT` (ex.: `C:/tmp`) é uso intencional do operador
> humano, fora das políticas de execução de agentes — o harness bloqueia
> caminhos externos para agentes (o pipeline rejeita escopos fora de ROOT); a
> CLI do nomeador não aplica esse bloqueio.

## Como nomear (Python)

```python
from harness.namer import criar_projeto, nome_projeto, slugify

nome = nome_projeto("Criação de API REST para pedidos")
# -> "criacao-api-rest-pedidos"

path = criar_projeto("Criação de API REST para pedidos")
# cria a pasta em sidePrjs/ e retorna o Path
```

Também é exportado pelo package: `from harness import criar_projeto`.

## Regras do nomeador

- Remove acentos e normaliza para minúsculas (`Criação` -> `criacao`).
- Mantém apenas palavras significativas (>= 3 letras, fora das stopwords em
  português: artigos, conectivos etc.).
- Junta com hífen até o limite de `MAX_NOME = 40` caracteres: na junção de
  várias palavras, novas palavras só entram inteiras (sem corte no meio); uma
  palavra única maior que o limite é truncada (limite duro).
- Nomes reservados do Windows (`CON`, `PRN`, `AUX`, `NUL`, `COM1-9`, `LPT1-9`)
  e nomes terminados em `.` ou espaço são recusados (slugify levanta ValueError)
  — inclusive com acento (ex.: `CÓN` vira `con` e é recusado).
- Colisões (case-insensitive) ganham sufixo numérico: `-2`, `-3`...; o sufixo
  conta no limite — a base é truncada para o nome final nunca passar de 40.
- Contexto sem palavras significativas (ou só stopwords) vira `projeto`
  (fallback).

## Exemplos

| Contexto pedido | Pasta criada |
| --------------- | ------------ |
| Criação de API REST para pedidos | `sidePrjs/criacao-api-rest-pedidos` |
| Ferramenta de backup em Python | `sidePrjs/ferramenta-backup-python` |
| (só conectivos: "de e para com") | `sidePrjs/projeto` |

## Convenção de conteúdo

Cada `sidePrjs/<nome>/` é um projeto independente do harness: pode ter código,
testes e documentação próprios, sem interferir na raiz. O harness trata o
escopo `sidePrjs/<nome>/` como relativo dentro de `ROOT` (o pipeline já aceita
esse escopo sem bloqueios).