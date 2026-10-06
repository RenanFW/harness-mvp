"""Modelos de dados do harness determinístico semântico (Etapa 1).

Dataclasses com validação básica. Nota: NENHUM código do motor filtra
segredos hoje (a antiga regra `config.is_secret` foi removida por não ter
chamadores) — a política de segredos fica a cargo do harness principal
(`harness/extractor.py`) e do executor.
"""

from __future__ import annotations

from dataclasses import dataclass, field
from datetime import datetime, timezone

try:  # importação como pacote (preferida)
    from . import config
except ImportError:  # execução direta (script): python models.py
    import config


@dataclass
class ExecutionResult:
    """Resultado de uma execução (engine e sandbox).

    ``success`` é o resumo booleano; ``exit_code`` (opcional) carrega o
    código de saída REAL do processo quando uma execução de sandbox ocorreu —
    usado pelo engine na regra zero-poisoning (nada com exit != 0 é
    persistido). ``artifact_id`` referencia o artefato do cache quando o
    resultado veio de (ou foi gravado em) um cache-hit.

    ``sandbox_modo`` (Item 7.1) é o modo de validação REAL do sandbox
    (``docker`` | ``podman`` | ``exec``) preenchido pelo engine em TODO
    resultado; campo ADITIVO com default vazio (``""``) para preservar a
    compatibilidade de quem constrói ``ExecutionResult`` sem informá-lo. Por
    ser campo REAL da dataclass, ``dataclasses.asdict()`` passa a incluí-lo.
    """

    success: bool
    output: str = ""
    executed_locally: bool = False
    execution_time_ms: float = 0.0
    artifact_id: str | None = None
    error: str | None = None
    exit_code: int | None = None
    sandbox_modo: str = ""


@dataclass
class ArtifactRecord:
    """Registro persistido no cache semântico (vetor + payload + schema).

    ``execution_payload`` é o template AST (dict com "template",
    "placeholders" e "ast_valid" — ver ast_abstraction.abstract_to_template);
    ``validation_schema`` é o schema JSON estrito da Etapa 2.
    """

    id: str = ""
    intent_vector: list[float] = field(default_factory=list)
    domain: str = ""
    execution_payload: dict = field(default_factory=dict)
    validation_schema: dict = field(default_factory=dict)
    hit_count: int = 0
    created_at: str = field(
        default_factory=lambda: datetime.now(timezone.utc).isoformat()
    )

    def validate(self) -> None:
        """Validação básica do registro. Levanta ValueError se inconsistente.

        A validação de domínio é estrita no momento de gravar; ao LER do banco
        o VectorStore captura ValueError e ignora registros inválidos (log e
        segue — nunca derruba a busca).
        """
        if self.domain not in config.DOMAINS:
            raise ValueError(f"domínio desconhecido: {self.domain!r}")
        if len(self.intent_vector) != config.EMBEDDING_DIM:
            raise ValueError(
                f"intent_vector com {len(self.intent_vector)} dimensões "
                f"(esperado {config.EMBEDDING_DIM})"
            )
        for valor in self.intent_vector:
            if not isinstance(valor, (int, float)):
                raise ValueError("intent_vector deve conter apenas números")
        if not isinstance(self.execution_payload, dict):
            raise ValueError("execution_payload deve ser um dict")
        if not isinstance(self.validation_schema, dict):
            raise ValueError("validation_schema deve ser um dict")
        if self.hit_count < 0:
            raise ValueError("hit_count não pode ser negativo")

    def __post_init__(self) -> None:
        self.validate()