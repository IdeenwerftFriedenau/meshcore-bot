"""Kurzfassung des Rundumtest-Berichts: Zusammenfassungen und auffällige Fälle, handytauglich."""

import re
import sys

LINES = open(sys.argv[1] if len(sys.argv) > 1 else "/tmp/rundumtest/bericht.txt", encoding="utf-8").read().splitlines()
LIMIT = int(sys.argv[2]) if len(sys.argv) > 2 else 40
KEEP = re.compile(r"^(Rundumtest |FERTIG|Dauer |1 echte|2 Börsen|3 Alarm|Echte Fragen:|Börsenbefehle:|\d+/\d+ OK|"
                  r"\d+ Fälle in|Annahme:|Antwortzeit:|Pakete:|Werkzeuge:|Auffälligkeiten:|Kategorien|"
                  r"\d+/\d+ Kennzahlen|SEC-Kontakt|#{6,}|=+ ZUSAMMENFASSUNG)")
BAD = re.compile(r"AUFFÄLLIG|^XX |^FEHLER")


def cut(text: str, n: int = 170) -> str:
    text = text.rstrip()
    return text if len(text) <= n else text[: n - 1] + "…"


shown = 0
printed: set[int] = set()
for i, line in enumerate(LINES):
    if i in printed:
        continue
    if KEEP.search(line.strip()):
        print(cut(line.strip(), 400))
    elif BAD.search(line.strip()):
        shown += 1
        if shown > LIMIT:
            continue
        print("--")
        if i and re.match(r"#\d+ ", LINES[i - 1].strip()) and i - 1 not in printed:
            print(cut(LINES[i - 1].strip()))
        print(cut(line.strip()))
        printed.add(i)
        if i + 1 < len(LINES) and LINES[i + 1].strip() and not KEEP.search(LINES[i + 1].strip()) \
                and not BAD.search(LINES[i + 1].strip()):
            print(cut(LINES[i + 1].strip()))
            printed.add(i + 1)
if shown > LIMIT:
    print(f"... und {shown - LIMIT} weitere auffällige Fälle in /tmp/rundumtest/bericht.txt")
