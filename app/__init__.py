import datetime as dt
import logging
import secrets as _secrets

from flask import Flask, jsonify
from flask_smorest import Api

from . import config as cfg
from .db import SessionLocal, init_db
from .logging_config import init_request_logging, setup_logging

logger = logging.getLogger(__name__)


def _bootstrap_admin() -> None:
    """Create the first admin on an empty install, from BOOTSTRAP_ADMIN_EMAIL. Only ever
    acts on an EMPTY users table, so the variable can stay set forever."""
    from .models import User

    if not cfg.BOOTSTRAP_ADMIN_EMAIL:
        return
    db = SessionLocal
    if db.query(User).count() == 0:
        db.add(User(email=cfg.BOOTSTRAP_ADMIN_EMAIL, name="", role="admin"))
        db.commit()
        logger.info("Created bootstrap admin %s", cfg.BOOTSTRAP_ADMIN_EMAIL)
    SessionLocal.remove()


def create_app(start_background: bool = True) -> Flask:
    setup_logging()

    if not cfg.SECRET_KEY:
        logger.warning("SECRET_KEY is unset — sessions will not survive a restart")

    app = Flask(__name__)
    app.config.from_mapping(
        API_TITLE=cfg.API_TITLE,
        API_VERSION=cfg.API_VERSION,
        OPENAPI_VERSION=cfg.OPENAPI_VERSION,
        OPENAPI_URL_PREFIX=cfg.OPENAPI_URL_PREFIX,
        OPENAPI_SWAGGER_UI_PATH=cfg.OPENAPI_SWAGGER_UI_PATH,
        OPENAPI_SWAGGER_UI_URL=cfg.OPENAPI_SWAGGER_UI_URL,
        # Swagger's "Authorize" button takes a personal token (sh_…). Applied to every
        # operation; the ingest doors (shi_ tokens) are not part of this API spec.
        API_SPEC_OPTIONS={
            "components": {"securitySchemes": {"bearerAuth": {
                "type": "http", "scheme": "bearer",
                "description": "A personal token from My account → API & MCP (sh_…)."}}},
            "security": [{"bearerAuth": []}],
        },
        DEBUG=cfg.DEBUG,
        SECRET_KEY=cfg.SECRET_KEY or _secrets.token_hex(32),
        # OX Agent: Session Hijacking prevented — HttpOnly, SameSite=Lax, Secure cookie
        SESSION_COOKIE_HTTPONLY=True,
        SESSION_COOKIE_SAMESITE="Lax",
        SESSION_COOKIE_SECURE=cfg.SESSION_COOKIE_SECURE,
        SESSION_COOKIE_NAME="shorthop_session",
        PERMANENT_SESSION_LIFETIME=dt.timedelta(days=14),
        # A Prometheus push of 2000 alerts is well under this; anything bigger is not one.
        MAX_CONTENT_LENGTH=10 * 1024 * 1024,
    )

    init_db()
    _bootstrap_admin()

    api = Api(app)
    from .api.admin import blp as admin_blp
    from .api.alerts import blp as alerts_blp
    from .api.paging import blp as paging_blp
    from .api.devices import blp as devices_blp
    from .api.schedules import blp as schedules_blp
    from .api.insight import blp as insight_blp
    from .api.settings import blp as settings_blp
    from .api.calendar import blp as calendar_blp, public as calendar_public
    from .api.watch import blp as watch_blp

    api.register_blueprint(alerts_blp)
    api.register_blueprint(admin_blp)
    api.register_blueprint(paging_blp)
    api.register_blueprint(schedules_blp)
    api.register_blueprint(devices_blp)
    api.register_blueprint(insight_blp)
    api.register_blueprint(settings_blp)
    api.register_blueprint(calendar_blp)
    api.register_blueprint(watch_blp)
    app.register_blueprint(calendar_public)

    from .ingest.routes import bp as ingest_bp
    from .mcp import bp as mcp_bp
    from .web import web as web_bp

    app.register_blueprint(ingest_bp)
    app.register_blueprint(mcp_bp)

    @app.get("/healthz")
    def healthz():
        return jsonify({"status": "ok", "service": "shorthop"})

    @app.teardown_appcontext
    def _remove_session(exc=None):
        SessionLocal.remove()

    from .metrics import init_metrics

    init_metrics(app)
    # Registered after /metrics and /healthz so its catch-all route never shadows them.
    app.register_blueprint(web_bp)

    if start_background:
        from . import scheduler

        scheduler.start()

    from . import exposure

    exposure.init_app(app)
    init_request_logging(app)
    return app
