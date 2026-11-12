from pathlib import Path

import textnav

DOCS = Path(__file__).parent.parent / "documents"


def load(overrides=None, extra=None):
    files = {p.name: p.read_text(encoding="utf-8") for p in sorted(DOCS.glob("*.txt"))}
    files.update(overrides or {})
    files.update(extra or {})
    return [{"name": n, "text": t} for n, t in files.items()]


def run(files, date="2026-03-10"):
    docs = textnav.extract_all(files, "stub")
    return textnav.resolve(docs, date)


def row(res, no):
    return next(r for r in res["contracts"] if r["contract_no"] == no)


def test_three_contracts_found():
    assert [r["contract_no"] for r in run(load())["contracts"]] == ["STN-101", "STN-102", "STN-103"]


def test_stn101_signed_amendment_applies():
    r = row(run(load()), "STN-101")
    assert r["price"]["value"] == 1_380_000
    assert r["delivery_date"]["value"] == "2026-04-30"
    assert r["delivery_date"]["changed_by"]["file"] == "02_amendment_signed.txt"
    assert r["delivery_date"]["base"]["value"] == "2026-04-15"


def test_stn101_draft_is_pending_not_applied():
    r = row(run(load()), "STN-101")
    pend = {(p["field"], p["value"]) for p in r["pending"]}
    assert ("price", 1_450_000) in pend and ("delivery_date", "2026-05-10") in pend


def test_stn102_price_not_advance_and_email_is_proposal():
    r = row(run(load()), "STN-102")
    assert r["price"]["value"] == 840_000
    assert r["delivery_date"]["value"] == "2026-03-20"      # не 10.04 (оплата) и не 27.03 (предложение)
    assert [(p["field"], p["value"]) for p in r["pending"]] == [("delivery_date", "2026-03-27")]


def test_stn103_missing_price_is_no_data():
    r = row(run(load()), "STN-103")
    assert r["price"]["value"] is None                      # 600 000 — лимит, не цена
    assert r["delivery_date"]["value"] == "2026-04-30"


def test_quote_is_substring_of_paragraph():
    res = run(load())
    docs = {d["file"]: d for d in res["docs"]}
    for r in res["contracts"]:
        for f in ("price", "delivery_date"):
            s = r[f]["source"]
            if s:
                para = next(p for p in docs[s["file"]]["paragraphs"] if p["n"] == s["para"])
                assert textnav.norm(s["quote"]) in textnav.norm(para["text"])


def test_amendment_not_yet_effective_is_pending():
    r = row(run(load(), date="2026-02-20"), "STN-101")      # допсоглашение от 01.03 ещё не в силе
    assert r["price"]["value"] == 1_200_000
    assert any(p["value"] == 1_380_000 for p in r["pending"])


def test_changed_field_is_picked_up():
    t = (DOCS / "03_contract.txt").read_text(encoding="utf-8").replace("840 000 (восемьсот сорок тысяч)", "910 000 (девятьсот десять тысяч)")
    assert row(run(load({"03_contract.txt": t})), "STN-102")["price"]["value"] == 910_000


def test_new_contract_same_format():
    t = (DOCS / "01_contract.txt").read_text(encoding="utf-8").replace("STN-101", "STN-200").replace("1 200 000", "77 000")
    res = run(load(extra={"07_new.txt": t}))
    assert row(res, "STN-200")["price"]["value"] == 77_000


def test_fabricated_quote_is_rejected():
    paras = textnav.split_paragraphs("ДОГОВОР № X-1\nОбщая стоимость 100 рублей")
    doc = textnav.empty_doc("a.txt")
    doc["facts"] = [{"field": "price", "value": 999, "quote": "Общая стоимость 999 рублей", "para": 2},
                    {"field": "price", "value": 500, "quote": "Общая стоимость 100 рублей", "para": 2}]
    out = textnav.verify(doc, paras)
    assert out["facts"] == [] and len(out["warnings"]) == 2


def test_wrong_paragraph_number_is_repaired():
    paras = textnav.split_paragraphs("A\nОбщая стоимость 100 рублей")
    doc = textnav.empty_doc("a.txt")
    doc["facts"] = [{"field": "price", "value": 100, "quote": "Общая стоимость 100 рублей", "para": 1}]
    assert textnav.verify(doc, paras)["facts"][0]["para"] == 2


def test_questions():
    res = run(load())
    a4 = textnav.answer(res, "early")["lines"]
    assert a4[0]["text"].startswith("Раньше 01.04.2026: STN-102")
    assert any("27.03.2026" in l["text"] for l in a4)
    assert "нет данных" in textnav.answer(res, "price", "STN-103")["lines"][0]["text"]
