"""Scorer determinístico de complexidade de tarefa (Update Final, Fase 1).

Calcula um **grau de complexidade** (baixo|medio|alto) e um **score 0-10** a
partir do texto do prompt, por heurística determinística (proxy documentado e
honesto — NÃO é avaliação semântica de verdade, é um somador de fatores
lexicais). Serve para derivar `tempo_maximo_seg` (timeout safety net) e
`usa_sandbox` (grau alto) no `harness/pipeline.py`, e para ativar o sandbox
condicional do `harness/executor.py`.

## Por que a heurística é honesta (proxy)

- É **determinística** e **reprodutível**: mesmo texto -> mesmo score, sem LLM.
- É **transparente**: cada fator que soma pontos é listado no dict de retorno
  (`fatores`), com a contribuição de cada chave.
- É **conservadora** no sentido de não fingir precisão: o campo `metrica`
  descreve exatamente O QUE foi contado (palavras do prompt presentes nos
  fatores), nunca "entendi o problema".
- O **override explícito** é documentado: se o chamador fornecer
  `grau_complexidade` explícito, a avaliação automática pode ser pulada (isso é
  tratado no pipeline/hub — ver `_monta_contrato` em `harness/pipeline.py`). O
  scorer em si apenas documenta esse contrato; não decide o override. NOTA de
  semântica: `nivel_risco` NÃO é um override do resultado — é um INPUT para o
  scorer (aplica o bônus ao score lexical).

## Fatores e pesos

Cada fator soma `peso × nº de palavras distintas do fator presentes no texto`
(case-insensitive, sem acento). Pesos por fator:

| Fator | Chave no dict | Peso/palavra | Exemplos de token |
| ----- | ------------- | ------------ | ----------------- |
| escopo/amplitude | `escopo` | 1 | arquivos, modulo, api, db, banco, integracao, microservicos, sistema, completo, framework |
| risco/efeitos colaterais | `risco` | 1 | producao, deploy, migracao, esquema, permissao, seguranca, auth, dados, reais, breaking, change |
| infraestrutura externa | `infra` | 1 | docker, container, rede, servidor, proxy, kubernetes, cloud, externo |
| escrita vs leitura | `escrita` | 2 | criar, modificar, refatorar, escrever, implementar, desenvolver, construir, adicionar |
| integração entre componentes/agentes | `integracao` | 1 | orquestracao, handoff, pipeline, integracao |
| magnitude | `magnitude` | 1 | grande, complexo, extenso, enterprise, critico, escala |
| critérios de aceite exigentes | `aceite` | 1 | testes, build, cobertura, ci |

Leitura/consulta (`consultar`, `ler`, `buscar`, `listar`, `exibir`) é o
**contrapeso**: tem peso 0 (não soma), tornando tarefas só de leitura
relativamente menos complexas.

### Bônus do nível de risco

O score recebe um bônus do `nivel_risco` fornecido: `baixo=+1`, `medio=+2`,
`alto=+3`. `nivel_risco` vazio/desconhecido -> sem bônus (0).

### Limite e classificação

- O **score final é limitado a 10** (se passar, vale 10).
- Classificação: `0-3` = baixo; `4-8` = médio; `9-10` = alto.

Retorno: dict com `score`, `grau_complexidade`, `metrica`, `fatores` e
`nivel_risco_contribuido`.
"""

from __future__ import annotations

import json
import re
import sys
import unicodedata

# Tokens por fator (palavras únicas, sem acento). Cada palavra distinta do
# texto presente em um fator contribui `peso` pontos para aquele fator.
_FATOR_ESCOPO = {
    "arquivo", "arquivos", "modulo", "modulos", "api", "db", "banco",
    "integracao", "microservicos", "sistema", "completo", "framework",
}
_FATOR_RISCO = {
    "producao", "deploy", "migracao", "esquema", "permissao", "seguranca",
    "auth", "dados", "reais", "breaking", "change",
}
_FATOR_INFRA = {
    "docker", "container", "rede", "servidor", "proxy", "kubernetes",
    "cloud", "externo",
}
_FATOR_ESCRITA = {
    "criar", "modificar", "refatorar", "escrever", "implementar",
    "desenvolver", "construir", "adicionar",
}
_FATOR_LEITURA = {
    "consulta", "consultar", "leitura", "ler", "buscar", "busca", "listar",
    "exibir", "mostrar",
}
_FATOR_INTEGRACAO = {
    "orquestracao", "handoff", "pipeline", "integracao",
}
_FATOR_MAGNITUDE = {
    "grande", "complexo", "extenso", "enterprise", "critico", "escala",
}
_FATOR_ACEITE = {
    "testes", "build", "cobertura", "ci",
}

# (chave_do_fator, tokens, peso_por_palavra)
_FATORES = (
    ("escopo", _FATOR_ESCOPO, 1),
    ("risco", _FATOR_RISCO, 1),
    ("infra", _FATOR_INFRA, 1),
    ("escrita", _FATOR_ESCRITA, 2),     # escrever/alterar pesa mais que ler
    ("integracao", _FATOR_INTEGRACAO, 1),
    ("magnitude", _FATOR_MAGNITUDE, 1),
    ("aceite", _FATOR_ACEITE, 1),
    ("leitura", _FATOR_LEITURA, 0),     # contrapeso: não soma
)

# Bônus do nível de risco (soma ao score).
_NIVEL_RISCO_BONUS = {"baixo": 1, "medio": 2, "alto": 3}

# Limite do score e faixas de classificação.
SCORE_MAX = 10
_FAIXA_BAIXA = (0, 3)   # inclusivo
_FAIXA_MEDIA = (4, 8)   # inclusivo
_FAIXA_ALTA = (9, 10)   # inclusivo


def _palavras(texto: str) -> set[str]:
    """Normaliza o texto para o conjunto de palavras (case-insensitive, sem
    acentos/cedilhas): "Integração" -> "integracao". Separa por não-letra."""
    low = texto.lower()
    low = "".join(
        c for c in unicodedata.normalize("NFKD", low)
        if not unicodedata.combining(c)
    )
    return set(re.findall(r"[a-z0-9_]+", low))


def calcular_grau_complexidade(texto: str, nivel_risco: str = "") -> dict:
    """Calcula o grau de complexidade da tarefa a partir do `texto` do prompt.

    Parâmetros:
        texto (str): texto do prompt (objetivo + escopo + restrições + critérios
            de aceite). Texto vazio -> score 0, grau baixo.
        nivel_risco (str): nível de risco ("baixo"/"medio"/"alto") para somar o
            bônus. Vazio/desconhecido -> sem bônus.

    Retorno (dict):
        score                 (int 0-10, limitado a 10)
        grau_complexidade     (str) "baixo" | "medio" | "alto"
        metrica               (str) descrição da heurística usada (proxy honesto)
        fatores               (dict) {chave_do_fator: pontos_contribuidos}
        nivel_risco_contribuido (bool) True se o bônus do nível de risco foi
            aplicado ao score.

    Sobre o override explícito: esta função SÓ faz a avaliação automática. O
    **override** (pular o scorer quando o chamador fornece `grau_complexidade`
    explícito) é decidido no **pipeline/hub** (ver `_monta_contrato` em
    `harness/pipeline.py`), não aqui — a interface da função é: quando o
    chamador quer override, ele NÃO chama esta função e usa o valor fornecido
    diretamente. Já `nivel_risco` é um INPUT (aplica o bônus ao score), não um
    override do resultado: `nivel_risco="alto"` soma +3 ao score lexical; um
    `nivel_risco` igual ao default (`"medio"`) NÃO é um sinal explícito no
    pipeline (deriva sem bônus — decidido em `_monta_contrato`).
    """
    palavras = _palavras(texto)

    # Cada fator soma `peso × nº de palavras distintas do fator presentes no
    # texto` (conjunto -> cada palavra conta no máximo 1, um proxy sensato de
    # amplitude: 3 menções de "api" não deixam a tarefa 3x maior).
    fatores: dict[str, int] = {}
    for chave, tokens, peso in _FATORES:
        presentes = palavras.intersection(tokens)
        fatores[chave] = len(presentes) * peso

    score = sum(fatores.values())

    nivel_risco_contribuido = False
    bonus = _NIVEL_RISCO_BONUS.get(nivel_risco.strip().lower(), 0)
    if bonus:
        score += bonus
        nivel_risco_contribuido = True

    score = min(score, SCORE_MAX)

    if _FAIXA_BAIXA[0] <= score <= _FAIXA_BAIXA[1]:
        grau = "baixo"
    elif _FAIXA_MEDIA[0] <= score <= _FAIXA_MEDIA[1]:
        grau = "medio"
    else:
        grau = "alto"

    metrica = (
        "Proxy determinístico de complexidade: soma de palavras distintas do "
        "prompt presentes em fatores lexicais (escopo, risco, infra, escrita "
        "x2, integração, magnitude, aceite; leitura não soma) + bônus do nível "
        "de risco (baixo+1/medio+2/alto+3), limitado a 10; 0-3 baixo, 4-8 "
        "médio, 9-10 alto. Não é análise semântica — é um somador transparente "
        "e reprodutível; override explícito é decidido no pipeline/hub."
    )

    return {
        "score": score,
        "grau_complexidade": grau,
        "metrica": metrica,
        "fatores": fatores,
        "nivel_risco_contribuido": nivel_risco_contribuido,
    }


def main(argv: list[str] | None = None) -> int:
    """CLI mínima do scorer (Lote 1 — granularidade de contexto).

    Uso: `python -m harness.complexity "<texto da tarefa>"
    [--risco baixo|medio|alto]`. Junta os argumentos posicionais em texto,
    chama `calcular_grau_complexidade(texto, nivel_risco)`, imprime o dict em
    JSON (ensure_ascii=False, indent=2) e retorna 0 em sucesso.

    É a ÚNICA permissão de bash do hub (frontmatter do agente) para derivar o
    grau de complexidade / `contexto_grau` de cada delegação.
    """
    # Console Windows (cp1252) não imprime todos os caracteres UTF-8 do JSON
    # (ex.: "í"); usa UTF-8 com substituição para nunca quebrar a saída (mesmo
    # padrão do pipeline.py main).
    try:
        sys.stdout.reconfigure(encoding="utf-8", errors="replace")
    except (AttributeError, ValueError):
        pass
    args = list(argv) if argv is not None else sys.argv[1:]
    nivel_risco = ""
    texto_args: list[str] = []
    i = 0
    while i < len(args):
        if args[i] == "--risco":
            if i + 1 >= len(args):
                print("erro: --risco exige um nível (baixo|medio|alto)")
                return 1
            nivel_risco = args[i + 1]
            i += 2
            continue
        texto_args.append(args[i])
        i += 1
    texto = " ".join(texto_args)
    if not texto.strip():
        print("uso: python -m harness.complexity '<texto da tarefa>' "
              "[--risco baixo|medio|alto]")
        return 1
    resultado = calcular_grau_complexidade(texto, nivel_risco)
    print(json.dumps(resultado, ensure_ascii=False, indent=2))
    return 0


if __name__ == "__main__":
    sys.exit(main())
