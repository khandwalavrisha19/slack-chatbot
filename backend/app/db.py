import psycopg2
from psycopg2 import pool
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
        connect_timeout=10
    )
    logger.info("Successfully initialized PostgreSQL connection pool")
except Exception as e:
    logger.error(f"Failed to initialize PostgreSQL connection pool: {e}")
    db_pool = None

@contextmanager
def get_conn():
    """
    Context manager to yield a db connection from the pool,
    and automatically return it after use.
    """
    if db_pool is None:
        raise RuntimeError("Database pool not initialized")
    
    conn = db_pool.getconn()
    try:
        # yield a dictionary-like cursor for easy access
        from psycopg2.extras import RealDictCursor
        with conn.cursor(cursor_factory=RealDictCursor) as cur:
            # We yield the connection so the caller can commit/rollback
            # But the caller might want to just `conn.execute` easily.
            # Let's add a helper run wrapper or just yield the conn
            # Actually, standard pattern is to yield connection and let them get cursor
            yield conn
        conn.commit()
    except Exception as e:
        conn.rollback()
        raise e
    finally:
        db_pool.putconn(conn)

def init_db():
    """Initialize tables if they don't exist"""
    try:
        with get_conn() as conn:
            with conn.cursor() as cur:
                # Messages table
                cur.execute("""
                CREATE TABLE IF NOT EXISTS messages (
                    pk VARCHAR(255) NOT NULL,
                    sk VARCHAR(255) NOT NULL,
                    ts VARCHAR(50),
                    user_id VARCHAR(50),
                    username VARCHAR(255),
                    text TEXT,
                    channel_id VARCHAR(50),
                    team_id VARCHAR(50),
                    subtype VARCHAR(50),
                    PRIMARY KEY (pk, sk)
                );
                """)
                # Optional index for searching by user faster
                cur.execute("CREATE INDEX IF NOT EXISTS idx_messages_userid ON messages (user_id);")

                # Sessions table
                cur.execute("""
                CREATE TABLE IF NOT EXISTS sessions (
                    session_id VARCHAR(255) PRIMARY KEY,
                    team_ids TEXT,
                    created_at TIMESTAMP DEFAULT CURRENT_TIMESTAMP,
                    expires_at TIMESTAMP
                );
                """)
                
                # Users Cache
                cur.execute("""
                CREATE TABLE IF NOT EXISTS users_cache (
                    team_id VARCHAR(50),
                    user_id VARCHAR(50),
                    display_name VARCHAR(255),
                    real_name VARCHAR(255),
                    cached_at TIMESTAMP DEFAULT CURRENT_TIMESTAMP,
                    PRIMARY KEY (team_id, user_id)
                );
                """)
                logger.info("Database schema initialized successfully")
    except Exception as e:
        logger.error(f"Failed to initialize database schema: {e}")
