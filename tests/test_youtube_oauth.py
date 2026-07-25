import json
from types import SimpleNamespace

import pytest

from videotrans.process.browser_publish import PublishAdapterError
from videotrans.process import youtube_oauth


class FakeCredentials:
    token = "authorized-token"

    def to_json(self):
        return json.dumps(
            {
                "token": self.token,
                "refresh_token": "refresh-token",
                "client_id": "client-id",
                "client_secret": "client-secret",
                "scopes": [youtube_oauth.YOUTUBE_UPLOAD_SCOPE],
            }
        )


def test_access_token_prefers_environment_without_persisting(monkeypatch, tmp_path):
    monkeypatch.setenv(youtube_oauth.YOUTUBE_TOKEN_ENV, " env-token ")
    token_path = tmp_path / "token.json"

    assert youtube_oauth.get_youtube_access_token(token_path=token_path) == "env-token"
    assert not token_path.exists()


def test_access_token_uses_saved_credentials(monkeypatch, tmp_path):
    monkeypatch.delenv(youtube_oauth.YOUTUBE_TOKEN_ENV, raising=False)
    credentials = SimpleNamespace(token="saved-token")
    monkeypatch.setattr(
        youtube_oauth,
        "load_youtube_credentials",
        lambda *_args, **_kwargs: credentials,
    )

    assert (
        youtube_oauth.get_youtube_access_token(
            token_path=tmp_path / "token.json"
        )
        == "saved-token"
    )


def test_authorize_youtube_opens_consent_and_atomically_saves_token(
    monkeypatch,
    tmp_path,
):
    from google_auth_oauthlib.flow import InstalledAppFlow

    secret = tmp_path / "client_secret.json"
    secret.write_text("{}", encoding="utf-8")
    token_path = tmp_path / "credentials" / "youtube_token.json"
    calls = {}

    class FakeFlow:
        def run_local_server(self, **kwargs):
            calls["run"] = kwargs
            return FakeCredentials()

    def fake_from_client_secrets_file(path, scopes):
        calls["path"] = path
        calls["scopes"] = scopes
        return FakeFlow()

    monkeypatch.setattr(
        InstalledAppFlow,
        "from_client_secrets_file",
        fake_from_client_secrets_file,
    )

    credentials = youtube_oauth.authorize_youtube(
        secret,
        token_path,
        open_browser=False,
    )

    assert credentials.token == "authorized-token"
    assert calls["path"] == str(secret)
    assert calls["scopes"] == [youtube_oauth.YOUTUBE_UPLOAD_SCOPE]
    assert calls["run"]["port"] == 0
    assert calls["run"]["open_browser"] is False
    assert json.loads(token_path.read_text(encoding="utf-8"))["refresh_token"] == (
        "refresh-token"
    )
    assert not token_path.with_suffix(".json.tmp").exists()


def test_access_token_requires_connection_when_no_source(monkeypatch, tmp_path):
    monkeypatch.delenv(youtube_oauth.YOUTUBE_TOKEN_ENV, raising=False)
    monkeypatch.setattr(
        youtube_oauth,
        "load_youtube_credentials",
        lambda *_args, **_kwargs: None,
    )

    with pytest.raises(PublishAdapterError, match="Chưa kết nối YouTube"):
        youtube_oauth.get_youtube_access_token(
            token_path=tmp_path / "missing.json"
        )
