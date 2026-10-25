import importlib

import run_log


def _fresh(monkeypatch, **env):
    for k in ("TIGER_DATABASE_URL", "LAMINAR_RUN_ID", "LAMINAR_TIGER"):
        monkeypatch.delenv(k, raising=False)
    for k, v in env.items():
        monkeypatch.setenv(k, v)
    mod = importlib.reload(run_log)
    monkeypatch.setattr(mod, "_load_env_file", lambda: None)
    return mod


def test_no_database_url_means_no_logging(monkeypatch):
    mod = _fresh(monkeypatch)
    mod.started()
    assert mod._run is False


def test_a_nested_run_logs_once(monkeypatch):
    mod = _fresh(monkeypatch, TIGER_DATABASE_URL="postgres://x", LAMINAR_RUN_ID="run-parent")
    mod.started()
    assert mod._run is False


def test_off_switch(monkeypatch):
    mod = _fresh(monkeypatch, TIGER_DATABASE_URL="postgres://x", LAMINAR_TIGER="0")
    mod.started()
    assert mod._run is False


def test_first_failure_is_kept(monkeypatch):
    mod = _fresh(monkeypatch)
    mod.failed("lost feedback from J6")
    mod.failed("later")
    assert mod._failed == "lost feedback from J6"
