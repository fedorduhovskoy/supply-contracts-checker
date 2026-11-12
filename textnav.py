"""Ядро прототипа: абзацы → извлечение (stub | LLM) → проверка цитат → резолвер действующих условий.

Принцип: модель (или правила) только ИЗВЛЕКАЕТ факты с цитатами.
Какое значение действует на дату проверки, решает детерминированный код (resolve).
"""
from __future__ import annotations

import json
import re
import urllib.request
from concurrent.futures import ThreadPoolExecutor
from dataclasses import dataclass, field
from datetime import date

FIELDS = ("price", "delivery_date", "supplier")
FIELD_LABEL = {"price": "Сумма", "delivery_date": "Срок поставки", "supplier": "Поставщик"}

DATE_RE = r"(\d{2})\.(\d{2})\.(\d{4})"
NUM_RE = r"(\d{1,3}(?:[\s ]\d{3})+|\d+)\s*(?:\([^)]*\)\s*)?руб"


# ───────────── абзацы ─────────────
def split_paragraphs(text: str) -> list[dict]:
    """Каждая непустая строка — абзац; номер = позиция (с 1). Ссылка на источник = файл + номер."""
    lines = [l.strip() for l in text.replace("\r\n", "\n").split("\n")]
    return [{"n": i + 1, "text": l} for i, l in enumerate(x for x in lines if x)]


def norm(s: str) -> str:
    return re.sub(r"\s+", " ", s.replace(" ", " ")).strip().lower()


def iso(d: str) -> str:
    m = re.fullmatch(DATE_RE, d)
    return f"{m[3]}-{m[2]}-{m[1]}"


def fmt_date(d: str | None) -> str:
    return f"{d[8:10]}.{d[5:7]}.{d[:4]}" if d else "нет данных"


def fmt_money(v) -> str:
    return f"{v:,}".replace(",", " ") + " ₽" if v is not None else "нет данных"


# ───────────── структура извлечения ─────────────
# doc = {file, doc_type: contract|amendment|draft|email|other, contract_no, status: signed|unsigned|proposal|unknown,
#        effective_date: ISO|None, facts: [{field, value, quote, para}], warnings: []}
def empty_doc(name: str) -> dict:
    return {"file": name, "doc_type": "other", "contract_no": None, "status": "unknown",
            "effective_date": None, "facts": [], "warnings": []}


# ───────────── STUB: правила под формат учебных документов ─────────────
def stub_extract(name: str, paras: list[dict]) -> dict:
    doc = empty_doc(name)
    text = "\n".join(p["text"] for p in paras)
    head = paras[0]["text"].lower() if paras else ""
    if "проект" in head:
        doc["doc_type"] = "draft"
    elif "дополнительное соглашение" in head:
        doc["doc_type"] = "amendment"
    elif "письмо" in head:
        doc["doc_type"] = "email"
    elif "договор" in head:
        doc["doc_type"] = "contract"

    m = re.search(r"[A-ZА-Я]{2,}-\d+", "\n".join(p["text"] for p in paras[:1]) + "\n" +
                  "\n".join(p["text"] for p in paras if p["text"].lower().startswith("тема")))
    m = m or re.search(r"[A-ZА-Я]{2,}-\d+", text)
    doc["contract_no"] = m[0] if m else None

    status_line = next((p["text"] for p in paras if p["text"].lower().startswith("статус")), "")
    low = status_line.lower()
    if re.search(r"не подписан|проект|на согласовании", low):
        doc["status"] = "unsigned"
    elif re.search(r"подписан\w* обеими", low):
        doc["status"] = "signed"
    elif doc["doc_type"] == "email":
        doc["status"] = "proposal"
    if doc["doc_type"] == "draft":
        doc["status"] = "unsigned"

    if doc["doc_type"] == "contract":
        dm = re.search(r"дата:\s*" + DATE_RE, text.lower())
        doc["effective_date"] = iso(".".join(dm.groups())) if dm else None
    elif doc["doc_type"] == "amendment":
        for p in paras:
            em = re.search(r"вступает в силу\s+" + DATE_RE, p["text"].lower())
            if em:
                doc["effective_date"] = iso(".".join(em.groups()))

    for p in paras:
        t, low = p["text"], p["text"].lower()
        if re.search(r"поставщик:\s*", low) and doc["doc_type"] == "contract":
            sm = re.search(r"поставщик:\s*(ооо «[^»]+»)", t, re.I)
            if sm:
                doc["facts"].append({"field": "supplier", "value": sm[1], "quote": sm[1], "para": p["n"]})
        if re.search(r"общ(?:ая|ую) стоимост|пункт 2\.1", low) and not re.search(r"аванс|лимит", low):
            nm = re.search(NUM_RE, t)
            if nm:
                doc["facts"].append({"field": "price", "value": int(re.sub(r"\D", "", nm[1])),
                                     "quote": t, "para": p["n"]})
        if (re.search(r"поставить|срок поставки|пункт 3\.1", low) and not re.search(r"оплат", low)):
            dates = re.findall(DATE_RE, t)
            if dates:
                doc["facts"].append({"field": "delivery_date", "value": iso(".".join(dates[-1])),
                                     "quote": t, "para": p["n"]})
    return doc


# ───────────── LLM (OpenAI-совместимый прокси, polza.ai) ─────────────
SYSTEM_PROMPT = """Ты извлекаешь факты из одного учебного документа по договору поставки. Верни ТОЛЬКО JSON:
{"doc_type": "contract|amendment|draft|email|other",
 "contract_no": "номер договора, к которому относится документ (напр. STN-101) или null",
 "status": "signed|unsigned|proposal|unknown",
 "effective_date": "YYYY-MM-DD или null",
 "facts": [{"field": "price|delivery_date|supplier", "value": ..., "quote": "...", "para": N}]}
Правила:
- doc_type: contract — договор; amendment — допсоглашение; draft — проект допсоглашения; email — письмо/предложение.
- status: signed — подписан обеими сторонами; unsigned — проект/не подписан; proposal — предложение в письме.
- effective_date: для договора — дата подписания; для допсоглашения — дата вступления в силу, если указана; иначе null.
- price — ИТОГОВАЯ стоимость договора в рублях, целое число. НЕ аванс, НЕ лимит бюджета, НЕ сроки оплаты. Если числа нет — не добавляй факт.
- delivery_date — срок ПОСТАВКИ в формате YYYY-MM-DD. НЕ срок оплаты, НЕ дата приёмки, НЕ дата письма. Для предложения о переносе — новая предлагаемая дата.
- supplier — наименование поставщика (только в договоре).
- Если в документе изменение условий только предлагается, всё равно извлеки значение, но status = unsigned или proposal.
- quote — ТОЧНАЯ дословная цитата из одного абзаца, para — номер этого абзаца (из префикса [N]). Цитата ОБЯЗАНА содержать само значение (число, дату или название) — не цитируй соседние абзацы. Ничего не выдумывай и не вычисляй: нет в тексте — нет факта."""


def llm_extract(name: str, paras: list[dict], api_key: str, base_url: str, model: str) -> dict:
    body = "\n".join(f"[{p['n']}] {p['text']}" for p in paras)
    payload = {"model": model, "temperature": 0, "response_format": {"type": "json_object"},
               "messages": [{"role": "system", "content": SYSTEM_PROMPT},
                            {"role": "user", "content": f"Имя файла: {name}\n\n{body}"}]}
    req = urllib.request.Request(base_url.rstrip("/") + "/chat/completions",
                                 data=json.dumps(payload).encode(),
                                 headers={"Authorization": f"Bearer {api_key}", "Content-Type": "application/json"})
    try:  # python.org-сборки на macOS не имеют системных сертификатов — берём certifi
        import certifi, ssl
        ctx = ssl.create_default_context(cafile=certifi.where())
    except ImportError:
        ctx = None
    with urllib.request.urlopen(req, timeout=90, context=ctx) as r:
        resp = json.load(r)
    raw = resp["choices"][0]["message"]["content"]
    data = json.loads(re.sub(r"^```(?:json)?|```$", "", raw.strip()).strip())
    doc = empty_doc(name)
    for k in ("doc_type", "contract_no", "status", "effective_date"):
        if data.get(k):
            doc[k] = data[k]
    doc["facts"] = [f for f in data.get("facts", []) if isinstance(f, dict) and f.get("field") in FIELDS]
    doc["cost_rub"] = resp.get("usage", {}).get("cost_rub") or resp.get("cost_rub")
    return doc


# ───────────── проверка цитат (защита от выдумок) ─────────────
def verify(doc: dict, paras: list[dict]) -> dict:
    """Факт остаётся, только если цитата дословно найдена в тексте, а значение есть в цитате."""
    ok = []
    for f in doc["facts"]:
        q, v = str(f.get("quote", "")), f.get("value")
        nq = norm(q)
        hit = next((p for p in paras if p["n"] == f.get("para") and nq and nq in norm(p["text"])), None) \
            or next((p for p in paras if nq and nq in norm(p["text"])), None)   # модель ошиблась номером абзаца
        if f["field"] == "supplier" and v:
            # модель часто путает цитату у названия: если название дословно есть в абзаце — берём абзац целиком
            sup = next((p for p in paras if norm(str(v)) in norm(p["text"])), None)
            if sup:
                q, nq, hit = sup["text"], norm(sup["text"]), sup
        if not hit:
            doc["warnings"].append(f"{FIELD_LABEL[f['field']]}: цитата не найдена в тексте — значение отброшено")
            continue
        try:
            if f["field"] == "price":
                v = int(v)
                good = str(v) in re.sub(r"\D", "", q)
            elif f["field"] == "delivery_date":
                good = re.fullmatch(r"\d{4}-\d{2}-\d{2}", str(v)) and fmt_date(v) in q
            else:
                good = norm(str(v)) in nq
        except (ValueError, TypeError):
            good = False
        if not good:
            doc["warnings"].append(f"{FIELD_LABEL[f['field']]}: значение «{v}» не подтверждается цитатой — отброшено")
            continue
        ok.append({"field": f["field"], "value": v, "quote": q, "para": hit["n"]})
    doc["facts"] = ok
    ed = doc.get("effective_date")
    if ed and not re.fullmatch(r"\d{4}-\d{2}-\d{2}", str(ed)):
        doc["effective_date"] = None
    return doc


def extract_all(files: list[dict], mode: str, api_key: str = "", base_url: str = "", model: str = "") -> list[dict]:
    parsed = [(f["name"], split_paragraphs(f["text"])) for f in files]

    def one(item):
        name, paras = item
        doc = llm_extract(name, paras, api_key, base_url, model) if mode == "llm" else stub_extract(name, paras)
        doc = verify(doc, paras)
        doc["paragraphs"] = paras
        doc["extractor"] = mode
        return doc

    with ThreadPoolExecutor(max_workers=6) as ex:
        return list(ex.map(one, parsed))


# ───────────── резолвер: что действует на дату проверки ─────────────
def _src(doc, fact):
    return {"file": doc["file"], "para": fact["para"], "quote": fact["quote"]}


def _facts(doc, fld):
    return [f for f in doc["facts"] if f["field"] == fld]


def resolve(docs: list[dict], check_date: str) -> dict:
    contracts = [d for d in docs if d["doc_type"] == "contract" and d["contract_no"]]
    nos = {d["contract_no"] for d in contracts}
    rows, used = [], set()
    for c in sorted(contracts, key=lambda d: d["contract_no"]):
        no = c["contract_no"]
        review = [w for w in c["warnings"]]
        if sum(1 for d in contracts if d["contract_no"] == no) > 1:
            review.append("Загружено несколько договоров с этим номером — выберите вручную")
        related = [d for d in docs if d is not c and d["contract_no"] == no and d["doc_type"] != "contract"]
        used.update(id(d) for d in related)
        row = {"contract_no": no, "file": c["file"], "related": [], "pending": [], "review": review}

        applied, pending = [], []
        for d in related:
            tag = {"file": d["file"], "doc_type": d["doc_type"], "status": d["status"],
                   "effective_date": d["effective_date"]}
            changes = [f for f in d["facts"] if f["field"] in ("price", "delivery_date")]
            if d["doc_type"] == "amendment" and d["status"] == "signed" and d["effective_date"]:
                if d["effective_date"] <= check_date:
                    tag["role"] = "applied"
                    applied.append(d)
                else:
                    tag["role"] = "not_in_force"
                    pending += [(d, f, f"подписано, вступает в силу {fmt_date(d['effective_date'])} — позже даты проверки") for f in changes]
            elif d["doc_type"] == "amendment" and d["status"] == "signed":
                tag["role"] = "review"
                review.append(f"{d['file']}: подписано, но дата вступления в силу не определена — нужна проверка")
                pending += [(d, f, "дата вступления в силу не определена") for f in changes]
            elif d["doc_type"] in ("draft", "amendment") and d["status"] == "unsigned":
                tag["role"] = "draft"
                pending += [(d, f, "проект, не подписан") for f in changes]
            elif d["doc_type"] == "email" or d["status"] == "proposal":
                tag["role"] = "proposal"
                pending += [(d, f, "предложение поставщика, согласие покупателя не получено") for f in changes]
            else:
                tag["role"] = "review"
                if changes:
                    review.append(f"{d['file']}: статус документа не определён — нужна проверка")
                    pending += [(d, f, "статус документа не определён") for f in changes]
            row["related"].append(tag)

        applied.sort(key=lambda d: d["effective_date"])
        for fld in ("supplier", "price", "delivery_date"):
            base = _facts(c, fld)
            if len(base) > 1 and len({str(b["value"]) for b in base}) > 1:
                review.append(f"{FIELD_LABEL[fld]}: в договоре несколько разных значений — нужна проверка")
            res = {"value": None, "source": None, "changed_by": None, "base": None}
            if base:
                res.update(value=base[0]["value"], source=_src(c, base[0]))
            for a in applied:
                for f in _facts(a, fld):
                    if res["value"] is not None and res["changed_by"] is None:
                        res["base"] = {"value": res["value"], "source": res["source"]}
                    res.update(value=f["value"], source=_src(a, f),
                               changed_by={"file": a["file"], "effective_date": a["effective_date"]})
            row[fld] = res

        for d, f, why in pending:
            cur = row[f["field"]]["value"]
            if f["value"] == cur:
                continue
            row["pending"].append({"field": f["field"], "value": f["value"], "file": d["file"],
                                   "doc_type": d["doc_type"], "reason": why, "quote": f["quote"], "para": f["para"]})
        rows.append(row)

    orphans = [{"file": d["file"], "doc_type": d["doc_type"], "contract_no": d["contract_no"]}
               for d in docs if d["doc_type"] != "contract" and id(d) not in used]
    stray = [{"file": d["file"], "note": "договор без номера"} for d in docs
             if d["doc_type"] == "contract" and not d["contract_no"]]
    return {"check_date": check_date, "contracts": rows, "orphans": orphans, "stray": stray,
            "docs": [{k: d[k] for k in ("file", "doc_type", "contract_no", "status", "effective_date",
                                         "warnings", "paragraphs", "extractor")} for d in docs]}


# ───────────── ответы на вопросы ─────────────
def _row(res, no):
    return next((r for r in res["contracts"] if r["contract_no"] == no), None)


def _line(text, src=None):
    return {"text": text, "source": src}


def _field_answer(res, no, fld):
    r = _row(res, no)
    if not r:
        return [_line(f"Договор {no} не найден среди загруженных документов.")]
    f, out = r[fld], []
    if f["value"] is None:
        out.append(_line(f"{FIELD_LABEL[fld]} по договору {no}: нет данных (в тексте документов значение не указано)."))
    else:
        v = fmt_money(f["value"]) if fld == "price" else fmt_date(f["value"])
        out.append(_line(f"{FIELD_LABEL[fld]} по договору {no} на {fmt_date(res['check_date'])}: {v}.", f["source"]))
        if f["changed_by"]:
            b = f["base"]
            bv = (fmt_money(b["value"]) if fld == "price" else fmt_date(b["value"])) if b else "—"
            out.append(_line(f"Изменено допсоглашением {f['changed_by']['file']} (в силе с "
                             f"{fmt_date(f['changed_by']['effective_date'])}). Было: {bv}.", b["source"] if b else None))
    for p in (x for x in r["pending"] if x["field"] == fld):
        v = fmt_money(p["value"]) if fld == "price" else fmt_date(p["value"])
        out.append(_line(f"⚠ Не согласовано: {v} — {p['reason']} ({p['file']}). Не учтено.",
                         {"file": p["file"], "para": p["para"], "quote": p["quote"]}))
    for w in r["review"]:
        out.append(_line(f"Нужна проверка: {w}"))
    return out


def answer(res: dict, kind: str, contract_no: str = "", before: str = "2026-04-01") -> dict:
    if kind == "delivery":
        return {"title": f"Срок поставки по {contract_no}", "lines": _field_answer(res, contract_no, "delivery_date")}
    if kind == "price":
        return {"title": f"Сумма договора {contract_no}", "lines": _field_answer(res, contract_no, "price")}
    lines = []
    for r in res["contracts"]:
        d = r["delivery_date"]["value"]
        early_pending = [p for p in r["pending"] if p["field"] == "delivery_date"]
        if d is None:
            lines.append(_line(f"{r['contract_no']}: срок поставки — нет данных."))
        elif d < before:
            lines.append(_line(f"{r['contract_no']}: поставка {fmt_date(d)} — раньше {fmt_date(before)}.", r["delivery_date"]["source"]))
        else:
            lines.append(_line(f"{r['contract_no']}: поставка {fmt_date(d)} — не раньше {fmt_date(before)}.", r["delivery_date"]["source"]))
        for p in early_pending:
            lines.append(_line(f"   ⚠ Несогласованное изменение срока: {fmt_date(p['value'])} — {p['reason']} ({p['file']}).",
                               {"file": p["file"], "para": p["para"], "quote": p["quote"]}))
    early = [r["contract_no"] for r in res["contracts"]
             if r["delivery_date"]["value"] and r["delivery_date"]["value"] < before]
    head = (f"Раньше {fmt_date(before)}: " + (", ".join(early) if early else "ни один договор") + ".")
    return {"title": f"Поставка раньше {fmt_date(before)} и несогласованные сроки", "lines": [_line(head)] + lines}
