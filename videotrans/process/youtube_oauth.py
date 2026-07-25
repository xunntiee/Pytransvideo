# -*- coding: utf-8 -*-
"""YouTube OAuth helpers with refresh-token persistence.

The OAuth client-secret JSON is only read from the user-selected location.
Only the authorized-user token is stored in the app output credentials folder.
"""

import os
from pathlib import Path

from videotrans.process.browser_publish import PublishAdapterError


YOUTUBE_UPLOAD_SCOPE = "https://www.googleapis.com/auth/youtube.upload"
YOUTUBE_TOKEN_ENV = "PYVIDEOTRANS_YOUTUBE_TOKEN"


def default_youtube_token_path():
    from videotrans.configure import config

    return Path(config.HOME_DIR) / ".credentials" / "youtube_token.json"


def _token_path(token_path=None):
    return Path(token_path) if token_path else default_youtube_token_path()


def _save_credentials(credentials, token_path=None):
    target = _token_path(token_path)
    target.parent.mkdir(parents=True, exist_ok=True)
    temporary = target.with_suffix(target.suffix + ".tmp")
    temporary.write_text(credentials.to_json(), encoding="utf-8")
    try:
        temporary.chmod(0o600)
    except OSError:
        pass
    os.replace(temporary, target)
    return target


def load_youtube_credentials(token_path=None, *, refresh=True, log=None):
    """Return valid saved credentials, or ``None`` when none are available."""
    target = _token_path(token_path)
    if not target.is_file():
        return None
    log = log or (lambda _message: None)
    try:
        from google.auth.transport.requests import Request
        from google.oauth2.credentials import Credentials

        credentials = Credentials.from_authorized_user_file(
            str(target),
            scopes=[YOUTUBE_UPLOAD_SCOPE],
        )
        if (
            refresh
            and credentials.expired
            and credentials.refresh_token
        ):
            log("YouTube OAuth: làm mới access token...")
            credentials.refresh(Request())
            _save_credentials(credentials, target)
        if credentials.valid:
            return credentials
    except Exception as exc:
        raise PublishAdapterError(
            f"Không đọc/làm mới được YouTube OAuth đã lưu: {exc}"
        ) from exc
    return None


def authorize_youtube(
    client_secret_path,
    token_path=None,
    *,
    open_browser=True,
    log=None,
):
    """Run Google's installed-app consent flow and persist the refresh token."""
    client_secret = Path(client_secret_path or "")
    if not client_secret.is_file():
        raise PublishAdapterError(
            f"Không tìm thấy OAuth client secret JSON: {client_secret}"
        )
    log = log or (lambda _message: None)
    try:
        from google_auth_oauthlib.flow import InstalledAppFlow

        flow = InstalledAppFlow.from_client_secrets_file(
            str(client_secret),
            scopes=[YOUTUBE_UPLOAD_SCOPE],
        )
        log("YouTube OAuth: mở trình duyệt để bạn chọn kênh và cấp quyền...")
        credentials = flow.run_local_server(
            host="localhost",
            port=0,
            open_browser=bool(open_browser),
            authorization_prompt_message=(
                "Mở URL này nếu trình duyệt không tự mở: {url}"
            ),
            success_message=(
                "Đã kết nối YouTube. Bạn có thể đóng tab này và quay lại tool."
            ),
            access_type="offline",
            prompt="consent",
        )
        target = _save_credentials(credentials, token_path)
        log(f"YouTube OAuth: đã lưu phiên đăng nhập tại {target}.")
        return credentials
    except PublishAdapterError:
        raise
    except Exception as exc:
        raise PublishAdapterError(f"Kết nối YouTube OAuth thất bại: {exc}") from exc


def get_youtube_access_token(
    *,
    client_secret_path=None,
    token_path=None,
    open_browser=True,
    log=None,
):
    """Resolve a token from env, saved OAuth credentials, or a consent flow."""
    environment_token = os.environ.get(YOUTUBE_TOKEN_ENV, "").strip()
    if environment_token:
        return environment_token
    credentials = load_youtube_credentials(token_path, log=log)
    if credentials:
        return credentials.token
    if client_secret_path:
        credentials = authorize_youtube(
            client_secret_path,
            token_path,
            open_browser=open_browser,
            log=log,
        )
        return credentials.token
    raise PublishAdapterError(
        "Chưa kết nối YouTube. Hãy chọn OAuth client secret JSON để đăng nhập."
    )


def has_saved_youtube_credentials(token_path=None):
    return _token_path(token_path).is_file()
