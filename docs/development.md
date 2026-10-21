# Development

```bash
python3 -m venv .venv && .venv/bin/pip install -r requirements-dev.txt
BOOTSTRAP_ADMIN_EMAIL=you@example.com SESSION_COOKIE_SECURE=false .venv/bin/python main.py
```

Without `POSTGRES_HOST` it uses a local SQLite file. API docs at http://localhost:8080/docs.

```bash
.venv/bin/python -m pytest -q          # tests (SQLite; set POSTGRES_* to run the HA tests too)
python scripts/config_reference.py     # regenerate docs/configuration.md after changing app/config.py
```

## Web UI

```bash
cd web && npm install && npm run dev          # http://localhost:5173, proxies to :8080
```

Render check against a seeded local instance:

```bash
DATABASE_URL=sqlite:///demo.db SESSION_COOKIE_SECURE=false .venv/bin/python scripts/demo_seed.py
DATABASE_URL=sqlite:///demo.db SESSION_COOKIE_SECURE=false SECRET_KEY=dev PORT=8099 .venv/bin/python main.py &
cd web && npm run build && SHORTHOP_URL=http://localhost:8099 node scripts/render.mjs
```
