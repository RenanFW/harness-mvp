"""Orquestrador determinístico do pipeline de agentes (sem LLM).

Implementa o delivery-protocol (estados, papéis, permissões, validações,
evidência) de forma determinística: entrada validada, memória consultada,
exploração informativa, passos sugeridos com aprovação HITL, revisão contra
critérios mínimos e consolidação do contrato de saída.

Não executa comandos por conta própria: cada comando sugerido só roda se o
callback `approve` retornar True (padrão: nenhum comando é aprovado). Quando
aprovado, o comando é executado DE FATO via Executor (evidência real: output
e exit_code do job), nunca fabricado.
"""

from __future__ import annotations

import json
import pathlib
import re
import shlex
import sys
import threading
import time
from dataclasses import dataclass, field
from typing import Callable

from . import config
from . import health
from .agents import Playbook, licoes_confiaveis_textos
from .complexity import calcular_grau_complexidade
from .eval import default_rules, evaluate
from .executor import Executor
from .memory import Memory

# Nomes de arquivos/diretórios sensíveis (heurística de segurança na exploração)
_SENSITIVE_NAMES = (
    ".env", "secret", "token", "credential", "chave",
    "id_rsa", ".pem", ".pfx", ".key", ".ssh",
)

# Separadores de shell que NUNCA podem existir em um comando auto-aprovado no
# risco baixo (Finding 1 da re-revisão do Item 2). Em cmd.exe/PowerShell
# (shell=True), `&`, `&&`, `|`, `||`, `;` encadeiam comandos e `<`, `>` (e
# `>>`, `2>&1`) fazem redirecionamento. A checagem é feita tanto na string crua
# quanto token a token (o `;` pode vir colado ao token: `git status;`).
_SEPARADORES_SHELL = "&|;<>"

# Normalização de texto para detecção de keywords da Fase 3 (testes ativos):
# remove acentos e deixa tudo minúsculo antes do match de substring.
_ACENTOS = str.maketrans(
    "áàâãäéèêëíìîïóòôõöúùûüç",
    "aaaaaeeeeiiiiooooouuuuc",
)


class PipelineError(Exception):
    """Erro de pipeline com indicação da etapa em que ocorreu."""

    def __init__(self, message: str, stage: str = "") -> None:
        super().__init__(message)
        self.stage = stage


class _TimeoutEtapa(Exception):
    """Sinaliza que uma etapa excedeu o timeout cooperativo (Item 7.2).

    NÃO é um erro fatal: o helper `_com_timeout` a levanta apenas para o
    `run_task` encerrar o pipeline com ressalva preservando o progresso. A
    thread da etapa continua rodando em segundo plano (daemon), pois o Python
    NÃO oferece preempção/kill de thread — por isso o timeout é dito
    COOPERATIVO (não preemptivo)."""

@dataclass
class TaskContract:
    """Entrada validada de uma tarefa (contrato de entrada do delivery-protocol)."""

    objetivo: str = ""
    escopo: str = ""
    restricoes: list[str] = field(default_factory=list)
    criterios_de_aceite: list[str] = field(default_factory=list)
    nivel_de_risco: str = "medio"
    # Update Final (Fase 1): grau de complexidade (baixo|medio|alto) derivado
    # do prompt pelo scorer OU fornecido explicitamente (override); timeout
    # safety net derivado; e flag de sandbox (True apenas p/ complexidade alta).
    grau_complexidade: str = "baixo"
    tempo_maximo_seg: int = 600
    usa_sandbox: bool = False
    # Lote 1 (otimização de contexto): quanto contexto enviar à LLM em cada
    # delegação (minimo|padrao|completo), derivado do grau de complexidade.
    contexto_grau: str = "padrao"
    # Fase 3 (testes ativos de segurança web, ex.: pentest ativo autorizado):
    # exige aprovação humana explícita além do gate de risco. Default False
    # (retrocompatível: tarefas comuns não passam pelo gate da Fase 3).
    testes_ativos: bool = False
    # Perfil de auditoria de segurança (osint|superficial|completo). Default
    # vazio: campo ADITIVO e opcional — não há validação que reprove (tarefas
    # comuns não informam perfil). Quando `completo`, o pipeline mapeia para
    # `testes_ativos=True` (o gate `PHASE3_GATE` existente cobre o ativo).
    perfil: str = ""
    # Etapa 5 (ladder de resposta adaptativa): transparência das AÇÕES aplicadas
    # pela ladder de saúde (ex.: ["risco_alto", "sandbox", "memoria_bloqueada"]).
    # Vazio quando o nível não escalona (SAUDAVEL/ATENCAO, bootstrap/neutro ou
    # detector indisponível). Aditivo e retrocompatível: o default não altera
    # nenhum comportamento existente.
    escalonamento: list[str] = field(default_factory=list)
    # Etapa 5 (Achado 2): True quando a ladder detectou que o detector de saúde
    # está INDISPONÍVEL (falha de medição). A ladder trata como ATENCAO
    # (informativo, sem escalar gates — evita DoS), mas expõe o motivo no
    # contrato de saída e na etapa; `_pode_gravar` segue conservador
    # (bloqueia a gravação). Aditivo e retrocompatível.
    saude_indisponivel: bool = False

    def validate(self) -> list[str]:
        """Retorna a lista de erros de validação (vazia se contrato completo)."""
        erros: list[str] = []
        if not self.objetivo.strip():
            erros.append("objetivo é obrigatório")
        if not self.escopo.strip():
            erros.append("escopo é obrigatório")
        if not self.criterios_de_aceite:
            erros.append("criterios_de_aceite é obrigatório")
        if self.nivel_de_risco not in config.NIVEIS_RISCO:
            erros.append(
                f"nivel_de_risco inválido: {self.nivel_de_risco!r} "
                f"(esperado {'/'.join(config.NIVEIS_RISCO)})"
            )
        if self.grau_complexidade not in config.NIVEIS_COMPLEXIDADE:
            erros.append(
                f"grau_complexidade inválido: {self.grau_complexidade!r} "
                f"(esperado {'/'.join(config.NIVEIS_COMPLEXIDADE)})"
            )
        if self.contexto_grau not in config.NIVEIS_CONTEXTO:
            erros.append(
                f"contexto_grau inválido: {self.contexto_grau!r} "
                f"(esperado {'/'.join(config.NIVEIS_CONTEXTO)})"
            )
        try:
            tempo = int(self.tempo_maximo_seg)
        except (TypeError, ValueError):
            tempo = 0
        if tempo <= 0:
            erros.append(
                f"tempo_maximo_seg inválido: {self.tempo_maximo_seg!r} "
                "(esperado int > 0)"
            )
        return erros


class AgentPipeline:
    """Orquestrador determinístico: roda uma tarefa pelos 6 estados e entrega
    o contrato de saída do delivery-protocol."""

    def __init__(
        self,
        playbook: Playbook | dict | None = None,
        executor: Executor | None = None,
        memory: Memory | None = None,
        approve: Callable[[str], bool] | None = None,
        gravar_registro: bool = True,
    ) -> None:
        """`approve` é o callback HITL: se None, comandos de escrita/execução
        são negados por padrão (segurança).

        `gravar_registro` (default True, retrocompatível): quando False, o
        pipeline NÃO grava registro episódico ao final (`_grava_registro` é
        pulado). Usado por execuções de teste/validação para não gerar LIXO de
        memória (ex.: registros `validacao-pipeline-risco-*`). O comportamento
        padrão `AgentPipeline()` continua gravando (não quebra o pipeline real)."""
        self._playbook = self._normaliza_playbook(playbook)
        self.executor = executor or Executor()
        self.memory = memory or Memory()
        self.approve = approve
        self.gravar_registro = gravar_registro
        self._etapas: list[dict] = []
        self._status = "RECEBIDA"
        self._resultado: dict | None = None
        # Etapa 3/M2: saúde de ENTRADA (dados anteriores à execução corrente),
        # capturada uma vez em `run_task` e usada pelo anti-envenomamento — o
        # histórico escrito pela própria execução não contamina o gate.
        self._saude_entrada: dict | None = None
        # Etapa 5 (ladder): nível efetivo da saúde de ENTRADA e ações aplicadas
        # pelo escalonamento. Resetados a cada `run_task`.
        self._nivel_saude: str = "SAUDAVEL"
        self._escalonamento: list[str] = []
        # Achado 2: True quando a saúde de ENTRADA veio com `indisponivel=True`
        # (falha do detector). A ladder trata como ATENCAO, mas expõe o motivo
        # no contrato de saída e na etapa. Resetado a cada `run_task`.
        self._saude_indisponivel: bool = False

    # ------------------------------------------------------------- execução
    def run_task(self, task: dict) -> dict:
        """Executa o pipeline e retorna o contrato de saída (mesmo formato do
        delivery-protocol), com log de etapas em 'etapas'."""
        # Achado 1 (correção): zera o log de etapas ANTES de qualquer validação
        # — inclusive o early-return de tarefa não-dict. Antes, o `_bloqueia`
        # do early-return anexava a etapa de bloqueio ao `_etapas` de uma
        # execução ANTERIOR (vazamento de etapas no contrato BLOQUEADA).
        self._etapas = []
        self._status = "RECEBIDA"
        self._saude_entrada = None
        self._nivel_saude = "SAUDAVEL"
        self._escalonamento = []
        self._saude_indisponivel = False
        if not isinstance(task, dict):
            return self._bloqueia("RECEBIDA", "tarefa deve ser um dict")

        contrato = self._monta_contrato(task)
        erros = contrato.validate()
        if erros:
            return self._bloqueia(
                "RECEBIDA", "contrato inválido: " + "; ".join(erros),
                perfil=contrato.perfil,
            )
        self._etapa("RECEBIDA", "contrato validado", ok=True)

        # Etapa 3/M2 (bootstrap robusto): snapshot da saúde ANTES de executar
        # qualquer comando. A decisão de gravação usa este snapshot — o
        # histórico que a execução corrente grava (efeito colateral) não
        # contamina o gate nem impede o bootstrap de um harness novo.
        self._saude_entrada = self._saude_atual()

        # Etapa 5 (ladder de resposta adaptativa): a saúde de ENTRADA age sobre
        # o contrato ANTES da avaliação do gate de risco, para o escalonamento
        # surtir efeito. DEGRADADO/CRITICO forçam risco "alto" (gate_global),
        # `usa_sandbox=True` e o bloqueio de gravação (anti-envenomamento). A
        # decisão usa `_saude_entrada` — a execução corrente NÃO contamina o
        # nível (a saúde pós-run é só exposta no contrato de saída).
        self._escalonamento = self._aplica_ladder(contrato, self._saude_entrada)

        # Gate de saúde CRITICA (Etapa 5): além do endurecimento, RECUSA a
        # execução por completo até aprovação humana explícita. Aprovado o gate,
        # o fluxo segue endurecido (risco "alto" -> gate_global do RISCO_GATE).
        if self._nivel_saude == "CRITICO":
            politica_critica = self._politica(contrato.nivel_de_risco)
            if self.approve is None:
                return self._bloqueia(
                    "RECEBIDA",
                    "saúde CRITICA exige aprovação humana explícita "
                    "(approve ausente)",
                    risco=contrato.nivel_de_risco,
                    politica=politica_critica,
                    usa_sandbox=contrato.usa_sandbox,
                    saude=self._saude_entrada,
                    escalonamento=list(self._escalonamento),
                )
            if not bool(self.approve(config.SAUDE_GATE_CRITICO)):
                return self._bloqueia(
                    "RECEBIDA",
                    "saúde CRITICA: aprovação humana negada",
                    risco=contrato.nivel_de_risco,
                    politica=politica_critica,
                    usa_sandbox=contrato.usa_sandbox,
                    saude=self._saude_entrada,
                    escalonamento=list(self._escalonamento),
                )
            self._etapa(
                "RECEBIDA",
                "saúde CRITICA: aprovação humana concedida (segue endurecido)",
                ok=True,
                saude="CRITICO",
            )

        # Update Final (Fase 1): timeout SAFETY NET (Exception Ch12). O
        # `tempo_maximo_seg` (derivado por complexidade ou explícito) é o
        # limite TOTAL entre etapas. Não é um watchdog agressivo que mata a
        # execução no meio: apenas checamos o tempo decorrido ENTRE etapas e, ao
        # ultrapassar, encerramos com APROVADA_COM_RESSALVAS + achado de timeout,
        # preservando o progresso já registrado. Detecta travamento, não corta
        # execução legítima.
        inicio = time.monotonic()
        self._tempo_limite = float(contrato.tempo_maximo_seg)
        self._tempo_inicio = inicio

        # Gate HITL por nível de risco (Item 2): antes de qualquer exploração/
        # implementação, o risco determina a política de aprovação. Risco alto
        # exige um "ok global" explícito ANTES de executar qualquer comando;
        # sem `approve` ou sem aprovação do gate, o pipeline BLOQUEIA e não
        # roda parcialmente.
        politica = self._politica(contrato.nivel_de_risco)
        self._etapa(
            "RECEBIDA",
            f"política HITL por nível de risco "
            f"{contrato.nivel_de_risco}: {politica}",
            ok=True,
            risco=contrato.nivel_de_risco,
            politica=politica,
        )
        if politica == "gate_global":
            if self.approve is None:
                return self._bloqueia(
                    "RECEBIDA",
                    "risco alto exige aprovação humana (approve ausente)",
                    risco=contrato.nivel_de_risco,
                    politica=politica,
                    perfil=contrato.perfil,
                    grau_complexidade=contrato.grau_complexidade,
                    tempo_maximo_seg=contrato.tempo_maximo_seg,
                    usa_sandbox=contrato.usa_sandbox,
                    contexto_grau=contrato.contexto_grau,
                    saude=self._saude_entrada,
                    escalonamento=list(self._escalonamento),
                )
            ok_global = bool(self.approve(config.RISCO_GATE_ALTO))
            if not ok_global:
                return self._bloqueia(
                    "RECEBIDA",
                    "risco alto: aprovação humana global negada",
                    risco=contrato.nivel_de_risco,
                    politica=politica,
                    perfil=contrato.perfil,
                    grau_complexidade=contrato.grau_complexidade,
                    tempo_maximo_seg=contrato.tempo_maximo_seg,
                    usa_sandbox=contrato.usa_sandbox,
                    contexto_grau=contrato.contexto_grau,
                    saude=self._saude_entrada,
                    escalonamento=list(self._escalonamento),
                )
            self._etapa(
                "RECEBIDA",
                "risco alto: aprovação humana global concedida",
                ok=True,
                politica=politica,
            )

        # Gate da Fase 3 (testes ativos): aprovação humana explícita, além do risco.
        if contrato.testes_ativos:
            if self.approve is None:
                return self._bloqueia("RECEBIDA", "fase 3 (testes ativos) exige aprovação humana (approve ausente)", risco=contrato.nivel_de_risco, politica=politica, testes_ativos=True, perfil=contrato.perfil)
            ok_fase3 = bool(self.approve(config.PHASE3_GATE))
            if not ok_fase3:
                return self._bloqueia("RECEBIDA", "fase 3 (testes ativos): aprovação humana negada", risco=contrato.nivel_de_risco, politica=politica, testes_ativos=True, perfil=contrato.perfil)
            self._etapa("RECEBIDA", "fase 3 (testes ativos): aprovação humana concedida", ok=True, fase3=True)

        # Safety net: encerra com ressalva se o tempo total (complexidade)
        # foi ultrapassado (entre etapas — não corta no meio).
        encerrar = self._encerra_por_timeout(contrato)
        if encerrar:
            return encerrar

        # Estado 2 — memória (não bloqueia)
        self._status = "CONSULTANDO_MEMORIA"
        query = f"{contrato.objetivo} {contrato.escopo}"
        # Lote 1 (otimização de contexto): limite de snippets do RAG por nível
        # de contexto (conservador: minimo 2, padrao 4, completo 6).
        rag_limit = config.RAG_LIMIT_POR_COMPLEXIDADE.get(
            contrato.contexto_grau, 4
        )
        # Item 2.2 (rotular -> filtrar): o pipeline só usa memória de trust
        # >= `config.RAG_MIN_TRUST` ("media"); memória fraca (e a
        # não-recuperável, já excluída no search) NÃO alimenta o pipeline como
        # conhecimento confirmado. A ordenação por relevância é preservada.
        # Item 7.2: a consulta roda sob timeout cooperativo de ETAPA (thread
        # daemon + join) para não travar o pipeline indefinidamente.
        try:
            hits = self._com_timeout(
                lambda: self.memory.search(
                    query, limit=rag_limit, min_trust=config.RAG_MIN_TRUST
                ),
                config.PIPELINE_ETAPA_TIMEOUT_SEG,
                nome="CONSULTANDO_MEMORIA",
            )
        except _TimeoutEtapa:
            return self._encerra_por_timeout_etapa(
                contrato, "CONSULTANDO_MEMORIA",
                config.PIPELINE_ETAPA_TIMEOUT_SEG,
            )
        self._etapa(
            "CONSULTANDO_MEMORIA",
            f"{len(hits)} registro(s) recuperado(s): {[h['id'] for h in hits]}",
            ok=True,
        )

        # Estado 3 — exploração (informativa; só leitura)
        self._status = "EM_EXPLORACAO"
        # Item 7.2: a exploração roda sob timeout cooperativo de ETAPA e é
        # limitada ao cap `PIPELINE_EXPLORACAO_MAX_ARQUIVOS` (anti-glob
        # patológico); o cap é registrado na etapa (`truncado=True`).
        try:
            arquivos, riscos, truncado = self._com_timeout(
                lambda: self._explora(contrato.escopo),
                config.PIPELINE_ETAPA_TIMEOUT_SEG,
                nome="EM_EXPLORACAO",
            )
        except _TimeoutEtapa:
            return self._encerra_por_timeout_etapa(
                contrato, "EM_EXPLORACAO", config.PIPELINE_ETAPA_TIMEOUT_SEG
            )
        detalhe_exploracao = (
            f"{len(arquivos)} arquivo(s) mapeado(s); riscos: {len(riscos)}"
        )
        if truncado:
            detalhe_exploracao += (
                f"; exploração TRUNCADA no cap de "
                f"{config.PIPELINE_EXPLORACAO_MAX_ARQUIVOS} arquivo(s)"
            )
        self._etapa(
            "EM_EXPLORACAO",
            detalhe_exploracao,
            ok=not riscos,
            arquivos=arquivos,
            truncado=truncado,
        )
        if riscos:
            return self._bloqueia(
                "EM_EXPLORACAO", "; ".join(riscos),
                perfil=contrato.perfil,
            )

        encerrar = self._encerra_por_timeout(contrato)
        if encerrar:
            return encerrar

        # Estado 4 — implementação: passos sugeridos com aprovação HITL
        self._status = "EM_IMPLEMENTACAO"
        texto_contrato = " ".join(
            [contrato.objetivo, contrato.escopo]
            + contrato.restricoes + contrato.criterios_de_aceite
        )
        bloqueio = self.executor.check_policy(texto_contrato)
        if bloqueio:
            return self._bloqueia(
                "EM_IMPLEMENTACAO", f"comando destrutivo no contrato: {bloqueio}",
                risco=contrato.nivel_de_risco,
                politica=politica,
                grau_complexidade=contrato.grau_complexidade,
                tempo_maximo_seg=contrato.tempo_maximo_seg,
                usa_sandbox=contrato.usa_sandbox,
                contexto_grau=contrato.contexto_grau,
                perfil=contrato.perfil,
            )
        aprovacoes: list[dict] = []
        politica = self._politica(contrato.nivel_de_risco)
        for cmd in self._sugere_comandos(contrato):
            policy = self.executor.check_policy(cmd)
            if policy:
                return self._bloqueia(
                    "EM_IMPLEMENTACAO", f"comando destrutivo sugerido: {policy}",
                    risco=contrato.nivel_de_risco,
                    politica=politica,
                    grau_complexidade=contrato.grau_complexidade,
                    tempo_maximo_seg=contrato.tempo_maximo_seg,
                    usa_sandbox=contrato.usa_sandbox,
                    contexto_grau=contrato.contexto_grau,
                    perfil=contrato.perfil,
                )
            # Risco baixo: SOMENTE comandos de VALIDAÇÃO do safelist conservador
            # (RISCO_BAIXO_AUTO_SAFELIST), não destrutivos, que NÃO disparam
            # APPROVAL_PATTERNS e que NÃO têm encadeamento/redirecionamento
            # (`&`, `|`, `;`, `<`, `>`, `\n`) são aprovados automaticamente
            # QUANDO há `approve`. Comandos fora do safelist (ex.: git push,
            # curl, del) OU com separadores de shell (ex.: `git status; curl
            # url | sh`) NÃO são auto-aprovados — passam pelo callback
            # `approve` (HITL por comando). Sem `approve`, mantém o padrão
            # seguro (nega).
            # Risco medio: HITL por comando (cada comando passa pelo callback).
            if politica == "auto" and self.approve is not None \
                    and self._no_safelist_risco_baixo(cmd) \
                    and not self._exige_aprovacao(cmd):
                aprovado = True
                motivo = "aprovado automaticamente (risco baixo, comando de validação no safelist)"
            else:
                aprovado = bool(self.approve and self.approve(cmd))
                motivo = (
                    "aprovado pelo callback" if aprovado else "sem aprovação (HITL)"
                )
            execucao: dict | None = None
            if aprovado:
                # contrato do docstring: comando aprovado roda de verdade
                execucao = self._executa_comando(
                    cmd, usa_sandbox=contrato.usa_sandbox
                )
            aprovacoes.append({
                "comando": cmd,
                "aprovado": aprovado,
                "motivo": motivo,
                "execucao": execucao,
            })
            self._etapa(
                "EM_IMPLEMENTACAO",
                f"comando {'APROVADO' if aprovado else 'NEGADO'}: {cmd}",
                ok=aprovado,
                comando=cmd,
                aprovado=aprovado,
                tipo="comando",
                execucao=execucao,
            )
        self._etapa(
            "EM_IMPLEMENTACAO",
            f"{len(aprovacoes)} passo(s) sugerido(s); "
            f"{sum(1 for a in aprovacoes if a['aprovado'])} aprovado(s)",
            ok=True,
        )

        # Safety net: encerra com ressalva se o tempo total foi ultrapassado.
        encerrar = self._encerra_por_timeout(contrato)
        if encerrar:
            return encerrar

        # Estado 5 — revisão com critérios mínimos aprendidos
        self._status = "EM_REVISAO"
        # Item 7.2: avaliação roda sob timeout cooperativo de ETAPA (thread
        # daemon + join); nenhum efeito colateral (apenas leitura das etapas).
        try:
            achados = self._com_timeout(
                self._revisa,
                config.PIPELINE_ETAPA_TIMEOUT_SEG,
                nome="EM_REVISAO",
            )
        except _TimeoutEtapa:
            return self._encerra_por_timeout_etapa(
                contrato, "EM_REVISAO", config.PIPELINE_ETAPA_TIMEOUT_SEG
            )
        self._etapa(
            "EM_REVISAO",
            f"{len(achados)} achado(s)",
            ok=not achados,
            achados=achados,
        )

        # Safety net: encerra com ressalva se o tempo total foi ultrapassado.
        encerrar = self._encerra_por_timeout(contrato)
        if encerrar:
            return encerrar

        # Estado 6 — encerramento
        self._status = "ENCERRAMENTO"
        resultado = self._consolida(contrato, aprovacoes, achados)
        self._etapa("ENCERRAMENTO", f"status final: {resultado['status']}", ok=True)
        # F4: BLOQUEADA (sem evidência, contrato inválido, risco/gate negado,
        # etc.) NÃO grava registro episódico — evita LIXO de memória em dry-run
        # e em bloqueios legítimos (nada foi aprendido/executado). Só grava
        # resultados que chegaram a um desfecho com execução (APROVADA*).
        # Etapa 3 (anti-envenomamento): quando a saúde do sistema está
        # DEGRADADO/CRITICO COM evidência real de degradação (fontes
        # presentes), NÃO grava o registro — evita que uma execução de sistema
        # degradado envenene a memória que o próprio detector usa (loop
        # vicioso). M2 (bootstrap robusto): num harness SEM fontes, NÃO bloqueia
        # — a instalação nova precisa gravar o primeiro registro. A decisão usa
        # a saúde de ENTRADA (`_saude_para_gate`), não contaminada pelo
        # histórico que a execução corrente acabou de escrever; falha do
        # detector -> saúde neutra (conservador) que bloqueia.
        if self.gravar_registro and resultado["status"] != "BLOQUEADA":
            saude = self._saude_para_gate()
            if not self._pode_gravar(saude):
                self._etapa(
                    "ENCERRAMENTO",
                    "gravação de registro episódico BLOQUEADA pelo detector "
                    f"de saúde (nível {saude.get('nivel')}, score "
                    f"{saude.get('score')}): anti-envenomamento",
                    ok=True,
                    saude=saude,
                    motivo="saude_baixa",
                )
            else:
                self._grava_registro(contrato, resultado)
        self._status = resultado["status"]
        resultado["etapas"] = list(self._etapas)
        self._resultado = resultado
        return resultado

    # ----------------------------------------------------------- estado 4
    def _sugere_comandos(self, contrato: TaskContract) -> list[str]:
        """Passos sugeridos: validações comuns aprendidas + comandos presentes
        nos critérios de aceite."""
        comandos: list[str] = []
        for item in self._validacoes_comuns():
            cmd = item.split(" (x")[0].strip()
            if cmd and cmd not in comandos:
                comandos.append(cmd)
        # A1 (extração ESTRITA de critérios): só comandos reais (executável
        # conhecido + argumentos não-prosa). Texto solto como "o codigo deve
        # ser python e funcionar" NÃO vira passo (nem achado falso).
        for cmd in health.extrai_comandos_criterios(
            " ".join(contrato.criterios_de_aceite)
        ):
            if cmd not in comandos:
                comandos.append(cmd)
        return comandos

    # ------------------------------------------------------------ estado 5
    def _executa_comando(self, cmd: str, timeout: float | None = None,
                         usa_sandbox: bool | None = None) -> dict:
        """Executa de fato um comando aprovado via Executor e aguarda o job
        terminar (polling curto com sleep; o Executor roda em thread).

        Retorna a evidência REAL do job: output acumulado + exit_code. Nunca
        chega aqui sem `check_policy` ter passado (bloqueio destrutivo acontece
        antes, no laço de comandos); o Executor reaplica a política como defesa
        em profundidade (job 'blocked' -> evidência marcada como bloqueada).

        `usa_sandbox` (Update Final, Fase 1): repassa ao Executor a ativação
        condicional do sandbox por complexidade (True apenas p/ grau alto).
        `timeout`/poll vêm de config (COMANDO_TIMEOUT_SEG / COMANDO_POLL_SEG)."""
        timeout = config.COMANDO_TIMEOUT_SEG if timeout is None else timeout
        job = self.executor.run(cmd, usa_sandbox=usa_sandbox)
        snap = job.snapshot()
        if snap["status"] == "blocked":
            return {
                "comando": cmd,
                "output": "",
                "exit_code": None,
                "bloqueado": snap.get("reason", "bloqueado pelo executor"),
            }
        deadline = time.monotonic() + timeout
        while snap["status"] == "running" and time.monotonic() < deadline:
            time.sleep(config.COMANDO_POLL_SEG)
            snap = job.snapshot()
        if snap["status"] == "running":
            self.executor.stop(job.id)
            snap = job.snapshot()
            return {
                "comando": cmd,
                "output": snap.get("output", ""),
                "exit_code": None,
                "timeout": True,
            }
        # A3 (cap de saída): o comando RODOU e foi encerrado por excesso de
        # saída (exit_code None + razão "saída excedeu") — é EVIDÊNCIA real de
        # execução, não "não executou". Marcador para `_consolida` contar como
        # ressalva (não deixar o contrato mentir que nada rodou).
        encerrado_cap = (
            snap.get("status") == "error"
            and snap.get("exit_code") is None
            and "saída excedeu" in (snap.get("reason") or "")
        )
        return {
            "comando": cmd,
            "output": snap.get("output", ""),
            "exit_code": snap.get("exit_code"),
            "status": snap.get("status"),
            "encerrado_por_cap": encerrado_cap,
        }

    def _revisa(self) -> list[dict]:
        """Avalia as evidências coletadas com critérios mínimos: cada comando
        aprovado é avaliado com o OUTPUT e o EXIT_CODE reais do job (nada de
        exit_code fabricado); cobertura incompleta vira achado informativo."""
        achados: list[dict] = []
        evidencias = [
            e for e in self._etapas
            if e.get("tipo") == "comando" and e.get("aprovado")
        ]
        if not evidencias:
            achados.append({
                "severidade": "informativa",
                "descricao": "nenhuma validação executada (nenhum comando aprovado)",
            })
            return achados
        for e in evidencias:
            execucao = e.get("execucao") or {}
            if execucao.get("bloqueado"):
                achados.append({
                    "severidade": "informativa",
                    "descricao": (
                        f"validação bloqueada pelo executor: {e.get('comando')} "
                        f"({execucao.get('bloqueado')})"
                    ),
                })
                continue
            if execucao.get("timeout"):
                achados.append({
                    "severidade": "informativa",
                    "descricao": (
                        f"validação excedeu o tempo limite: {e.get('comando')}"
                    ),
                })
                continue
            if execucao.get("encerrado_por_cap"):
                achados.append({
                    "severidade": "informativa",
                    "descricao": (
                        f"validação encerrada por excesso de saída: "
                        f"{e.get('comando')}"
                    ),
                })
                continue
            texto = str(execucao.get("output", ""))
            exit_code = execucao.get("exit_code")
            res = evaluate(texto, default_rules(), exit_code=exit_code)
            if res["status"] != "APROVADA":
                achados.append({
                    "severidade": "informativa",
                    "descricao": (
                        f"validação sem evidência sólida: {e.get('comando')} "
                        f"(exit={exit_code}, saída {len(texto)} chars)"
                    ),
                })
        return achados

    # --------------------------------------------------------- timeout safety
    def _encerra_por_timeout(self, contrato: TaskContract) -> dict | None:
        """Safety net de timeout (Update Final, Fase 1, Exception Ch12).

        Checado ENTRE etapas do run_task: se o tempo decorrido desde o início
        ultrapassou `tempo_maximo_seg` (derivado por complexidade ou explícito),
        encerra o pipeline com `APROVADA_COM_RESSALVAS` + achado de timeout,
        PRESERVANDO o progresso já registrado (as etapas já logadas em
        `self._etapas` continuam no resultado). Retorna None se o tempo NÃO foi
        ultrapassado (pipeline segue).

        É um SAFETY NET: detecta travamento, NÃO corta execução legítima no
        meio (o pipeline roda etapas síncronas; a checagem é pontual entre
        etapas, não um watchdog que mata a thread).

        EXCEÇÃO ÚNICA À REGRA 9B (documentada): a regra "sem evidência ->
        BLOQUEADA" (`_consolida` com `validacoes_executadas` vazio) tem UMA
        exceção por design — este timeout safety net. Ele encerra com
        APROVADA_COM_RESSALVAS mesmo sem validações executadas, de PROPÓSITO:
        preserva o progresso já registrado em `self._etapas` em vez de
        descartar a execução inteira (Exception Ch12; registrado na memória).
        O achado de timeout em `achados_da_revisao` mantém a rastreabilidade.
        NÃO mudar para BLOQUEADA: o status com ressalva é intencional.
        """
        if not hasattr(self, "_tempo_inicio"):
            return None
        decorrido = time.monotonic() - self._tempo_inicio
        if decorrido <= self._tempo_limite:
            return None
        limite_s = int(round(self._tempo_limite))
        achado = {
            "severidade": "informativa",
            "descricao": (
                f"timeout safety net: execução ultrapassou o tempo máximo "
                f"({limite_s}s, complexidade {contrato.grau_complexidade}); "
                f"encerrado com ressalva preservando o progresso"
            ),
        }
        return self._finaliza_por_timeout(
            contrato,
            achado,
            resumo=(
                f"pipeline encerrado por timeout safety net "
                f"({limite_s}s, complexidade {contrato.grau_complexidade})"
            ),
            flags={"timeout_safety_net": True},
        )

    @staticmethod
    def _com_timeout(fn: Callable[[], object], timeout: float,
                     nome: str = "etapa") -> object:
        """Executa `fn()` numa thread daemon aguardando no máximo `timeout` s.

        Item 7.2 — timeout COOPERATIVO, NÃO preemptivo: no estouro, a thread
        NÃO é morta (o Python não oferece kill/preempção de thread); levanta
        `_TimeoutEtapa` para o `run_task` encerrar com ressalva preservando o
        progresso. Quando `fn` termina no prazo, retorna o valor normalmente e
        uma exceção de `fn` é propagada (comportamento inalterado).

        Use APENAS em etapas SEM efeitos colaterais (consulta de memória,
        exploração, revisão): a thread que estourar segue viva até terminar, o
        que é inofensivo para leitura, mas perigoso para execução de comandos.
        """
        caixa: dict = {}

        def _alvo() -> None:
            try:
                caixa["valor"] = fn()
            except BaseException as exc:  # noqa: BLE001 — re-levantada abaixo
                caixa["erro"] = exc

        thread = threading.Thread(
            target=_alvo, name=f"pipeline-{nome}", daemon=True
        )
        thread.start()
        thread.join(float(timeout))
        if thread.is_alive():
            raise _TimeoutEtapa(
                f"{nome} excedeu {float(timeout):.0f}s (timeout cooperativo)"
            )
        if "erro" in caixa:
            raise caixa["erro"]
        return caixa.get("valor")

    def _encerra_por_timeout_etapa(
        self, contrato: TaskContract, etapa: str, timeout: float
    ) -> dict:
        """Encerra o pipeline por timeout de ETAPA (Item 7.2, cooperativo).

        Diferente do `_encerra_por_timeout` (teto TOTAL entre etapas), aqui uma
        etapa específica excedeu `PIPELINE_ETAPA_TIMEOUT_SEG`. A thread da etapa
        NÃO é interrompida (limitação do Python: não há preempção/kill de
        thread), por isso o timeout é COOPERATIVO: o pipeline encerra com
        `APROVADA_COM_RESSALVAS` + achado de timeout de etapa, PRESERVANDO o
        progresso já registrado (`progresso_preservado=True`). Reusa o shape do
        safety net via `_finaliza_por_timeout`.

        NOTA (regra 9B): assim como o safety net total, é uma exceção
        documentada à regra "sem evidência -> BLOQUEADA" — encerra com ressalva
        de propósito para não descartar o progresso já registrado.
        """
        limite_s = f"{float(timeout):g}"
        achado = {
            "severidade": "informativa",
            "descricao": (
                f"timeout de etapa (cooperativo): {etapa} excedeu {limite_s}s; "
                f"encerrado com ressalva preservando o progresso (a thread da "
                f"etapa NÃO foi interrompida — Python não oferece preempção)"
            ),
        }
        return self._finaliza_por_timeout(
            contrato,
            achado,
            resumo=(
                f"pipeline encerrado por timeout da etapa {etapa} ({limite_s}s)"
            ),
            flags={"timeout_etapa": etapa, "progresso_preservado": True},
        )

    def _finaliza_por_timeout(
        self,
        contrato: TaskContract,
        achado: dict,
        *,
        resumo: str,
        flags: dict,
    ) -> dict:
        """Constrói o contrato de encerramento por timeout (total ou de etapa),
        compartilhado por `_encerra_por_timeout` e `_encerra_por_timeout_etapa`.

        Reconstrói as evidências REAIS já registradas (`_evidencias_das_etapas`,
        mesmos critérios de `_consolida`) e aplica o anti-envenomamento da
        gravação (`_pode_gravar`) sobre a saúde de ENTRADA. `flags` são chaves
        ADITIVAS do contrato (ex.: `timeout_safety_net`, `timeout_etapa`,
        `progresso_preservado`) — o shape base é o mesmo para os dois caminhos.
        """
        self._status = "APROVADA_COM_RESSALVAS"
        self._etapa(
            "ENCERRAMENTO",
            f"status final: APROVADA_COM_RESSALVAS ({resumo})",
            ok=True,
            timeout=True,
            **flags,
        )
        # A1 (evidências reais): o contrato de timeout NÃO pode zerar
        # `validacoes_executadas`/`aprovacoes_solicitadas` quando comandos já
        # rodaram antes da checagem — reconstrói das etapas (mesmos critérios
        # de `_consolida`). Se nada foi executado, mantém listas vazias.
        aprovacoes, executadas = self._evidencias_das_etapas()
        # Etapa 3: expõe a saúde no contrato de timeout e aplica o
        # anti-envenomamento (saúde DEGRADADO/CRITICO COM fontes degradadas não
        # grava; M2: bootstrap sem fontes NÃO bloqueia). A decisão usa a saúde
        # de ENTRADA (`_saude_para_gate`) e fica registrada como etapa ANTES do
        # snapshot de `etapas`.
        saude = self._saude_atual()
        saude_gate = self._saude_para_gate()
        gravar = bool(self.gravar_registro and executadas)
        if gravar and not self._pode_gravar(saude_gate):
            self._etapa(
                "ENCERRAMENTO",
                "gravação de registro episódico BLOQUEADA pelo detector de "
                f"saúde (nível {saude_gate.get('nivel')}, score "
                f"{saude_gate.get('score')}): anti-envenomamento",
                ok=True,
                saude=saude_gate,
                motivo="saude_baixa",
            )
            gravar = False
        resultado = {
            "status": "APROVADA_COM_RESSALVAS",
            "resumo": resumo,
            "risco": contrato.nivel_de_risco,
            "politica": self._politica(contrato.nivel_de_risco),
            "grau_complexidade": contrato.grau_complexidade,
            "tempo_maximo_seg": contrato.tempo_maximo_seg,
            "usa_sandbox": contrato.usa_sandbox,
            "contexto_grau": contrato.contexto_grau,
            "testes_ativos": contrato.testes_ativos,
            "perfil": contrato.perfil,
            "arquivos_alterados": [],
            "validacoes_executadas": executadas,
            "achados_da_revisao": [achado],
            "riscos_residuais": [achado["descricao"]],
            "aprovacoes_solicitadas": aprovacoes,
            "saude": saude,
            "escalonamento": list(getattr(contrato, "escalonamento", []) or []),
            "saude_indisponivel": bool(
                getattr(contrato, "saude_indisponivel", False)
            ),
            "etapas": list(self._etapas),
        }
        resultado.update(flags)
        self._resultado = resultado
        # F4/A1: timeout com EXECUÇÃO (comandos já aprovados/executados) grava
        # registro episódico, igual ao caminho normal (APROVADA_COM_RESSALVAS
        # é um desfecho com execução). Timeout ANTES de qualquer comando não
        # grava (evita lixo de memória em dry-run, mesma regra do BLOQUEADA).
        # Etapa 3: `gravar` já considera o anti-envenomamento (saúde baixa).
        if gravar:
            self._grava_registro(contrato, resultado)
        return resultado

    def _saude_atual(self) -> dict:
        """Calcula a saúde determinística (Etapa 3) de forma TOLERANTE.

        Qualquer falha (fonte inválida, erro inesperado) vira saúde NEUTRA
        (`health.saude_neutra`: score 0.5 / DEGRADADO, coerente com os
        limiares) — o detector nunca derruba o pipeline. Usada em `_consolida`
        (contrato de saída) e nos caminhos de gravação (anti-envenomamento)."""
        try:
            return health.gerar_saude(
                memory=self.memory, playbook=self._playbook
            )
        except Exception:  # noqa: BLE001 — detector jamais derruba o pipeline
            return health.saude_neutra("falha tolerada no detector")

    def _saude_para_gate(self) -> dict:
        """Saúde usada no anti-envenomamento (Etapa 3/M2): o snapshot de
        ENTRADA (`_saude_entrada`, capturado antes de rodar qualquer comando)
        quando disponível; senão calcula agora. Tolerante — `_saude_atual`
        nunca levanta."""
        snap = getattr(self, "_saude_entrada", None)
        if isinstance(snap, dict):
            return snap
        return self._saude_atual()

    @staticmethod
    def _nivel_ladder(saude) -> str:
        """Nível EFETIVO da ladder (Etapa 5) a partir da saúde passiva.

        Regras de segurança/retrocompatibilidade:
          - saúde ausente/indefinida -> SAUDAVEL (não escala);
          - `bootstrap=True` (harness novo OU só registros de máquina) ->
            SAUDAVEL: não há fonte real de LLM, então não há EVIDÊNCIA de
            degradação a acionar (o anti-envenomamento continua valendo na
            gravação);
          - `indisponivel=True` (falha do detector) -> ATENCAO: ausência de
            MEDIÇÃO não é medição de degradação, então NÃO escala gates (evita
            denial-of-service), mas também NÃO é "SAUDAVEL" silencioso — a
            ladder sinaliza o motivo no contrato (`saude_indisponivel`) e na
            etapa, e `_pode_gravar` segue conservador;
          - caso contrário, o `nivel` reportado pelo detector (normalizado),
            limitado ao domínio conhecido (valor estranho -> SAUDAVEL)."""
        if not isinstance(saude, dict):
            return "SAUDAVEL"
        if saude.get("indisponivel"):
            return "ATENCAO"
        if saude.get("bootstrap"):
            return "SAUDAVEL"
        nivel = str(saude.get("nivel") or "").strip().upper()
        if nivel in ("SAUDAVEL", "ATENCAO", "DEGRADADO", "CRITICO"):
            return nivel
        return "SAUDAVEL"

    def _aplica_ladder(self, contrato: TaskContract, saude) -> list[str]:
        """Ladder de resposta adaptativa (Etapa 5): converte o nível de saúde de
        ENTRADA em AÇÃO sobre o contrato, ANTES da avaliação do gate de risco.

        - SAUDAVEL: comportamento normal (nenhuma ação);
        - ATENCAO:  informativo — NÃO muda gates; registra etapa (o `saude` já é
          exposto no contrato de saída);
        - DEGRADADO/CRITICO: endurece automaticamente — força
          `nivel_de_risco="alto"` (gate_global -> exige `approve(RISCO_GATE_ALTO)`
          UMA vez) e `usa_sandbox=True`; o bloqueio de gravação de memória
          (anti-envenomamento, Etapa 3) permanece via `_pode_gravar` sobre a
          saúde de ENTRADA. CRITICO ainda exige o gate `SAUDE_GATE_CRITICO` (o
          chamador o verifica logo após esta chamada).

        Retorna a lista de ações aplicadas (transparência `escalonamento`),
        também gravada em `contrato.escalonamento`."""
        nivel = self._nivel_ladder(saude)
        self._nivel_saude = nivel
        # Achado 2: propaga a indisponibilidade do detector para o contrato e
        # para o estado da instância (transparência nos contratos BLOQUEADA/
        # sucesso/timeout). A ladder a trata como ATENCAO (sem escalonamento).
        indisponivel = bool(isinstance(saude, dict) and saude.get("indisponivel"))
        self._saude_indisponivel = indisponivel
        contrato.saude_indisponivel = indisponivel
        acoes: list[str] = []
        if nivel == "SAUDAVEL":
            # Comportamento normal: sem etapa adicional e sem escalonamento.
            contrato.escalonamento = []
            return acoes
        if nivel == "ATENCAO":
            # Informativo: NÃO muda gates; apenas registra a etapa (o `saude`
            # já é exposto no contrato de saída). Quando a origem é a FALHA do
            # detector (`indisponivel=True`), registra o motivo explícito e
            # marca a etapa — sem escalar gates (evita DoS) e sem silenciar a
            # falha como se fosse SAUDAVEL.
            contrato.escalonamento = []
            if indisponivel:
                self._etapa(
                    "RECEBIDA",
                    "ladder de saúde: ATENCAO (detector indisponível; gates "
                    "inalterados; gravação conservadora)",
                    ok=True,
                    saude=nivel,
                    escalonamento=[],
                    saude_indisponivel=True,
                )
            else:
                self._etapa(
                    "RECEBIDA",
                    "ladder de saúde: ATENCAO (informativo; gates inalterados)",
                    ok=True,
                    saude=nivel,
                    escalonamento=[],
                )
            return acoes
        contrato.nivel_de_risco = "alto"
        contrato.usa_sandbox = True
        acoes = ["risco_alto", "sandbox", "memoria_bloqueada"]
        if nivel == "CRITICO":
            acoes.append("gate_critico")
        contrato.escalonamento = list(acoes)
        self._etapa(
            "RECEBIDA",
            f"ladder de saúde: {nivel} -> escalonamento {acoes}",
            ok=True,
            saude=nivel,
            escalonamento=list(acoes),
        )
        return acoes

    @staticmethod
    def _pode_gravar(saude) -> bool:
        """Anti-envenomamento (Etapa 3 + M2): permite gravar a menos que a
        saúde esteja DEGRADADO/CRITICO COM evidência REAL de degradação.

        `bootstrap=True` NÃO bloqueia: significa que NÃO há fonte de LLM
        (`fontes_llm == 0`) — harness novo OU só registros de máquina do
        pipeline (a saúde ignora os registros que o pipeline grava sobre si
        mesmo; a carência evita o auto-bloqueio do bootstrap). Saúde
        ausente/indefinida -> permite (tolerância). `saude_neutra` (falha do
        detector) traz `bootstrap=False` -> bloqueia (conservador)."""
        if not isinstance(saude, dict):
            return True
        if saude.get("nivel") not in ("DEGRADADO", "CRITICO"):
            return True
        return bool(saude.get("bootstrap"))

    # ------------------------------------------------------------ estado 6
    def _consolida(self, contrato: TaskContract, aprovacoes: list[dict],
                   achados: list[dict]) -> dict:
        """Consolida o contrato de saída: APROVADA / APROVADA_COM_RESSALVAS /
        BLOQUEADA com base nas evidências REAIS. `validacoes_executadas` lista
        SOMENTE comandos que realmente rodaram (exit 0 ou não); comandos
        negados por HITL e execuções com exit != 0 (ou timeout/bloqueadas)
        viram riscos residuais.

        NOTA (regra 9B — "sem evidência -> BLOQUEADA"): o ÚNICO caminho que
        produz APROVADA_COM_RESSALVAS sem `validacoes_executadas` é o timeout
        safety net (`_encerra_por_timeout`) — exceção documentada por design
        (preserva progresso; Exception Ch12, ver memória). Não burla a regra:
        o achado de timeout fica em `achados_da_revisao` e o status é
        ressalva, nunca APROVADA limpa."""
        negadas = [a for a in aprovacoes if not a["aprovado"]]
        executadas = [
            a for a in aprovacoes
            if a.get("execucao") and (
                a["execucao"].get("exit_code") is not None
                or a["execucao"].get("encerrado_por_cap")
            )
        ]
        validacoes = [a["comando"] for a in executadas]
        falhas = [a for a in executadas if a["execucao"]["exit_code"] != 0]
        timeouts = [
            a for a in aprovacoes
            if a.get("execucao") and a["execucao"].get("timeout")
        ]
        bloqueados = [
            a for a in aprovacoes
            if a.get("execucao") and a["execucao"].get("bloqueado")
        ]
        caps = [
            a for a in aprovacoes
            if a.get("execucao") and a["execucao"].get("encerrado_por_cap")
        ]
        if not validacoes:
            status = "BLOQUEADA"
            resumo = "bloqueada: nenhuma evidência de validação executada"
        elif negadas or achados or falhas or timeouts or bloqueados or caps:
            status = "APROVADA_COM_RESSALVAS"
            resumo = "pipeline concluído com ressalvas (aprovações negadas, achados ou execuções com falha)"
        else:
            status = "APROVADA"
            resumo = "pipeline concluído e aprovado"
        riscos: list[str] = []
        if negadas:
            riscos.append(
                "validações sugeridas negadas por HITL: "
                + ", ".join(a["comando"] for a in negadas)
            )
        if falhas:
            riscos.append(
                "validação executada com falha (exit != 0): "
                + ", ".join(
                    f"{a['comando']} (exit={a['execucao']['exit_code']})"
                    for a in falhas
                )
            )
        if timeouts:
            riscos.append(
                "validação excedeu o tempo limite: "
                + ", ".join(a["comando"] for a in timeouts)
            )
        if bloqueados:
            riscos.append(
                "comando bloqueado pelo executor: "
                + ", ".join(a["comando"] for a in bloqueados)
            )
        if caps:
            riscos.append(
                "validação encerrada por excesso de saída: "
                + ", ".join(a["comando"] for a in caps)
            )
        if not validacoes:
            riscos.append("nenhuma evidência de validação executada")
        # Etapa 3 (Família C): verificador de contradição — compara o
        # ALEGADO/registrado com a evidência REAL das etapas (exit_code) e com
        # os comandos EXIGIDOS pelos critérios de aceite (filtro acionável A1:
        # critério cuja validação extraída não foi executada), de forma
        # determinística e sem I/O. Os achados são ANEXADOS a
        # `achados_da_revisao` (aditivo) e NÃO alteram o status já decidido
        # acima: são achados de revisão (bloqueantes/altos/informativos), não
        # uma nova transição de estado.
        contradicoes = health.verificar_contradicoes(
            validacoes_executadas=validacoes,
            status=status,
            etapas=self._etapas,
            comandos_criterios=health.extrai_comandos_criterios(
                " ".join(contrato.criterios_de_aceite)
            ),
        )
        achados_revisao = list(achados) + contradicoes
        return {
            "status": status,
            "resumo": resumo,
            "risco": contrato.nivel_de_risco,
            "politica": self._politica(contrato.nivel_de_risco),
            "grau_complexidade": contrato.grau_complexidade,
            "tempo_maximo_seg": contrato.tempo_maximo_seg,
            "usa_sandbox": contrato.usa_sandbox,
            "contexto_grau": contrato.contexto_grau,
            "testes_ativos": contrato.testes_ativos,
            "perfil": contrato.perfil,
            "arquivos_alterados": [],
            "validacoes_executadas": validacoes,
            "achados_da_revisao": achados_revisao,
            "riscos_residuais": riscos,
            "aprovacoes_solicitadas": [a["comando"] for a in aprovacoes],
            "saude": self._saude_atual(),
            "escalonamento": list(getattr(contrato, "escalonamento", []) or []),
            "saude_indisponivel": bool(
                getattr(contrato, "saude_indisponivel", False)
            ),
        }

    def _grava_registro(self, contrato: TaskContract, resultado: dict) -> None:
        """Fecha o loop de aprendizado: grava registro episódico via Memory.

        Achado ALTA (raiz secundária): o Resultado é gravado em MARKDOWN
        PARSEÁVEL (não mais JSON de uma linha). `agents.parse_episode` /
        `_extrai_validacoes_achados` só extraem `validacoes_executadas` e
        `achados_da_revisao` de bullets indentados sob esses campos — o JSON
        deixava o registro opaco para o playbook e para a saúde. O Contexto usa
        as lições do run; sem lições, grava um resumo real curto do que
        ocorreu (nunca `_não informado_` havendo execução)."""
        try:
            keywords = re.findall(r"[a-z0-9]{3,}", contrato.objetivo.lower())[:5]
            fluxo = "\n".join(
                f"- {e['etapa']}: {e['detalhe']}" for e in self._etapas
            )
            contexto = "\n".join(self._licoes()) or self._contexto_do_run(resultado)
            self.memory.record(
                keywords=keywords,
                agente="pipeline",
                tema=contrato.objetivo[:60] or "registro",
                entrada=contrato.escopo,
                fluxo=fluxo,
                resultado=self._resultado_markdown(resultado),
                contexto=contexto,
                status=resultado["status"].lower(),
            )
        except Exception:  # noqa: BLE001 — falha de gravação não derruba a execução
            pass

    @staticmethod
    def _contexto_do_run(resultado: dict) -> str:
        """Resumo real curto do run, usado como Contexto quando o playbook ainda
        não tem lições — nunca `_não informado_` havendo execução."""
        status = str(resultado.get("status") or "")
        n_val = len(resultado.get("validacoes_executadas") or [])
        n_ach = len(resultado.get("achados_da_revisao") or [])
        return (
            "Execução determinística do pipeline concluída com status "
            f"{status}; {n_val} validação(ões) executada(s) e {n_ach} "
            "achado(s) de revisão registrado(s)."
        )

    @staticmethod
    def _resultado_markdown(resultado: dict) -> str:
        """Serializa o contrato de saída como markdown PARSEÁVEL por
        `parse_episode` (`validacoes_executadas`/`achados_da_revisao` em
        bullets indentados). `achados_da_revisao` fica POR ÚLTIMO para o leitor
        de observability não contar bullets de riscos como achados."""
        linhas: list[str] = [f"- status: {resultado.get('status', '')}"]
        resumo = str(resultado.get("resumo") or "").strip().replace("\n", " ")
        if resumo:
            linhas.append(f"- resumo: {resumo}")
        linhas.append("- validacoes_executadas:")
        for cmd in resultado.get("validacoes_executadas") or []:
            linhas.append(f"  - {str(cmd).replace(chr(10), ' ')}")
        linhas.append("- riscos_residuais:")
        for risco in resultado.get("riscos_residuais") or []:
            linhas.append(f"  - {str(risco).replace(chr(10), ' ')}")
        linhas.append("- achados_da_revisao:")
        for achado in resultado.get("achados_da_revisao") or []:
            if isinstance(achado, dict):
                desc = str(achado.get("descricao") or "").replace("\n", " ")
                sev = str(achado.get("severidade") or "").strip()
                texto = f"[{sev}] {desc}" if sev else desc
            else:
                texto = str(achado).replace("\n", " ")
            linhas.append(f"  - {texto}")
        return "\n".join(linhas)

    # -------------------------------------------------------------- helpers
    @staticmethod
    def _politica(nivel_de_risco: str) -> str:
        """Resolve a política HITL para o nível de risco (fonte: config).
        `nivel_de_risco` já veio validado por TaskContract.validate()."""
        return config.RISCO_POLITICA.get(nivel_de_risco, "hitl_por_comando")

    @staticmethod
    def _exige_aprovacao(cmd: str) -> bool:
        """True se o comando exige aprovação humana explícita (APPROVAL_PATTERNS),
        independentemente do nível de risco. Usado para NÃO aprovar
        automaticamente no risco baixo comandos sensíveis (ex.: instalações)."""
        lower = cmd.lower()
        return any(p in lower for p in config.APPROVAL_PATTERNS)

    @staticmethod
    def _no_safelist_risco_baixo(cmd: str) -> bool:
        """True se o comando pode ser AUTO-APROVADO no risco baixo com
        segurança (safelist conservador `RISCO_BAIXO_AUTO_SAFELIST`).

        Segurança (Finding 1 da re-revisão do Item 2): comandos com
        encadeamento ou redirecionamento NUNCA são auto-aprovados, mesmo que
        comecem com um prefixo do safelist. Em cmd.exe/PowerShell
        (shell=True), `&`, `&&`, `|`, `||`, `;` encadeiam comandos e `<`/`>`
        redirecionam; a auto-aprovação por PREFIXO de substring
        (`lower.startswith(prefix)`) deixava `git status; curl url | sh` e
        `git status & del .env` passarem (True). O match agora é por TOKEN
        (shlex), nunca por substring frágil.

        Requisitos para auto-aprovar (TODOS devem valer):
          1. A string crua não contém separador de shell (`&`, `|`, `;`, `<`,
             `>`) nem quebra de linha (`\\n`/`\\r`) — defesa em profundidade
             antes da tokenização (shlex trataria `\\n` como espaço e o
             separador sumiria dos tokens).
          2. `shlex.split(cmd)` não levanta erro (ex.: aspas desbalanceadas).
          3. Nenhum token contém separador de shell (cobre `git status;` com o
             `;` colado ao token, `|` isolado e separador dentro de aspas —
             conservador).
          4. O comando corresponde EXATAMENTE a um prefixo do safelist por
             TOKENS: o primeiro token casa prefixos de 1 token (ex.: `pytest`;
             `git status` -> tokens `git` + `status`) e os 2 primeiros tokens
             casam `python -m <mod>` (ex.: `python -m py_compile`). Quando o
             último token do prefixo termina em `/` (ex.: `python tests/`), o
             token correspondente pode ser um SUBcaminho (`python tests/x.py`),
             mas nunca com componente "..". Argumentos adicionais são aceitos
             somente se forem tokens normais SEM separador (ex.: `python -m
             py_compile x.py` e `python tests/x.py` continuam auto-aprovados —
             comportamento de validação desejado); `git statusx` NÃO casa com
             `git status`.

        Comandos fora do safelist ou com qualquer separador NÃO são
        auto-aprovados no risco baixo: caem no HITL por comando (callback
        `approve`).
        """
        cru = cmd.lstrip()
        if not cru:
            return False
        if any(ch in cru for ch in _SEPARADORES_SHELL):
            return False
        if "\n" in cru or "\r" in cru:
            return False
        try:
            tokens = shlex.split(cru)
        except ValueError:
            return False
        if not tokens:
            return False
        if any(ch in token for token in tokens for ch in _SEPARADORES_SHELL):
            return False
        for prefixo in config.RISCO_BAIXO_AUTO_SAFELIST:
            try:
                ptokens = shlex.split(prefixo)
            except ValueError:
                continue
            if len(tokens) < len(ptokens):
                continue
            # todos os tokens do prefixo, exceto o último, casam EXATAMENTE
            if tokens[: len(ptokens) - 1] != ptokens[: len(ptokens) - 1]:
                continue
            ultimo = ptokens[-1]
            alvo = tokens[len(ptokens) - 1]
            # A1 (path traversal): NENHUM token adicional pode conter
            # componente ".." — antes a checagem só olhava o token do último
            # prefixo, e `python tests/x.py ../../secret.py` passava com o
            # argumento extra apontando para fora do projeto.
            if any(".." in part for tok in tokens for part in pathlib.PurePath(tok).parts):
                return False
            if ultimo.endswith("/"):
                # Prefixo de caminho (ex.: `python tests/`): aceita o
                # subcaminho correspondente (`python tests/x.py`), mas NUNCA
                # com componente ".." (evita `python tests/../evil`).
                if alvo.startswith(ultimo) \
                        and ".." not in pathlib.PurePath(alvo).parts:
                    return True
            elif alvo == ultimo:
                # Match exato por token (ex.: `git status`, `python -m
                # py_compile`, `pytest`): `git statusx` NÃO casa com `status`.
                return True
        return False

    def _bloqueia(self, stage: str, motivo: str, **extra) -> dict:
        self._status = "BLOQUEADA"
        self._etapa(stage, motivo, ok=False, bloqueio=True)
        # A1 (evidências reais): quando o bloqueio acontece NO MEIO da
        # implementação (ex.: 3º comando destrutivo após 1º e 2º já rodarem),
        # o contrato BLOQUEADA NÃO pode zerar as evidências — reconstrói das
        # etapas (mesmos critérios de `_consolida`). Bloqueios ANTES de
        # qualquer comando (contrato inválido, gate alto negado, exploração
        # com risco) continuam com listas vazias (correto).
        aprovacoes, executadas = self._evidencias_das_etapas()
        # Etapa 5: a saúde de ENTRADA e o escalonamento aplicado pela ladder são
        # explícitos (quando conhecidos) para o contrato BLOQUEADA ser coerente
        # com a decisão que o gerou (ex.: gate CRITICO). Sem eles, usa o
        # snapshot de entrada ou recalcula.
        saude_extra = extra.pop("saude", None)
        escalonamento_extra = extra.pop("escalonamento", None)
        resultado = {
            "status": "BLOQUEADA",
            "resumo": f"bloqueada em {stage}: {motivo}",
            "arquivos_alterados": [],
            "validacoes_executadas": executadas,
            "achados_da_revisao": [{"severidade": "bloqueante", "descricao": motivo}],
            "riscos_residuais": [motivo],
            "aprovacoes_solicitadas": aprovacoes,
            "etapas": list(self._etapas),
            "saude_indisponivel": bool(
                getattr(self, "_saude_indisponivel", False)
            ),
        }
        # Ressalva 1: quando o nível de risco/política são conhecidos (ex.: gate
        # alto ausente/negado), expõe `risco`/`politica` no contrato BLOQUEADA,
        # como já faz o caminho de sucesso `_consolida()`. Sem eles, um
        # consumidor de /api/pipeline não leria o nível de risco de uma tarefa
        # bloqueada por gate. Campos do contrato (grau_complexidade,
        # tempo_maximo_seg, usa_sandbox, contexto_grau) também são expostos
        # quando conhecidos, para o BLOQUEADA ter o mesmo shape do sucesso.
        for campo in (
            "risco", "politica", "grau_complexidade",
            "tempo_maximo_seg", "usa_sandbox", "contexto_grau",
            "testes_ativos", "perfil",
        ):
            if campo in extra and extra[campo] is not None:
                resultado[campo] = extra[campo]
        # Etapa 5: transparência do escalonamento (lista de ações). Usa o valor
        # explícito, senão o da instância (ladder já aplicada), senão vazio.
        if isinstance(escalonamento_extra, list):
            resultado["escalonamento"] = list(escalonamento_extra)
        else:
            resultado["escalonamento"] = list(
                getattr(self, "_escalonamento", []) or []
            )
        # B2: expõe a saúde no contrato BLOQUEADA (mesmo shape do sucesso/
        # timeout), quando calculável. A saúde de ENTRADA (se disponível)
        # preserva a coerência com a decisão da ladder; senão `_saude_atual` é
        # tolerante (nunca levanta) e a checagem defensiva extra mantém o
        # bloqueio à prova de falha do detector.
        if isinstance(saude_extra, dict):
            resultado["saude"] = saude_extra
        else:
            snap = getattr(self, "_saude_entrada", None)
            if isinstance(snap, dict):
                resultado["saude"] = snap
            else:
                try:
                    resultado["saude"] = self._saude_atual()
                except Exception:  # noqa: BLE001 — BLOQUEADA nunca quebra
                    pass
        self._resultado = resultado
        return resultado

    def _evidencias_das_etapas(self) -> tuple[list[str], list[str]]:
        """Reconstrói `aprovacoes_solicitadas` e `validacoes_executadas` a
        partir das etapas já registradas (mesmos critérios de `_consolida`):
        solicitação = etapa `tipo == "comando"`; execução = comando APROVADO
        com `exit_code` real no job. Usado pelos caminhos que encerram ANTES de
        `_consolida` (timeout safety net e bloqueio no meio da implementação)
        para não mentir sobre as evidências reais já coletadas."""
        aprovacoes: list[str] = []
        executadas: list[str] = []
        for e in self._etapas:
            if e.get("tipo") != "comando":
                continue
            comando = e.get("comando")
            if comando and comando not in aprovacoes:
                aprovacoes.append(comando)
            execucao = e.get("execucao") or {}
            if e.get("aprovado") and execucao.get("exit_code") is not None:
                executadas.append(comando)
        return aprovacoes, executadas

    def _etapa(self, nome: str, detalhe: str, ok: bool = True, **extra) -> None:
        item: dict = {"etapa": nome, "detalhe": detalhe, "ok": ok}
        item.update(extra)
        self._etapas.append(item)

    def _monta_contrato(self, task: dict) -> TaskContract:
        restricoes = task.get("restricoes") or []
        criterios = task.get("criterios_de_aceite") or []
        if isinstance(restricoes, str):
            restricoes = [restricoes]
        if isinstance(criterios, str):
            criterios = [criterios]
        criterios = [str(c) for c in criterios]
        restricoes = [str(r) for r in restricoes]

        nivel_de_risco = str(task.get("nivel_de_risco") or "medio")

        # Update Final (Fase 1) + re-revisão (Achados 1 e 2): grau de
        # complexidade. SEMÂNTICA de override vs. input de risco:
        #
        #   - `grau_complexidade` EXPLÍCITO no task -> OVERRIDE: PULA o scorer
        #     e usa o valor fornecido (o cliente diz o resultado final desejado).
        #   - `nivel_de_risco` EXPLÍCITO e DIFERENTE do default ("baixo"/"alto")
        #     -> é um INPUT para o scorer: CHAMA `calcular_grau_complexidade`
        #     com `nivel_risco=<risco_explícito>` para APLICAR o bônus
        #     (baixo+1/medio+2/alto+3) ao score lexical. NÃO pula o scorer — o
        #     nível de risco é um sinal que alimenta a derivação, não o
        #     resultado.
        #   - `nivel_de_risco` None/vazio/igual ao default ("medio") -> NÃO é um
        #     sinal explícito: deriva do texto SEM bônus (o default "medio" não
        #     soma bônus ao score automático).
        grau_explicito = task.get("grau_complexidade")
        grau = str(grau_explicito).strip().lower() if grau_explicito else ""
        risco_explicito = str(task.get("nivel_de_risco") or "").strip().lower()
        texto = " ".join(
            [str(task.get("objetivo") or ""), str(task.get("escopo") or "")]
            + restricoes + criterios
        )
        if grau:
            # OVERRIDE de grau_complexidade: pula o scorer, usa o valor direto.
            grau_complexidade = grau
        elif risco_explicito and risco_explicito != config.NIVEL_RISCO_DEFAULT:
            # nivel_de_risco EXPLÍCITO e diferente do default: é um INPUT para o
            # scorer (aplica o bônus de risco ao score lexical), NÃO um override
            # do resultado. O bônus (baixo+1/medio+2/alto+3) soma ao score; um
            # nível desconhecido é tratado pelo scorer como sem bônus.
            grau_complexidade = calcular_grau_complexidade(
                texto, nivel_risco=risco_explicito
            )["grau_complexidade"]
        else:
            # Sem sinal explícito de risco (None/vazio/igual ao default "medio"):
            # deriva do texto SEM bônus de risco.
            grau_complexidade = calcular_grau_complexidade(texto)["grau_complexidade"]

        # Timeout por complexidade (safety net). Override: task forneceu
        # tempo_maximo_seg -> respeita (configurável); senão deriva do grau.
        tempo_explicito = task.get("tempo_maximo_seg")
        if tempo_explicito is not None:
            try:
                tempo_maximo_seg = max(1, int(tempo_explicito))
            except (TypeError, ValueError):
                tempo_maximo_seg = config.TEMPO_MAXIMO_DEFAULT
        else:
            tempo_maximo_seg = config.TEMPO_MAXIMO_POR_COMPLEXIDADE.get(
                grau_complexidade, config.TEMPO_MAXIMO_DEFAULT
            )

        # usa_sandbox: True apenas para complexidade ALTA (o executor decide se
        # há Docker disponível; senão fallback host).
        usa_sandbox = bool(grau_complexidade == "alto")

        # Lote 1 (otimização de contexto): nível de contexto da delegação
        # derivado do grau de complexidade (conservador — não corta conteúdo
        # de arquivos; só cria o mecanismo de escala).
        contexto_grau = config.CONTEXTO_POR_COMPLEXIDADE.get(
            grau_complexidade, "padrao"
        )

        # Fase 3 (testes ativos de segurança web): deriva do campo EXPLÍCITO
        # (`testes_ativos: true` no task) OU por keyword no texto
        # (objetivo+escopo+restrições+critérios, normalizado sem acentos e em
        # lowercase). Sinais FORTES ("pentest ativo", "auditoria de seguranca",
        # "teste de seguranca") disparam sozinhos; sinais ATIVOS ("fase 3",
        # "testes ativos", "payload", "probe") só disparam em CONTEXTO de
        # segurança ("auditoria", "seguranca", "security", "vulnerabilidade",
        # "pentest") — sem contexto de segurança, o sinal ativo sozinho NÃO
        # dispara (elimina falso-positivo de tarefas benignas, ex.: "implementar
        # a fase 3 do projeto de migracao", "validar o payload JSON da API").
        # O gate de aprovação humana só dispara quando True.
        #
        # PERFIL de auditoria (aditivo): `osint|superficial|completo` (default
        # vazio). O perfil `completo` EXIGE testes ativos autorizados, então é
        # mapeado para `testes_ativos=True` — o gate `PHASE3_GATE` existente já
        # cobre a aprovação humana do ativo (sem novo gate). Perfis
        # osint/superficial e perfil ausente não alteram a lógica de keywords.
        perfil = str(task.get("perfil") or "").strip().lower()
        flag = task.get("testes_ativos") is True
        texto_normalizado = texto.lower().translate(_ACENTOS)
        sinal_forte = any(
            k in texto_normalizado
            for k in ("pentest ativo", "auditoria de seguranca",
                      "teste de seguranca")
        )
        sinal_ativo = any(
            k in texto_normalizado
            for k in ("fase 3", "testes ativos", "payload", "probe")
        )
        contexto_seg = any(
            k in texto_normalizado
            for k in ("auditoria", "seguranca", "security",
                      "vulnerabilidade", "pentest")
        )
        testes_ativos = bool(
            flag or sinal_forte or (sinal_ativo and contexto_seg)
            or perfil == "completo"
        )

        return TaskContract(
            objetivo=str(task.get("objetivo") or ""),
            escopo=str(task.get("escopo") or ""),
            restricoes=restricoes,
            criterios_de_aceite=criterios,
            nivel_de_risco=nivel_de_risco,
            grau_complexidade=grau_complexidade,
            tempo_maximo_seg=tempo_maximo_seg,
            usa_sandbox=usa_sandbox,
            contexto_grau=contexto_grau,
            testes_ativos=testes_ativos,
            perfil=perfil,
        )

    def _explora(
        self, escopo: str, max_arquivos: int | None = None
    ) -> tuple[list[str], list[str], bool]:
        """Coleta arquivos existentes do escopo (glob/pathlib) e aponta riscos
        básicos (nomes sensíveis). Não executa comandos.

        Item 7.2 (cap anti-glob-patológico): enumera no máximo `max_arquivos`
        (default `config.PIPELINE_EXPLORACAO_MAX_ARQUIVOS`) arquivos. O
        truncamento é sinalizado (`truncado=True`) SOMENTE quando o total
        EXCEDE o cap (havia mais arquivos do que o limite); com o total
        EXATAMENTE igual ao cap NÃO há truncamento (falso-positivo corrigido:
        a checagem usa `>` e não `>=`). `max_arquivos` <= 0 desativa o cap.

        Segurança (path traversal): tokens que escapam de config.ROOT — caminho
        absoluto (ex.: "c:/Windows", "d:/..."), com drive (ex.: "d:x") ou com
        componente ".." (ex.: "../*.md") — viram RISCO ANTES de qualquer
        glob/resolve (N1/N2): nunca são enumerados e nunca chegam ao glob, que
        em Python 3.12+ levantaria `NotImplementedError` em padrões absolutos
        (crashearia o run_task em vez de bloquear). Como defesa em profundidade,
        qualquer exceção do glob (OSError, ValueError, NotImplementedError)
        também vira RISCO -> BLOQUEADA, nunca crash. Mesma disciplina de
        `executor._resolve_cwd`: o caminho resolvido precisa ser `== ROOT` ou
        ter `ROOT` entre os pais (`ROOT in resolved.parents`).
        """
        if max_arquivos is None:
            max_arquivos = config.PIPELINE_EXPLORACAO_MAX_ARQUIVOS
        limite = int(max_arquivos) if max_arquivos and max_arquivos > 0 else 0
        arquivos: list[str] = []
        riscos: list[str] = []
        truncado = False
        for token in re.split(r"[,\s]+", escopo):
            if not token:
                continue
            if self._escopo_escapado(token):
                riscos.append(f"escopo fora do projeto: {token!r}")
                continue
            matches: list[str] = []
            try:
                if any(ch in token for ch in "*?["):
                    for x in config.ROOT.glob(token):
                        if not x.is_file():
                            continue
                        if self._dentro_do_projeto(x):
                            matches.append(str(x))
                        else:
                            riscos.append(
                                f"glob resolve fora do projeto: {token!r} -> {x}"
                            )
                        if limite and len(arquivos) + len(matches) > limite:
                            truncado = True
                            break
                else:
                    caminho = (config.ROOT / token).resolve()
                    if not self._dentro_do_projeto(caminho):
                        riscos.append(f"escopo fora do projeto: {token!r} -> {caminho}")
                        continue
                    if caminho.is_dir():
                        for x in caminho.rglob("*"):
                            if not (x.is_file() and self._dentro_do_projeto(x)):
                                continue
                            matches.append(str(x))
                            if limite and len(arquivos) + len(matches) > limite:
                                truncado = True
                                break
                    elif caminho.exists():
                        matches = [str(caminho)]
            except (OSError, ValueError, NotImplementedError) as exc:
                # N1 (defesa em profundidade): exceção de glob/exploração vira
                # RISCO -> BLOQUEADA, nunca crash.
                riscos.append(
                    f"escopo inválido ou fora do projeto: {token!r} "
                    f"({type(exc).__name__})"
                )
                matches = []
            for m in matches:
                if limite and len(arquivos) >= limite:
                    truncado = True
                    break
                arquivos.append(m)
                if _nome_sensivel(m):
                    riscos.append(f"arquivo sensível no escopo: {m}")
            if truncado:
                break
        return sorted(set(arquivos)), riscos, truncado

    @staticmethod
    def _escopo_escapado(token: str) -> bool:
        """True se o token escapa de config.ROOT e NUNCA deve chegar ao
        glob/resolve: caminho absoluto (`PurePath.is_absolute()`), com drive
        (ex.: "d:x" — drive-relative, `PurePath.drive` não vazio) ou com
        componente "..". A checagem antes do glob garante que padrões absolutos
        não disparam `NotImplementedError` (Python 3.12+) e que "../*.md" não
        passa silenciosamente retornando 0 itens (N1/N2)."""
        try:
            p = pathlib.PurePath(token)
        except (OSError, ValueError):
            # Indecifrável como caminho: o except de defesa do glob/resolve
            # cuida do token (vira RISCO se quebrar).
            return False
        return p.is_absolute() or bool(p.drive) or ".." in p.parts

    @staticmethod
    def _dentro_do_projeto(caminho: pathlib.Path) -> bool:
        """True se o caminho resolvido está DENTRO de config.ROOT (mesma
        disciplina de `executor._resolve_cwd`: resolved == ROOT ou ROOT entre
        os pais do resolved)."""
        root = config.ROOT.resolve()
        resolved = caminho.resolve()
        return resolved == root or root in resolved.parents

    def _validacoes_comuns(self) -> list[str]:
        if self._playbook is None:
            return []
        aprendido = self._playbook.get("learned", {})
        if isinstance(aprendido, dict):
            return aprendido.get("validacoes_comuns", []) or []
        return []

    def _licoes(self) -> list[str]:
        """Lições CONFIÁVEIS (trust `alta`/`media`) do playbook, sem meta-ruído.

        Achado M4: antes expunha `learned.licoes` plano (a maioria `fraca`),
        contrariando a regra "lição `fraca` nunca é exposta". Agora reusa a
        seleção do feed (`licoes_confiaveis_textos`): só alta/media, sem META,
        encurtadas e deduplicadas. Sem playbook/sem lições confiáveis -> []."""
        if self._playbook is None:
            return []
        return licoes_confiaveis_textos(self._playbook)

    @staticmethod
    def _normaliza_playbook(playbook) -> dict | None:
        if playbook is None:
            return None
        if isinstance(playbook, Playbook):
            return playbook.to_dict()
        if isinstance(playbook, dict):
            return playbook
        return None


def _nome_sensivel(path: str) -> bool:
    """Heurística: nome do arquivo contém marcador de segredo/sensível."""
    nome = pathlib.Path(path).name.lower()
    return any(m in nome for m in _SENSITIVE_NAMES)


def main(argv: list[str] | None = None) -> int:
    """CLI mínima para testar o pipeline com uma tarefa em JSON."""
    # Console Windows (cp1252) não imprime todos os caracteres UTF-8 do JSON
    # (ex.: "í"); usa UTF-8 com substituição para nunca quebrar a saída.
    try:
        sys.stdout.reconfigure(encoding="utf-8", errors="replace")
    except (AttributeError, ValueError):
        pass
    args = list(argv) if argv is not None else sys.argv[1:]
    if not args:
        print("uso: python -m harness.pipeline '<json da tarefa>'")
        return 1
    try:
        task = json.loads(args[0])
    except json.JSONDecodeError as exc:
        print(f"erro: JSON inválido: {exc}")
        return 1
    resultado = AgentPipeline().run_task(task)
    print(json.dumps(resultado, ensure_ascii=False, indent=2))
    return 0 if resultado["status"] != "BLOQUEADA" else 1


if __name__ == "__main__":
    sys.exit(main())