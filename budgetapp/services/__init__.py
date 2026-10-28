"""Business logic. Services take a SQLAlchemy ``Session`` first, never commit (the caller owns the
transaction), never import FastAPI or Jinja, and return frozen dataclasses (ARCHITECTURE §1)."""
