"""Alembic 迁移环境。数据库地址优先取调用方传入的连接或 sqlalchemy.url，否则按项目配置解析。"""

from __future__ import annotations

from alembic import context

from market_risk.storage import db
from market_risk.storage.schema import metadata

config = context.config
target_metadata = metadata


def _url() -> str:
    url = config.get_main_option("sqlalchemy.url")
    if url:
        return url
    from market_risk.config import load_settings

    return db.resolve_database_url(load_settings())


def run_migrations_offline() -> None:
    context.configure(url=_url(), target_metadata=target_metadata, literal_binds=True)
    with context.begin_transaction():
        context.run_migrations()


def run_migrations_online() -> None:
    connection = config.attributes.get("connection")
    if connection is not None:
        context.configure(connection=connection, target_metadata=target_metadata, render_as_batch=True)
        with context.begin_transaction():
            context.run_migrations()
        return
    engine = db.make_engine(_url())
    try:
        with engine.connect() as conn:
            context.configure(connection=conn, target_metadata=target_metadata, render_as_batch=True)
            with context.begin_transaction():
                context.run_migrations()
    finally:
        engine.dispose()


if context.is_offline_mode():
    run_migrations_offline()
else:
    run_migrations_online()
