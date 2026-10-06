"""Memória local do Brain — compartilhada com o agente Brain do opencode.

Lê e escreve em `memory/` (core.md e episodic/). O web shell pode consultar e
registrar memória; o agente Brain do opencode usa a mesma base.

Confiança (Trust): todo registro episódico gravado via `record()` carrega
`trust` (alta|media|fraca), `origem` (rastreável) e `validado_por` no
frontmatter. Registros antigos SEM `trust` assumem `fraca` na leitura
(conservador — memória não validada não é conhecimento confirmado); use
`trust_de(meta)` para ler com o default.

Recuperabilidade (Item 2.1): campo booleano `recuperavel` (default `True`
quando ausente). Apenas registros marcados explicitamente com
`recuperavel: false` (ex.: alerta de fabricação) são NÃO-recuperáveis: o
`search()` NUNCA os devolve — nem com `min_trust`, nem por relevância. Use
`recuperavel_de(meta)` para ler com o default.
"""

from __future__ import annotations

import datetime
import pathlib
import re

from . import config
from .frontmatter import parse as _parse_frontmatter


def _trust_valido(value) -> str:
    """Normaliza um valor de trust: aceita apenas alta/media/fraca (case-
    insensitive); qualquer outro valor (vazio, inválido) vira `fraca`."""
    t = str(value or "").strip().lower()
    return t if t in config.TRUST else config.TRUST_DEFAULT


def trust_de(meta: dict) -> str:
    """Trust de um registro episódico a partir do meta (frontmatter).

    Registros sem a chave `trust` assumem `fraca` (tolerância à ausência —
    memória antiga não validada não é tratada como confirmada).
    """
    return _trust_valido(meta.get("trust") if isinstance(meta, dict) else None)


def recuperavel_de(meta: dict) -> bool:
    """Recuperabilidade de um registro a partir do meta (frontmatter).

    Default conservador `True` quando a chave AUSENTE/`None` (não perde memória
    antiga). Somente um `false` explícito marca NÃO-recuperável — é o que o
    webscraper grava no alerta de fabricação.

    Aceita como falso: `False`/`0` (bool/int do Python, ex.: `webscraper`) e
    as strings `false`/`0`/`no`/`nao`/`não` (case-insensitive). IMPORTANTE: a
    coerção NÃO usa `valor or ""` (que mascarava `False`/`0` do Python como
    recuperável); ausência (`None`) é o ÚNICO caso de default — qualquer outro
    valor é recuperável.
    """
    if not isinstance(meta, dict):
        return config.RECUPERAVEL_DEFAULT
    valor = meta.get("recuperavel")
    if valor is None:
        # Ausente/None: default conservador (não perde memória antiga).
        return config.RECUPERAVEL_DEFAULT
    if isinstance(valor, bool):  # bool antes de int (bool é subclasse de int)
        return valor
    if isinstance(valor, (int, float)):
        return bool(valor)
    texto = str(valor).strip().lower()
    if texto in ("false", "0", "no", "nao", "não"):
        return False
    return True


class Memory:
    def __init__(self) -> None:
        config.EPISODIC_DIR.mkdir(parents=True, exist_ok=True)
        config.MEMORY_DIR.mkdir(parents=True, exist_ok=True)

    # --------------------------------------------------------------- leitura
    def core(self) -> str:
        path = config.MEMORY_DIR / "core.md"
        return path.read_text(encoding="utf-8") if path.exists() else ""

    def list_records(self) -> list[dict]:
        records = []
        for path in sorted(config.EPISODIC_DIR.glob("*.md")):
            if path.name == "index.md":
                continue
            records.append(self._parse(path))
        return records

    def get(self, record_id: str) -> dict | None:
        if not _safe_id(record_id):
            return None
        path = config.EPISODIC_DIR / f"{record_id}.md"
        if not path.exists():
            return None
        return self._parse(path)

    # -------------------------------------------------------------- busca RAG
    def search(self, query: str, limit: int = 5, snippet: bool = False,
               min_trust: str | None = None) -> list[dict]:
        """Busca por relevância (Ch14): tokeniza a consulta e pontua cada
        registro pela interseção de termos com keywords (peso 3), título (2)
        e corpo (1). Sem dependências externas.

        `snippet=True` (Lote 1 — otimização de contexto): cada item retornado
        mantém `meta`, `id`, `file`, `score` e `trust`, e substitui `body` por
        um trecho de no máximo ~300 chars do início do corpo (preservando o
        título `# ` quando existir) — reduz o contexto enviado à LLM no RAG.
        Retrocompatível: default `False` devolve o `body` completo.

        `min_trust` (Item 2.2 — rotular -> filtrar): quando informado,
        devolve somente registros cujo `trust` é IGUAL OU MAIS CONFIÁVEL que o
        nível pedido (ordem `fraca < media < alta`). Default `None` = SEM
        filtro (retrocompatível). O pipeline passa `config.RAG_MIN_TRUST`
        (`media`) para que memória `fraca` não alimente o pipeline como
        conhecimento confirmado. Nível inválido cai no default conservador de
        `_trust_valido` (`fraca`), o que não filtra nada além do já fraco.

        Registros NÃO-recuperáveis (`recuperavel: false`, ex.: alerta de
        fabricação) são SEMPRE excluídos, independentemente de relevância ou
        de `min_trust`/snippet — fabricação explícita nunca é recuperada.
        """
        q_tokens = _tokens(query)
        if not q_tokens:
            return []

        min_rank: int | None = None
        if min_trust is not None:
            min_rank = config.TRUST_ORDEM.index(_trust_valido(min_trust))

        scored: list[tuple[int, dict]] = []
        for rec in self.list_records():
            meta = rec["meta"]
            # Não-recuperável nunca é candidato (fabricação explícita).
            if not recuperavel_de(meta):
                continue
            trust = trust_de(meta)
            if min_rank is not None \
                    and config.TRUST_ORDEM.index(trust) < min_rank:
                continue
            kw_text = meta.get("keywords", "").strip("[]")
            body = rec["body"]
            title = ""
            for line in body.splitlines():
                if line.startswith("# "):
                    title = line.lstrip("# ").strip()
                    break
            kw_tokens = _tokens(kw_text)
            title_tokens = _tokens(title)
            body_tokens = _tokens(body)

            score = 3 * len(q_tokens & kw_tokens) \
                + 2 * len(q_tokens & title_tokens) \
                + 1 * len(q_tokens & body_tokens)
            if score:
                scored.append((score, rec))

        scored.sort(key=lambda item: item[0], reverse=True)
        result = []
        for score, rec in scored[:limit]:
            item = dict(rec)
            item["score"] = score
            # Trust/recuperabilidade expostos a consumidores (RAG/brain):
            # memória fraca não deve ser tratada como conhecimento confirmado e
            # fabricação nunca é devolvida (filtrada acima).
            item["trust"] = trust_de(rec["meta"])
            item["recuperavel"] = recuperavel_de(rec["meta"])
            if snippet:
                item["body"] = _snippet_body(rec["body"])
            result.append(item)
        return result

    @staticmethod
    def _parse(path: pathlib.Path) -> dict:
        text = path.read_text(encoding="utf-8")
        # Parser de frontmatter COMPARTILHADO (harness/frontmatter.py):
        # tolerante a CRLF (arquivos editados no Windows) e a linhas de
        # continuação — os antigos regex LF-only quebravam silenciosamente
        # (meta vazio + trust default `fraca` sem aviso).
        meta, body = _parse_frontmatter(text)
        return {
            "id": path.stem,
            "file": str(path),
            "meta": meta,
            "body": body.strip(),
        }

    # -------------------------------------------------------------- gravação
    def record(self, *, keywords: list[str], agente: str, tema: str,
               entrada: str, fluxo: str, resultado: str,
               contexto: str, status: str = "completed",
               trust: str = config.TRUST_DEFAULT, origem: str = "",
               validado_por: str = "") -> dict:
        """Grava um registro episódico seguindo o template do Brain.

        Parâmetros novos (Trust): `trust` (alta|media|fraca; default fraca),
        `origem` (texto rastreável; default marcado como não informada) e
        `validado_por` (quem validou: reviewer/implementer/hub/motor/human).
        Defaults seguros: chamadas existentes continuam gravando com trust
        fraca e origem explicitamente marcada como ausente.
        """
        safe = _slug(tema)
        data = datetime.date.today().isoformat()
        record_id = self._next_id(safe, data)
        keywords = [k for k in (keywords or []) if k]
        trust = _trust_valido(trust)
        origem_txt = origem.strip() or "_não informada_"
        validado = validado_por.strip() or "_não informado_"
        meta_lines = [
            "---",
            f"id: {record_id}",
            f"keywords: [{', '.join(keywords)}]",
            f"data: {data}",
            f"agente: {agente}",
            f"status: {status}",
            f"trust: {trust}",
            f"origem: {origem_txt}",
            f"validado_por: {validado}",
            "---",
            "",
            f"# {tema}",
            "",
            "## Contrato de entrada",
            entrada.strip() or "_não informado_",
            "",
            "## Fluxo",
            fluxo.strip() or "_não informado_",
            "",
            "## Resultado",
            resultado.strip() or "_não informado_",
            "",
            "## Contexto",
            contexto.strip() or "_não informado_",
            "",
        ]
        path = config.EPISODIC_DIR / f"{record_id}.md"
        path.write_text("\n".join(meta_lines), encoding="utf-8")
        self._refresh_index()
        return self.get(record_id)

    def _next_id(self, safe: str, data: str) -> str:
        """Gera um id único para hoje, evitando colisão mesmo com registros
        removidos (conta apenas arquivos existentes com o mesmo prefixo)."""
        prefix = f"{safe}-{data}"
        numbers = []
        for path in config.EPISODIC_DIR.glob(f"{prefix}-*.md"):
            stem = path.stem
            tail = stem.rsplit("-", 1)[-1]
            if tail.isdigit():
                numbers.append(int(tail))
        seq = max(numbers, default=0) + 1
        return f"{prefix}-{seq:02d}"

    def _refresh_index(self) -> None:
        """Reconstrói `memory/episodic/index.md` como mapa compacto
        keyword -> id (Lote 2 — enxugamento de contexto): cada linha traz
        apenas keywords, id e data; tema/resumo ficam no próprio registro."""
        rows = ["| Keywords | Id | Data |", "| --- | --- | --- |"]
        for rec in self.list_records():
            meta = rec["meta"]
            keywords = meta.get("keywords", "[]").strip("[]")
            rows.append(f"| {keywords} | {rec['id']} | {meta.get('data', '')} |")
        content = (
            "# Índice Episódico\n\n"
            "Mapa: keyword -> id do registro. Atualizado automaticamente.\n\n"
            + "\n".join(rows)
            + "\n"
        )
        config.INDEX_FILE.write_text(content, encoding="utf-8")


def _slug(value: str) -> str:
    value = value.lower().strip()
    value = re.sub(r"[^a-z0-9]+", "-", value)
    return value.strip("-")[:32] or "registro"


def _tokens(text: str) -> set[str]:
    """Tokens de busca: palavras alfanuméricas >= 3 chars, minúsculas."""
    return {w for w in re.findall(r"[a-z0-9]{3,}", text.lower())}


def _safe_id(value: str) -> bool:
    """Evita path traversal: ids são apenas slug de arquivo `xxx-yyy-...`."""
    return bool(re.fullmatch(r"[a-z0-9][a-z0-9-]*", value or ""))


def _snippet_body(body: str, limite: int | None = None) -> str:
    """Trecho de no máximo ~`limite` chars do início do corpo, preservando o
    título `# ` quando existir (Lote 1 — RAG com snippet reduz o contexto
    enviado à LLM). Conservador: nunca devolve mais que o corpo original."""
    limite = config.SNIPPET_LIMITE if limite is None else limite
    if not body:
        return ""
    linhas = body.splitlines()
    if linhas and linhas[0].startswith("# "):
        titulo = linhas[0]
        resto = "\n".join(linhas[1:]).lstrip("\n")
        sobra = limite - len(titulo)
        if sobra <= 0:
            return titulo[:limite]
        if len(resto) > sobra:
            resto = resto[:sobra].rstrip() + "…"
        return (titulo + "\n" + resto).strip()
    if len(body) > limite:
        return body[:limite].rstrip() + "…"
    return body.strip()