"""Validação estrita da resposta do LLM (Etapa 2).

Contrato do closed-loop: o provedor externo devolve JSON com a chave
``code`` (código a executar no sandbox) e, opcionalmente, ``explicacao``.
Esta validação é o guardrail do contrato: resposta malformada vira
ValueError (que o LLMClient converte em LLMError) — nunca chega ao sandbox.

Zero-deps: Pydantic é opcional (import tolerante); sem ele, a validação
nativa por dict é a única via.
"""

from __future__ import annotations

try:  # importação como pacote (preferida)
    from . import config
except ImportError:  # execução direta (script)
    import config


def validate_code_response(data: dict) -> str:
    """Extrai e valida o campo ``code`` de uma resposta LLM.

    Exige: dict; chave ``code`` com str não vazia (após strip).
    ``explicacao`` é opcional (se presente, deve ser str). ValueError com
    o detalhe se qualquer exigência falhar. Nunca crasha com entradas
    malformadas — levanta ValueError (contrato de erro explícito).
    """
    if not isinstance(data, dict):
        raise ValueError(
            f"resposta deve ser um dict, recebido {type(data).__name__}"
        )
    if "code" not in data:
        raise ValueError("resposta sem a chave obrigatória 'code'")
    codigo = data["code"]
    if not isinstance(codigo, str):
        raise ValueError(
            f"campo 'code' deve ser str, recebido {type(codigo).__name__}"
        )
    if not codigo.strip():
        raise ValueError("campo 'code' não pode ser vazio")
    if "explicacao" in data and not isinstance(data["explicacao"], str):
        raise ValueError(
            "campo 'explicacao' deve ser str quando presente"
        )
    return codigo


def default_validation_schema(domain: str) -> dict:
    """Schema JSON básico por domínio (contrato com o LLM).

    - code_gen/devsecops/patching: exige ``code`` (str);
    - pentest: exige ``code`` + ``target`` (placeholder do alvo);
    - domínio desconhecido: ValueError (validação estrita, sem schema
      genérico silencioso).
    """
    base = {
        "type": "object",
        "required": ["code"],
        "properties": {
            "code": {"type": "string"},
            "explicacao": {"type": "string"},
        },
    }
    if domain == "pentest":
        return {
            "type": "object",
            "required": ["code", "target"],
            "properties": {
                "code": {"type": "string"},
                "target": {
                    "type": "string",
                    "description": "placeholder do alvo (host/url)",
                },
                "explicacao": {"type": "string"},
            },
        }
    if domain in config.DOMAINS:
        return base
    raise ValueError(f"domínio desconhecido: {domain!r}")


def pydantic_validate(data, schema_cls):
    """Valida ``data`` contra ``schema_cls`` (Pydantic) se disponível.

    Pydantic é opcional: sem o pacote instalado, ou com ``schema_cls``
    None, retorna ``data`` sem validação (fallback dict — o chamador usa
    validate_code_response). Com Pydantic, retorna a instância validada;
    ValueError se o modelo rejeitar os dados.
    """
    if schema_cls is None:
        return data
    try:
        import pydantic  # noqa: F401 — dependência opcional
    except Exception:  # noqa: BLE001 — ImportError ou runtime quebrado
        return data
    try:
        return schema_cls(**data)
    except Exception as exc:  # noqa: BLE001 — ValidationError/TypeError
        raise ValueError(f"schema Pydantic rejeitou a resposta: {exc}") from exc