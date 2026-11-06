import importlib.util
import json
import re
import struct
from pathlib import Path
from types import SimpleNamespace

import pytest


@pytest.fixture
def remote(tmp_path):
    path = Path(__file__).parents[1] / 'deploy/local-console/phone_remote.py'
    spec = importlib.util.spec_from_file_location('phone_remote_test', path)
    module = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(module)
    profile = tmp_path / 'device.json'
    profile.write_text(json.dumps({'serial': 'test-device'}))
    calls = []
    now = [1000.0]

    def runner(command, **kwargs):
        calls.append((command, kwargs))
        return SimpleNamespace(returncode=0, stdout=b'')

    phone = module.PhoneRemote(tmp_path / 'private', profile, runner, lambda: now[0])
    return module, phone, calls, now


def pair(phone, calls):
    result = phone.pair()
    script = calls[-1][1]['input'].decode()
    code = re.search(r'Freigabecode: (\d{8})', script)[1]
    assert code not in json.dumps(result)
    assert code not in str(calls[-1][0])
    token = phone.authorize(code)
    return f'meshcore_phone={token}'


def test_pairing_private_persistence_csrf_expiry_and_logout(remote):
    _, phone, calls, now = remote
    cookie = pair(phone, calls)
    session = phone.session(cookie)
    assert session['authorized']
    phone.require(cookie, session['csrf'])
    with pytest.raises(PermissionError):
        phone.require(cookie, '')
    stored = (phone.root / 'sessions.json').read_text()
    assert cookie.split('=')[1] not in stored and 'test-device' not in stored
    assert (phone.root / 'sessions.json').stat().st_mode & 0o777 == 0o600
    phone.logout(cookie)
    assert not phone.session(cookie)['authorized']
    now[0] += 61
    cookie = pair(phone, calls)
    now[0] += 7 * 86400 + 1
    assert not phone.session(cookie)['authorized']


def test_pairing_limits_attempts_and_rate_and_device_binding(remote):
    _, phone, calls, now = remote
    cookie = pair(phone, calls)
    phone.profile.write_text('{"serial":"different-device"}')
    assert not phone.session(cookie)['authorized']
    with pytest.raises(ValueError, match='warten'):
        phone.pair()
    now[0] += 61
    phone.pair()
    for _ in range(5):
        with pytest.raises(ValueError):
            phone.authorize('not-a-code')
    with pytest.raises(ValueError, match='gesperrt'):
        phone.authorize('12345678')


@pytest.mark.parametrize('address,expected', [('127.0.0.1', True), ('::1', True),
    ('100.110.16.69', True), ('fd7a:115c:a1e0::1', True), ('192.168.8.200', False),
    ('8.8.8.8', False), ('100.128.0.1', False), ('invalid', False)])
def test_remote_access_addresses(remote, address, expected):
    assert remote[0].trusted_address(address) is expected


def test_frame_validation_cache_and_no_disk_frames(remote):
    _, phone, calls, now = remote
    image = b'\x89PNG\r\n\x1a\n' + struct.pack('>I', 13) + b'IHDR' + struct.pack('>II', 1080, 2400) + b'\0' * 20
    phone.runner = lambda *args, **kwargs: SimpleNamespace(returncode=0, stdout=image)
    assert phone.frame() == image
    assert phone.dimensions == (1080, 2400)
    phone.runner = lambda *args, **kwargs: pytest.fail('cache not reused')
    assert phone.frame() == image
    now[0] += 2
    phone.runner = lambda *args, **kwargs: SimpleNamespace(returncode=0, stdout=b'private error text')
    with pytest.raises(ValueError):
        phone.frame()
    assert not phone.root.exists()


def test_input_bounds_and_shell_quoting(remote):
    _, phone, calls, now = remote
    phone.dimensions = (1080, 2400)
    for data in [{'action': 'tap', 'x': 1080, 'y': 10}, {'action': 'key', 'key': 'call'},
                 {'action': 'tap', 'x': True, 'y': 1}, {'action': 'text', 'text': 'a\nb'},
                 {'action': 'text', 'text': '%s'}, {'action': 'shell', 'text': 'reboot'}]:
        with pytest.raises(ValueError):
            phone.control(data)
    phone.control({'action': 'text', 'text': "hello';$(echo bad)"})
    assert calls[-1][1]['input'] == b'input text \'hello\'"\'"\';$(echo%sbad)\'\n'
    assert calls[-1][0][:5] == ['/usr/bin/adb', '-H', '127.0.0.1', '-P', '18137']
    now[0] += 1
    phone.control({'action': 'swipe', 'x': 2, 'y': 3, 'x2': 9, 'y2': 20})
    assert calls[-1][1]['input'] == b'input swipe 2 3 9 20 350\n'


def test_status_whitelists_only_device_and_battery(remote):
    _, phone, _, _ = remote
    phone.runner = lambda *args, **kwargs: SimpleNamespace(returncode=0, stdout=b'moto g35 5G\n16\n USB powered: true\n level: 49\n temperature: 240\n secret: private\n')
    assert phone.status() == {'connected': True, 'model': 'moto g35 5G', 'android': '16',
                              'battery': 49, 'temperature': 24, 'charging': True}


def test_fixed_code_persists_hashed_and_lockout_survives_restart(remote):
    module, phone, calls, now = remote
    phone.set_access_code('826395')
    access = (phone.root / 'access.json').read_text()
    assert '826395' not in access and 'test-device' not in access
    assert (phone.root / 'access.json').stat().st_mode & 0o777 == 0o600
    token = phone.authorize('826395')
    cookie = 'meshcore_phone=' + token
    assert phone.session(cookie)['authorized']
    for _ in range(5):
        with pytest.raises(ValueError):
            phone.authorize('wrong')
    restarted = module.PhoneRemote(phone.root, phone.profile, phone.runner, phone.clock)
    with pytest.raises(ValueError, match='gesperrt'):
        restarted.authorize('826395')
    now[0] += 1801
    assert restarted.authorize('826395')
    restarted.set_access_code('923857')
    assert not restarted.session(cookie)['authorized']
    with pytest.raises(ValueError):
        restarted.authorize('826395')
    assert restarted.authorize('923857')


def test_telephone_validation_and_no_implicit_call(remote):
    _, phone, calls, now = remote
    for value in ['*123#', '123;reboot', 'tel:123', '', None, '+12+3']:
        with pytest.raises(ValueError):
            phone.telephone({'action': 'dial', 'number': value})
    assert not calls
    result = phone.telephone({'action': 'dial', 'number': '+49 (30) 123-456'})
    script = calls[-1][1]['input'].decode()
    assert 'android.intent.action.DIAL' in script and 'tel:+4930123456' in script
    assert 'action.CALL' not in script and 'bestätigen' in result['message']
    with pytest.raises(ValueError, match='warten'):
        phone.telephone({'action': 'dial', 'number': '123'})


def test_contacts_safe_parsing_no_disk(remote):
    _, phone, calls, _ = remote
    phone.runner = lambda *a, **k: SimpleNamespace(returncode=0, stdout=(
        b'Row: 0 _id=1, data1=+49 30 123, display_name=Test, Firma\n'
        b'Row: 1 _id=2, data1=*123#, display_name=Service\n'
        b'Row: 2 _id=3, data1=+49 30 123, display_name=Test, Firma\n'
        b'Row: 3 _id=4, data1=456, display_name=<script>dummy</script>\n'))
    result = phone.contacts()['contacts']
    assert len(result) == 2
    assert {'name': 'Test, Firma', 'number': '+4930123'} in result
    assert not phone.root.exists()


def test_call_status_and_hangup_guard(remote):
    _, phone, calls, _ = remote
    def output(text):
        phone.runner = lambda *a, **k: SimpleNamespace(returncode=0, stdout=text.encode())
    output('mCallState=0\nmCallState=0\nmCallIncomingNumber=123')
    assert phone.call_status() == {'state': 'idle', 'number': None}
    with pytest.raises(ValueError, match='Kein laufender'):
        phone.telephone({'action': 'hangup'})
    output('mCallState=1\nmCallIncomingNumber=123\n')
    assert phone.call_status() == {'state': 'ringing', 'number': '123'}
    output('permission error')
    assert phone.call_status()['state'] == 'unknown'


def test_direct_call_only_on_explicit_action_and_idle_phone(remote):
    _, phone, calls, now = remote
    original = phone.runner
    def runner(command, **kwargs):
        result = original(command, **kwargs)
        if kwargs.get('input') == b'dumpsys telephony.registry\n':
            result.stdout = b'mCallState=0\n'
        return result
    phone.runner = runner
    phone.telephone({'action': 'call', 'number': '+49 (30) 123-456'})
    assert calls[-1][1]['input'] == b'am start -a android.intent.action.CALL -d tel:+4930123456 2>&1\n'
    now[0] += 3
    for state in ['mCallState=1', 'mCallState=2', 'unknown']:
        phone.runner = lambda *a, state=state, **k: SimpleNamespace(returncode=0, stdout=state.encode())
        with pytest.raises(ValueError, match='Anruf aktiv'):
            phone.telephone({'action': 'call', 'number': '030123456'})
    for number in ['*123#', '123;reboot', None]:
        with pytest.raises(ValueError):
            phone.telephone({'action': 'call', 'number': number})
