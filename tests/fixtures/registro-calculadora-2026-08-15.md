---
id: registro-calculadora-2026-08-15
keywords: [calculadora, cli, python, testes]
data: 2026-08-15
agente: hub
status: completed
---

# Calculadora com as 4 operações básicas

## Contrato de entrada
- objetivo: calculadora funcional com as 4 operações básicas
- escopo: arquivo único novo na raiz do projeto
- restricoes: zero deps, stdlib apenas
- criterios_de_aceite: soma/subtração/multiplicação/divisão corretas

## Fluxo
1. RECEBIDA — contrato extraído.
2. CONSULTANDO_MEMORIA — core.md lido.
3. EM_IMPLEMENTACAO — criado `calculadora.py` e a suíte de testes.
4. EM_REVISAO — testes executados: 28/28 passaram.

## Resultado
- status: APROVADA
- resumo: calculadora validada; suíte ampliada para 28 casos
- validacoes_executadas:
  - `python tests/calculadora_test.py` → 28/28 PASS
  - demo CLI das 4 operações

## Contexto
- Fixture sintética da suíte (cópia limpa, sem memória pessoal).
