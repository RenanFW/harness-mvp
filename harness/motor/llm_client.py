"""LLMClient — provedor REST externo opcional (Etapa 2).

Modos:
  "offline" — endpoint None: nenhuma chamada HTTP; generate() levanta
              LLMError (o engine captura e nunca crasha);
  "http"    — POST JSON para o endpoint com urllib.request (stdlib pura,
              sem dependências), timeout, headers Content-Type e
              Authorization (Bearer) se api_key_env fornecer uma chave.

Contrato de resposta: JSON dict com a chave ``code`` (str). Se
``schema_cls`` (modelo Pydantic) for fornecido, valida com ele (import
tolerante — Pydantic é opcional); senão, validação nativa via
schema.validate_code_response. Qualquer falha de rede/HTTP/validação vira
LLMError — o provedor externo nunca derruba o motor.
"""

from __future__ import annotations

import json
import os
import urllib.request
import warnings

try:  # importação como pacote (preferida)
    from .schema import (
        default_validation_schema,
        pydantic_validate,
        validate_code_response,
    )
except ImportError:  # execução direta (script)
    from schema import (
        default_validation_schema,
        pydantic_validate,
        validate_code_response,
    )


class LLMError(Exception):
    """Falha do provedor LLM externo (rede, HTTP, validação ou offline)."""


class LLMClient:
    """Cliente REST opcional do closed-loop (miss do cache semântico -> LLM).

    ``endpoint`` None configura o modo "offline" (nenhuma chamada HTTP —
    útil em testes e em operação sem provedor). ``api_key_env`` é o NOME da
    variável de ambiente com a chave (nunca a chave em si — segredos não
    entram no código). ``schema_cls`` é um modelo Pydantic opcional para
    validação estrita da resposta.
    """

    def __init__(
        self,
        endpoint: str | None,
        api_key_env: str | None = None,
        model: str | None = None,
        timeout: float = 30.0,
        schema_cls=None,
    ):
        self.endpoint = endpoint
        self.api_key_env = api_key_env
        self.model = model
        self.timeout = float(timeout)
        self.schema_cls = schema_cls
        self._mode = "offline" if not endpoint else "http"

    @property
    def mode(self) -> str:
        """"offline" (sem endpoint) ou "http" (chamadas REST reais)."""
        return self._mode

    def generate(
        self,
        task: str,
        domain: str,
        validation_schema: dict | None = None,
    ) -> str:
        """Monta o prompt (task + domain + schema) e chama o provedor.

        Retorna o código da chave ``code`` (validado). Qualquer falha
        (offline, rede, HTTP, JSON inválido, validação) levanta LLMError.
        Nunca crasha com entradas malformadas — erro explícito.
        """
        if self.mode == "offline":
            raise LLMError(
                "LLMClient em modo offline: nenhum endpoint configurado"
            )
        if not isinstance(task, str):
            task = "" if task is None else str(task)
        if not isinstance(domain, str):
            domain = "" if domain is None else str(domain)
        try:
            schema = validation_schema or default_validation_schema(domain)
        except ValueError as exc:
            raise LLMError(
                f"schema inválido para o domínio {domain!r}: {exc}"
            ) from exc

        payload: dict = {
            "task": task,
            "domain": domain,
            "validation_schema": schema,
        }
        if self.model:
            payload["model"] = self.model
        corpo = json.dumps(payload, ensure_ascii=False).encode("utf-8")
        cabecalhos = {
            "Content-Type": "application/json",
            "Accept": "application/json",
        }
        if self.api_key_env:
            chave = os.environ.get(self.api_key_env)
            if chave:
                cabecalhos["Authorization"] = f"Bearer {chave}"
            elif self.mode == "http":
                # achado A4: api_key_env definido mas variável ausente no
                # ambiente — warning claro ANTES de prosseguir sem header
                warnings.warn(
                    f"LLMClient: variável de ambiente '{self.api_key_env}' "
                    "não encontrada — prosseguindo sem header Authorization "
                    "(modo http)",
                    stacklevel=2,
                )
        requisicao = urllib.request.Request(
            self.endpoint, data=corpo, headers=cabecalhos, method="POST"
        )
        try:
            with urllib.request.urlopen(requisicao, timeout=self.timeout) as resp:
                bruto = resp.read().decode("utf-8", errors="replace")
        except Exception as exc:  # noqa: BLE001 — rede/HTTP/timeout/URL
            raise LLMError(
                f"falha de rede/HTTP no endpoint {self.endpoint}: {exc}"
            ) from exc
        try:
            dados = json.loads(bruto)
        except (json.JSONDecodeError, TypeError) as exc:
            raise LLMError(f"resposta do LLM não é JSON válido: {exc}") from exc
        try:
            return self._extrair_codigo(dados)
        except ValueError as exc:
            raise LLMError(f"resposta LLM inválida: {exc}") from exc

    # ------------------------------------------------------------- internos
    def _extrair_codigo(self, dados) -> str:
        """Extrai ``code`` com Pydantic (se schema_cls) ou validação nativa."""
        if self.schema_cls is not None:
            modelo = pydantic_validate(dados, self.schema_cls)
            codigo = (
                modelo.get("code")
                if isinstance(modelo, dict)
                else getattr(modelo, "code", None)
            )
            if not isinstance(codigo, str) or not codigo.strip():
                raise ValueError(
                    "campo 'code' ausente/vazio após validação Pydantic"
                )
            return codigo
        return validate_code_response(dados)