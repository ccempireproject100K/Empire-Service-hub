"""Fail-closed production guards (2026-11-05 gap-audit hardening).

Two loaded defaults used to sit on main: ``HUB_JWT_SECRET`` fell back to
the public ``demo-secret-change-me``, and ``scripts/seed_demo.py`` seeded
``admin@demo.local`` / ``demo-admin-pass``. Both were safe only because
the hub was never deployed. With ``HUB_ENV=production`` the app now
refuses to boot on the demo secret and the seed script refuses the demo
credentials; every other mode keeps the old behavior (plus a loud
warning at boot).

Config functions read the environment live, so the matrix is unit-tested
with monkeypatched env. The actual boot/seed refusals are additionally
proven end-to-end in fresh subprocesses, the way an operator hits them.
"""
import importlib.util
import os
import subprocess
import sys

import pytest

sys.path.insert(0, os.path.join(os.path.dirname(__file__), ".."))

from lib import config  # noqa: E402

BACKEND_DIR = os.path.abspath(os.path.join(os.path.dirname(__file__), ".."))
SEED_SCRIPT = os.path.join(BACKEND_DIR, "scripts", "seed_demo.py")
REAL_SECRET = "test-only-real-secret-0123456789abcdef"


def _load_seed_module():
    spec = importlib.util.spec_from_file_location("seed_demo", SEED_SCRIPT)
    module = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(module)
    return module


def _clean_env(**overrides):
    """os.environ minus every var under test, with overrides applied."""
    env = dict(os.environ)
    for var in ("HUB_ENV", "HUB_JWT_SECRET", "HUB_ADMIN_EMAIL",
                "HUB_ADMIN_PASSWORD", "HUB_SCHEDULER"):
        env.pop(var, None)
    env.update(overrides)
    return env


def _run_python(code, env):
    return subprocess.run(
        [sys.executable, "-c", code],
        cwd=BACKEND_DIR, env=env, capture_output=True, text=True, timeout=120,
    )


# ------------------------------------------------------------- config matrix
def test_is_production_parsing(monkeypatch):
    for value in ("production", "PRODUCTION", " Production "):
        monkeypatch.setenv("HUB_ENV", value)
        assert config.is_production() is True
    for value in ("development", "test", "prod", "staging", ""):
        monkeypatch.setenv("HUB_ENV", value)
        assert config.is_production() is False
    monkeypatch.delenv("HUB_ENV")
    assert config.is_production() is False


def test_production_refuses_unset_secret(monkeypatch):
    monkeypatch.setenv("HUB_ENV", "production")
    monkeypatch.delenv("HUB_JWT_SECRET", raising=False)
    with pytest.raises(RuntimeError, match="HUB_JWT_SECRET"):
        config.validate_runtime_config()


def test_production_refuses_demo_default_secret(monkeypatch):
    monkeypatch.setenv("HUB_ENV", "production")
    monkeypatch.setenv("HUB_JWT_SECRET", "demo-secret-change-me")
    with pytest.raises(RuntimeError, match="HUB_JWT_SECRET"):
        config.validate_runtime_config()


def test_production_accepts_real_secret(monkeypatch):
    monkeypatch.setenv("HUB_ENV", "production")
    monkeypatch.setenv("HUB_JWT_SECRET", REAL_SECRET)
    config.validate_runtime_config()  # must not raise
    assert config.jwt_secret_is_demo_default() is False


def test_nonproduction_allows_unset_and_default_secret(monkeypatch):
    for env_value in (None, "development", "test"):
        if env_value is None:
            monkeypatch.delenv("HUB_ENV", raising=False)
        else:
            monkeypatch.setenv("HUB_ENV", env_value)
        monkeypatch.delenv("HUB_JWT_SECRET", raising=False)
        config.validate_runtime_config()  # unset -> demo default, still fine
        monkeypatch.setenv("HUB_JWT_SECRET", "demo-secret-change-me")
        config.validate_runtime_config()  # explicit default, still fine
        assert config.jwt_secret_is_demo_default() is True


# ------------------------------------------------------------ server boot
def test_server_refuses_to_boot_in_production_without_secret():
    proc = _run_python("import server", _clean_env(HUB_ENV="production"))
    assert proc.returncode != 0
    assert "HUB_JWT_SECRET" in proc.stderr


def test_server_refuses_to_boot_in_production_with_demo_secret():
    proc = _run_python(
        "import server",
        _clean_env(HUB_ENV="production", HUB_JWT_SECRET="demo-secret-change-me"),
    )
    assert proc.returncode != 0
    assert "HUB_JWT_SECRET" in proc.stderr


def test_server_boots_in_production_with_real_secret():
    proc = _run_python(
        "import server; print('boot-ok')",
        _clean_env(HUB_ENV="production", HUB_JWT_SECRET=REAL_SECRET),
    )
    assert proc.returncode == 0, proc.stderr
    assert "boot-ok" in proc.stdout


def test_server_boots_in_development_without_secret_and_warns_loudly():
    proc = _run_python("import server; print('boot-ok')", _clean_env())
    assert proc.returncode == 0, proc.stderr
    assert "boot-ok" in proc.stdout
    # The unmissable warning: the demo default is named in the log output.
    assert "demo-secret-change-me" in proc.stderr
    assert "WARNING" in proc.stderr


# -------------------------------------------------------------- seed guard
def test_seed_refuses_in_production_without_credentials(monkeypatch):
    monkeypatch.setenv("HUB_ENV", "production")
    monkeypatch.delenv("HUB_ADMIN_EMAIL", raising=False)
    monkeypatch.delenv("HUB_ADMIN_PASSWORD", raising=False)
    seed = _load_seed_module()
    with pytest.raises(SystemExit) as excinfo:
        seed.assert_seed_credentials_safe()
    assert "HUB_ADMIN_EMAIL" in str(excinfo.value)
    assert "HUB_ADMIN_PASSWORD" in str(excinfo.value)


def test_seed_refuses_in_production_with_only_email_set(monkeypatch):
    monkeypatch.setenv("HUB_ENV", "production")
    monkeypatch.setenv("HUB_ADMIN_EMAIL", "owner@example.com")
    monkeypatch.delenv("HUB_ADMIN_PASSWORD", raising=False)
    seed = _load_seed_module()
    with pytest.raises(SystemExit) as excinfo:
        seed.assert_seed_credentials_safe()
    assert "HUB_ADMIN_PASSWORD" in str(excinfo.value)
    assert "HUB_ADMIN_EMAIL" not in str(excinfo.value)


def test_seed_refuses_in_production_with_demo_values_set_explicitly(monkeypatch):
    monkeypatch.setenv("HUB_ENV", "production")
    monkeypatch.setenv("HUB_ADMIN_EMAIL", "admin@demo.local")
    monkeypatch.setenv("HUB_ADMIN_PASSWORD", "demo-admin-pass")
    seed = _load_seed_module()
    with pytest.raises(SystemExit) as excinfo:
        seed.assert_seed_credentials_safe()
    assert "HUB_ADMIN_EMAIL" in str(excinfo.value)
    assert "HUB_ADMIN_PASSWORD" in str(excinfo.value)


def test_seed_allows_production_with_real_credentials(monkeypatch):
    monkeypatch.setenv("HUB_ENV", "production")
    monkeypatch.setenv("HUB_ADMIN_EMAIL", "owner@example.com")
    monkeypatch.setenv("HUB_ADMIN_PASSWORD", "a-real-password-not-the-demo-one")
    seed = _load_seed_module()
    seed.assert_seed_credentials_safe()  # must not raise


def test_seed_allows_demo_mode_with_defaults(monkeypatch):
    monkeypatch.delenv("HUB_ENV", raising=False)
    monkeypatch.delenv("HUB_ADMIN_EMAIL", raising=False)
    monkeypatch.delenv("HUB_ADMIN_PASSWORD", raising=False)
    seed = _load_seed_module()
    seed.assert_seed_credentials_safe()  # must not raise
    assert seed.ADMIN_EMAIL == "admin@demo.local"
    assert seed.ADMIN_PASSWORD == "demo-admin-pass"


def test_seed_script_process_refuses_in_production_before_touching_db():
    # MONGO_URL points at a port where nothing listens: if the guard did
    # not fire first, the script would die in a Mongo connection error
    # instead of with the refusal message.
    env = _clean_env(HUB_ENV="production",
                     MONGO_URL="mongodb://127.0.0.1:1")
    proc = subprocess.run(
        [sys.executable, SEED_SCRIPT],
        cwd=BACKEND_DIR, env=env, capture_output=True, text=True, timeout=120,
    )
    assert proc.returncode != 0
    combined = proc.stdout + proc.stderr
    assert "Refusing to seed in production" in combined
    assert "HUB_ADMIN_PASSWORD" in combined
