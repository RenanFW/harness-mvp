"""Abstração AST: literais -> placeholders {{nome}} (Etapa 1).

Converte código Python em um template com placeholders para literais
(str/int/float/bool/None). Código inválido vira {"ast_valid": False,
"template": <original>, "placeholders": {}}. A interpolação substitui
{{nome}} pelos valores fornecidos; variável ausente -> ValueError.
"""

from __future__ import annotations

import ast

# Mapeamento tipo Python -> rótulo do placeholder
_TIPOS = {str: "str", int: "int", float: "float", bool: "bool", type(None): "none"}


def abstract_to_template(code: str) -> dict:
    """Extrai o template AST do código.

    Retorna {"template": str, "placeholders": {nome: tipo}, "ast_valid": bool}.
    Literais (str/int/float/bool/None) viram placeholders únicos {{tipo_n}}.
    Literais dentro de f-strings (JoinedStr) NÃO são abstraídos (quebrariam a
    sintaxe do template após interpolação). Código inválido -> ast_valid False
    com o código original intacto. Nunca crasha com entradas malformadas.
    """
    if not isinstance(code, str):
        code = "" if code is None else str(code)
    try:
        tree = ast.parse(code)
    except SyntaxError:
        return {"template": code, "placeholders": {}, "ast_valid": False}

    # mapa de pais para detectar literais dentro de f-strings
    pais: dict[ast.AST, ast.AST] = {}
    for node in ast.walk(tree):
        for filho in ast.iter_child_nodes(node):
            pais[filho] = node

    literais: list[tuple[ast.Constant, str, str]] = []
    contadores: dict[str, int] = {}
    for node in ast.walk(tree):
        if not isinstance(node, ast.Constant):
            continue
        valor = node.value
        if not (valor is None or isinstance(valor, (str, int, float, bool))):
            continue
        if _dentro_de_fstring(node, pais):
            continue
        if not all(
            hasattr(node, a)
            for a in ("lineno", "col_offset", "end_lineno", "end_col_offset")
        ):
            continue  # nós sintéticos sem posição: ignora
        tipo = _TIPOS[type(valor)]
        contadores[tipo] = contadores.get(tipo, 0) + 1
        nome = f"{tipo}_{contadores[tipo]}"
        literais.append((node, nome, tipo))

    template = _substituir(code, literais)
    placeholders = {nome: tipo for _, nome, tipo in literais}
    return {"template": template, "placeholders": placeholders, "ast_valid": True}


def interpolate_template(template: dict, valores: dict) -> str:
    """Substitui os placeholders {{nome}} pelos valores fornecidos.

    - str -> repr(valor) (preserva as aspas do literal original);
    - bool -> True/False; None -> None; números -> str(valor);
    - variável ausente para um placeholder -> ValueError;
    - template com ast_valid False -> retorna o código original intacto.
    """
    if not isinstance(template, dict) or "template" not in template:
        raise ValueError("template inválido: esperado dict com chave 'template'")
    if template.get("ast_valid") is False:
        return template["template"]
    placeholders = template.get("placeholders", {})
    texto = template["template"]
    if valores is None:
        valores = {}
    # do nome mais longo para o mais curto (evita colisão de prefixos)
    for nome in sorted(placeholders, key=len, reverse=True):
        if nome not in valores:
            raise ValueError(f"variável ausente para placeholder: {nome}")
        texto = texto.replace("{{" + nome + "}}", _formatar(valores[nome]))
    return texto


# ------------------------------------------------------------------ helpers
def _dentro_de_fstring(node: ast.AST, pais: dict) -> bool:
    """True se o nó está dentro de uma JoinedStr (f-string)."""
    atual = pais.get(node)
    while atual is not None:
        if isinstance(atual, ast.JoinedStr):
            return True
        atual = pais.get(atual)
    return False


def _substituir(code: str, literais: list[tuple[ast.Constant, str, str]]) -> str:
    """Troca cada literal pelo placeholder {{nome}} no fonte original.

    Substitui do fim para o início (posições posteriores primeiro), então os
    offsets de linha permanecem válidos durante a reconstrução.
    """
    if not literais:
        return code
    offsets: list[int] = []
    pos = 0
    for linha in code.splitlines(keepends=True):
        offsets.append(pos)
        pos += len(linha)

    ordenados = sorted(
        literais, key=lambda t: (t[0].lineno, t[0].col_offset), reverse=True
    )
    for node, nome, _ in ordenados:
        start = offsets[node.lineno - 1] + node.col_offset
        end = offsets[node.end_lineno - 1] + node.end_col_offset
        code = code[:start] + "{{" + nome + "}}" + code[end:]
    return code


def _formatar(valor) -> str:
    """Formata o valor para re-inserção no código-fonte."""
    if isinstance(valor, str):
        return repr(valor)
    if isinstance(valor, bool):
        return "True" if valor else "False"
    if valor is None:
        return "None"
    return str(valor)