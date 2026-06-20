import psycopg2
from psycopg2.pool import SimpleConnectionPool
from config import * 
pool = SimpleConnectionPool(
    minconn=1,
    maxconn=10,
    host=DB_HOST,
    port=DB_PORT,
    database=DB_NAME,
    user=DB_USER,
    password=DB_PASSWORD,
    sslmode="require"
)
def get_connection():
    return pool.getconn()

def return_connection(conn):
    pool.putconn(conn)