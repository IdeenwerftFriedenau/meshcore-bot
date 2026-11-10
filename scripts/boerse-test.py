"""Börsenbot-Härtetest gegen echte Kursdaten: Frage, Kanal, erwartetes Muster.

    PAUSE=1.2 python scripts/boerse-test.py                 # mit Schein-KI ([KI] = an die KI weitergereicht)
    PAUSE=1.2 python scripts/boerse-test.py /etc/.../config.toml   # auf dem Pi mit echter KI
"""
import asyncio
import re
import sys
import time

import httpx

from meshcore_ai_bot.assistant import Assistant
from meshcore_ai_bot.config import Config, load_config
from meshcore_ai_bot.settings import refresh_secrets
from meshcore_ai_bot.store import Store

Q = "ASSET"  # quote expected (price + 52W)
KI = "KI"     # model expected (no quote)
OV = "OV"     # overview
CASES = [
 # Deutsche Aktien
 ("Wie steht Rheinmetall?", Q, r"^Rheinmetall [\d.]+,\d\d €"), ("SAP", Q, r"^SAP [\d.,]+ €"), ("siemens aktie", Q, r"^Siemens [\d.,]+ €"),
 ("Allianz?", Q, r"^Allianz"), ("Wie steht die Deutsche Bank?", Q, r"^Deutsche Bank"), ("Commerzbank Kurs", Q, r"^Commerzbank"),
 ("VW", Q, r"^Volkswagen"), ("Volkswagen Aktie", Q, r"^Volkswagen"), ("Mercedes", Q, r"^Mercedes"), ("BMW", Q, r"^(BMW|Bayerische)"),
 ("Porsche", Q, r"^Porsche"), ("Telekom", Q, r"Telekom"), ("Deutsche Post", Q, r"DHL|Post"), ("DHL", Q, r"DHL"),
 ("Lufthansa", Q, r"Lufthansa"), ("Bayer", Q, r"^Bayer"), ("BASF", Q, r"^BASF"), ("Adidas", Q, r"^[Aa]didas"), ("Puma", Q, r"^Puma"),
 ("Infineon", Q, r"^Infineon"), ("Hensoldt", Q, r"^Hensoldt"), ("Renk", Q, r"^Renk"), ("Zalando", Q, r"^Zalando"),
 ("Siemens Energy", Q, r"^Siemens Energy"), ("Munich Re", Q, r"Munich"), ("Münchener Rück", Q, r"Munich"), ("Eon", Q, r"E\.?ON"),
 ("RWE", Q, r"^RWE"), ("Hannover Rück", Q, r"Hannover"), ("Heidelberg Materials", Q, r"Heidelberg"),
 # US/international
 ("Apple", Q, r"^Apple [\d.,]+ €"), ("Wie steht Nvidia?", Q, r"NVIDIA|Nvidia"), ("Tesla Aktie", Q, r"^Tesla"), ("Microsoft", Q, r"^Microsoft"),
 ("Amazon", Q, r"^Amazon"), ("Google", Q, r"Alphabet"), ("Meta", Q, r"^Meta"), ("Netflix", Q, r"^Netflix"), ("Coca Cola", Q, r"Coca"),
 ("Novo Nordisk", Q, r"Novo"), ("ASML", Q, r"ASML"), ("Nestle", Q, r"Nestl"), ("Palantir", Q, r"Palantir"), ("AMD", Q, r"Advanced Micro|AMD"),
 ("Berkshire", Q, r"Berkshire"), ("Toyota", Q, r"Toyota"), ("Samsung", Q, r"Samsung"), ("Alibaba", Q, r"Alibaba"),
 # Ticker/ISIN
 ("AAPL", Q, r"Apple"), ("NVDA", Q, r"NVIDIA|Nvidia"), ("DE0007164600", Q, r"^SAP"), ("US0378331005", Q, r"Apple"), ("RHM.DE", Q, r"Rheinmetall"),
 # Indizes
 ("DAX", Q, r"^DAX [\d.]+ Pkt"), ("Wie steht der Dax heute?", Q, r"^DAX"), ("MDAX", Q, r"^MDAX"), ("TecDAX", Q, r"^TecDAX"), ("Dow Jones", Q, r"^Dow"),
 ("S&P 500", Q, r"^S&P 500"), ("Nasdaq", Q, r"^Nasdaq"), ("Nikkei", Q, r"^Nikkei"), ("Euro Stoxx", Q, r"^Euro Stoxx"),
 # ETFs
 ("MSCI World", Q, r"MSCI World"), ("Wie läuft der MSCI World ETF?", Q, r"MSCI World"), ("All World", Q, r"All-World"), ("Emerging Markets", Q, r"MSCI EM"),
 # Krypto
 ("Bitcoin", Q, r"^Bitcoin [\d.]+ €"), ("btc", Q, r"^Bitcoin"), ("Ethereum", Q, r"^Ethereum"), ("Solana", Q, r"^Solana"), ("XRP", Q, r"^XRP"),
 ("Dogecoin", Q, r"^Dogecoin"), ("Was macht Bitcoin?", Q, r"^Bitcoin"),
 # Rohstoffe / Währungen
 ("Gold", Q, r"^Gold"), ("Goldpreis", Q, r"^Gold"), ("Silber", Q, r"^Silber"), ("Ölpreis", Q, r"Öl"), ("Brent", Q, r"Brent"), ("Erdgas", Q, r"Erdgas"),
 ("Kupfer", Q, r"Kupfer"), ("Kaffee Preis", Q, r"Kaffee"), ("Euro Dollar", Q, r"^Euro in Dollar 1,\d{4}"), ("Dollarkurs", Q, r"^Euro in Dollar"), ("Franken", Q, r"Franken"),
 # Vergleiche / Rat
 ("Vergleiche Apple, Nvidia und Microsoft", Q, r"Apple.*N[Vv][Ii][Dd][Ii][Aa].*Microsoft"), ("SAP oder Siemens?", Q, r"SAP.*Siemens"),
 ("Bitcoin vs Gold", Q, r"Bitcoin.*Gold"), ("Soll ich Rheinmetall kaufen?", Q, r"keine Anlageberatung"), ("Lohnt sich Tesla noch?", Q, r"Tesla.*keine Anlageberatung"),
 ("Was hältst du von Nvidia?", Q, r"N[Vv][Ii][Dd][Ii][Aa]"), ("Einschätzung Bitcoin", Q, r"Einschätzung"), ("Tageshoch von SAP", Q, r"Tag [\d.,]+–"),
 ("52 Wochen Hoch Allianz", Q, r"52W"), ("Rheinmetall Prognose", Q, r"Rheinmetall"),
 # Tippfehler
 ("rheinmetal", Q, r"Rheinmetall"), ("bitcon", Q, r"Bitcoin"), ("siemns", Q, r"Siemens"), ("nvdia", Q, r"NVIDIA|Nvidia"), ("telkom", Q, r"Telekom"),
 # Überblick
 ("Wie läuft die Börse?", OV, r"^Märkte: DAX"), ("Marktüberblick", OV, r"^Märkte"), ("Was machen die Märkte?", OV, r"^Märkte"), ("Börse heute?", OV, r"^Märkte"),
 # KI / Smalltalk / Erklärungen
 ("Hallo", KI, None), ("Danke!", KI, None), ("Was ist ein ETF?", KI, None), ("Was bedeutet KGV?", KI, None), ("Erklär mir Dividende", KI, None),
 ("Warum fällt Rheinmetall?", KI, None), ("Was ist der Unterschied zwischen Aktie und ETF?", KI, None), ("Wie kaufe ich Aktien?", KI, None),
 ("Was ist ein Sparplan?", KI, None), ("Welche Aktie soll ich kaufen?", KI, None), ("Wer bist du?", KI, None), ("Wie wird das Wetter?", None, r"."),
 ("ping", None, r"."), ("hilfe", None, r"."),
]
GENERAL = [  # #fragdenbot: only clear quote questions are taken
 ("Wie steht der DAX?", Q, r"^DAX"), ("Was macht die SAP Aktie?", Q, r"^SAP"), ("Bitcoin Kurs", Q, r"^Bitcoin"), ("Goldpreis aktuell", Q, r"^Gold"),
 ("Was ist Gold?", KI, None), ("Was kostet ein Döner?", KI, None), ("Wie hoch ist der Fernsehturm?", KI, None), ("Erste Hilfe Kurs Berlin", KI, None),
 ("Wann kommt der Bus?", None, r"."), ("Kurs auf Hamburg", KI, None), ("Wie viel ist ein Pfund in Gramm?", KI, None),
]

async def main():
    path = sys.argv[1] if len(sys.argv) > 1 else None
    config = load_config(path) if path else Config()
    if path:
        refresh_secrets(config, path)
    store = Store(":memory:")
    fails = 0
    async with httpx.AsyncClient(timeout=20) as client:
        bot = Assistant(config, store, client)
        if not path:
            config.ai._api_key = "x"
            async def fake(messages, *, tools, model=None):
                return {"choices": [{"message": {"content": "[KI]"}}]}
            bot._completion = fake
        for topic, cases in (("boerse", CASES), ("", GENERAL)):
            for q, kind, pattern in cases:
                await asyncio.sleep(float(__import__("os").environ.get("PAUSE", "0")))
                t = time.monotonic()
                a = await bot.answer(q, topic=topic)
                text = a.text
                quote = bool(re.search(r"52W|^Märkte", text))
                ok = len(text.encode()) <= 480
                if kind in (Q, OV):
                    ok = ok and quote and bool(re.search(pattern, text))
                elif kind == KI:
                    ok = ok and not quote
                else:
                    ok = ok and bool(re.search(pattern, text))
                fails += not ok
                print(f"{'OK ' if ok else 'XX '}[{topic or 'allg'}] {q} ({time.monotonic()-t:.1f}s) -> {text[:300]} [{len(text.encode())}B]", flush=True)
    store.close()
    total = len(CASES) + len(GENERAL)
    print(f"\n{total - fails}/{total} OK")

asyncio.run(main())
