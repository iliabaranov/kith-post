from kith.config import SendMode, Settings
from kith.core.reminders import resolve_reminder_config


def test_send_mode_defaults_to_dry_run():
    assert Settings().send_mode == SendMode.dry_run


def test_reminder_defaults_and_nested_env_override(monkeypatch):
    s = Settings()
    assert s.reminders.enabled is True
    assert s.reminders.target == "no-rsvp"
    monkeypatch.setenv("KITH_REMINDERS__ENABLED", "false")
    monkeypatch.setenv("KITH_REMINDERS__MAX_PER_RECIPIENT", "5")
    s2 = Settings()
    assert s2.reminders.enabled is False
    assert s2.reminders.max_per_recipient == 5


def test_resolve_config_uses_settings_as_base():
    # A ReminderSettings instance must work as the base for resolve_reminder_config.
    cfg = resolve_reminder_config(Settings().reminders, {"target": "not-clicked"})
    assert cfg.target == "not-clicked"
    assert cfg.offsets == ("halfway", "7d", "3d")  # coerced to tuple, from the base


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


def test_a_public_url_refuses_the_placeholder_session_key():
    """cp .env.example .env used to hand a public deployment a session key that
    anyone with the repo could forge cookies with."""
    from kith.config import DEV_SECRET_KEY

    s = Settings(base_url="https://example.com", secret_key=DEV_SECRET_KEY,
                 fernet_key="k")
    problems = s.check_production_ready()
    assert any("KITH_SECRET_KEY" in p for p in problems)


def test_a_public_url_wants_a_real_fernet_key():
    s = Settings(base_url="https://example.com", secret_key="a-real-one",
                 fernet_key="")
    assert any("KITH_FERNET_KEY" in p for p in s.check_production_ready())


def test_a_properly_configured_public_deployment_passes():
    s = Settings(base_url="https://example.com", secret_key="a-real-one",
                 fernet_key="also-real")
    assert s.check_production_ready() == []


def test_a_local_http_run_is_allowed_to_be_insecure():
    """The defaults exist so `make dev` just works."""
    assert Settings(base_url="http://localhost:8000").check_production_ready() == []
