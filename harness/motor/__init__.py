"""Harness determinístico semântico (Etapas 1 e 2) — closed-loop
semantic-cache-first com LLM externo opcional e validação estrita.

PACOTE AUTÔNOMO: importável sob demanda (`import harness.motor`); o runtime
do harness (pipeline/executor/servidor) NÃO o importa no boot — existe para
uso direto (CLI/API própria) ou futura integração. Zero-deps stdlib;
ONNX (onnxruntime), Docker e Pydantic são opcionais com failover para safe
mode. A Etapa 2 adiciona: LLMClient (provedor REST opcional com LLMError),
schema (validação estrita da resposta do LLM), ARC no engine (teto
MAX_DISK_GB=2), stats() e benchmark.py (prova dos critérios de aceite).
"""

from __future__ import annotations

from . import config
from .ast_abstraction import abstract_to_template, interpolate_template
from .cache import ARCCache
from .embedder import LocalEmbedder
from .engine import AgenticHarnessEngine
from .llm_client import LLMClient, LLMError
from .models import ArtifactRecord, ExecutionResult
from .sandbox import SandboxRunner
from .schema import default_validation_schema, pydantic_validate, validate_code_response
from .vector_store import VectorStore

__version__ = "0.2.0"

__all__ = [
    "AgenticHarnessEngine",
    "LocalEmbedder",
    "VectorStore",
    "ARCCache",
    "SandboxRunner",
    "LLMClient",
    "LLMError",
    "ArtifactRecord",
    "ExecutionResult",
    "abstract_to_template",
    "interpolate_template",
    "default_validation_schema",
    "pydantic_validate",
    "validate_code_response",
    "config",
    "__version__",
]