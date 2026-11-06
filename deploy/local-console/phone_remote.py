"""Paired, private ADB screen/control bridge. No frames or raw tokens on disk."""

import hashlib
import hmac
import ipaddress
import json
import os
import re
import secrets
import shlex
import struct
import subprocess
import tempfile
import threading
import time
from http.cookies import CookieError, SimpleCookie
from pathlib import Path

PROFILE = Path('/var/lib/meshcore-ai-bot/phone/device.json')
COOKIE = 'meshcore_phone'
KEYS = {'home': 3, 'back': 4, 'recent': 187, 'wake': 224, 'sleep': 223,
        'volume_up': 24, 'volume_down': 25}


def trusted_address(address):
    try:
        ip = ipaddress.ip_address(address)
        if getattr(ip, 'ipv4_mapped', None):
            ip = ip.ipv4_mapped
        return ip.is_loopback or ip in ipaddress.ip_network('100.64.0.0/10') or ip in ipaddress.ip_network('fd7a:115c:a1e0::/48')
    except ValueError:
        return False


def digest(value):
    return hashlib.sha256(value.encode()).hexdigest()


class PhoneRemote:
    def __init__(self, root, profile=PROFILE, runner=subprocess.run, clock=time.time):
        self.root, self.profile, self.runner, self.clock = Path(root), Path(profile), runner, clock
        self.lock = threading.RLock()
        self.pending = None
        self.requests = []
        self.frame_cache = None
        self.dimensions = None
        self.last_control = 0

    def device(self):
        try:
            serial = json.loads(self.profile.read_text())['serial']
        except (OSError, ValueError, KeyError, TypeError):
            raise ValueError('Motorola-Verbindung noch nicht eingerichtet') from None
        if not isinstance(serial, str) or not re.fullmatch(r'[A-Za-z0-9_-]{1,80}', serial):
            raise ValueError('Ungültige Gerätekonfiguration')
        return serial

    def adb(self, *args, script=None, timeout=10):
        # Explicit host prevents starting an unmanaged ADB daemon if watchdog is down.
        command = ['/usr/bin/adb', '-H', '127.0.0.1', '-P', '18137', '-s', self.device(), *args]
        try:
            result = self.runner(command, input=script.encode() if script is not None else None,
                                 capture_output=True, timeout=timeout, check=False)
        except (OSError, subprocess.SubprocessError):
            raise ValueError('Handy nicht erreichbar; USB-Verbindung prüfen') from None
        if result.returncode:
            raise ValueError('Handy nicht erreichbar oder Aktion nicht verfügbar')
        return result.stdout

    def _read(self):
        try:
            data = json.loads((self.root / 'sessions.json').read_text())
            return {k: v for k, v in data.items() if isinstance(v, dict) and v.get('expires', 0) > self.clock()}
        except (OSError, ValueError, TypeError):
            return {}

    def _save(self, sessions):
        self.root.mkdir(mode=0o700, parents=True, exist_ok=True)
        fd, name = tempfile.mkstemp(prefix='.sessions-', dir=self.root)
        try:
            with os.fdopen(fd, 'w') as stream:
                json.dump(sessions, stream)
            os.replace(name, self.root / 'sessions.json')
        finally:
            if os.path.exists(name):
                os.unlink(name)

    def token(self, cookie):
        jar = SimpleCookie()
        try:
            jar.load(cookie or '')
            token = jar[COOKIE].value
        except (CookieError, KeyError, ValueError):
            return ''
        return token if re.fullmatch(r'[A-Za-z0-9_-]{43}', token) else ''

    def session(self, cookie):
        with self.lock:
            token = self.token(cookie)
            row = self._read().get(digest(token)) if token else None
            if not row or row.get('device') != digest(self.device()):
                return {'authorized': False}
            return {'authorized': True, 'expires': row['expires'],
                    'csrf': hmac.new(token.encode(), b'phone-control', hashlib.sha256).hexdigest()}

    def require(self, cookie, csrf):
        session = self.session(cookie)
        if not session['authorized'] or not hmac.compare_digest(session['csrf'], csrf or ''):
            raise PermissionError('Bitte Handy-Fernbedienung zuerst freigeben')

    def pair(self):
        with self.lock:
            now = self.clock()
            self.requests = [t for t in self.requests if now - t < 3600]
            if self.requests and (now - self.requests[-1] < 60 or len(self.requests) >= 5):
                raise ValueError('Bitte warten, bevor du einen neuen Freigabecode anforderst')
            self.requests.append(now)
            code = f'{secrets.randbelow(100000000):08d}'
            salt = secrets.token_hex(16)
            self.pending = None
            # Code only travels through ADB stdin, never argv, HTTP responses or logs.
            self.adb('shell', '-T', 'sh', script='cmd notification post -t "Handy-Fernbedienung" meshcore-remote '
                     + shlex.quote('Freigabecode: ' + code + ' · 2 Minuten gültig. Nur im eigenen Pi-Dashboard eingeben.') + '\n')
            self.pending = {'hash': digest(salt + code), 'salt': salt, 'expires': now + 120,
                            'attempts': 0, 'device': digest(self.device())}
            return {'message': 'Freigabecode in der Benachrichtigung am Motorola ablesen (2 Minuten gültig).'}

    def set_access_code(self, code):
        """Provision only locally, never through the public HTTP API."""
        if not isinstance(code, str) or not re.fullmatch(r'[0-9]{4,12}', code):
            raise ValueError('Zugangscode benötigt 4–12 Ziffern')
        with self.lock:
            salt = secrets.token_hex(32)
            row = {'salt': salt, 'iterations': 600000,
                   'hash': hashlib.pbkdf2_hmac('sha256', code.encode(), bytes.fromhex(salt), 600000).hex(),
                   'device': digest(self.device())}
            self._save_private('access.json', row)
            self._save_private('attempts.json', {'failures': 0, 'blocked_until': 0})
            # Changing a credential revokes existing browser sessions.
            self._save({})

    def _save_private(self, name, data):
        self.root.mkdir(mode=0o700, parents=True, exist_ok=True)
        fd, temporary = tempfile.mkstemp(prefix='.access-', dir=self.root)
        try:
            with os.fdopen(fd, 'w') as stream:
                json.dump(data, stream)
            os.replace(temporary, self.root / name)
        finally:
            if os.path.exists(temporary):
                os.unlink(temporary)

    def _private(self, name):
        try:
            data = json.loads((self.root / name).read_text())
            return data if isinstance(data, dict) else {}
        except (OSError, ValueError):
            return {}

    def authorize(self, code):
        with self.lock:
            now = self.clock()
            attempts = self._private('attempts.json')
            if attempts.get('blocked_until', 0) > now:
                raise ValueError('Zugang nach Fehlversuchen gesperrt; bitte in 30 Minuten erneut versuchen')
            fixed = self._private('access.json')
            valid_fixed = False
            if fixed and isinstance(code, str) and re.fullmatch(r'[0-9]{4,12}', code):
                value = hashlib.pbkdf2_hmac('sha256', code.encode(), bytes.fromhex(fixed['salt']), fixed['iterations']).hex()
                valid_fixed = hmac.compare_digest(value, fixed['hash']) and fixed['device'] == digest(self.device())
            pending = self.pending
            valid_once = bool(pending and now <= pending['expires'] and pending['attempts'] < 5
                              and isinstance(code, str) and re.fullmatch(r'[0-9]{8}', code)
                              and hmac.compare_digest(pending['hash'], digest(pending['salt'] + code))
                              and pending['device'] == digest(self.device()))
            if not valid_fixed and not valid_once:
                if pending:
                    pending['attempts'] += 1
                failures = attempts.get('failures', 0) + 1
                self._save_private('attempts.json', {'failures': failures if failures < 5 else 0,
                                   'blocked_until': now + 1800 if failures >= 5 else 0})
                raise ValueError('Zugangscode ungültig oder abgelaufen')
            self.pending = None
            self._save_private('attempts.json', {'failures': 0, 'blocked_until': 0})
            device = digest(self.device())
            token = secrets.token_urlsafe(32)
            sessions = self._read()
            # Bounded state; pairing does not silently revoke the current browser.
            sessions = dict(sorted(sessions.items(), key=lambda item: item[1]['expires'])[-7:])
            sessions[digest(token)] = {'expires': self.clock() + 7 * 86400, 'device': device}
            self._save(sessions)
            return token

    def logout(self, cookie):
        with self.lock:
            sessions = self._read()
            sessions.pop(digest(self.token(cookie)), None)
            self._save(sessions)
            self.frame_cache = None
            return {'message': 'Dieser Browser wurde abgemeldet.'}

    def status(self):
        # Explicit whitelist: no SIM identifiers, phone numbers, apps, WLAN names or location.
        output = self.adb('shell', '-T', 'sh', script='getprop ro.product.model; getprop ro.build.version.release; dumpsys battery\n').decode(errors='replace')
        lines = output.splitlines()
        fields = dict(re.findall(r'^\s*(level|temperature|USB powered|AC powered):\s*([^\n]+)', output, re.M))
        return {'connected': True, 'model': lines[0][:60] if lines else 'Motorola',
                'android': lines[1][:16] if len(lines) > 1 else '',
                'battery': int(fields['level']) if fields.get('level', '').isdigit() else None,
                'temperature': int(fields['temperature']) / 10 if fields.get('temperature', '').isdigit() else None,
                'charging': fields.get('USB powered', '').strip() == 'true' or fields.get('AC powered', '').strip() == 'true'}

    def frame(self):
        with self.lock:
            if self.frame_cache and self.clock() - self.frame_cache[0] < 1:
                return self.frame_cache[1]
            frame = self.adb('exec-out', 'screencap', '-p', timeout=12)
            if len(frame) < 33 or len(frame) > 12 * 1024 * 1024 or frame[:8] != b'\x89PNG\r\n\x1a\n' or frame[12:16] != b'IHDR':
                raise ValueError('Kein gültiges Handybild empfangen')
            width, height = struct.unpack('>II', frame[16:24])
            if not 100 <= width <= 8000 or not 100 <= height <= 8000:
                raise ValueError('Ungültige Bildschirmgröße')
            self.dimensions = (width, height)
            self.frame_cache = (self.clock(), frame)
            return frame

    def control(self, data):
        with self.lock:
            if self.clock() - self.last_control < .15:
                raise ValueError('Bitte einen Moment warten')
            kind = data.get('action')
            if kind == 'key' and isinstance(data.get('key'), str) and data['key'] in KEYS:
                command = 'input keyevent ' + str(KEYS[data['key']])
            elif kind in ('tap', 'swipe'):
                if not self.dimensions:
                    raise ValueError('Livebild zuerst einschalten')
                width, height = self.dimensions
                names = ('x', 'y') if kind == 'tap' else ('x', 'y', 'x2', 'y2')
                values = [data.get(name) for name in names]
                if any(type(v) is not int or not 0 <= v < (width if i % 2 == 0 else height) for i, v in enumerate(values)):
                    raise ValueError('Ungültige Bildschirmposition')
                command = 'input ' + kind + ' ' + ' '.join(map(str, values))
                if kind == 'swipe':
                    duration = data.get('duration', 350)
                    if type(duration) is not int or not 100 <= duration <= 2000:
                        raise ValueError('Ungültige Wischdauer')
                    command += ' ' + str(duration)
            elif kind == 'text':
                text = data.get('text')
                if not isinstance(text, str) or not 1 <= len(text) <= 300 or any(not 32 <= ord(c) <= 126 for c in text) or '%s' in text:
                    raise ValueError('Bitte 1–300 Zeichen ohne Umlaute oder Zeilenumbrüche eingeben; alternativ Bildschirmtastatur nutzen')
                command = 'input text ' + shlex.quote(text.replace(' ', '%s'))
            else:
                raise ValueError('Diese Aktion ist nicht erlaubt')
            self.adb('shell', '-T', 'sh', script=command + '\n')
            self.last_control = self.clock()
            self.frame_cache = None
            return {'ok': True}


if __name__ == '__main__':
    import getpass

    code = getpass.getpass('Fester Motorola-Zugangscode: ')
    PhoneRemote('/var/lib/meshcore-gateway/phone-remote').set_access_code(code)
    print('Privater Zugangscode eingerichtet; bisherige Browserfreigaben widerrufen.')
