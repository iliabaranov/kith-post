from kith.config import SendMode, Settings


def test_send_mode_defaults_to_dry_run():
    assert Settings().send_mode == SendMode.dry_run


def test_env_overrides_send_mode(monkeypatch):
    monkeypatch.setenv("KITH_SEND_MODE", "live")
    assert Settings().send_mode == SendMode.live


def test_google_configured_flag():
    assert Settings().google_configured is False
    s = Settings(google_client_id="cid", google_client_secret="secret")
    assert s.google_configured is True


def test_https_only_follows_base_url():
    assert Settings(base_url="http://localhost:8000").https_only is False
    assert Settings(base_url="https://x.ts.net").https_only is True


def test_derived_paths_live_under_data_dir():
    s = Settings(data_dir="/tmp/kith-x")
    assert str(s.db_path).startswith("/tmp/kith-x")
    assert str(s.outbox_dir).endswith("outbox")
