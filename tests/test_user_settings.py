import json

from web.user_settings import DEFAULT_PROXY, UserSettingsStore


class FakeCredentialStore:
    def __init__(self):
        self.secret = ""

    def read(self) -> str:
        return self.secret

    def write(self, secret: str) -> None:
        self.secret = secret

    def delete(self) -> None:
        self.secret = ""


class FailingCredentialStore:
    def read(self) -> str:
        raise OSError("credential backend unavailable")

    def write(self, secret: str) -> None:
        raise OSError("credential backend unavailable")

    def delete(self) -> None:
        raise OSError("credential backend unavailable")


class ReadFailingCredentialStore(FakeCredentialStore):
    def read(self) -> str:
        raise OSError("credential read unavailable")


class WriteFailingCredentialStore(FakeCredentialStore):
    def write(self, secret: str) -> None:
        raise OSError("credential write unavailable")


def test_first_read_has_fixed_proxy_and_no_model_or_endpoint(tmp_path):
    store = UserSettingsStore(
        tmp_path / "settings.json", credential_store=FakeCredentialStore()
    )

    public = store.get_public_settings()

    assert public == {
        "version": 1,
        "proxy": DEFAULT_PROXY,
        "base_url": "",
        "model": "",
        "round2_model": "",
        "batch_size": 28,
        "local_whisper": False,
        "api_key_configured": False,
        "api_key_persistence": "none",
        "credential_warning": "",
    }


def test_non_secret_settings_survive_a_new_store_instance(tmp_path):
    path = tmp_path / "settings.json"
    credentials = FakeCredentialStore()
    first = UserSettingsStore(path, credential_store=credentials)

    first.update({
        "proxy": "http://127.0.0.1:9000",
        "base_url": "https://api.example.test/v1",
        "model": "chosen-model",
        "round2_model": "review-model",
        "batch_size": 12,
        "local_whisper": True,
    })

    second = UserSettingsStore(path, credential_store=credentials)
    public = second.get_public_settings()
    assert public["proxy"] == "http://127.0.0.1:9000"
    assert public["base_url"] == "https://api.example.test/v1"
    assert public["model"] == "chosen-model"
    assert public["round2_model"] == "review-model"
    assert public["batch_size"] == 12
    assert public["local_whisper"] is True


def test_unknown_fields_are_not_written_and_corrupt_json_falls_back(tmp_path):
    path = tmp_path / "settings.json"
    store = UserSettingsStore(path, credential_store=FakeCredentialStore())
    store.update({"model": "kept-model", "workflow_mode": "quality"})

    stored = json.loads(path.read_text(encoding="utf-8"))
    assert stored["model"] == "kept-model"
    assert "workflow_mode" not in stored

    path.write_text("{not valid json", encoding="utf-8")
    recovered = UserSettingsStore(
        path, credential_store=FakeCredentialStore()
    ).get_public_settings()
    assert recovered["proxy"] == DEFAULT_PROXY
    assert recovered["model"] == ""


def test_api_key_uses_credential_store_and_never_enters_public_json(tmp_path):
    path = tmp_path / "settings.json"
    credentials = FakeCredentialStore()
    first = UserSettingsStore(path, credential_store=credentials)

    public = first.update({
        "model": "chosen-model",
        "api_key": "super-secret-value",
    })

    assert public["api_key_configured"] is True
    assert public["api_key_persistence"] == "secure"
    assert "super-secret-value" not in json.dumps(public)
    assert "super-secret-value" not in path.read_text(encoding="utf-8")
    second = UserSettingsStore(path, credential_store=credentials)
    assert second.get_public_settings()["api_key_configured"] is True
    assert second.resolve_api_key() == "super-secret-value"


def test_updating_other_fields_does_not_delete_saved_api_key(tmp_path):
    credentials = FakeCredentialStore()
    store = UserSettingsStore(
        tmp_path / "settings.json", credential_store=credentials
    )
    store.update({"api_key": "super-secret-value"})

    store.update({"model": "new-model", "api_key": ""})

    assert store.resolve_api_key() == "super-secret-value"
    assert store.get_public_settings()["api_key_configured"] is True


def test_delete_api_key_removes_secure_credential(tmp_path):
    credentials = FakeCredentialStore()
    store = UserSettingsStore(
        tmp_path / "settings.json", credential_store=credentials
    )
    store.update({"api_key": "super-secret-value"})

    public = store.delete_api_key()

    assert public["api_key_configured"] is False
    assert public["api_key_persistence"] == "none"
    assert store.resolve_api_key() == ""


def test_unavailable_secure_store_keeps_key_in_session_only(tmp_path):
    path = tmp_path / "settings.json"
    store = UserSettingsStore(path, credential_store=FailingCredentialStore())

    public = store.update({
        "model": "chosen-model",
        "api_key": "session-only-secret",
    })

    assert public["api_key_configured"] is True
    assert public["api_key_persistence"] == "session"
    assert "仅本次会话" in public["credential_warning"]
    assert store.resolve_api_key() == "session-only-secret"
    assert "session-only-secret" not in path.read_text(encoding="utf-8")
    restarted = UserSettingsStore(path, credential_store=FailingCredentialStore())
    assert restarted.get_public_settings()["api_key_configured"] is False
    assert restarted.resolve_api_key() == ""


def test_session_only_key_can_be_forgotten_when_secure_store_is_unavailable(tmp_path):
    store = UserSettingsStore(
        tmp_path / "settings.json", credential_store=FailingCredentialStore()
    )
    store.update({"api_key": "session-only-secret"})

    public = store.delete_api_key()

    assert public["api_key_configured"] is False
    assert public["api_key_persistence"] == "none"
    assert store.resolve_api_key() == ""


def test_secure_store_read_failure_is_exposed_without_returning_a_secret(tmp_path):
    store = UserSettingsStore(
        tmp_path / "settings.json",
        credential_store=ReadFailingCredentialStore(),
    )

    public = store.get_public_settings()

    assert public["api_key_configured"] is False
    assert public["api_key_persistence"] == "none"
    assert "系统安全存储不可用" in public["credential_warning"]


def test_forgetting_session_fallback_also_deletes_an_older_secure_key(tmp_path):
    credentials = WriteFailingCredentialStore()
    credentials.secret = "older-secure-secret"
    store = UserSettingsStore(
        tmp_path / "settings.json", credential_store=credentials
    )

    public = store.update({"api_key": "new-session-secret"})
    assert public["api_key_persistence"] == "session"

    public = store.delete_api_key()

    assert credentials.secret == ""
    assert public["api_key_configured"] is False
    assert public["api_key_persistence"] == "none"
    assert store.resolve_api_key() == ""
