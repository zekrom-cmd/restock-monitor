#!/usr/bin/python3
"""Prueft Shopify-Produkte auf Restock und meldet per ntfy + Mac-Mitteilung.

- Meldet Restocks und Preissenkungen (Sale wird mit Prozent angezeigt).
- Wiederholt eine Meldung, bis in der Nachricht auf "Gesehen" getippt wird,
  das Teil wieder weg ist oder die Frist abgelaufen ist.
  "Gesehen" schreibt die Meldungs-ID in das ntfy-Thema <thema>-ok, das jeder
  Lauf liest (so stoppen Mac und GitHub gleichzeitig).
"""
import json, os, shutil, subprocess, sys, time, urllib.request, datetime

DIR = os.path.dirname(os.path.abspath(__file__))
CONFIG = os.path.join(DIR, "config.json")
STATE = os.path.join(DIR, "state.json")
UA = "Mozilla/5.0 (Macintosh; Intel Mac OS X 10_15_7) AppleWebKit/605.1.15 Safari/605.1.15"

WIEDERHOLEN_ALLE = 4 * 60      # Sekunden zwischen zwei Erinnerungen (Mac laeuft alle 5 Min)
WIEDERHOLEN_BIS = 90 * 60      # danach gibt der Melder Ruhe
WIEDERHOLEN_MAX = 10           # hoechstens so viele Erinnerungen


def log(msg):
    print(f"{datetime.datetime.now():%Y-%m-%d %H:%M:%S} {msg}", flush=True)


def fetch(url):
    req = urllib.request.Request(url.split("?")[0].rstrip("/") + ".js", headers={"User-Agent": UA})
    with urllib.request.urlopen(req, timeout=20) as r:
        return json.load(r)


def euro(cent):
    return f"{cent / 100:.2f}".replace(".", ",") + " EUR"


def notify(topic, title, text, url, meldung_id=None):
    if topic:
        actions = [{"action": "view", "label": "Zum Shop", "url": url}]
        if meldung_id:
            actions.insert(0, {"action": "http", "label": "Gesehen", "url": f"https://ntfy.sh/{topic}-ok",
                               "method": "POST", "body": meldung_id, "clear": True})
        body = {"topic": topic, "title": title, "message": text, "click": url,
                "priority": 5, "tags": ["shirt"], "actions": actions}
        try:
            req = urllib.request.Request("https://ntfy.sh/", data=json.dumps(body).encode(),
                                         headers={"Content-Type": "application/json"})
            urllib.request.urlopen(req, timeout=20)
        except Exception as e:
            log(f"ntfy-Fehler: {e}")
    t = text.replace('"', "'"); ti = title.replace('"', "'")
    if shutil.which("osascript"):
        subprocess.run(["osascript", "-e", f'display notification "{t}" with title "{ti}" sound name "Glass"'])


def gesehen(topic):
    """IDs, auf die in den letzten 12 h 'Gesehen' getippt wurde."""
    if not topic:
        return set()
    try:
        req = urllib.request.Request(f"https://ntfy.sh/{topic}-ok/json?poll=1&since=12h", headers={"User-Agent": UA})
        with urllib.request.urlopen(req, timeout=20) as r:
            zeilen = r.read().decode().splitlines()
        return {json.loads(z).get("message", "").strip() for z in zeilen if z.strip()}
    except Exception as e:
        log(f"Gesehen-Abfrage fehlgeschlagen: {e}")
        return set()


def main():
    cfg = json.load(open(CONFIG))
    topic = os.environ.get("NTFY_TOPIC") or cfg.get("ntfy_topic")
    state = json.load(open(STATE)) if os.path.exists(STATE) else {}
    preise = state.setdefault("_preise", {})
    offen = state.setdefault("_offen", {})
    jetzt = int(time.time())
    verfuegbar = {}

    for p in cfg["products"]:
        try:
            data = fetch(p["url"])
        except Exception as e:
            log(f"Abruf fehlgeschlagen {p['url']}: {e}")
            continue
        for v in data["variants"]:
            if p.get("variants") and v["title"] not in p["variants"]:
                continue
            key = str(v["id"])
            avail = bool(v["available"])
            verfuegbar[key] = avail
            was = state.get(key)
            preis = int(v["price"])
            vorher = preise.get(key)
            statt = int(v.get("compare_at_price") or 0)
            rabatt = round((1 - preis / statt) * 100) if statt > preis else 0

            grund = None
            if avail and was is not True:
                grund = "restock"
            elif avail and vorher and preis < vorher:
                grund = "billiger"

            if grund:
                link = p["url"].split("?")[0] + f"?variant={v['id']}"
                size = v["title"].split("/")[-1].strip()
                wer = f"Größe {size}" if size != "Default Title" else "Das Teil"
                if grund == "restock":
                    text = f"{wer} ist wieder da: {euro(preis)}" + (f" statt {euro(statt)}" if rabatt else "")
                else:
                    text = f"{wer} jetzt günstiger: {euro(preis)}, vorher {euro(vorher)}"
                name = p.get("name") or data["title"]
                title = f"SALE −{rabatt} %: {name}" if rabatt >= 10 else name
                mid = f"{key}-{jetzt}"
                notify(topic, title, text, link, mid)
                offen[mid] = {"variante": key, "titel": title, "text": text, "link": link,
                              "seit": jetzt, "zuletzt": jetzt, "anzahl": 0}
                log(f"{'RESTOCK' if grund == 'restock' else 'BILLIGER'} {data['title']} {v['title']} {euro(preis)}"
                    + (f" -{rabatt}%" if rabatt else ""))
            elif was is not None and was != avail:
                log(f"ausverkauft {data['title']} {v['title']}")
            state[key] = avail
            preise[key] = preis
        log(f"geprueft: {data['title']}")

    # Erinnerungen
    if offen:
        ok = gesehen(topic)
        for mid, m in list(offen.items()):
            weg = verfuegbar.get(m["variante"]) is False
            if mid in ok or weg or jetzt - m["seit"] > WIEDERHOLEN_BIS or m["anzahl"] >= WIEDERHOLEN_MAX:
                log(f"Erinnerung beendet {mid} ({'gesehen' if mid in ok else 'ausverkauft' if weg else 'Frist'})")
                del offen[mid]
            elif jetzt - m["zuletzt"] >= WIEDERHOLEN_ALLE:
                m["anzahl"] += 1
                m["zuletzt"] = jetzt
                notify(topic, f"Noch nicht gesehen: {m['titel']}", m["text"], m["link"], mid)
                log(f"Erinnerung {m['anzahl']} {mid}")

    json.dump(state, open(STATE, "w"))


if __name__ == "__main__":
    if "--test" in sys.argv:
        cfg = json.load(open(CONFIG))
        notify(os.environ.get("NTFY_TOPIC") or cfg.get("ntfy_topic"), "Restock-Monitor Test",
               "Benachrichtigungen funktionieren. Tippe zum Testen auf „Gesehen“.",
               cfg["products"][0]["url"], f"test-{int(time.time())}")
    else:
        main()
