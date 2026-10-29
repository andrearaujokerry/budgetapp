"""One module per page; each defines ``router = APIRouter()``.

``ALL_ROUTERS`` is in nav order (``templating.NAV_ITEMS``), then ``dev``. ``create_app()``
includes every router with the schema guard as a router-level dependency. A feature package
replaces its page modules wholesale and keeps the name ``router``.
"""

from budgetapp.web.routes import (
    budgets,
    categories,
    connection,
    dashboard,
    dev,
    imports,
    recurring,
    rules,
    settings,
    statements,
    transactions,
    trends,
)

ALL_ROUTERS = (
    dashboard.router,
    transactions.router,
    budgets.router,
    trends.router,
    recurring.router,
    statements.router,
    categories.router,
    rules.router,
    imports.router,
    connection.router,
    settings.router,
    dev.router,
)
