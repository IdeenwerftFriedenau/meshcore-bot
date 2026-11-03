"""Federal-state codes and official warning area identifiers."""

STATES = {
    "BW": ("Baden-Württemberg", "08"),
    "BY": ("Bayern", "09"),
    "BER": ("Berlin", "11"),
    "BB": ("Brandenburg", "12"),
    "HB": ("Bremen", "04"),
    "HH": ("Hamburg", "02"),
    "HE": ("Hessen", "06"),
    "MV": ("Mecklenburg-Vorpommern", "13"),
    "NI": ("Niedersachsen", "03"),
    "NW": ("Nordrhein-Westfalen", "05"),
    "RP": ("Rheinland-Pfalz", "07"),
    "SL": ("Saarland", "10"),
    "SN": ("Sachsen", "14"),
    "ST": ("Sachsen-Anhalt", "15"),
    "SH": ("Schleswig-Holstein", "01"),
    "TH": ("Thüringen", "16"),
}
ALIASES = {"BE": "BER", "BL": "BER", "NS": "NI", "NRW": "NW", "SA": "ST"}


def canonical(value):
    value = str(value).upper().removeprefix("DE-")
    return ALIASES.get(value, value)


def ordered(values):
    values = set(values)
    return [code for code in STATES if code in values]


def bbk_states(info):
    """Use target ARS/AGS, never sender identity or ambiguous place names."""
    keys = []
    for item in info.get("parameter", []):
        if item.get("valueName") == "warnVerwaltungsbereiche":
            import re

            keys.extend(re.findall(r"(?<![0-9])[0-9]{12}(?![0-9])", str(item.get("value", ""))))
    for area in info.get("area", []):
        for item in area.get("geocode", []):
            if item.get("valueName", "").upper() in {"ARS", "AGS", "GEOCODE"}:
                value = str(item.get("value", ""))
                if value.isdigit() and len(value) in {8, 12}:
                    keys.append(value)
    if any(set(key) == {"0"} for key in keys):
        return set(STATES)
    return {code for code, (_, prefix) in STATES.items() if any(k.startswith(prefix) for k in keys)}
