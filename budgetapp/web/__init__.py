"""The web app: FastAPI + server-rendered Jinja templates + HTMX (ARCHITECTURE §9).

``create_app()`` in :mod:`budgetapp.web.app` builds it; ``./run serve`` / ``./run start`` serve it
on 127.0.0.1 only. Conventions for pages (route modules, ``render()``, HTMX partials, flash
messages, forms, CSP) are in ``budgetapp/web/README.md``.
"""
