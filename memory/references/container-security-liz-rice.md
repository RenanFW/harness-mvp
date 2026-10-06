---
id: container-security
tipo: livro
titulo: Container Security (1ª ed.: ...That Protect Containerized Applications; 2ª ed.: ...That Protect Cloud Native Applications)
fonte: https://containersecurity.tech/ (site oficial do livro) | O'Reilly: https://www.oreilly.com/library/view/container-security/9781492056690
autores: Liz Rice (Isovalent/Cisco; ex-Aqua Security)
editora: O'Reilly Media — 1ª ed. abr/2020, ISBN 9781492056706; 2ª ed. out/2025, ISBN 9798341627673
data: 2026-08-16
tags: [container-security, namespaces, cgroups, seccomp, capabilities, container-isolation, supply-chain, ebpf]
trust: alta
origem: https://containersecurity.tech/ (site oficial do livro) | O'Reilly: https://www.oreilly.com/library/view/container-security/9781492056690
validado_por: motor
---

# Container Security (Liz Rice, O'Reilly)

## Conceitos-chave
- Container = processo Linux isolado: namespaces (UTS, PID, mount, network,
  user...) controlam visibilidade; cgroups controlam recursos; capabilities
  e seccomp/AppArmor/SELinux limitam privilégios.
- Threat model: atores externos/internos e processos de aplicação;
  princípios: least privilege, defense in depth, reduzir attack surface,
  limitar blast radius, segregação de funções.
- Isolamento (Ch4/10/11): strengthening (seccomp filters, capabilities
  mínimas, no_new_privs, rootless, gVisor/Kata microVMs) vs breaking
  (rodar como root, --privileged, montar /var/run/docker.sock, compartilhar
  namespaces com o host, sidecars de debug).
- Imagens (Ch6): OCI standards, image layers, multiplatform, registries.
  Supply chain (Ch7): SBOM, SLSA, dependency confusion, package
  hallucination, base images minimalistas, Dockerfile security, assinatura
  e admission control.
- Vulnerabilidades em imagens (Ch8); rede (Ch12-13): CNI, criptografia
  entre componentes, service mesh; secrets (Ch14).
- Runtime protection (Ch15): Falco, Cilium Tetragon, Tracee, Inspektor
  Gadget (eBPF) — alerta vs quarentena; OWASP Top 10 (Ch16).
- 2ª ed. (2025) adiciona eBPF, tooling AI-driven, Kubernetes, IaC/GitOps.

## Padrões e regras acionáveis
- Isolar com: usuário não-root + no_new_privs + drop de capabilities +
  seccomp profile + readonly rootfs + rede isolada.
- Nunca montar o docker socket dentro de um container; nunca rodar
  --privileged em produção.
- Verificar assinatura/proveniência da imagem antes de executar (Docker
  Content Trust, admission control, SBOM).

## Aplicação no harness
- Sandbox de validação de código: aplicar exatamente esse modelo —
  namespaces + cgroups + seccomp + capabilities (cap drop, no_new_privs).
- Docker lifecycle (build -> run -> audit) com imagens minimalistas reduz a
  superfície de ataque do ambiente de execução do harness.
- eBPF/Falco para observabilidade do runtime das validações.
- Princípios de supply chain (SBOM, imagem assinada) para o ambiente de
  execução ser reproduzível e auditável.

## Pontos de atenção
- PDF NÃO é gratuito de forma legítima (assinatura O'Reilly Learning ou
  compra). Cópias piratas circulam (GitHub/dokumen.pub) — NÃO usar.
- Site oficial da autora (containersecurity.tech) tem TOC completo, exemplos
  de código e references por capítulo (GitHub: lizrice/container-security).
- Título difere entre edições: "Containerized Applications" (1ª ed.) vs
  "Cloud Native Applications" (2ª ed.).

## Fontes e status
- https://containersecurity.tech/ (site oficial do livro, TOC 16 cap., OK)
- https://github.com/lizrice/container-security (exemplos oficiais, OK)
- https://www.oreilly.com/library/view/container-security/9781492056690 (403 via fetch; metadados via search, OK)
- Status: SIM — metadados + sumário completo; sem PDF legítimo gratuito.
