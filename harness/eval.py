"""Avaliação automática de saídas (Ch19).

Valida um texto (saída de um job ou entrega de um agente) contra uma lista de
critérios declarativos e responde com status claro: APROVADA / BLOQUEADA.

Critérios suportados (formato: lista de dicts):
  {"type": "contains",      "value": "..."}      texto contém o valor
  {"type": "not_contains",  "value": "..."}      texto NÃO contém o valor
  {"type": "regex",         "pattern": "..."}    texto casa com a regex
  {"type": "min_length",    "value": 10}         tamanho mínimo de texto
  {"type": "max_length",    "value": 100}        tamanho máximo de texto
  {"type": "json_valid",    "value": true}       texto é JSON válido
  {"type": "exit_zero",     "value": true}       exige exit_code == 0

Cada critério pode ter uma `message` opcional para o relatório.
"""

from __future__ import annotations

import json
import re
from typing import Any


def evaluate(text: str, criteria: list[dict], exit_code: int | None = None) -> dict:
    """Aplica os critérios ao texto e retorna {status, checks, passed}."""
    checks = []
    for rule in criteria:
        checks.append(_check(rule, text, exit_code))

    passed = sum(1 for c in checks if c["passed"])
    status = "APROVADA" if passed == len(checks) and checks else "BLOQUEADA"
    return {
        "status": status,
        "passed": passed,
        "total": len(checks),
        "checks": checks,
    }


def _check(rule: dict, text: str, exit_code: int | None) -> dict:
    name = rule.get("name") or rule.get("type", "check")
    message = rule.get("message", "")
    rule_type = rule.get("type", "")
    try:
        ok, detail = _apply(rule_type, rule, text, exit_code)
    except Exception as exc:  # noqa: BLE001 — regra malformada não derruba a avaliação
        ok, detail = False, f"erro na regra: {exc}"
    detail = message or detail
    return {"name": name, "type": rule_type, "passed": ok, "detail": detail}


def _apply(rule_type: str, rule: dict, text: str, exit_code: int | None) -> tuple[bool, str]:
    value = rule.get("value")
    if rule_type == "contains":
        return value in text, f"contém '{value}'"
    if rule_type == "not_contains":
        return value not in text, f"não contém '{value}'"
    if rule_type == "regex":
        return bool(re.search(rule["pattern"], text)), f"casa regex {rule['pattern']}"
    if rule_type == "min_length":
        return len(text) >= value, f"tamanho {len(text)} >= {value}"
    if rule_type == "max_length":
        return len(text) <= value, f"tamanho {len(text)} <= {value}"
    if rule_type == "json_valid":
        ok = _is_json(text)
        return ok, "JSON válido" if ok else "não é JSON válido"
    if rule_type == "exit_zero":
        ok = exit_code == 0
        return ok, f"exit_code={exit_code}" if ok else f"exit_code={exit_code} (esperado 0)"
    raise ValueError(f"tipo de critério desconhecido: {rule_type}")


def _is_json(text: str) -> bool:
    try:
        json.loads(text)
        return True
    except (json.JSONDecodeError, TypeError):
        return False


def default_rules() -> list[dict[str, Any]]:
    """Critérios padrão para uma entrega mínima aceitável."""
    return [
        {"type": "min_length", "value": 1, "name": "saída não vazia",
         "message": "a saída não pode estar vazia"},
        {"type": "exit_zero", "value": True, "name": "exit 0",
         "message": "o comando deve terminar com exit 0"},
    ]