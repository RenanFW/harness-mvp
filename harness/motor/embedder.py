"""Embeddings locais com fast vectorizer (Etapa 1).

Zero-deps: o modo ONNX (onnxruntime) é OPcional — sem runtime/modelo o
LocalEmbedder cai no ``FastVectorizer`` puro-Python (safe mode, fidelidade
reduzida, determinístico). Nunca crasha: entrada vazia/não-str vira vetor
de zeros normalizado (norma zero).
"""

from __future__ import annotations

import math
import pathlib
import re
import zlib

try:  # importação como pacote (preferida)
    from . import config
except ImportError:  # execução direta (script)
    import config

# Tokenização do FastVectorizer: palavras/IDs alfanuméricos com >= 3 chars
_TOKEN_RE = re.compile(r"[a-z0-9]{3,}")

# Janela de tokens (truncamento) do FastVectorizer
MAX_TOKENS = 512


def l2_normalize(vetor: list[float]) -> list[float]:
    """Normaliza L2. Vetor nulo (norma 0) permanece como está (zeros)."""
    norma = math.sqrt(sum(x * x for x in vetor))
    if norma == 0.0:
        return vetor
    return [x / norma for x in vetor]


def cosine(a: list[float], b: list[float]) -> float:
    """Similaridade cosseno entre dois vetores.

    Robusto: vetores vazios, dimensões diferentes ou norma zero retornam
    0.0 (nunca levanta).
    """
    if not a or not b or len(a) != len(b):
        return 0.0
    produto = sum(x * y for x, y in zip(a, b))
    na = math.sqrt(sum(x * x for x in a))
    nb = math.sqrt(sum(y * y for y in b))
    if na == 0.0 or nb == 0.0:
        return 0.0
    return produto / (na * nb)


class FastVectorizer:
    """Vetorizador determinístico puro-Python (safe mode, fidelidade reduzida).

    - tokenização por regex ``[a-z0-9]{3,}`` sobre o texto em minúsculas;
    - pesos por frequência (tf) acumulados em buckets de ``dim`` dimensões via
      hash estável (zlib.crc32 — determinístico entre processos);
    - padding/truncamento: a janela de tokens é truncada em MAX_TOKENS; a
      saída tem EXATAMENTE ``dim`` floats (padding de zeros implícito);
    - normalização L2; entrada vazia/não-str -> vetor de zeros.

    É o modo "safe": não depende de runtime/modelo externo, com fidelidade
    semântica reduzida em relação ao ONNX real.
    """

    def __init__(self, dim: int = config.EMBEDDING_DIM):
        self.dim = dim

    def embed(self, text: str) -> list[float]:
        if text is None:
            return [0.0] * self.dim
        if not isinstance(text, str):
            text = str(text)
        tokens = _TOKEN_RE.findall(text.lower())[:MAX_TOKENS]
        if not tokens:
            return [0.0] * self.dim
        vetor = [0.0] * self.dim
        for token in tokens:
            idx = zlib.crc32(token.encode("utf-8")) % self.dim
            vetor[idx] += 1.0  # peso por frequência (tf)
        return l2_normalize(vetor)


class LocalEmbedder:
    """Embedder local com modo ONNX opcional e fallback determinístico.

    ``mode``:
      "onnx"     — onnxruntime disponível E modelo INT8 carregado com
                   sucesso (InferenceSession);
      "fallback" — onnxruntime disponível mas o modelo não está presente ou
                   falhou ao carregar (usa FastVectorizer);
      "safe"     — onnxruntime indisponível (usa FastVectorizer; fidelidade
                   reduzida — documentado como safe mode).

    API: generate_embedding(text)->list[float] (sempre len==EMBEDDING_DIM,
    normalizado L2), cosine(a,b)->float, e a propriedade ``mode``.
    """

    def __init__(
        self,
        model_path: str | pathlib.Path | None = None,
        dim: int = config.EMBEDDING_DIM,
    ):
        self.dim = dim
        self._fast = FastVectorizer(dim)
        self._session = None
        self._mode = "safe"

        caminho = pathlib.Path(model_path) if model_path else config.EMBEDDER_MODEL_PATH
        ort_disponivel = False
        try:
            import onnxruntime  # noqa: F401 — dependência opcional
            ort_disponivel = True
        except Exception:  # noqa: BLE001 — ImportError ou runtime quebrado
            ort_disponivel = False

        if ort_disponivel and caminho.exists():
            try:
                import onnxruntime as ort

                self._session = ort.InferenceSession(
                    str(caminho), providers=["CPUExecutionProvider"]
                )
                self._mode = "onnx"
            except Exception:  # noqa: BLE001 — modelo inválido/incompatível
                self._session = None
                self._mode = "fallback"
        elif ort_disponivel:
            self._mode = "fallback"
        else:
            self._mode = "safe"

    # ------------------------------------------------------------- API
    def generate_embedding(self, text: str) -> list[float]:
        """Retorna vetor len==dim normalizado L2. Nunca crasha: qualquer
        falha (ONNX incluso) cai no FastVectorizer determinístico."""
        if self._mode == "onnx" and self._session is not None:
            try:
                vetor = self._onnx_embed(text)
                if vetor is not None and len(vetor) == self.dim:
                    return l2_normalize(vetor)
            except Exception:  # noqa: BLE001 — inferência ONNX falhou
                pass  # fallback determinístico abaixo
        return self._fast.embed(text)

    def cosine(self, a: list[float], b: list[float]) -> float:
        """Delega para a função module-level (robusta)."""
        return cosine(a, b)

    @property
    def mode(self) -> str:
        return self._mode

    # ------------------------------------------------------------- ONNX
    def _onnx_embed(self, text: str) -> list[float] | None:
        """Inferência ONNX aproximada (Etapa 1).

        AVISO documentado: o tokenizer wordpiece completo do MiniLM exige um
        vocab externo (Etapa futura). Aqui os tokens são mapeados por hash
        estável no espaço de vocab do BERT (30522). O resultado só é fiel com
        o modelo real + tokenizer adequado — plugável depois. Qualquer falha
        é capturada pelo chamador (fallback determinístico).
        """
        import numpy as np  # exigido pelo onnxruntime; import local tolerante

        tokens = _TOKEN_RE.findall(text.lower())[:MAX_TOKENS]
        vocab_size = 30522
        ids = (
            [zlib.crc32(t.encode("utf-8")) % (vocab_size - 2) + 2 for t in tokens]
            if tokens
            else []
        )
        ids = [101] + ids + [102]  # [CLS] + tokens + [SEP]
        atencao = [1] * len(ids)

        entradas = self._session.get_inputs()
        feeds = {entradas[0].name: np.array([ids], dtype=np.int64)}
        for entrada in entradas[1:]:
            feeds[entrada.name] = np.array([atencao], dtype=np.int64)

        saidas = self._session.run(None, feeds)
        v = np.asarray(saidas[0])
        if v.ndim == 3:  # (1, seq, hidden) -> mean pooling
            v = v.mean(axis=1)
        plano = v.reshape(-1).tolist()
        if len(plano) < self.dim:
            plano = plano + [0.0] * (self.dim - len(plano))
        return plano[: self.dim]