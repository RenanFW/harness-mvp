"""Testes do módulo de autenticação por hash (harness/auth.py) — F0/F1.

Cobre:
  - set_senha: força mínima rejeita fracas, aceita forte, grava os arquivos;
  - validate: correta True, errada False, username errado False, fail-closed
    quando não configurado (arquivo ausente/corrompido);
  - prompt_set_senha: getpass mockado — sucesso (True + arquivos), senhas não
    conferem (False), senha fraca (False) e Ctrl+C (False);
  - salt aleatório: duas chamadas geram salts diferentes;
  - get_password: lê HARNESS_PASSWORD de config/secrets.env (None se ausente);
  - config/secrets.env contém a senha após set_senha.

Isolamento: cada caso roda em TemporaryDirectory com monkeypatch de
auth.AUTH_FILE, auth.SECRETS_FILE e config.ROOT (mesmo padrão de
engine_test/executor_test) — o config/auth.json e config/secrets.env REAIS do
projeto NUNCA são tocados.

Rode com:
    python tests/auth_test.py
"""

from __future__ import annotations

import contextlib
import json
import pathlib
import sys
import tempfile
import unittest.mock as mock

sys.path.insert(0, str(pathlib.Path(__file__).resolve().parent.parent))

from harness import auth, config  # noqa: E402


@contextlib.contextmanager
def _auth_isolado():
    """Isola o auth num TemporaryDirectory: patch de auth.AUTH_FILE,
    auth.SECRETS_FILE e config.ROOT. O projeto real não é tocado."""
    with tempfile.TemporaryDirectory(prefix="auth_test_") as tmp:
        root = pathlib.Path(tmp)
        with mock.patch.object(config, "ROOT", root), \
                mock.patch.object(auth, "AUTH_FILE", root / "config" / "auth.json"), \
                mock.patch.object(auth, "SECRETS_FILE", root / "config" / "secrets.env"):
            yield root


def main_test() -> int:
    passed = 0
    failed = 0

    def check(name, cond):
        nonlocal passed, failed
        if cond:
            passed += 1
            print(f"  [PASS] {name}")
        else:
            failed += 1
            print(f"  [FAIL] {name}")

    # ------------------------------------------------------------- set_senha
    with _auth_isolado():
        # força mínima: curta demais
        for fraca in ("curta", "1234567"):
            try:
                auth.set_senha(fraca)
                check(f"set_senha rejeita senha fraca ({fraca!r})", False)
            except ValueError:
                check(f"set_senha rejeita senha fraca ({fraca!r})", True)
        # força mínima: senhas proibidas (case-insensitive)
        for fraca in ("password", "opencode", "harness", "12345678", "PASSWORD"):
            try:
                auth.set_senha(fraca)
                check(f"set_senha rejeita proibida ({fraca!r})", False)
            except ValueError:
                check(f"set_senha rejeita proibida ({fraca!r})", True)

        # aceita forte e grava os arquivos
        info = auth.set_senha("Senha-Forte-2026!", username="deploy")
        check("set_senha aceita forte + retorna caminhos",
              info["username"] == "deploy"
              and info["auth_file"].endswith("auth.json")
              and info["secrets_file"].endswith("secrets.env"))
        check("set_senha grava config/auth.json",
              auth.AUTH_FILE.is_file() and pathlib.Path(info["auth_file"]).is_file())
        check("set_senha grava config/secrets.env",
              auth.SECRETS_FILE.is_file() and pathlib.Path(info["secrets_file"]).is_file())

        # auth.json: username + scrypt (n, r, p, salt, hash)
        data = json.loads(auth.AUTH_FILE.read_text(encoding="utf-8"))
        scrypt = data.get("scrypt") or {}
        check("auth.json tem username e parametros scrypt",
              data.get("username") == "deploy"
              and scrypt.get("n") == auth.SCRYPT_N
              and scrypt.get("r") == auth.SCRYPT_R
              and scrypt.get("p") == auth.SCRYPT_P
              and bool(scrypt.get("salt"))
              and bool(scrypt.get("hash")))

        # config/secrets.env contém a senha (consumido pelos .bat)
        secrets = auth.SECRETS_FILE.read_text(encoding="utf-8")
        check("config/secrets.env tem HARNESS_USERNAME/HARNESS_PASSWORD",
              "HARNESS_USERNAME=deploy" in secrets
              and "HARNESS_PASSWORD=Senha-Forte-2026!" in secrets)

        # get_password lê a senha de secrets.env
        check("get_password retorna a senha personalizada",
              auth.get_password() == "Senha-Forte-2026!")

        # has_config True após set
        check("has_config True após set_senha", auth.has_config() is True)

    # ------------------------------------------------------------- validate
    with _auth_isolado() as root:
        # fail-closed: sem arquivo configurado -> False (nunca True)
        check("validate fail-closed sem configuracao",
              auth.validate("opencode", "qualquer") is False)
        auth.set_senha("Senha-Forte-2026!", username="opencode")
        check("validate correta True",
              auth.validate("opencode", "Senha-Forte-2026!") is True)
        check("validate senha errada False",
              auth.validate("opencode", "senha-errada") is False)
        check("validate username errado False",
              auth.validate("outro-user", "Senha-Forte-2026!") is False)
        check("validate senha vazia False",
              auth.validate("opencode", "") is False)

        # arquivo corrompido -> fail-closed False
        auth.AUTH_FILE.write_text("{corrompido", encoding="utf-8")
        check("validate arquivo corrompido -> False (fail-closed)",
              auth.validate("opencode", "Senha-Forte-2026!") is False)

    # ------------------------------------------------------ salt aleatório
    with _auth_isolado() as root:
        auth.set_senha("Primeira-Senha-1!")
        d1 = json.loads(auth.AUTH_FILE.read_text(encoding="utf-8"))
        auth.set_senha("Segunda-Senha-2!")
        d2 = json.loads(auth.AUTH_FILE.read_text(encoding="utf-8"))
        check("salt aleatorio (duas chamadas -> salts diferentes)",
              d1["scrypt"]["salt"] != d2["scrypt"]["salt"])

    # ------------------------------------------------------ prompt_set_senha
    with _auth_isolado():
        # sucesso: getpass devolve senha forte nas duas chamadas -> True
        # e grava os arquivos (fluxo da PRIMEIRA execução do app.py)
        with mock.patch.object(auth.getpass, "getpass",
                               return_value="Senha-Prompt-2026!"):
            ok = auth.prompt_set_senha()
        check("prompt_set_senha sucesso -> True", ok is True)
        check("prompt_set_senha grava auth.json + secrets.env",
              auth.AUTH_FILE.is_file() and auth.SECRETS_FILE.is_file())
        check("prompt_set_senha valida a senha gravada",
              auth.validate("opencode", "Senha-Prompt-2026!") is True)

    with _auth_isolado():
        # senhas não conferem -> False (nada gravado)
        with mock.patch.object(auth.getpass, "getpass",
                               side_effect=["abc-123-xyz", "outra-senha"]):
            ok = auth.prompt_set_senha()
        check("prompt_set_senha senhas não conferem -> False", ok is False)
        check("prompt_set_senha falha não grava arquivos",
              not auth.AUTH_FILE.exists() and not auth.SECRETS_FILE.exists())

    with _auth_isolado():
        # senha fraca (padrão proibido) -> False
        with mock.patch.object(auth.getpass, "getpass",
                               return_value="opencode"):
            ok = auth.prompt_set_senha()
        check("prompt_set_senha senha fraca -> False", ok is False)

    with _auth_isolado():
        # usuário apertou Ctrl+C -> False
        with mock.patch.object(auth.getpass, "getpass",
                               side_effect=KeyboardInterrupt):
            ok = auth.prompt_set_senha()
        check("prompt_set_senha Ctrl+C -> False", ok is False)

    # -------------------------------------------------------- get_password
    with _auth_isolado() as root:
        check("get_password None quando secrets.env ausente",
              auth.get_password() is None)
        (root / "config").mkdir(parents=True, exist_ok=True)
        (root / "config" / "secrets.env").write_text(
            "# comentario\nHARNESS_USERNAME=opencode\nHARNESS_PASSWORD=abc-123\n",
            encoding="utf-8",
        )
        check("get_password parseia secrets.env com comentario",
              auth.get_password() == "abc-123")

    print(f"\nRESULTADO (auth): {passed} passaram, {failed} falharam")
    return 1 if failed else 0


if __name__ == "__main__":
    sys.exit(main_test())