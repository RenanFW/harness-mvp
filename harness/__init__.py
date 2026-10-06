"""Harness local — execução de comandos, web shell e memória do Brain.

Portável: apenas biblioteca padrão do Python. Rode com `python app.py`.

O subpackage ``harness.motor`` (motor determinístico semântico —
semantic-cache-first: embedding local, cosseno >= 0.92, sandbox, LLM opcional)
é um PACOTE AUTÔNOMO, importável sob demanda com `import harness.motor`.
Ele NÃO é importado no boot do harness (import lazy): o runtime do harness
(pipeline/executor/servidor) não o usa — o motor existe para uso direto ou
futura integração.
"""

import warnings

# Mitigação do RuntimeWarning do runpy (`'harness.complexity' found in
# sys.modules`): o __init__ importa `.pipeline` (que importa `.complexity`)
# ANTES do runpy executar `-m harness.complexity`. Ao registrar o filtro aqui,
# no topo (antes dos imports eager), o warning já está suprimido quando o
# runpy o emite — o stderr do CLI fica limpo (stdout JSON puro).
warnings.filterwarnings(
    "ignore", message=".*found in sys.modules.*", category=RuntimeWarning
)

__version__ = "1.3.0"

from .config import ROOT, MEMORY_DIR, DEFAULT_PORT, SIDE_PRJS_DIR
from .eval import evaluate
from .executor import Executor
from .extractor import extract
from .memory import Memory
from .namer import criar_projeto, nome_projeto, slugify
from .agents import Playbook, compile_playbook, load_playbook
from .pipeline import AgentPipeline, TaskContract, PipelineError
from .server import HarnessServer

__all__ = [
    "ROOT",
    "MEMORY_DIR",
    "DEFAULT_PORT",
    "SIDE_PRJS_DIR",
    "Executor",
    "Memory",
    "HarnessServer",
    "Playbook",
    "compile_playbook",
    "load_playbook",
    "AgentPipeline",
    "TaskContract",
    "PipelineError",
    "extract",
    "evaluate",
    "criar_projeto",
    "nome_projeto",
    "slugify",
    "__version__",
]