import datetime as dt
import json
import os
import re
import sys
from pathlib import Path
from zoneinfo import ZoneInfo

import requests
from bs4 import BeautifulSoup

BASIS = "https://sws2.maxmanager.xyz"
ABRUF = BASIS + "/inc/ajax-php_konnektor.inc.php"
SEITEN = [BASIS + "/index.php?mode=bed", BASIS + "/"]
KOPF = {"Origin": BASIS, "Referer": SEITEN[0], "X-Requested-With": "XMLHttpRequest"}
GROQ_URL = "https://api.groq.com/openai/v1/chat/completions"
GROQ_MODELL = "openai/gpt-oss-120b"
ZONE = ZoneInfo("Europe/Berlin")
TAGE = ["Montag", "Dienstag", "Mittwoch", "Donnerstag", "Freitag"]
KATEGORIEN = ["VEGANER RENNER", "HAUPTGERICHT", "MENSA SPECIAL", "ALL TIME CLASSICS"]
MENSEN = {"flandernstrasse": "FLANDERN", "stadtmitte": "STADTMITTE"}


def zahl(s):
    m = re.search(r"\d+(?:[.,]\d+)?", s or "")
    return float(m.group(0).replace(",", ".")) if m else None


def text(el):
    return " ".join(el.get_text(" ", strip=True).split())


def finde_nummern(sitzung):
    """Sucht die Mensa-Nummern per Textsuche im rohen HTML der Auswahlliste."""
    for url in SEITEN:
        html = sitzung.get(url, timeout=30).text
        nummern = {}
        for name, wort in MENSEN.items():
            for m in re.finditer(wort, html, re.I):
                vorn = html[max(0, m.start() - 300):m.start()]
                werte = re.findall(r"value\s*=\s*[\"']?(\d+)", vorn, re.I)
                if werte:
                    nummern[name] = int(werte[-1])
                    ausschnitt = html[max(0, m.start() - 80):m.start() + 60]
                    print(f"Nummer {name} = {nummern[name]} (Seite {url})")
                    print("  Ausschnitt:", ausschnitt.replace("\n", " "))
                    break
        if len(nummern) == len(MENSEN):
            return nummern
        print(f"Nicht beide Nummern gefunden auf {url}. Anfang der Antwort:")
        print(html[:800])
    sys.exit("FEHLER: Mensa-Nummern nicht gefunden.")


def wochentage():
    heute = dt.datetime.now(ZONE).date()
    montag = heute - dt.timedelta(days=heute.weekday())
    if heute.weekday() >= 5:
        montag += dt.timedelta(days=7)
    return [montag + dt.timedelta(days=i) for i in range(5)]


def hole_tag(sitzung, nummer, datum):
    montag = datum - dt.timedelta(days=datum.weekday())
    daten = {
        "func": "make_spl", "locId": str(nummer), "date": datum.isoformat(), "lang": "de",
        "startThisWeek": montag.isoformat(),
        "startNextWeek": (montag + dt.timedelta(days=7)).isoformat(),
    }
    for versuch in range(3):
        try:
            r = sitzung.post(ABRUF, data=daten, headers=KOPF, timeout=30)
            r.raise_for_status()
            return r.text
        except requests.RequestException as fehler:
            print(f"  Abruf-Fehler (Versuch {versuch + 1}): {fehler}")
    sys.exit(f"FEHLER: Abruf für {datum} nicht möglich.")


def preis_studis(t):
    t = t.replace("€", "EUR").replace("\xa0", " ")
    m = re.search(r"STUDIS\s*\|\s*(\d+(?:[.,]\d{1,2})?)\s*EUR", t, re.I)
    if not m:
        m = re.search(r"EUR\s*(\d+(?:[.,]\d{1,2})?)\s*/", t)
    return zahl(m.group(1)) if m else None


def naehrwert(t, label):
    m = re.search(re.escape(label) + r"\s*(\d+(?:[.,]\d+)?)", t, re.I)
    return zahl(m.group(1)) if m else None


def kcal(t):
    m = re.search(r"Brennwert:\s*[\d.,]+\s*kj\s*/\s*(\d+(?:[.,]\d+)?)\s*kcal", t, re.I)
    return zahl(m.group(1)) if m else None


def gericht_name(zeile):
    for sel in (".mealText span", ".splMealText span", ".col-md-8 span", "span"):
        el = zeile.select_one(sel)
        if el and text(el):
            return text(el)
    return text(zeile)


def lies_tag(html):
    suppe = BeautifulSoup(html, "html.parser")
    kat, gerichte, kategorien = None, [], set()
    for zeile in suppe.select(".row"):
        klassen = zeile.get("class", [])
        if "gruppenkopf" in klassen:
            kat = re.split(r"\s+PREIS\b", text(zeile), flags=re.I)[0].strip()
            kategorien.add(kat)
        elif "splMeal" in klassen and kat and any(k in kat.upper() for k in KATEGORIEN):
            t = text(zeile)
            symbole = " ".join(
                (e.get("title") or e.get("alt") or "") for e in zeile.find_all(["img", "span"])
            ).lower()
            vegan = "vegan" in symbole
            gerichte.append({
                "kategorie": kat.title(), "name": gericht_name(zeile),
                "preis_studierende": preis_studis(t),
                "vegan": vegan, "vegetarisch": vegan or "vegetar" in symbole,
                "kcal_100g": kcal(t), "eiweiss_100g": naehrwert(t, "Eiweiß:"),
            })
    return gerichte, kategorien


def bewerte(alle):
    """Ein einziger Groq-Aufruf für alle verschiedenen Gerichte."""
    kopf = {"Authorization": "Bearer " + os.environ.get("GROQ_API_KEY", "")}
    info = {}
    for g in alle:
        info.setdefault(g["name"], g)
    namen = sorted(info)
    zeilen = [f"{i}|{n}|{info[n]['kcal_100g']}|{info[n]['eiweiss_100g']}" for i, n in enumerate(namen)]
    auftrag = (
        "Du bewertest Mensa-Gerichte für Studierende. Jede Zeile: id|Name|kcal pro 100 g|Eiweiß g pro 100 g. "
        'Antworte NUR mit JSON: {"bewertungen":[{"id":0,"gesundheit":7,"kcal_portion":650,'
        '"begruendung":"höchstens 12 Wörter"}]}. '
        "gesundheit ist eine ganze Zahl von 1 (sehr ungesund) bis 10 (sehr gesund). "
        "kcal_portion ist die geschätzte Kalorienzahl einer üblichen Mensa-Portion. "
        "Genau ein Eintrag pro Zeile."
    )
    body = {
        "model": GROQ_MODELL, "temperature": 0.2, "reasoning_effort": "low",
        "max_completion_tokens": 6000, "response_format": {"type": "json_object"},
        "messages": [{"role": "system", "content": auftrag}, {"role": "user", "content": "\n".join(zeilen)}],
    }
    try:
        r = requests.post(GROQ_URL, headers=kopf, json=body, timeout=120)
        if r.status_code != 200:
            raise RuntimeError(f"HTTP {r.status_code}: {r.text[:600]}")
        antwort = r.json()
        print("Verbrauchte Tokens:", antwort.get("usage"))
        inhalt = json.loads(antwort["choices"][0]["message"]["content"])
        ergebnis = {}
        for b in inhalt["bewertungen"]:
            i = b.get("id")
            if isinstance(i, int) and 0 <= i < len(namen):
                ergebnis[namen[i]] = b
        print(f"Bewertet: {len(ergebnis)} von {len(namen)} Gerichten")
        return ergebnis
    except Exception as fehler:
        print("GROQ-FEHLER:", fehler)
        try:
            m = requests.get("https://api.groq.com/openai/v1/models", headers=kopf, timeout=30).json()
            print("Verfügbare Modelle:", ", ".join(x["id"] for x in m.get("data", [])))
        except Exception as fehler2:
            print("Modellliste nicht abrufbar:", fehler2)
        return None


def main():
    sitzung = requests.Session()
    nummern = finde_nummern(sitzung)
    ergebnis, alle = {}, []
    for name, nummer in nummern.items():
        tage, kategorien = [], set()
        for datum in wochentage():
            html = hole_tag(sitzung, nummer, datum)
            gerichte, kats = lies_tag(html)
            kategorien |= kats
            print(f"{name} {datum} ({TAGE[datum.weekday()]}): {len(gerichte)} Gerichte")
            if not gerichte:
                print("  Anfang der Antwort:", html[:500].replace("\n", " "))
            tage.append({"datum": datum.isoformat(), "wochentag": TAGE[datum.weekday()], "gerichte": gerichte})
            alle += gerichte
        print(f"{name}: Kategorien in der Quelle: {sorted(kategorien)}")
        ergebnis[name] = tage

    bew = bewerte(alle) if alle else None
    for g in alle:
        b = (bew or {}).get(g["name"], {})
        gs, kp = b.get("gesundheit"), b.get("kcal_portion")
        g["gesundheit"] = min(10, max(1, round(gs))) if isinstance(gs, (int, float)) else None
        g["kcal_portion"] = round(kp) if isinstance(kp, (int, float)) else None
        g["begruendung"] = b.get("begruendung")

    stand = dt.datetime.now(ZONE).replace(microsecond=0).isoformat()
    Path("data").mkdir(exist_ok=True)
    for name, tage in ergebnis.items():
        daten = {"stand": stand, "tage": tage}
        Path(f"data/{name}.json").write_text(json.dumps(daten, ensure_ascii=False, indent=1), encoding="utf-8")
        print(f"Geschrieben: data/{name}.json")
    if bew is None:
        sys.exit(1)  # Dateien sind geschrieben (ohne KI-Werte), der Lauf wird aber rot


if __name__ == "__main__":
    main()
