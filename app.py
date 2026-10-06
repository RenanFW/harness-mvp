"""Ponto de entrada do harness local (portável).

Uso:
    python app.py                # inicia o web shell na porta 8500
    python app.py --port 9000    # porta customizada
    python app.py --host 0.0.0.0 # expõe na rede local
    python app.py --version      # versão
"""

from __future__ import annotations

import argparse
import os
import sys

from harness import DEFAULT_PORT, HarnessServer, __version__, auth, config


def _stdin_interativo() -> bool:
    """True apenas quando há console real no stdin.

    Quirk do Windows (auditoria): com stdin=DEVNULL/nul, `sys.stdin.isatty()`
    retorna True e `getpass.getpass` bloquearia para sempre (hang em
    serviço/CI sem HARNESS_PASSWORD). Aqui, no Windows, confirmamos com
    `GetConsoleMode` via ctypes/msvcrt (stdlib) — falha = não é console;
    em POSIX, `isatty()` é suficiente."""
    if not sys.stdin.isatty():
        return False
    if sys.__stdin__ is None:
        return False
    if os.name == "nt":
        try:
            import ctypes
            import msvcrt
            handle = msvcrt.get_osfhandle(sys.stdin.fileno())
            return bool(ctypes.windll.kernel32.GetConsoleMode(
                handle, ctypes.byref(ctypes.c_ulong())))
        except (OSError, ValueError, AttributeError):
            return False
    return True


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(
        prog="harness",
        description="Harness local: web shell, execução de comandos e memória do Brain. Zero dependências.",
    )
    parser.add_argument("--host", default="127.0.0.1", help="endereço de escuta (padrão: 127.0.0.1)")
    parser.add_argument("--port", type=int, default=DEFAULT_PORT, help=f"porta (padrão: {DEFAULT_PORT})")
    parser.add_argument("--version", action="store_true", help="mostra a versão e sai")
    args = parser.parse_args(argv)

    if args.version:
        print(f"harness {__version__}")
        return 0

    server = HarnessServer(host=args.host, port=args.port)
    if config.AUTH_MODE == "hash":
        # F0: senha personalizada via config/auth.json (scrypt). O username
        # vem do arquivo (fallback: AUTH_USERNAME).
        username = config.AUTH_USERNAME
        try:
            import json as _json
            _data = _json.loads(config.AUTH_HASH_FILE.read_text(encoding="utf-8"))
            username = _data.get("username", username)
        except (OSError, ValueError, _json.JSONDecodeError):
            pass
        print(f"[harness] usando senha personalizada (config/auth.json, usuário: {username})")
    elif config.AUTH_MODE == "env":
        print(f"[harness] usando HARNESS_PASSWORD da variável de ambiente (usuário: {config.AUTH_USERNAME})")
    else:
        # AUTH_MODE == "padrao": credenciais de fábrica opencode/opencode.
        print("[harness] credenciais padrão opencode/opencode ativas (primeira execução)")
        print("[harness] recomendado definir uma senha personalizada")
        if _stdin_interativo():
            print("[harness] definindo senha agora...")
            if auth.prompt_set_senha():
                config.AUTH_MODE = "hash"  # config/auth.json foi criado
                print("[harness] senha configurada com sucesso (modo hash)")
            else:
                print("[harness] continuando com o padrão opencode/opencode")
                print("[harness] para definir depois: python -m harness.auth set")
        else:
            print("[harness] sem terminal para prompt (execução não interativa)")
            print("[harness] para definir senha: python -m harness.auth set")
    if config.HARNESS_PUBLIC:
        print("[harness] modo PÚBLICO (HARNESS_PUBLIC=1): exige HTTPS via Tailscale e sandbox Docker/Podman nas execuções")
    try:
        server.serve_forever()
        if server._httpd_thread is not None:
            # M3: aguarda a thread do servidor (sem busy-wait de 3600s);
            # Ctrl+C interrompe o join e cai no except abaixo.
            server._httpd_thread.join()
    except KeyboardInterrupt:
        print("\n[harness] encerrando...")
        server.shutdown()
    return 0


if __name__ == "__main__":
    sys.exit(main())