"""
Tests for environment-variable parsing.

The readers are exercised directly rather than through module-level constants,
which are resolved once at import time.
"""

from cryptoviz.config import _env_bool, _env_int, _env_str


def test_env_bool_truthy_spellings(monkeypatch):
    """The accepted truthy spellings all resolve to True."""
    for value in ("1", "true", "TRUE", "yes", "on", " true "):
        monkeypatch.setenv("CRYPTOVIZ_TEST_FLAG", value)
        assert _env_bool("CRYPTOVIZ_TEST_FLAG") is True


def test_env_bool_falsy_spellings(monkeypatch):
    """Anything else is False."""
    for value in ("0", "false", "no", "off", "", "maybe"):
        monkeypatch.setenv("CRYPTOVIZ_TEST_FLAG", value)
        assert _env_bool("CRYPTOVIZ_TEST_FLAG") is False


def test_env_bool_default_when_unset(monkeypatch):
    """An unset variable uses the supplied default."""
    monkeypatch.delenv("CRYPTOVIZ_TEST_FLAG", raising=False)

    assert _env_bool("CRYPTOVIZ_TEST_FLAG", True) is True
    assert _env_bool("CRYPTOVIZ_TEST_FLAG", False) is False


def test_env_int_parses_and_falls_back(monkeypatch):
    """Valid integers parse; anything else uses the default."""
    monkeypatch.setenv("CRYPTOVIZ_TEST_INT", "42")
    assert _env_int("CRYPTOVIZ_TEST_INT", 7) == 42

    for bad in ("", "   ", "abc", "1.5"):
        monkeypatch.setenv("CRYPTOVIZ_TEST_INT", bad)
        assert _env_int("CRYPTOVIZ_TEST_INT", 7) == 7


def test_env_str_treats_blank_as_unset(monkeypatch):
    """
    A present-but-empty variable falls back to the default.

    This is the shape .env.example ships (`CRYPTOVIZ_DB_PATH=`), and without it
    an empty database path would resolve to the current directory instead of
    the intended default file.
    """
    monkeypatch.setenv("CRYPTOVIZ_TEST_STR", "")
    assert _env_str("CRYPTOVIZ_TEST_STR", "fallback") == "fallback"

    monkeypatch.setenv("CRYPTOVIZ_TEST_STR", "   ")
    assert _env_str("CRYPTOVIZ_TEST_STR", "fallback") == "fallback"

    monkeypatch.delenv("CRYPTOVIZ_TEST_STR", raising=False)
    assert _env_str("CRYPTOVIZ_TEST_STR", "fallback") == "fallback"


def test_env_str_strips_surrounding_whitespace(monkeypatch):
    """A set value comes back trimmed."""
    monkeypatch.setenv("CRYPTOVIZ_TEST_STR", "  /var/lib/cryptoviz/crypto.db  ")

    assert _env_str("CRYPTOVIZ_TEST_STR", "x") == "/var/lib/cryptoviz/crypto.db"


def test_database_path_is_absolute():
    """The resolved database path is absolute, so it does not depend on cwd."""
    from cryptoviz import config

    assert config.DB_PATH.is_absolute()


def test_env_example_documents_every_variable():
    """
    Every variable config.py reads appears in .env.example.

    Keeps the sample file from drifting out of date as options are added.
    """
    import re

    from cryptoviz import config

    source = (config.PACKAGE_ROOT / "config.py").read_text(encoding="utf-8")
    referenced = set(re.findall(r'_env_\w+\(\s*"([A-Z_]+)"', source))
    referenced |= set(re.findall(r'os\.getenv\(\s*"([A-Z_]+)"', source))

    documented = (config.PROJECT_ROOT / ".env.example").read_text(encoding="utf-8")

    missing = {name for name in referenced if name not in documented}
    assert not missing, f"Undocumented in .env.example: {sorted(missing)}"
