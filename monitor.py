#!/usr/bin/python3
"""Prueft Shopify-Produkte auf Restock und meldet per ntfy + Mac-Mitteilung."""
import json, os, shutil, subprocess, sys, urllib.request, datetime

DIR = os.path.dirname(os.path.abspath(__file__))
CONFIG = os.path.join(DIR, "config.json")
STATE = os.path.join(DIR, "state.json")
UA = "Mozilla/5.0 (Macintosh; Intel Mac OS X 10_15_7) AppleWebKit/605.1.15 Safari/605.1.15"


def log(msg):
    print(f"{datetime.datetime.now():%Y-%m-%d %H:%M:%S} {msg}", flush=True)


def fetch(url):
    req = urllib.request.Request(url.split("?")[0].rstrip("/") + ".js", headers={"User-Agent": UA})
    with urllib.request.urlopen(req, timeout=20) as r:
        return json.load(r)


def notify(topic, title, text, url):
    if topic:
        try:
            req = urllib.request.Request(
                f"https://ntfy.sh/{topic}", data=text.encode(),
                headers={"Title": title.encode("ascii", "replace").decode(), "Click": url,
                         "Priority": "high", "Tags": "shirt"})
            urllib.request.urlopen(req, timeout=20)
        except Exception as e:
            log(f"ntfy-Fehler: {e}")
    t = text.replace('"', "'"); ti = title.replace('"', "'")
    if shutil.which("osascript"):
        subprocess.run(["osascript", "-e", f'display notification "{t}" with title "{ti}" sound name "Glass"'])


def main():
    cfg = json.load(open(CONFIG))
    state = json.load(open(STATE)) if os.path.exists(STATE) else {}
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
            was = state.get(key)
            if avail and was is not True:
                link = p["url"].split("?")[0] + f"?variant={v['id']}"
                price = f"{v['price'] / 100:.2f} EUR".replace(".", ",")
                notify(os.environ.get("NTFY_TOPIC") or cfg.get("ntfy_topic"), f"Restock: {data['title']}",
                       f"{v['title']} ist wieder da ({price})", link)
                log(f"RESTOCK {data['title']} {v['title']}")
            elif was is not None and was != avail:
                log(f"ausverkauft {data['title']} {v['title']}")
            state[key] = avail
        log(f"geprueft: {data['title']}")
    json.dump(state, open(STATE, "w"))


if __name__ == "__main__":
    if "--test" in sys.argv:
        cfg = json.load(open(CONFIG))
        notify(os.environ.get("NTFY_TOPIC") or cfg.get("ntfy_topic"), "Restock-Monitor Test", "Benachrichtigungen funktionieren", cfg["products"][0]["url"])
    else:
        main()
