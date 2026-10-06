"""ARCCache — Adaptive Replacement Cache em memória sobre a tabela (Etapa 1).

O estado ARC (T1/B1/T2/B2) vive em memória; a persistência desse estado é
opcional e NÃO implementada (documentado — o banco SQLite é a fonte de
verdade). A proteção real de disco é ``evict_if_over_disk``: acima de
MAX_DISK_GB, entradas menos recentes com menor hit_count são removidas
(VACUUM + DELETE) — combinação recência/frequência.
"""

from __future__ import annotations

import logging
import pathlib

try:  # importação como pacote (preferida)
    from . import config
    from .vector_store import VectorStore
except ImportError:  # execução direta (script)
    import config
    from vector_store import VectorStore

logger = logging.getLogger(__name__)


class ARCCache:
    """ARC (Adaptive Replacement Cache) puro-Python por id de artefato.

    Estruturas (por id):
      T1 — recência recente; T2 — frequência recente;
      B1/B2 — ghosts (histórico de evictions) usados para adaptar o alvo ``p``.
    ``hit_count`` do banco é usado como frequência nas decisões de eviction
    de disco (evict_if_over_disk).

    O ARC NÃO substitui o VectorStore: acelera consultas em memória por id e
    orquestra a eviction de disco.
    """

    def __init__(self, store: VectorStore, cap: int | None = None):
        self.store = store
        self.cap = max(1, cap or config.ARC_CAP)
        self.t1: list[str] = []
        self.b1: list[str] = []
        self.t2: list[str] = []
        self.b2: list[str] = []
        self.p = max(1, self.cap // 2)  # alvo adaptativo inicial

    # ------------------------------------------------------------- acesso
    def get(self, artifact_id: str) -> bool:
        """Consulta o id no ARC (hit/miss) e atualiza as listas.

        Hit (T1/T2): promove para T2 (frequência) e incrementa hit_count no
        banco (frequência). Hit fantasma (B1/B2): re-insere em T2 e adapta o
        alvo ``p``. Miss: insere em T1.
        """
        if artifact_id in self.t1:
            self.t1.remove(artifact_id)
            self.t2.append(artifact_id)
            self._registrar_hit(artifact_id)
            return True
        if artifact_id in self.t2:
            self.t2.remove(artifact_id)
            self.t2.append(artifact_id)
            self._registrar_hit(artifact_id)
            return True
        if artifact_id in self.b1:
            self.p = min(
                self.cap,
                self.p + max(1, len(self.b2) // max(1, len(self.b1))),
            )
            self._reinserir(artifact_id, self.b1)
            return True
        if artifact_id in self.b2:
            self.p = max(
                0,
                self.p - max(1, len(self.b1) // max(1, len(self.b2))),
            )
            self._reinserir(artifact_id, self.b2)
            return True
        self._inserir_t1(artifact_id)
        return False

    def put(self, artifact_id: str) -> None:
        """Insere/atualiza o id no cache (chamado após um save)."""
        if artifact_id not in self.t1 and artifact_id not in self.t2:
            self._inserir_t1(artifact_id)

    # ------------------------------------------------------------- eviction
    def evict_if_over_disk(self, limit_bytes: int | None = None) -> int:
        """Se o tamanho do DB exceder ``limit_bytes``, descarta entradas
        (DELETE + VACUUM) com combinação recência/frequência (menos recentes
        com menor hit_count). Retorna quantas entradas foram removidas."""
        limite = (
            limit_bytes
            if limit_bytes is not None
            else config.MAX_DISK_GB * 1024 ** 3
        )
        removidos = 0
        while self._db_size() > limite:
            restantes = self.store.count()
            if restantes == 0:
                break
            antes = self.store.all_ids()
            n = self.store.evict_entries(max(1, restantes // 2))
            self.store.vacuum()
            removidos += n
            if n == 0:
                logger.warning("eviction estagnada; parando")
                break
            self._esquecer(antes - self.store.all_ids())
        return removidos

    def ensure_disk_limit(self) -> int:
        """Garante o limite de disco padrão (MAX_DISK_GB)."""
        return self.evict_if_over_disk(config.MAX_DISK_GB * 1024 ** 3)

    def warm(self) -> int:
        """Popula o ARC com os ids existentes no banco (até o cap).

        Persistência de estado ARC é opcional; ``warm`` é a reidratação em
        memória a partir da fonte de verdade (o banco).
        """
        ids = sorted(self.store.all_ids())
        for rid in ids[: self.cap]:
            if rid not in self.t1 and rid not in self.t2:
                self.t1.append(rid)
        return len(self.t1)

    # ------------------------------------------------------------- internos
    def _registrar_hit(self, artifact_id: str) -> None:
        try:
            self.store.increment_hit_count(artifact_id)
        except Exception as exc:  # noqa: BLE001 — banco não derruba o cache
            logger.warning("falha ao incrementar hit_count de %s: %s", artifact_id, exc)

    def _inserir_t1(self, artifact_id: str) -> None:
        self.t1.append(artifact_id)
        self._rebalancear()

    def _reinserir(self, artifact_id: str, ghost: list[str]) -> None:
        if artifact_id in ghost:
            ghost.remove(artifact_id)
        if artifact_id not in self.t2:
            self.t2.append(artifact_id)
        self._rebalancear()

    def _rebalancear(self) -> None:
        """Mantém T1+T2 <= cap, movendo LRUs para os ghosts (B1/B2)."""
        while len(self.t1) + len(self.t2) > self.cap:
            if len(self.t1) > self.p and self.t1:
                lru = self.t1.pop(0)
                self.b1.append(lru)
                if len(self.b1) > self.cap:
                    self.b1.pop(0)
            elif self.t2:
                lru = self.t2.pop(0)
                self.b2.append(lru)
                if len(self.b2) > self.cap:
                    self.b2.pop(0)
            else:
                break  # não há o que evictar

    def _esquecer(self, ids: set[str]) -> None:
        """Remove ids evictados do disco das listas ARC."""
        for lista in (self.t1, self.b1, self.t2, self.b2):
            for rid in list(lista):
                if rid in ids:
                    lista.remove(rid)

    def _db_size(self) -> int:
        try:
            return pathlib.Path(self.store.db_path).stat().st_size
        except OSError:
            return 0