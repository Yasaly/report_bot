# db.py
import os

import psycopg
from dotenv import load_dotenv

load_dotenv()


def get_conn():
    """
    Возвращает синхронное подключение к PostgreSQL.
    Настройки берутся из переменных окружения:

    POSTGRES_HOST
    POSTGRES_PORT
    POSTGRES_DB
    POSTGRES_USER
    POSTGRES_PASSWORD
    """
    return psycopg.connect(
        host=os.getenv("POSTGRES_HOST", "localhost"),
        port=os.getenv("POSTGRES_PORT", "5432"),
        dbname=os.getenv("POSTGRES_DB", "notifier"),
        user=os.getenv("POSTGRES_USER", "notifier"),
        password=os.getenv("POSTGRES_PASSWORD", "password123"),
    )
