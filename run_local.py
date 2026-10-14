import os
from pathlib import Path

os.environ.setdefault("CONTACTSFREESHARE_USE_APP_DATA", "1")
os.environ.setdefault(
    "CONTACTSFREESHARE_DATA_DIR",
    str(Path.home() / "ContactsFreeShare-SingleEditor"),
)
os.environ.setdefault("CONTACTSFREESHARE_PORT", "8001")
os.environ.setdefault(
    "GOOGLE_OAUTH_REDIRECT_URI",
    "http://127.0.0.1:8001/google/connect/callback",
)

import uvicorn

from app.config import HOST, PORT


if __name__ == "__main__":
    print(f"Single Editor local server: http://{HOST}:{PORT}")
    uvicorn.run("asgi:app", host=HOST, port=PORT, reload=True)
