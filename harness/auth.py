"""Autenticação por hash — senha personalizada (stdlib only).

Fluxo CLI:
    python -m harness.auth set       # define a senha (getpass interativo)
    python -m harness.auth password  # imprime a senha (consumido pelos .bat)
    python -m harness.auth check     # "configurado" ou "não configurado"

Arquivos gravados:
    config/auth.json      — username + parâmetros scrypt + salt/hash (hex)
    config/secrets.env    — HARNESS_USERNAME / HARNESS_PASSWORD (texto puro,
                            consumido pelos .bat do painel opencode)

A senha NUNCA é impressa na saída padrão pelo fluxo de definição; apenas o
subcomando `password` a imprime (para scripts capturarem via for /f).
"""

from __future__ import annotations

import getpass
import hashlib
import hmac
import json
import secrets
import sys

from . import config

# Parâmetros scrypt (n=2**14=16384, r=8, p=1) — usados para derivar E validar.
# São os MESMOS valores gravados em config/auth.json e relidos na validação.
SCRYPT_N = 2 ** 14
SCRYPT_R = 8
SCRYPT_P = 1

# Senhas rejeitadas por força mínima (case-insensitive).
_SENHAS_FRACAS = ("password", "opencode", "harness", "12345678")

AUTH_FILE = config.ROOT / "config" / "auth.json"
SECRETS_FILE = config.ROOT / "config" / "secrets.env"


def set_senha(senha: str, username: str = "opencode") -> dict:
    """Valida força, deriva scrypt e grava config/auth.json + config/secrets.env.

    Retorna dict com os caminhos gravados e o username. A senha não é
    impressa na saída padrão.
    """
    if len(senha) < 8 or senha.strip().lower() in _SENHAS_FRACAS:
        raise ValueError(
            "senha fraca: mínimo 8 caracteres e não pode ser "
            "'password'/'opencode'/'harness'/'12345678' (case-insensitive)"
        )
    salt = secrets.token_bytes(16)
    derived = hashlib.scrypt(
        senha.encode("utf-8"), salt=salt, n=SCRYPT_N, r=SCRYPT_R, p=SCRYPT_P
    )
    auth = {
        "username": username,
        "scrypt": {
            "n": SCRYPT_N,
            "r": SCRYPT_R,
            "p": SCRYPT_P,
            "salt": salt.hex(),
            "hash": derived.hex(),
        },
    }
    config.ROOT.joinpath("config").mkdir(parents=True, exist_ok=True)
    AUTH_FILE.write_text(
        json.dumps(auth, ensure_ascii=False, indent=2) + "\n",
        encoding="utf-8",
    )
    SECRETS_FILE.write_text(
        f"HARNESS_USERNAME={username}\nHARNESS_PASSWORD={senha}\n",
        encoding="utf-8",
    )
    return {
        "auth_file": str(AUTH_FILE),
        "secrets_file": str(SECRETS_FILE),
        "username": username,
    }


def validate(username: str, senha: str) -> bool:
    """Valida user/senha contra config/auth.json (tempo constante).

    Usa os MESMOS n/r/p/salt gravados no arquivo. Retorna False se o arquivo
    não existir, estiver corrompido ou as credenciais não baterem.
    """
    if not AUTH_FILE.exists():
        return False
    try:
        data = json.loads(AUTH_FILE.read_text(encoding="utf-8"))
        scrypt = data.get("scrypt") or {}
        salt = bytes.fromhex(scrypt.get("salt", ""))
        expected = scrypt.get("hash", "")
        n = int(scrypt.get("n", SCRYPT_N))
        r = int(scrypt.get("r", SCRYPT_R))
        p = int(scrypt.get("p", SCRYPT_P))
    except (OSError, ValueError, TypeError, json.JSONDecodeError):
        return False
    if not hmac.compare_digest(username, str(data.get("username", ""))):
        return False
    try:
        derived = hashlib.scrypt(senha.encode("utf-8"), salt=salt, n=n, r=r, p=p)
    except (ValueError, TypeError):
        return False
    return hmac.compare_digest(derived.hex(), expected)


def has_config() -> bool:
    """True se config/auth.json existe (senha personalizada configurada)."""
    return AUTH_FILE.exists()


def prompt_set_senha() -> bool:
    """Prompt interativo (getpass) para definir a senha do harness.

    Retorna True se definiu com sucesso; False se cancelou/falhou (usuário
    apertou Ctrl+C, senhas não conferem, senha fraca, etc.). Em sucesso grava
    config/auth.json + config/secrets.env via set_senha.
    """
    try:
        senha = getpass.getpass("Senha do harness: ")
        confirma = getpass.getpass("Confirme a senha: ")
    except (KeyboardInterrupt, EOFError):  # F9: EOF (stdin fechado) também
        # não pode derrubar o prompt — trata como cancelamento.
        print("\n[harness] interrompido", file=sys.stderr)
        return False
    if senha != confirma:
        print("[harness] as senhas não conferem", file=sys.stderr)
        return False
    try:
        info = set_senha(senha)
    except ValueError as exc:
        print(f"[harness] {exc}", file=sys.stderr)
        return False
    print(f"[harness] senha configurada para o usuário '{info['username']}'")
    print(f"[harness] arquivos: {info['auth_file']} e {info['secrets_file']}")
    return True


def get_password() -> str | None:
    """Lê HARNESS_PASSWORD de config/secrets.env; None se não existir.

    Usado pelos .bat via `python -m harness.auth password` (for /f).
    """
    if not SECRETS_FILE.exists():
        return None
    try:
        for line in SECRETS_FILE.read_text(encoding="utf-8").splitlines():
            line = line.strip()
            if not line or line.startswith("#") or "=" not in line:
                continue
            chave, _, valor = line.partition("=")
            if chave.strip() == "HARNESS_PASSWORD":
                return valor.strip()
    except OSError:
        return None
    return None


def _cmd_set(args: list[str]) -> int:
    """Define a senha. Interativo via prompt_set_senha (getpass); com
    `--password=<senha>` (e stdin não-TTY, para testes/scripts) usa o valor
    do argumento."""
    senha = None
    for arg in args:
        if arg.startswith("--password="):
            senha = arg.split("=", 1)[1]
            break
    if senha is None and not sys.stdin.isatty():
        print(
            "[harness] stdin não interativo — use: "
            "python -m harness.auth set --password=<senha>",
            file=sys.stderr,
        )
        return 1
    if senha is None:
        if not prompt_set_senha():
            return 1
        return 0
    try:
        info = set_senha(senha)
    except ValueError as exc:
        print(f"[harness] {exc}", file=sys.stderr)
        return 1
    print(f"[harness] senha configurada para o usuário '{info['username']}'")
    print(f"[harness] arquivos: {info['auth_file']} e {info['secrets_file']}")
    return 0


def main(argv: list[str] | None = None) -> int:
    """CLI: `set` | `password` | `check`. Retorna 0 em sucesso, 1 em erro."""
    args = list(argv) if argv is not None else sys.argv[1:]
    try:
        if not args:
            print(
                "[harness] uso: python -m harness.auth {set|password|check}",
                file=sys.stderr,
            )
            return 1
        cmd = args[0]
        if cmd == "set":
            return _cmd_set(args[1:])
        if cmd == "password":
            pwd = get_password()
            if pwd is None:
                print(
                    "[harness] senha não configurada (config/secrets.env)",
                    file=sys.stderr,
                )
                return 1
            print(pwd)
            return 0
        if cmd == "check":
            print("configurado" if has_config() else "não configurado")
            return 0
        print(f"[harness] comando desconhecido: {cmd}", file=sys.stderr)
        return 1
    except (KeyboardInterrupt, EOFError):  # F9: mesmo tratamento do prompt
        print("\n[harness] interrompido", file=sys.stderr)
        return 1


if __name__ == "__main__":
    sys.exit(main())