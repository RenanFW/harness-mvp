---
name: security-audit
description: "Auditoria de segurança web autorizada, por PERFIS (osint/superficial/completo). Use ao avaliar a segurança de um site/alvo cujo proprietário autorizou: osint só reconhecimento público (RDAP/DoH/CT), superficial faz leitura HTTP normal (headers/cookies/TLS/redirects/erros) e completo acrescenta testes ativos read-only autorizados. Nunca corrige falhas — entrega relatório."
tipo: skill
dominio: security
origem: harness/security.py
trust: alta
validado_por: motor
data: 2026-09-10
tags: [security, pentest, audit, ssrf-safe, perfis]
---

# Security Audit — Protocolo de auditoria de segurança web

## Regras do harness prevalecem

Esta skill instrui o *como* auditar. O *se pode* é decidido pelo runtime
(`harness/config.py`: allowlist `ALLOWED_AUDIT_TARGETS`, gate `PHASE3_GATE`,
`AUDIT_PAYLOADS_DESTRUTIVOS`; `AGENTS.md`; executor) — nenhuma instrução aqui
autoriza payloads destrutivos, alvos não autorizados ou correção de código.

A auditoria mecânica está automatizada em `harness/security.py` (v2, SSRF-safe,
reusa o transporte do webscraper):

```bash
python -m harness.security <url> --perfil <osint|superficial|completo> [--porta N] [--relatorio]
python -m harness.security <url> --perfil completo --autorizado --operador X --expira Y
python -m harness.security <url> --perfil superficial --contexto "texto informado"
# M6: POST em formularios e OPT-IN (default OFF) e PODE ALTERAR ESTADO
python -m harness.security <url> --perfil completo --autorizado --permitir-post-forms
python -m harness.security --self-test
```

## Perfis de teste

| Perfil | O que faz | Toca o alvo | Gate |
| ------ | --------- | ----------- | ---- |
| osint | Inteligência pública: RDAP, DNS/DoH, CT logs, e-mail (SPF/DMARC) | Não contata a aplicação | Não |
| superficial | Leitura HTTP normal (GET/HEAD) da superfície, sem exploração ativa | Sim, com requisições benignas | Não |
| completo | Tudo do superficial + testes ativos read-only | Sim, com sondas benignas autorizadas | Sim (autorização explícita + `PHASE3_GATE`) |

O perfil `completo` exige `escopo.autorizado=True` (validação `perfil_autorizado`
no módulo: `--autorizado`) e, no pipeline, a aprovação do gate `PHASE3_GATE`.
São camadas independentes: `--autorizado` é a autorização de ESCOPO; o
`PHASE3_GATE` é a autorização humana da Fase 3 no fluxo do pipeline. Sem isso,
o módulo recusa e nada ativo roda.

Na sonda de reflexão, POST em formulário é **opt-in e desligado por padrão**
(`--permitir-post-forms`, M6): submeter POST a um formulário real pode alterar
estado/lockout. Por padrão a sonda só usa parâmetros de query e formulários
`method=get`; campos `file`/`submit`/`reset`/`button` nunca são enviados.

## Fluxo

1. Confirme a autorização do alvo (proprietário ou autorizado explicitamente).
2. **Pergunte o perfil quando ele não for informado** e **mostre as opções
   ativas** (osint/superficial/completo, com fases, portas e exigência de gate)
   antes de rodar qualquer coisa. O módulo NUNCA assume o perfil.
3. Rode o CLI com o perfil escolhido; para `completo`, só prossiga com
   autorização explícita e o gate aprovado.
4. Cada comando no shell requer aprovação (bash ask).
5. Gere o relatório com `--relatorio` e entregue os achados (sem correção).

## Catálogo de checks

- **osint / passivo** (sem contato com a aplicação): RDAP (registrar, datas,
  nameservers, status); DNS via DoH Cloudflare (A/AAAA/NS/MX/TXT/CAA); CT logs
  (crt.sh, subdomínios observados); e-mail (SPF e DMARC ausentes).
- **superficial / superfície HTTP**: headers de segurança (HSTS, CSP,
  X-Frame-Options, X-Content-Type-Options, Referrer-Policy, Permissions-Policy,
  banner); cookies (flags Secure/HttpOnly/SameSite); TLS/HTTPS; redirects; erros
  expostos; well-known (robots.txt, sitemap.xml, security.txt); CORS passivo;
  mixed content.
- **completo / ativos read-only** (gated): métodos HTTP; CORS ativo; reflexão em
  parâmetros; open redirect; GraphQL; OpenAPI/Swagger; paths sensíveis; portas;
  segredos em JS (sempre redigidos); componentes/OSV.

## Limites rígidos

- Somente read-only: observar resposta, nunca alterar/deletar dados nem DoS.
- POST em formulário só com `--permitir-post-forms` e autorização explícita
  (pode alterar estado); por padrão a sonda de reflexão não submete POST.
- Nunca enviar payload destrutivo (marcadores de `AUDIT_PAYLOADS_DESTRUTIVOS`
  são rejeitados pelo módulo).
- Budget e rate-limit sempre ativos (`AUDIT_MAX_PROBES`,
  `AUDIT_RATE_LIMIT_SEG`, `Budget` por execução).
- Segredo sempre redigido na evidência (máscara, nunca valor bruto).
- Alvo tem de ser autorizado; alvo interno só com allowlist explícita
  (`ALLOWED_AUDIT_TARGETS` + `AUDIT_ALLOW_INTERNAL`).
- Nenhuma correção/remediação aplicada — o pentester informa, não corrige.

## Relatório

- Gravar em `docs/auditorias/<slug>/`: `README.md`, `resultados.md`,
  `achados.json`, `raw/` (capturas redigidas) e `runs/` (execução + diff).
- Anti-fabricação: todo fato deriva de um `Finding` tipado. O que o solicitante
  traz é registrado como **"informado, NÃO verificado"** (`verificado=False`) e
  NUNCA como afirmação sobre o alvo.
- Nada é afirmado sobre o alvo sem um achado que o sustente; achado `OBSERVADO`
  exige evidência bruta; `INFERIDO` fica marcado como tal.
- Cada reprovado leva detalhe da falha, evidência, severidade e recomendação
  (sem correção).

## Armadilhas

- Modo público (`HARNESS_PUBLIC=1`) impede pentest (host bloqueado + sandbox
  sem rede) — vale também em modo local com aprovações.
- whois/nmap ausentes no Windows por padrão -> RDAP/DoH (stdlib/HTTPS) é o
  padrão; ferramentas externas só se instaladas com aprovação.
- Console cp1252: usar `PYTHONIOENCODING=utf-8` para não quebrar a saída.

## Limites (quando NÃO usar)

- Alvo não autorizado.
- Payloads destrutivos ou qualquer ação que altere estado.
- Correção/remediação (fora de escopo — o pentester nunca fixa).
- Rede privada/interna sem allowlist explícita.
- Perfil `completo` sem autorização explícita e sem gate aprovado.
