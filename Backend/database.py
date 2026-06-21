from contextlib import contextmanager

from psycopg.conninfo import make_conninfo
from psycopg_pool import ConnectionPool

from config import (
    DB_HOST,
    DB_PORT,
    DB_NAME,
    DB_USER,
    DB_PASSWORD,
    DB_SSLMODE,
)

# psycopg3 connection pool. Same getconn/putconn surface as the old
# psycopg2 SimpleConnectionPool, so app.py is unchanged. On putconn the
# pool resets the connection (rolls back any open transaction).
conninfo = make_conninfo(
    host=DB_HOST,
    port=DB_PORT,
    dbname=DB_NAME,
    user=DB_USER,
    password=DB_PASSWORD,
    sslmode=DB_SSLMODE,
)

# autocommit=True: every statement commits on its own, so read endpoints
# (/check, /archive) never return a connection mid-transaction. /add stays
# safe because the UNIQUE index on normalized_text prevents duplicate inserts.
pool = ConnectionPool(
    conninfo,
    min_size=1,
    max_size=10,
    open=True,
    kwargs={"autocommit": True},
)


def close_pool():
    pool.close()


def get_connection():
    return pool.getconn()


def return_connection(conn):
    pool.putconn(conn)


@contextmanager
def db_cursor():
    """Borrow a pooled connection and yield a cursor, returning the connection
    to the pool afterwards. The pool runs in autocommit, so each statement
    commits on its own.

        with db_cursor() as cur:
            cur.execute(...)
            rows = cur.fetchall()
    """
    conn = get_connection()
    try:
        yield conn.cursor()
    finally:
        return_connection(conn)
