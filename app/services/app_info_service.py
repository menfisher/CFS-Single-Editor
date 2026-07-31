from app.config import PROJECT_ROOT


DEFAULT_APP_VERSION = "1.0"
APP_VERSION_PATH = PROJECT_ROOT / "APP_VERSION"


def get_current_app_version() -> str:
    try:
        version = APP_VERSION_PATH.read_text(encoding="utf-8").strip()
    except OSError:
        version = ""
    return version or DEFAULT_APP_VERSION
