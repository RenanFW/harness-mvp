"""Configurações do harness determinístico semântico (Etapa 1).

Closed-loop semantic-cache-first: embedding local -> busca vetorial
(>= THRESHOLD) -> fast-path em sandbox -> persistência de artefatos.
Zero-deps stdlib; ONNX/docker são opcionais com failover para safe mode.
"""

from __future__ import annotations

import os
import pathlib

# ------------------------------------------------------------------ raiz
# Raiz do projeto (diretório do package)
ROOT = pathlib.Path(__file__).resolve().parent

# Banco SQLite do cache semântico (criado sob demanda pelo VectorStore)
DATA_DIR = ROOT / "data"
DB_PATH = DATA_DIR / "cache.db"

# ------------------------------------------------------------------ cache
# Limiar de similaridade cosseno para considerar um cache-hit
THRESHOLD = 0.92

# Limite de disco do banco (evict_if_over_disk descarta entradas acima disso)
MAX_DISK_GB = 2

# Capacidade do ARCCache em memória (entradas)
ARC_CAP = 128

# ------------------------------------------------------------------ domínios
# Domínios suportados pelo motor (validação estrita de ArtifactRecord/engine)
DOMAINS = {"devsecops", "pentest", "patching", "code_gen"}

# ------------------------------------------------------------------ embedding
# Dimensão do vetor de embedding (all-MiniLM-L6-v2 -> 384)
DIM = 384
EMBEDDING_DIM = 384

# Caminho do modelo ONNX INT8 (opcional). Configurável via env var
# EMBEDDER_MODEL_PATH ou por argumento do LocalEmbedder.
EMBEDDER_MODEL_PATH = pathlib.Path(
    os.environ.get(
        "EMBEDDER_MODEL_PATH",
        str(ROOT / "models" / "all-MiniLM-L6-v2-int8.onnx"),
    )
)

# ------------------------------------------------------------------ sandbox
# Timeout padrão de execução no sandbox (segundos)
TIMEOUT_SANDBOX = 10.0


def _env_bool(name: str, default: bool = False) -> bool:
    """Lê `name` de os.environ e interpreta como booleano (estilo harness).

    Aceita "1"/"true"/"yes"/"on" -> True; "0"/"false"/"no"/"off" -> False;
    ausente ou valor inválido -> `default` (False por padrão). A variável é
    lida NO IMPORT deste módulo: mudá-la em runtime NÃO tem efeito."""
    val = os.environ.get(name)
    if val is None:
        return default
    v = val.strip().lower()
    if v in ("1", "true", "yes", "on"):
        return True
    if v in ("0", "false", "no", "off"):
        return False
    return default


# Endurecimento opt-in (Item 7.1): quando True, o motor NÃO persiste artefatos
# validados FORA de um container (docker/podman). O isolamento do SandboxRunner
# resolve "docker -> podman -> exec"; o modo "exec" é o SAFE MODE (subprocess
# com shell=False) que roda no HOST, SEM isolamento de host. Com esta flag, o
# código validado só é persistido se tiver rodado em container; caso contrário,
# o engine retorna success=False ("isolamento de container exigido; nada
# persistido") SEM gravar. Default False: preserva o comportamento (persiste o
# que passou no sandbox, independente do modo). Lida do ambiente
# `MOTOR_REQUER_CONTAINER` no import (estilo harness).
MOTOR_REQUER_CONTAINER = _env_bool("MOTOR_REQUER_CONTAINER", False)