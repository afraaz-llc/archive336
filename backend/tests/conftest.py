"""Shared test fixtures.

In-memory SQLite session so tests don't touch the dev DB and run
instantly. Bridges the gap between SQLAlchemy's declarative Base in
``app.db`` and pytest's per-function tear-down.
"""
from __future__ import annotations

import pytest
from sqlalchemy import create_engine
from sqlalchemy.orm import sessionmaker

# Import all model modules so Base.metadata knows about every table
# before we call create_all. SQLAlchemy registers tables as a side
# effect of the class definitions, so importing the module is enough.
from app import models  # noqa: F401
from app.db import Base


@pytest.fixture
def db():
    """Fresh in-memory SQLite for each test. Tables are created
    once per fixture invocation; the session is closed and the engine
    disposed when the test finishes.
    """
    engine = create_engine(
        "sqlite:///:memory:",
        future=True,
        connect_args={"check_same_thread": False},
    )
    Base.metadata.create_all(engine)
    Session = sessionmaker(bind=engine, future=True)
    session = Session()
    try:
        yield session
    finally:
        session.close()
        engine.dispose()
