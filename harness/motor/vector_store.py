"""VectorStore — cache semântico persistente em SQLite puro (Etapa 1).

Sem extensões: apenas sqlite3 da stdlib. Guarda vetor (BLOB struct pack de
384 floats), payload e schema (JSON). Leitura de registros corrompidos é
tolerada: loga e segue (nunca derruba a busca).
"""

from __future__ import annotations

import json
import logging
import os
import pathlib
import sqlite3
import struct
import uuid
from contextlib import contextmanager

try:  # importação como pacote (preferida)
    from . import config
    from .embedder import cosine
    from .models import ArtifactRecord
except ImportError:  # execução direta (script)
    import config
    from embedder import cosine
    from models import ArtifactRecord

logger = logging.getLogger(__name__)

# Pack/unpack de exatamente EMBEDDING_DIM floats little-endian
_FMT = f"<{config.EMBEDDING_DIM}f"


class VectorStore:
    """Armazenamento SQLite do cache semântico.

    Schema:
        artifacts(id TEXT PRIMARY KEY,
                  intent_vector BLOB,        -- struct.pack('<384f')
                  domain TEXT,
                  execution_payload TEXT,    -- JSON
                  validation_schema TEXT,    -- JSON
                  hit_count INTEGER DEFAULT 0,
                  needs_revalidation INTEGER NOT NULL DEFAULT 0,
                  created_at TEXT)
    ``find_nearest`` faz scan linear O(n) com cosseno (HNSW é opcional futuro
    — documentado) e IGNORA registros marcados com ``needs_revalidation=1``
    (replay falhou — revalidação pendente).
    """

    def __init__(self, db_path: str | os.PathLike | None = None):
        self.db_path = pathlib.Path(db_path) if db_path else config.DB_PATH
        self.db_path.parent.mkdir(parents=True, exist_ok=True)
        self._init_db()

    # ------------------------------------------------------------- conexão
    def _connect(self) -> sqlite3.Connection:
        conn = sqlite3.connect(str(self.db_path))
        return conn

    @contextmanager
    def _conexao(self):
        """Conexão com commit no sucesso e close garantido.

        Nota (Windows): o context manager de sqlite3.Connection NÃO fecha a
        conexão (só gerencia transação) e o close() com transação pendente faz
        ROLLBACK. Este helper commita antes de fechar — nada se perde e o
        arquivo do banco é liberado para remoção.
        """
        conn = self._connect()
        try:
            yield conn
            conn.commit()
        finally:
            conn.close()

    def _init_db(self) -> None:
        with self._conexao() as conn:
            conn.execute(
                """CREATE TABLE IF NOT EXISTS artifacts (
                    id TEXT PRIMARY KEY,
                    intent_vector BLOB NOT NULL,
                    domain TEXT NOT NULL,
                    execution_payload TEXT NOT NULL,
                    validation_schema TEXT NOT NULL,
                    hit_count INTEGER NOT NULL DEFAULT 0,
                    needs_revalidation INTEGER NOT NULL DEFAULT 0,
                    created_at TEXT NOT NULL
                )"""
            )
            self._migrar_revalidacao(conn)

    @staticmethod
    def _migrar_revalidacao(conn: sqlite3.Connection) -> None:
        """Migração de bancos antigos: adiciona ``needs_revalidation`` se a
        coluna ainda não existir (bancos criados antes do achado A1)."""
        colunas = {
            row[1]
            for row in conn.execute("PRAGMA table_info(artifacts)").fetchall()
        }
        if "needs_revalidation" not in colunas:
            conn.execute(
                "ALTER TABLE artifacts ADD COLUMN "
                "needs_revalidation INTEGER NOT NULL DEFAULT 0"
            )

    # ------------------------------------------------------------- gravação
    def save(self, record: ArtifactRecord) -> str:
        """Persiste o registro e retorna o id (UUID4 hex se vazio)."""
        rid = record.id or uuid.uuid4().hex
        blob = struct.pack(_FMT, *record.intent_vector)
        with self._conexao() as conn:
            conn.execute(
                "INSERT OR REPLACE INTO artifacts "
                "(id, intent_vector, domain, execution_payload, "
                " validation_schema, hit_count, created_at) "
                "VALUES (?, ?, ?, ?, ?, ?, ?)",
                (
                    rid,
                    blob,
                    record.domain,
                    json.dumps(record.execution_payload, ensure_ascii=False),
                    json.dumps(record.validation_schema, ensure_ascii=False),
                    record.hit_count,
                    record.created_at,
                ),
            )
        return rid

    # ------------------------------------------------------------- leitura
    def find_nearest(
        self,
        query_vec: list[float],
        threshold: float = 0.92,
    ) -> ArtifactRecord | None:
        """Busca o registro mais próximo do vetor de consulta.

        Scan linear O(n) sobre todos os registros (HNSW é opcional futuro —
        documentado no docstring). Retorna o registro com maior cosseno
        >= threshold; None se nenhum atingir o limiar.

        Registros corrompidos (BLOB/JSON inválidos) são ignorados com log —
        nunca derrubam a busca. Registros com ``needs_revalidation=1``
        (replay falhou) também são IGNORADOS: não podem ser reusados pelo
        fast-path até uma revalidação pendente (achado A1 da revisão).
        """
        with self._conexao() as conn:
            rows = conn.execute(
                "SELECT id, intent_vector, domain, execution_payload, "
                "       validation_schema, hit_count, created_at "
                "FROM artifacts "
                "WHERE needs_revalidation = 0"
            ).fetchall()

        melhor: ArtifactRecord | None = None
        melhor_sim = -1.0
        for row in rows:
            rec = self._deserialize(row)
            if rec is None:
                continue
            sim = cosine(query_vec, rec.intent_vector)
            if sim > melhor_sim:
                melhor_sim = sim
                melhor = rec
        if melhor is not None and melhor_sim >= threshold:
            return melhor
        return None

    def _deserialize(self, row: tuple) -> ArtifactRecord | None:
        """Converte uma linha crua em ArtifactRecord. Corrompido -> None
        (log e segue)."""
        rid, blob, domain, payload_json, schema_json, hit_count, created_at = row
        try:
            vetor = list(struct.unpack(_FMT, blob))
        except (struct.error, TypeError):
            logger.warning("artefato %s: intent_vector corrompido (ignorado)", rid)
            return None
        try:
            payload = json.loads(payload_json)
            schema = json.loads(schema_json)
        except (json.JSONDecodeError, TypeError):
            logger.warning("artefato %s: payload/schema corrompido (ignorado)", rid)
            return None
        if not isinstance(payload, dict) or not isinstance(schema, dict):
            logger.warning("artefato %s: payload/schema não-dict (ignorado)", rid)
            return None
        try:
            return ArtifactRecord(
                id=rid,
                intent_vector=vetor,
                domain=domain,
                execution_payload=payload,
                validation_schema=schema,
                hit_count=int(hit_count),
                created_at=created_at,
            )
        except ValueError as exc:  # validação estrita do modelo
            logger.warning("artefato %s: inválido (%s) — ignorado", rid, exc)
            return None

    def get(self, artifact_id: str) -> ArtifactRecord | None:
        """Recupera um registro por id (sem filtrar por revalidação). None se
        não existir ou estiver corrompido."""
        with self._conexao() as conn:
            row = conn.execute(
                "SELECT id, intent_vector, domain, execution_payload, "
                "       validation_schema, hit_count, created_at "
                "FROM artifacts WHERE id = ?",
                (artifact_id,),
            ).fetchone()
        if row is None:
            return None
        return self._deserialize(row)

    # ------------------------------------------------------------- métricas
    def increment_hit_count(self, artifact_id: str) -> bool:
        """Incrementa hit_count do artefato. False se o id não existir."""
        with self._conexao() as conn:
            cur = conn.execute(
                "UPDATE artifacts SET hit_count = hit_count + 1 WHERE id = ?",
                (artifact_id,),
            )
            return cur.rowcount > 0

    def invalidate(self, artifact_id: str) -> bool:
        """Invalida um artefato após replay falho (achado A1).

        Zera o ``hit_count`` e marca ``needs_revalidation=1`` — o registro
        deixa de ser encontrado por ``find_nearest`` (fica pendente de
        revalidação, listável via ``pending_revalidation``). Nada é apagado:
        o artefato permanece no banco para diagnóstico. False se o id não
        existir.
        """
        with self._conexao() as conn:
            cur = conn.execute(
                "UPDATE artifacts SET hit_count = 0, needs_revalidation = 1 "
                "WHERE id = ?",
                (artifact_id,),
            )
            return cur.rowcount > 0

    def pending_revalidation(self) -> list[str]:
        """Ids com revalidação pendente (replay falhou; excluídos do
        fast-path por ``find_nearest``). Ordenados por inserção (rowid)."""
        with self._conexao() as conn:
            rows = conn.execute(
                "SELECT id FROM artifacts WHERE needs_revalidation = 1 "
                "ORDER BY rowid"
            ).fetchall()
        return [r[0] for r in rows]

    def clear_revalidation(self, artifact_id: str) -> bool:
        """Limpa a marca de revalidação pendente (o replay voltou a passar;
        o registro volta ao fast-path). Mantém hit_count zerado — recomeça
        do zero. False se o id não existir."""
        with self._conexao() as conn:
            cur = conn.execute(
                "UPDATE artifacts SET needs_revalidation = 0 "
                "WHERE id = ?",
                (artifact_id,),
            )
            return cur.rowcount > 0

    def delete(self, artifact_id: str) -> bool:
        """Remove um registro (usado pela revalidação: artefato que falhou de
        novo é apagado — fecha o dead-end A1 sem acumular duplicatas)."""
        with self._conexao() as conn:
            cur = conn.execute(
                "DELETE FROM artifacts WHERE id = ?",
                (artifact_id,),
            )
            return cur.rowcount > 0

    def integrity_check(self) -> bool:
        """True se PRAGMA integrity_check == 'ok'."""
        with self._conexao() as conn:
            row = conn.execute("PRAGMA integrity_check").fetchone()
        return bool(row) and row[0] == "ok"

    def count(self) -> int:
        with self._conexao() as conn:
            row = conn.execute("SELECT COUNT(*) FROM artifacts").fetchone()
        return int(row[0]) if row else 0

    def all_ids(self) -> set[str]:
        """Todos os ids presentes no banco (usado pelo ARCCache)."""
        with self._conexao() as conn:
            rows = conn.execute("SELECT id FROM artifacts").fetchall()
        return {r[0] for r in rows}

    def stats(self) -> dict:
        """Resumo: total, por domínio, hits totais e tamanho do arquivo."""
        with self._conexao() as conn:
            total = conn.execute("SELECT COUNT(*) FROM artifacts").fetchone()[0]
            por_domain = dict(
                conn.execute(
                    "SELECT domain, COUNT(*) FROM artifacts GROUP BY domain"
                ).fetchall()
            )
            hits = conn.execute(
                "SELECT COALESCE(SUM(hit_count), 0) FROM artifacts"
            ).fetchone()[0]
        return {
            "total": int(total),
            "por_domain": por_domain,
            "hits_totais": int(hits),
            "db_bytes": self._db_size(),
        }

    def _db_size(self) -> int:
        try:
            return os.path.getsize(self.db_path)
        except OSError:
            return 0

    # ------------------------------------------------------- eviction (ARC)
    def evict_entries(self, n: int) -> int:
        """Remove os ``n`` registros menos recentes e com menor hit_count
        (combinação recência/frequência usada pelo ARCCache). Retorna a
        quantidade removida."""
        if n <= 0:
            return 0
        with self._conexao() as conn:
            cur = conn.execute(
                "DELETE FROM artifacts WHERE id IN ("
                "  SELECT id FROM artifacts"
                "  ORDER BY created_at ASC, hit_count ASC"
                "  LIMIT ?"
                ")",
                (n,),
            )
            return cur.rowcount

    def vacuum(self) -> None:
        """Compacta o arquivo do banco (VACUUM)."""
        with self._conexao() as conn:
            conn.execute("VACUUM")