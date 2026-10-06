---
id: automated-program-repair
tipo: artigo
titulo: Automated Program Repair (artigo de síntese — CACM)
fonte: https://doi.org/10.1145/3318162 | PDF: https://clairelegoues.com/assets/papers/legoues-cacm2019.pdf
autores: Claire Le Goues (CMU), Michael Pradel (TU Darmstadt/Univ. Stuttgart), Abhik Roychoudhury (NUS)
editora: Communications of the ACM 62(12), pp. 56-65, dez/2019
data: 2026-08-16
tags: [apr, program-repair, ast, symbolic-execution, learning, generate-validate, overfitting]
trust: alta
origem: https://doi.org/10.1145/3318162 | PDF: https://clairelegoues.com/assets/papers/legoues-cacm2019.pdf
validado_por: motor
---

# Automated Program Repair (CACM 2019)

## Conceitos-chave
- APR: dado um programa com bug + test-suite (oráculo de correção), gerar
  patch que passa na suíte; validação = rodar os testes com o patch aplicado.
- Generate-and-validate: loop (localizar -> gerar candidato -> validar ->
  aplicar) sobre um espaço de busca de modificações sintáticas.
- Três famílias: (a) heuristic repair (ex.: GenProg — transformações no
  Abstract Syntax Tree, busca estocástica com fitness por testes);
  (b) constraint-based repair (ex.: SemFix, Angelix — reparo semântico:
  path conditions, repair constraints, angelic values, síntese de código);
  (c) learning-aided repair (ex.: DeepFix — redes seq2seq RNN; código é
  abstraído com placeholders VAR1/VAR2 antes de alimentar o modelo).
- Overfitting: patch que passa na suíte mas não generaliza; o test-suite é
  especificação incompleta — risco central de qualidade.
- Fault localization precede a geração: o AST delimita onde o patch pode ser
  aplicado (transformações no AST).
- Use cases: CI/regressão, patching de vulnerabilidades (Heartbleed),
  educação (exercícios), reparo de propriedades não funcionais (performance,
  control-flow integrity, sanitizers).
- Desafios abertos (Seção 5): qualidade (medidas de correção, oráculos
  alternativos, garantias de correção), escopo e integração no workflow.

## Padrões e regras acionáveis
- Pipeline do reparo = localizar (análise estática) -> gerar candidato
  (transformação no AST) -> validar (oráculo/testes) -> aplicar se passar.
- Abstração do código antes de qualquer modelo: substituir identificadores
  de aplicação por placeholders genéricos (padrão AST abstraction).
- Oráculos alternativos (asserções, invariantes, contratos) aumentam a
  qualidade do reparo além do test-suite.

## Aplicação no harness
- O motor determinístico pode espelhar o generate-and-validate como
  "repair loop" do sandbox: estados encadeados com validação explícita.
- AST abstraction (placeholder de identificadores) = base da abstração
  AST do motor integrado (harness/motor).
- Overfitting = lição para a Evaluation (Ch19): exigir evidência por
  múltiplos critérios, nunca só "passou nos testes".
- Patching de vulnerabilidades conecta APR a seccomp/container security.

## Pontos de atenção
- NÃO é livro de Foundations and Trends: é artigo de síntese da CACM,
  acesso aberto no site do autor (PDF legítimo baixado e registrado).
- PDF não versionado neste template; baixe pela fonte oficial indicada acima.
- DOI oficial: 10.1145/3318162. Autores: CMU + Stuttgart + NUS.

## Fontes e status
- https://clairelegoues.com/assets/papers/legoues-cacm2019.pdf (PDF oficial, OK)
- https://www.comp.nus.edu.sg/~abhik/pdf/cacm19.pdf (mirror NUS, OK)
- https://cacm.acm.org/research/automated-program-repair/ (resumo, OK)
- Status: SIM — PDF legítimo baixado; conteúdo extraído do PDF.
