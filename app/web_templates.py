from __future__ import annotations

from fastapi.templating import Jinja2Templates

from app.config import SINGLE_EDITOR_ONLY, TEMPLATES_DIR
from app.services.app_info_service import get_current_app_version
from app.services.google_sync_service import get_google_sync_summary, is_share_contacts_nav_visible
from app.services.share_web_app_service import share_web_app_url


templates = Jinja2Templates(directory=str(TEMPLATES_DIR))
templates.env.globals["app_version"] = get_current_app_version
templates.env.globals["google_sync"] = get_google_sync_summary
templates.env.globals["share_contacts_nav_visible"] = is_share_contacts_nav_visible
templates.env.globals["share_web_app_url"] = share_web_app_url
templates.env.globals["single_editor_only"] = SINGLE_EDITOR_ONLY
