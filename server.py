"""Локальный сервер: статика + API. Ключ приходит в запросе (или из env POLZA_API_KEY), нигде не сохраняется."""
import os
import urllib.error
from pathlib import Path

from fastapi import FastAPI, HTTPException
from fastapi.responses import FileResponse
from pydantic import BaseModel

import textnav

ROOT = Path(__file__).parent
DEFAULT_BASE = os.getenv("LLM_BASE_URL", "https://api.polza.ai/api/v1")
DEFAULT_MODEL = os.getenv("LLM_MODEL", "openai/gpt-6-luna")

app = FastAPI(title="Semantic TextNavigator — проверка договоров")


class File(BaseModel):
    name: str
    text: str


class AnalyzeIn(BaseModel):
    files: list[File]
    api_key: str = ""
    base_url: str = ""
    model: str = ""
    check_date: str = "2026-03-10"


@app.get("/")
def index():
    return FileResponse(ROOT / "static" / "index.html")


@app.get("/api/config")
def config():
    return {"base_url": DEFAULT_BASE, "model": DEFAULT_MODEL, "env_key": bool(os.getenv("POLZA_API_KEY"))}


@app.get("/api/sample")
def sample():
    d = ROOT / "documents"
    return [{"name": p.name, "text": p.read_text(encoding="utf-8")} for p in sorted(d.glob("*.txt"))]


@app.post("/api/analyze")
def analyze(body: AnalyzeIn):
    key = body.api_key.strip() or os.getenv("POLZA_API_KEY", "")
    mode = "llm" if key else "stub"
    try:
        docs = textnav.extract_all([f.model_dump() for f in body.files], mode, key,
                                   body.base_url or DEFAULT_BASE, body.model or DEFAULT_MODEL)
    except urllib.error.HTTPError as e:
        raise HTTPException(502, f"LLM-прокси вернул {e.code}: {e.read().decode()[:200]}")
    except Exception as e:  # сеть, невалидный JSON от модели и т.п.
        raise HTTPException(502, f"Ошибка извлечения ({mode}): {e}")
    res = textnav.resolve(docs, body.check_date)
    res["mode"] = mode
    res["cost_rub"] = round(sum(d.get("cost_rub") or 0 for d in docs), 4) if mode == "llm" else 0
    return res


class AskIn(BaseModel):
    result: dict
    kind: str
    contract_no: str = ""
    before: str = "2026-04-01"


@app.post("/api/ask")
def ask(body: AskIn):
    return textnav.answer(body.result, body.kind, body.contract_no, body.before)
