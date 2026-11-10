#!/usr/bin/env python3
"""Preiswaechter: ein bestimmtes Teil in einer Groesse bei allen serioesen Shops beobachten und sofort melden,
sobald es lieferbar ist und hoechstens max_preis kostet.

Laeuft doppelt wie der Restock-Melder: auf dem Mac per launchd alle 2 Minuten und auf GitHub Actions
(zekrom-cmd/restock-monitor) fuer die Zeit, in der der Mac schlaeft. Deshalb ohne Abhaengigkeit zum Finder,
nur curl_cffi. Jede Kopie hat ihren eigenen Stand (preiswaechter-stand.json).

preiswaechter.json -> "teile": [{"name", "groesse", "max_preis", "seiten": [{"shop", "url", ...}]}]
"github": false an einer Seite: nur der Mac prueft sie (P&C und Anson's sperren GitHub mit 403).
Push, wenn ein Angebot in der Groesse lieferbar ist, <= max_preis kostet und das neu ist (vorher nicht
lieferbar oder zu teuer, oder jetzt billiger als zuletzt). Ist ein Shop mehrmals hintereinander nicht lesbar
(gesperrt, umgebaut), kommt eine Warn-Push, damit nichts unbemerkt durchrutscht.

Aufruf: preiswaechter.py [--zeigen] [--test]
  --zeigen  aktuellen Stand aller Angebote in der Groesse ausgeben, nichts melden, nichts speichern
  --test    eine Test-Push schicken
"""
import datetime
import html
import json
import os
import re
import subprocess
import sys
import time
from pathlib import Path

from curl_cffi import requests

DIR = Path(__file__).resolve().parent
CONFIG = DIR / "preiswaechter.json"
STAND = DIR / "preiswaechter-stand.json"
FINDER_CONFIG = DIR / "config.json"  # ntfy-Thema des Grail Radar, das abonniert das Handy
WARNEN_NACH = 15  # so viele Fehlversuche hintereinander, dann Warn-Push (Mac: 30 Minuten)
DEC = json.JSONDecoder()


def log(msg):
    print(f"{datetime.datetime.now():%Y-%m-%d %H:%M:%S} [preiswaechter] {msg}", flush=True)


def sitzung():
    return requests.Session(impersonate="chrome", timeout=30, headers={"Accept-Language": "de-DE,de;q=0.9"})


def seite_laden(sz, url):
    """Bei Sperre (403/429) oder Aussetzer einmal mit frischer Sitzung nachfassen (P&C sperrt gern kurz)."""
    r = sz.get(url)
    if r.status_code != 200:
        time.sleep(4)
        r = sitzung().get(url)
    if r.status_code != 200:
        raise ValueError(f"HTTP {r.status_code}")
    return r.text


def groesse_kurz(g):
    """'S = 48' -> 'S', 'xl' -> 'XL'."""
    return (g or "").split("=")[0].strip().upper()


def euro(x):
    return f"{x:,.2f} €".replace(",", "X").replace(".", ",").replace("X", ".")


def angebot(shop, key, groesse, da, preis, url):
    return {"shop": shop, "key": key, "groesse": groesse_kurz(groesse), "da": bool(da),
            "preis": float(preis) if preis else None, "url": url}


def _ld_produkte(text):
    for m in re.finditer(r'<script[^>]+application/ld\+json[^>]*>(.*?)</script>', text, re.S):
        try:
            d = json.loads(m.group(1))
        except ValueError:
            continue
        for x in (d if isinstance(d, list) else d.get("@graph", [d])):
            if isinstance(x, dict) and x.get("@type") in ("Product", "ProductGroup"):
                yield x


def _ld_angebote(text):
    """Alle Einzelangebote aus ld+json, auch die in einem AggregateOffer verschachtelten."""
    for p in _ld_produkte(text):
        o = p.get("offers") or []
        for x in (o if isinstance(o, list) else [o]):
            if x.get("@type") == "AggregateOffer":
                yield from (x.get("offers") or [])
            else:
                yield x


# --- Shops: jede Funktion liefert eine Liste von angebot(...) fuer alle Groessen ---

def zalando(sz, seite, stand):
    """Groessen aus dem Seiten-Cache (runtime['hydratePartial']), Preis gilt fuer alle Groessen."""
    text = seite_laden(sz, seite["url"])
    groessen = {}
    for m in re.finditer(r"runtime\['hydratePartial'\]\(", text):
        try:
            obj, _ = DEC.raw_decode(text[m.end():])
        except ValueError:
            continue
        for v in (obj.get("cache") or {}).values():
            for s in (((v or {}).get("data") or {}).get("product") or {}).get("simples") or []:
                menge = ((s.get("offer") or {}).get("stock") or {}).get("quantity")
                if s.get("size") and menge:
                    groessen[s["size"]] = menge != "OUT_OF_STOCK"
    preis = re.search(r'"trackingCurrentAmount":([\d.]+)', text)
    preis = float(preis.group(1)) if preis else None
    return [angebot("Zalando", f"zalando:{g}", g, da, preis, seite["url"]) for g, da in groessen.items()]


def sportano(sz, seite, stand):
    text = seite_laden(sz, seite["url"])
    preis = next((float(o["price"]) for o in _ld_angebote(text) if o.get("price")), None)
    return [angebot("Sportano", f"sportano:{m.group(1)}", m.group(1), m.group(2) == "true", preis, seite["url"])
            for m in re.finditer(r'"label":"([A-Z0-9]+)","products":\["\d+"\](?:,"outOfStockLabel":"[^"]*")?,'
                                 r'"isInStock":(true|false)', text)]


def peek(sz, seite, stand):
    """Peek & Cloppenburg und Anson's (gleiche Technik): ld+json mit einem Angebot je Groesse."""
    name = "Anson's" if "ansons." in seite["url"] else "Peek & Cloppenburg"
    return [angebot(name, f"{name}:{o['size']}", o["size"], "InStock" in (o.get("availability") or ""),
                    o.get("price"), seite["url"]) for o in _ld_angebote(seite_laden(sz, seite["url"])) if o.get("size")]


def engelhorn(sz, seite, stand):
    """ld+json-Angebote je Groesse, die Groesse steht nur im Link (…-schwarz-s-A1358928B.html)."""
    erg = []
    for o in _ld_angebote(seite_laden(sz, seite["url"])):
        m = re.search(r"-([a-z0-9]{1,4})-A\w+\.html", o.get("url") or "")
        if m:
            erg.append(angebot("Engelhorn", f"engelhorn:{o.get('sku')}", m.group(1), "InStock" in (o.get("availability") or ""),
                               o.get("price"), "https://www.engelhorn.de" + o["url"]))
    return erg


def breuninger(sz, seite, stand):
    """Produktdaten (entsProductData) enthalten alle Farben; Groessen ohne outOfStock sind lieferbar."""
    text = seite_laden(sz, seite["url"])
    m = re.search(r'<script id="entsProductData" type="application/json">(.*?)</script>', text, re.S)
    if not m:
        raise ValueError("Produktdaten fehlen")
    for nr, a in json.loads(html.unescape(m.group(1)))["articles"].items():
        if (a["colors"]["active"]["name"] or "").upper() == seite["farbe"].upper():
            preis = (a.get("price") or {}).get("schemaPriceInCents")
            link = seite["url"].split("?")[0] + f"?variant={nr}"
            return [angebot("Breuninger", f"breuninger:{groesse_kurz(v['name'])}", v["name"], not v.get("outOfStock"),
                            preis / 100 if preis else None, link) for v in a["sizes"]["values"]]
    raise ValueError(f"Farbe {seite['farbe']} nicht gefunden")


def miinto(sz, seite, stand):
    """Marktplatz: dasselbe Modell gibt es mehrfach von verschiedenen Boutiquen. Die Uebersichtsseiten liefern
    alle Links; neue Links werden einmal gelesen (Modellnummer + Farbe, gemerkt in stand['miinto']), passende
    jede Runde mit ihren Groessen und Preisen."""
    meta = stand.setdefault("miinto", {})
    links = set()
    for url in seite["suche"]:
        links |= set(re.findall(seite["link_muster"], seite_laden(sz, url)))
    if not links:
        raise ValueError("Uebersicht ohne Treffer")
    erg = []
    for link in sorted(links):
        url = "https://www.miinto.de" + link
        if link in meta and not meta[link]["passt"]:
            continue
        text = seite_laden(sz, url)
        if link not in meta:
            klar = re.sub(r"\s+", " ", html.unescape(re.sub(r"<[^>]+>", " ", text)))
            modell = re.search(r"Designer ID : (\S+)", klar)
            farbe = " ".join(re.findall(r"(?:Designer farbe|Farbe) : (\S+)", klar))
            meta[link] = {"passt": bool(modell and seite["modell"] in modell.group(1)
                                        and seite["farbe"].lower() in farbe.lower())}
            if not meta[link]["passt"]:
                continue
        m = re.search(r'"sizes":(\[.*?\])', text)
        for s in json.loads(m.group(1)) if m else []:
            erg.append(angebot("Miinto", f"miinto:{link[-36:]}:{s['marketSize']}", s["marketSize"], s.get("isAvailable"),
                               s["price"] / 100, url))
        time.sleep(0.3)
    if not any(v["passt"] for v in meta.values()):
        raise ValueError("kein passendes Angebot mehr in der Uebersicht")
    return erg


ABRUFE = {"zalando": zalando, "sportano": sportano, "peek": peek, "engelhorn": engelhorn,
          "breuninger": breuninger, "miinto": miinto}


def ntfy_thema(cfg):
    if os.environ.get("NTFY_TOPIC"):
        return os.environ["NTFY_TOPIC"]
    if cfg.get("ntfy_topic"):
        return cfg["ntfy_topic"]
    if FINDER_CONFIG.exists():
        return json.loads(FINDER_CONFIG.read_text()).get("ntfy_topic")
    return None


def melden(cfg, titel, text, url, prio=5):
    thema = ntfy_thema(cfg)
    if thema:
        body = {"topic": thema, "title": titel, "message": text, "click": url, "priority": prio}
        for versuch in range(3):
            try:
                r = requests.post("https://ntfy.sh/", json=body, timeout=20)
                if r.status_code == 200:
                    break
                log(f"ntfy antwortet {r.status_code}: {r.text[:100]}")
            except Exception as e:
                log(f"ntfy-Fehler: {e}")
            time.sleep(5)
    else:
        log("kein ntfy-Thema gesetzt")
    if sys.platform == "darwin":
        t, ti = text.replace('"', "'"), titel.replace('"', "'")
        subprocess.run(["osascript", "-e", f'display notification "{t}" with title "{ti}" sound name "Glass"'])


def main():
    cfg = json.loads(CONFIG.read_text())
    if "--test" in sys.argv:
        melden(cfg, "Preiswächter Test", "Benachrichtigungen funktionieren", "https://ntfy.sh/")
        return
    zeigen = "--zeigen" in sys.argv
    stand = json.loads(STAND.read_text()) if STAND.exists() else {}
    stand.setdefault("angebote", {})
    stand.setdefault("fehler", {})
    sz = sitzung()
    for teil in cfg["teile"]:
        gr = teil["groesse"].upper()
        for seite in teil["seiten"]:
            if os.environ.get("GITHUB_ACTIONS") and seite.get("github") is False:
                continue  # Shop sperrt Rechenzentrums-Adressen, prueft nur der Mac
            sk = f"{teil['name']}|{seite['shop']}|{seite.get('url') or seite['suche'][0]}"
            try:
                angebote = ABRUFE[seite["shop"]](sz, seite, stand)
                if not angebote:
                    raise ValueError("keine Groessen gefunden (Seite umgebaut?)")
            except Exception as e:
                n = stand["fehler"].get(sk, 0) + 1
                stand["fehler"][sk] = n
                log(f"{teil['name']} / {seite['shop']}: {str(e)[:150]} ({n}x hintereinander)")
                if n == WARNEN_NACH and not zeigen:
                    melden(cfg, "Preiswächter: Shop nicht lesbar",
                           f"{seite['shop']} geht seit {n} Versuchen nicht ({str(e)[:80]}). Bitte selbst reinschauen.",
                           seite.get("url") or seite["suche"][0], prio=3)
                continue
            stand["fehler"][sk] = 0
            treffer = [a for a in angebote if a["groesse"] == gr]
            if not treffer:
                log(f"{teil['name']} / {seite['shop']}: Größe {gr} nicht im Angebot")
            for a in treffer:
                k = f"{teil['name']}|{a['key']}"
                alt = stand["angebote"].get(k, {})
                passt = bool(a["da"] and a["preis"] and a["preis"] <= teil["max_preis"])
                billiger = passt and alt.get("passt") and alt.get("preis") and a["preis"] < alt["preis"] - 0.5
                log(f"{teil['name']} / {a['shop']}: {gr} {'lieferbar' if a['da'] else 'weg'}, "
                    f"{euro(a['preis']) if a['preis'] else '?'}{'  <-- PASST' if passt else ''}")
                if not zeigen and passt and (not alt.get("passt") or billiger):
                    melden(cfg, teil["name"], f"Größe {gr} bei {a['shop']}: {euro(a['preis'])}"
                           + (f" (vorher {euro(alt['preis'])})" if billiger else ""), a["url"])
                stand["angebote"][k] = {"da": a["da"], "preis": a["preis"], "passt": passt}
    if not zeigen:
        stand["zuletzt"] = datetime.datetime.now().isoformat(timespec="seconds")
        STAND.write_text(json.dumps(stand, ensure_ascii=False, indent=1))


if __name__ == "__main__":
    main()
