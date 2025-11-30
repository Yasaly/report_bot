import os

from contextlib import closing

from fastapi import FastAPI, HTTPException
from pydantic import BaseModel

from dotenv import load_dotenv
import requests

from db import get_conn

load_dotenv()

BOT_TOKEN = os.getenv("TELEGRAM_BOT_TOKEN")
if not BOT_TOKEN:
    raise RuntimeError("TELEGRAM_BOT_TOKEN не установлен")

TG_API_URL = f"https://api.telegram.org/bot{BOT_TOKEN}/sendMessage"

API_SECRET = os.getenv("NOTIFIER_API_SECRET")
if not API_SECRET:
    raise RuntimeError("NOTIFIER_API_SECRET не установлен")

app = FastAPI()


class NotifyRequest(BaseModel):
    secret: str
    nickname: str
    text: str


def get_chat_id(nickname: str) -> int:
    """
    Возвращает chat_id для указанного nickname.
    Если запись не найдена — бросает KeyError.
    """
    with get_conn() as conn:
        with conn.cursor() as cur:
            cur.execute(
                """
                SELECT chat_id
                FROM recipients
                WHERE nickname = %s
                """,
                (nickname,),
            )
            row = cur.fetchone()
    if row is None:
        raise KeyError(f"nickname={nickname!r} not found")
    (chat_id,) = row
    return int(chat_id)


def send_telegram_message(chat_id: int, text: str) -> None:
    resp = requests.post(
        TG_API_URL,
        json={"chat_id": chat_id, "text": text},
        timeout=10,
    )
    if not resp.ok:
        raise RuntimeError(f"Telegram error: {resp.status_code}, {resp.text}")


@app.post("/notify")
def notify(payload: NotifyRequest):
    # авторизация по shared secret
    if payload.secret != API_SECRET:
        raise HTTPException(status_code=403, detail="Invalid secret")

    try:
        chat_id = get_chat_id(payload.nickname)
    except KeyError:
        raise HTTPException(status_code=404, detail="nickname not found")

    try:
        send_telegram_message(chat_id, payload.text)
    except RuntimeError as e:
        raise HTTPException(status_code=502, detail=str(e))

    return {"status": "ok"}
