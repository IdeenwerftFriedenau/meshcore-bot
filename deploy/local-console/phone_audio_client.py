"""Authenticated loopback client; the browser never receives the bridge key."""

import json
import re
import urllib.error
import urllib.request
from pathlib import Path


class PhoneAudioClient:
    def __init__(self, root):
        self.root = Path(root)
        self.opener = urllib.request.build_opener(urllib.request.ProxyHandler({}), NoRedirect)

    def request(self, path, owner, data=None):
        if path not in ("status", "offer", "heartbeat", "stop") or not re.fullmatch("[a-f0-9]{64}", owner):
            raise ValueError("Ungültige Audioanfrage")
        try:
            key = (self.root / "audio.key").read_text().strip()
            if not re.fullmatch("[a-f0-9]{64}", key):
                raise OSError()
            request = urllib.request.Request(
                "http://127.0.0.1:18181/" + path,
                data=None if data is None else json.dumps(data).encode(),
                headers={
                    "Authorization": "Bearer " + key,
                    "X-Audio-Owner": owner,
                    "Content-Type": "application/json",
                },
            )
            with self.opener.open(request, timeout=35 if path == "offer" else 8) as response:
                result = json.loads(response.read(64001))
            if path == "status":
                result["available"] = True
                try:
                    url = json.loads((self.root / "audio-access.json").read_text()).get("url", "")
                    if re.fullmatch(r"https://[a-z0-9.-]+\.ts\.net(?::[0-9]{1,5})?/", url):
                        result["secure_url"] = url + "#phone"
                except (OSError, ValueError):
                    pass
            return result
        except urllib.error.HTTPError as error:
            if error.code == 409:
                raise ValueError(
                    "Gesprächston noch nicht bereit oder bereits in einem anderen Browser belegt"
                ) from None
            raise ValueError("Privater Audiodienst ist nicht verfügbar") from None
        except (OSError, ValueError):
            if path == "status":
                return {
                    "available": False,
                    "paired": False,
                    "ready": False,
                    "busy": False,
                    "message": "Gesprächston-Dienst ist noch nicht bereit",
                }
            raise ValueError("Gesprächston-Dienst nicht erreichbar; Bluetooth-Verbindung prüfen") from None


class NoRedirect(urllib.request.HTTPRedirectHandler):
    def redirect_request(self, req, fp, code, msg, headers, newurl):
        return None
