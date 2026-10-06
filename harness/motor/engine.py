"""AgenticHarnessEngine — motor do closed-loop semantic-cache-first (Etapa 2).

Fluxo de execute_task:
  1. embedding = embedder.generate_embedding(task);
  2. hit = store.find_nearest(embedding, threshold) — se achou: aplica o
     gate de container (Item 7.1): com MOTOR_REQUER_CONTAINER=True e sandbox
     fora de docker/podman, NÃO executa no host nem muta o store —
     ExecutionResult(success=False, "isolamento de container exigido;
     cache-hit não servido") —; caso contrário, interpola o
     payload (ast_abstraction.interpolate_template) -> sandbox.run -> exit 0
     -> increment_hit_count -> fast-path local; exit != 0 -> store.invalidate
     (zera hit_count + marca needs_revalidation=1) -> ExecutionResult
     "artefato local falhou; revalidacao pendente" (nada persiste);
  3. miss: sem llm_client -> error "cache-miss sem LLM"; com llm_client
     (LLMClient real da Etapa 2) -> generate(task, domain, schema) -> se
     LLMError, ExecutionResult(success=False, error) SEM persistir nada ->
     código gerado -> sandbox.run -> exit != 0 -> RuntimeError (nada
     persiste — zero-poisoning) -> exit 0 -> abstract_to_template ->
     store.save + cache.put + cache.ensure_disk_limit() (teto MAX_DISK_GB=2).

Mede execution_time_ms com time.perf_counter e mantém estatísticas
(hits/misses/avg_time_ms/hit_rate via stats()). Nunca crasha: exceções
viram ExecutionResult(success=False, error=str(exc)).

CLI: ``python -m harness.motor.engine "tarefa" --domain code_gen [--dry-run]``
(imprime JSON do resultado) — o pacote é importável via ``python -m``.
"""

from __future__ import annotations

import argparse
import asyncio
import json
import sys
import time
import uuid
from dataclasses import asdict

try:  # importação como pacote (preferida)
    from . import config
    from .ast_abstraction import abstract_to_template, interpolate_template
    from .cache import ARCCache
    from .embedder import LocalEmbedder
    from .llm_client import LLMError
    from .models import ArtifactRecord, ExecutionResult
    from .sandbox import SandboxRunner
    from .vector_store import VectorStore
except ImportError:  # execução direta: python engine.py
    import config
    from ast_abstraction import abstract_to_template, interpolate_template
    from cache import ARCCache
    from embedder import LocalEmbedder
    from llm_client import LLMError
    from models import ArtifactRecord, ExecutionResult
    from sandbox import SandboxRunner
    from vector_store import VectorStore


class AgenticHarnessEngine:
    """Motor determinístico do cache semântico (classe principal, assíncrona).

    ``llm_client`` (LLMClient da Etapa 2) deve expor
    ``generate(task: str, domain: str, schema: dict) -> str`` retornando
    código Python, e levantar LLMError em qualquer falha. ``cache``
    (ARCCache opcional) recebe o artefato após cada save e garante o teto
    de disco (MAX_DISK_GB=2) via ensure_disk_limit().
    """

    # Schema JSON estrito para o contrato LLM (validação real na Etapa 2)
    SCHEMA_JSON = {
        "type": "object",
        "required": ["code"],
        "properties": {"code": {"type": "string"}},
    }

    def __init__(
        self,
        embedder,
        store,
        sandbox,
        llm_client=None,
        threshold: float = 0.92,
        cache: ARCCache | None = None,
    ):
        self.embedder = embedder
        self.store = store
        self.sandbox = sandbox
        self.llm_client = llm_client
        self.threshold = float(threshold)
        self.schema = self.SCHEMA_JSON
        self.cache = cache
        # estatísticas (Etapa 2)
        self._hits = 0
        self._misses = 0
        self._tempos: list[float] = []

    @property
    def safe_mode(self) -> bool:
        """True se embedding ou sandbox operam em modo reduzido."""
        return self.embedder.mode == "safe" or self.sandbox.mode == "exec"

    def _threshold_efetivo(self) -> float:
        """Limiar efetivo do cache-first, ajustado pela FIDELIDADE do embedder.

        Com embedder de baixa fidelidade (mode "safe" = bag-of-words com hash,
        ou "fallback" = onnx falhou e caiu no hash), o cosseno é pouco
        discriminativo: tarefas DISTINTAS que apenas compartilham tokens
        alcançam cosseno alto facilmente -> o motor reexecutaria o artefato da
        tarefa errada e retornaria success=True (false-hit). Conservador: nesses
        modos o limiar SOBE (+0.06, teto 0.98). Em modo "onnx" (vetores
        semânticos reais), usa o limiar configurado. Ainda assim, o cache-first
        é best-effort: o artefato é revalidado no sandbox (exit 0), mas a
        adequação SEMÂNTICA à tarefa atual é responsabilidade do chamador."""
        if self.embedder.mode in ("safe", "fallback"):
            return min(0.98, self.threshold + 0.06)
        return self.threshold

    # ------------------------------------------------------------- motor
    async def execute_task(
        self,
        task: str,
        domain: str,
        vars: dict | None = None,
        test_cases: list | None = None,
    ) -> ExecutionResult:
        """Executa uma tarefa pelo closed-loop semantic-cache-first.

        Mede execution_time_ms (perf_counter) e atualiza as estatísticas
        (hits/misses/avg_time_ms/hit_rate — ver stats()).

        ``test_cases`` (interpretação da Etapa 1): lista de códigos (str ou
        dict com "code") que devem passar no sandbox (exit 0) ANTES de o
        artefato ser persistido — validação estrita anti-poisoning.
        """
        inicio = time.perf_counter()
        resultado = await self._executar_inner(task, domain, vars, test_cases)
        resultado.execution_time_ms = self._ms(inicio)
        # Transparência (Item 7.1): expõe o modo de validação REAL do sandbox
        # (docker|podman|exec) em TODO resultado. O ISOLAMENTO depende deste
        # modo: só docker/podman são container; "exec" é subprocess no HOST.
        # `sandbox_modo` é campo REAL da dataclass ExecutionResult (aditivo,
        # default ""), logo `dataclasses.asdict()` o inclui.
        try:
            resultado.sandbox_modo = self.sandbox.mode
        except Exception:  # noqa: BLE001 — sandbox sem `.mode` não derruba
            pass
        self._registrar_estatistica(resultado)
        return resultado

    async def _executar_inner(
        self,
        task: str,
        domain: str,
        vars: dict | None,
        test_cases: list | None,
    ) -> ExecutionResult:
        """Núcleo do motor (sem medição/estatística — privado)."""
        executou_local = False
        try:
            if domain not in config.DOMAINS:
                return ExecutionResult(
                    success=False,
                    executed_locally=False,
                    error=f"domínio desconhecido: {domain}",
                )

            # 1. embedding local
            vetor = self.embedder.generate_embedding(task)

            # 2. busca vetorial: fast-path local (limiar ajustado por
            #    fidelidade do embedder — ver `_threshold_efetivo`)
            hit = self.store.find_nearest(vetor, self._threshold_efetivo())
            if hit is not None:
                executou_local = True
                # Item 7.1 (endurecimento opt-in) — GATE DE CONTAINER TAMBÉM NO
                # CACHE-HIT (achado MÉDIA R1): o fast-path NÃO pode servir um
                # artefato executando-o no HOST quando
                # MOTOR_REQUER_CONTAINER=True e o sandbox está fora de
                # container ("exec"). O gate é aplicado ANTES de interpolar/
                # executar e SEM qualquer mutação de store: não incrementa
                # hit_count, não invalida o registro (só um replay que rodou de
                # fato conta como hit). Apenas docker/podman servem o cache-hit.
                if config.MOTOR_REQUER_CONTAINER and self.sandbox.mode not in (
                    "docker", "podman",
                ):
                    return ExecutionResult(
                        success=False,
                        output="",
                        executed_locally=True,
                        artifact_id=hit.id,
                        error=(
                            "isolamento de container exigido; cache-hit não "
                            "servido (MOTOR_REQUER_CONTAINER=True, sandbox no "
                            f"modo {self.sandbox.mode!r}; só docker/podman "
                            "servem o cache-hit) — nada persistido/incrementado"
                        ),
                        exit_code=None,
                    )
                try:
                    codigo = interpolate_template(hit.execution_payload, vars or {})
                except Exception as exc:  # noqa: BLE001
                    # artefato não-interpolável (placeholder sem valor): não é
                    # reusável -> invalida (mesma disciplina do replay falho).
                    self.store.invalidate(hit.id)
                    return ExecutionResult(
                        success=False,
                        output="",
                        executed_locally=True,
                        artifact_id=hit.id,
                        error=f"artefato local não-interpolável; revalidacao "
                              f"pendente ({exc})",
                        exit_code=None,
                    )
                r = self.sandbox.run(codigo)
                if r.exit_code == 0:
                    self.store.increment_hit_count(hit.id)
                    return ExecutionResult(
                        success=True,
                        output=r.output,
                        executed_locally=True,
                        artifact_id=hit.id,
                        exit_code=0,
                    )
                # replay falhou (achado A1): zera hit_count e marca o artefato
                # como pendente de revalidação ANTES de retornar — nada é
                # persistido (zero-poisoning mantido) e o fast-path passa a
                # ignorar o registro (find_nearest filtra needs_revalidation=1).
                self.store.invalidate(hit.id)
                return ExecutionResult(
                    success=False,
                    output=r.output,
                    executed_locally=True,
                    artifact_id=hit.id,
                    error="artefato local falhou; revalidacao pendente",
                    exit_code=r.exit_code,
                )

            # 3. miss: fallback LLM real (Etapa 2)
            if self.llm_client is None:
                return ExecutionResult(
                    success=False,
                    executed_locally=False,
                    error="cache-miss sem LLM",
                )

            try:
                codigo = self.llm_client.generate(task, domain, self.schema)
            except LLMError as exc:
                # erro do provedor: não crasha o motor e nada persiste
                return ExecutionResult(
                    success=False,
                    executed_locally=False,
                    error=str(exc),
                )

            # validação estrita: test_cases devem passar no sandbox
            if test_cases:
                for caso in test_cases:
                    tc = caso.get("code") if isinstance(caso, dict) else str(caso)
                    rt = self.sandbox.run(tc)
                    if rt.exit_code != 0:
                        return ExecutionResult(
                            success=False,
                            output=rt.output,
                            executed_locally=False,
                            error=(
                                f"test case falhou (exit {rt.exit_code}): "
                                f"{rt.error or ''} — nada persistido"
                            ),
                            exit_code=rt.exit_code,
                        )

            r = self.sandbox.run(codigo)
            if r.exit_code != 0:
                # zero-poisoning: nada com exit != 0 é persistido
                raise RuntimeError(
                    f"código LLM falhou (exit {r.exit_code}): {r.error or ''} "
                    f"— nada persistido"
                )

            # Item 7.1 (endurecimento opt-in): com MOTOR_REQUER_CONTAINER=True,
            # NÃO persistir artefato validado fora de container. O modo "exec"
            # é o safe mode (subprocess no HOST), que NÃO é isolamento; apenas
            # docker/podman contam como container. Nada é salvo e o resultado
            # deixa explícito o motivo e o modo real.
            if config.MOTOR_REQUER_CONTAINER and self.sandbox.mode not in (
                "docker", "podman",
            ):
                return ExecutionResult(
                    success=False,
                    output=r.output,
                    executed_locally=False,
                    error=(
                        "isolamento de container exigido; nada persistido "
                        f"(MOTOR_REQUER_CONTAINER=True, sandbox no modo "
                        f"{self.sandbox.mode!r}; só docker/podman persistem)"
                    ),
                    exit_code=r.exit_code,
                )

            # 4. abstração AST + persistência + ARC (teto de disco)
            template = abstract_to_template(codigo)
            record = ArtifactRecord(
                id=uuid.uuid4().hex,
                intent_vector=vetor,
                domain=domain,
                execution_payload=template,
                validation_schema=self.schema,
            )
            artifact_id = self.store.save(record)
            if self.cache is not None:
                self.cache.put(artifact_id)
                self.cache.ensure_disk_limit()
            return ExecutionResult(
                success=True,
                output=r.output,
                executed_locally=False,
                artifact_id=artifact_id,
                exit_code=0,
            )
        except Exception as exc:  # noqa: BLE001 — nunca crashar
            return ExecutionResult(
                success=False,
                executed_locally=executou_local,
                error=str(exc),
            )

    def revalidar_pendentes(self, domain: str | None = None) -> dict:
        """Fecha o dead-end A1 da revalidação: reexecuta no sandbox cada
        artefato marcado ``needs_revalidation=1``.

          - exit 0 (falha transitória/environment): limpa a marca
            (``clear_revalidation``) — o artefato volta ao fast-path;
          - exit != 0 (genuinamente quebrado): apaga (``delete``) — não
            acumula duplicatas mortas no disco.

        Retorna resumo {revalidados, apagados, ainda_pendentes, erros}.
        ``domain`` (opcional) limita a revalidação ao domínio. Nunca crasha."""
        pendentes = self.store.pending_revalidation()
        if not pendentes:
            return {"revalidados": 0, "apagados": 0,
                    "ainda_pendentes": [], "erros": []}
        revalidados = 0
        apagados = 0
        ainda: list[str] = []
        erros: list[str] = []
        for rid in pendentes:
            rec = self.store.get(rid)
            if rec is None:
                continue
            if domain is not None and rec.domain != domain:
                ainda.append(rid)
                continue
            try:
                codigo = interpolate_template(rec.execution_payload, {})
            except Exception as exc:  # noqa: BLE001
                # placeholder obrigatório sem valor: NÃO é prova de artefato
                # quebrado — mantém pendente (não apaga dado persistido).
                ainda.append(rid)
                erros.append(f"{rid}: {exc}")
                continue
            try:
                r = self.sandbox.run(codigo)
                if r.exit_code == 0:
                    self.store.clear_revalidation(rid)
                    revalidados += 1
                else:
                    self.store.delete(rid)
                    apagados += 1
            except Exception as exc:  # noqa: BLE001
                # falha de infra (ex.: sandbox indisponível): não destrói —
                # mantém pendente para uma próxima revalidação.
                ainda.append(rid)
                erros.append(f"{rid}: {exc}")
        return {
            "revalidados": revalidados,
            "apagados": apagados,
            "ainda_pendentes": ainda,
            "erros": erros,
        }

    # ------------------------------------------------------------- stats
    def stats(self) -> dict:
        """Estatísticas do motor: hits, misses, avg_time_ms e hit_rate.

        ``avg_time_ms`` é a média de todas as execuções registradas;
        ``hit_rate`` = hits / (hits + misses) — 0.0 se nada executou.
        """
        total = self._hits + self._misses
        return {
            "hits": self._hits,
            "misses": self._misses,
            "execucoes": total,
            "avg_time_ms": (
                round(sum(self._tempos) / len(self._tempos), 3)
                if self._tempos
                else 0.0
            ),
            "hit_rate": round(self._hits / total, 4) if total else 0.0,
        }

    def _registrar_estatistica(self, resultado: ExecutionResult) -> None:
        self._tempos.append(resultado.execution_time_ms)
        if resultado.executed_locally:
            self._hits += 1
        else:
            self._misses += 1

    @staticmethod
    def _ms(inicio: float) -> float:
        return round((time.perf_counter() - inicio) * 1000, 3)


# ------------------------------------------------------------------ CLI
def main(argv: list[str] | None = None) -> int:
    """CLI simples: imprime JSON do resultado. Retorna 0 sempre que o CLI
    executou (o sucesso da tarefa está no campo ``success`` do JSON)."""
    parser = argparse.ArgumentParser(
        prog="agentic-harness-engine",
        description="Harness determinístico semântico (Etapa 2) — CLI.",
    )
    parser.add_argument("tarefa", help="texto da tarefa a executar")
    parser.add_argument(
        "--domain", choices=sorted(config.DOMAINS), default="code_gen"
    )
    parser.add_argument(
        "--threshold", type=float, default=config.THRESHOLD,
        help="limiar de similaridade cosseno (padrão 0.92)",
    )
    parser.add_argument(
        "--dry-run",
        action="store_true",
        help="só embedding + busca; não executa sandbox nem persiste",
    )
    args = parser.parse_args(argv)

    embedder = LocalEmbedder()
    store = VectorStore()
    sandbox = SandboxRunner()
    engine = AgenticHarnessEngine(
        embedder, store, sandbox, llm_client=None, threshold=args.threshold
    )

    if args.dry_run:
        vetor = embedder.generate_embedding(args.tarefa)
        hit = store.find_nearest(vetor, engine._threshold_efetivo())
        print(
            json.dumps(
                {
                    "dry_run": True,
                    "tarefa": args.tarefa,
                    "domain": args.domain,
                    "embedding_dim": len(vetor),
                    "embedder_mode": embedder.mode,
                    "sandbox_modo": sandbox.mode,
                    "safe_mode": engine.safe_mode,
                    "hit": hit.id if hit else None,
                },
                ensure_ascii=False,
            )
        )
        return 0

    resultado = asyncio.run(
        engine.execute_task(args.tarefa, args.domain)
    )
    # `sandbox_modo` agora é campo REAL de ExecutionResult (Item 7.1), então
    # `asdict` já o inclui — sem remendo manual. Chave unificada: `sandbox_modo`
    # (mesmo nome usado no dry-run, no motor e nos testes).
    dados = asdict(resultado)
    print(json.dumps(dados, ensure_ascii=False))
    return 0


if __name__ == "__main__":
    sys.exit(main())