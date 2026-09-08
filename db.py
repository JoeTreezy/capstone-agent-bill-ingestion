"""Database access layer supporting two separate connection pools:
'retool' (writable staging DB) and read-only production DB.
Every call specifies which one it needs -- there's no default that could
accidentally send a write to the read-only connection.
"""
from contextlib import contextmanager
import psycopg2
import psycopg2.extras
from psycopg2.pool import ThreadedConnectionPool

from config import settings

_pools: dict[str, ThreadedConnectionPool] = {}

_POOL_CONFIGS = {
    "retool": lambda: dict(
        host=settings.retool_db_host, port=settings.retool_db_port,
        dbname=settings.retool_db_name, user=settings.retool_db_user, password=settings.retool_db_password,
        sslmode=settings.retool_db_sslmode,
    ),
    "coyote": lambda: dict(
        host=settings.coyote_db_host, port=settings.coyote_db_port,
        dbname=settings.coyote_db_name, user=settings.coyote_db_user, password=settings.coyote_db_password,
        sslmode=settings.coyote_db_sslmode,
    ),
}


def get_pool(pool_name: str) -> ThreadedConnectionPool:
    if pool_name not in _POOL_CONFIGS:
        raise ValueError(f"Unknown pool '{pool_name}' -- expected 'retool' or 'coyote'")
    if pool_name not in _pools:
        _pools[pool_name] = ThreadedConnectionPool(minconn=1, maxconn=10, **_POOL_CONFIGS[pool_name]())
    return _pools[pool_name]


@contextmanager
def get_cursor(pool_name: str = "retool", commit: bool = False):
    pool = get_pool(pool_name)
    conn = pool.getconn()
    try:
        cur = conn.cursor(cursor_factory=psycopg2.extras.RealDictCursor)
        yield cur
        if commit:
            conn.commit()
    except Exception:
        conn.rollback()
        raise
    finally:
        pool.putconn(conn)


def fetch_all(query: str, params: tuple = (), pool_name: str = "retool") -> list[dict]:
    with get_cursor(pool_name) as cur:
        cur.execute(query, params)
        return [dict(row) for row in cur.fetchall()]


def execute(query: str, params=(), pool_name: str = "retool") -> None:
    """pool_name defaults to 'retool' since that's the only writable connection --
    Coyote should only ever be reached through fetch_all."""
    with get_cursor(pool_name, commit=True) as cur:
        cur.execute(query, params)