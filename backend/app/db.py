import psycopg2
import psycopg2.pool
from psycopg2.extras import RealDictCursor
from contextlib import contextmanager

from app.constants import DB_HOST, DB_USER, DB_PASSWORD, DB_NAME, DB_PORT
from app.logger import logger

# Initialize a global connection pool
try:
    db_pool = psycopg2.pool.ThreadedConnectionPool(
        minconn=1,
        maxconn=10,
        host=DB_HOST,
        user=DB_USER,
        password=DB_PASSWORD,
        dbname=DB_NAME,
        port=DB_PORT,
        connect_timeout=10,
    )
    logger.info("Successfully initialized PostgreSQL connection pool")
except Exception as e:
    logger.error(f"Failed to initialize PostgreSQL connection pool: {e}")
    db_pool = None


@contextmanager
def get_conn():
    """
    Yield a psycopg2 connection from the pool.
    All cursors opened on this connection will return RealDictRow (dict-like) objects.
    Automatically commits on success, rolls back on exception, and returns the
    connection to the pool when the `with` block exits.
    """
    if db_pool is None:
        raise RuntimeError("Database pool is not initialized — check DB_HOST/DB_USER/DB_PASSWORD env vars")

    conn = db_pool.getconn()
    # Set RealDictCursor as the default so every conn.cursor() call returns dicts
    conn.cursor_factory = RealDictCursor
    try:
        yield conn
        conn.commit()
    except Exception as e:
        conn.rollback()
        raise e
    finally:
        # Reset cursor factory before returning to the pool
        conn.cursor_factory = psycopg2.extensions.cursor
        db_pool.putconn(conn)


def init_db():
    """Create tables if they do not exist yet."""
    try:
        with get_conn() as conn:
            with conn.cursor() as cur:
                # Messages table
                cur.execute("""
                CREATE TABLE IF NOT EXISTS messages (
                    pk          VARCHAR(255) NOT NULL,
                    sk          VARCHAR(255) NOT NULL,
                    ts          VARCHAR(50),
                    user_id     VARCHAR(50),
                    username    VARCHAR(255),
                    text        TEXT,
                    channel_id  VARCHAR(50),
                    team_id     VARCHAR(50),
                    subtype     VARCHAR(50),
                    PRIMARY KEY (pk, sk)
                );
                """)

                cur.execute(
                    "CREATE INDEX IF NOT EXISTS idx_messages_userid ON messages (user_id);"
                )
                cur.execute(
                    "CREATE INDEX IF NOT EXISTS idx_messages_team_channel ON messages (team_id, channel_id);"
                )

                # Sessions table
                cur.execute("""
                CREATE TABLE IF NOT EXISTS sessions (
                    session_id  VARCHAR(255) PRIMARY KEY,
                    team_ids    TEXT         DEFAULT '[]',
                    created_at  TIMESTAMP    DEFAULT CURRENT_TIMESTAMP,
                    expires_at  TIMESTAMP
                );
                """)

                # Users cache
                cur.execute("""
                CREATE TABLE IF NOT EXISTS users_cache (
                    team_id      VARCHAR(50),
                    user_id      VARCHAR(50),
                    display_name VARCHAR(255),
                    real_name    VARCHAR(255),
                    cached_at    TIMESTAMP DEFAULT CURRENT_TIMESTAMP,
                    PRIMARY KEY (team_id, user_id)
                );
                """)

                logger.info("Database schema initialized successfully")
    except Exception as e:
        logger.error(f"Failed to initialize database schema: {e}")
