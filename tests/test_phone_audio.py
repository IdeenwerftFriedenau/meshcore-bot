import asyncio
import hashlib
import importlib.util
import json
import time
from pathlib import Path
from types import SimpleNamespace

import pytest
from aiohttp.test_utils import TestClient, TestServer
from aiortc import AudioStreamTrack, RTCConfiguration, RTCPeerConnection, RTCSessionDescription


@pytest.fixture
def audio(tmp_path):
    spec = importlib.util.spec_from_file_location('phone_audio_test', Path(__file__).parents[1] / 'deploy/phone_audio.py')
    module = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(module)
    device = tmp_path / 'usb.json'
    device.write_text('{"serial":"test-phone"}')
    address = 'AA:BB:CC:DD:EE:FF'
    (tmp_path / 'audio-device.json').write_text(json.dumps({
        'address': address, 'device_path': '/org/bluez/hci0/dev_AA_BB_CC_DD_EE_FF',
        'serial_hash': hashlib.sha256(b'test-phone').hexdigest()}))
    return module, tmp_path, device


def test_profile_binds_bluetooth_to_configured_usb_phone(audio):
    module, root, device = audio
    assert module.profile(root, device)['address'] == 'AA:BB:CC:DD:EE:FF'
    device.write_text('{"serial":"other-phone"}')
    with pytest.raises(ValueError):
        module.profile(root, device)


def test_audio_nodes_never_fall_back_to_other_phone_or_default_mic(audio):
    module, _, _ = audio
    def node(name, media, device=None):
        return {'info': {'props': {'node.name': name, 'media.class': media, 'device.id': device}}}
    objects = [node('alsa_input.default', 'Audio/Source'), node('bluez_input.11_22_33_44_55_66.0', 'Audio/Source'),
               node('bluez_input.AA_BB_CC_DD_EE_FF.0', 'Audio/Source'),
               node('bluez_output.AA_BB_CC_DD_EE_FF.1', 'Audio/Sink')]
    assert module.audio_nodes(objects, 'AA:BB:CC:DD:EE:FF') == {
        'Audio/Source': 'bluez_input.AA_BB_CC_DD_EE_FF.0', 'Audio/Sink': 'bluez_output.AA_BB_CC_DD_EE_FF.1'}
    assert module.audio_nodes(objects, '00:00:00:00:00:00') == {}


async def test_capture_silence_while_phone_idle_then_exact_pcm_frames(audio):
    module, _, _ = audio
    capture = module.Capture()
    first = await capture.recv()
    assert first.pts == 0 and first.sample_rate == 48000 and first.samples == 960
    assert bytes(first.planes[0]) == bytes(1920)
    reader = asyncio.StreamReader()
    process = SimpleNamespace(stdout=reader, returncode=None)
    capture.attach(process)
    pcm = b'\x01\x00' * 960
    reader.feed_data(pcm * 5)
    await asyncio.sleep(.001)
    assert capture.queue.qsize() == 3
    frame = await capture.recv()
    assert frame.pts == 960 and bytes(frame.planes[0]) == pcm
    capture.detach()
    assert capture.queue.empty()
    assert not any(bytes((await capture.recv()).planes[0]))
    capture.stop()


async def test_pcm_process_is_targeted_and_never_writes_recording_files(audio, monkeypatch):
    module, _, _ = audio
    calls = []
    async def spawn(*args, **kwargs):
        calls.append((args, kwargs))
    monkeypatch.setattr(module.asyncio, 'create_subprocess_exec', spawn)
    await module.pcm_process('record', 'bluez_input.AA_BB_CC_DD_EE_FF.0')
    args, options = calls[0]
    assert args[args.index('--target') + 1] == 'bluez_input.AA_BB_CC_DD_EE_FF.0'
    assert 'node.dont-fallback = true' in args[-2]
    assert args[-1] == '-' and options['stdout'] == asyncio.subprocess.PIPE


async def test_real_webrtc_audio_negotiation_owner_isolation_and_stop(audio):
    module, root, device = audio
    class FakeRoute:
        def __init__(self):
            self.root, self.device, self.disconnected = root, device, 0
        async def connect(self):
            return {}
        async def nodes(self):
            return {}
        async def disconnect(self):
            self.disconnected += 1
    route = FakeRoute()
    bridge = module.Bridge(route)
    peer = RTCPeerConnection(RTCConfiguration(iceServers=[]))
    peer.addTrack(AudioStreamTrack())
    received = asyncio.Future()
    @peer.on('track')
    def track(track):
        if not received.done():
            received.set_result(track)
    try:
        await peer.setLocalDescription(await peer.createOffer())
        answer = await bridge.offer('a' * 64, {'type': 'offer', 'sdp': peer.localDescription.sdp})
        await peer.setRemoteDescription(RTCSessionDescription(type=answer['type'], sdp=answer['sdp']))
        remote = await asyncio.wait_for(received, 5)
        frame = await asyncio.wait_for(remote.recv(), 10)
        assert frame.sample_rate == 48000
        assert bridge.current['processes'] == []  # no call, no device capture
        with pytest.raises(ValueError):
            bridge.owned('b' * 64, answer['id'])
        with pytest.raises(ValueError):
            bridge.owned('a' * 64, 'wrong-id')
        assert bridge.owned('a' * 64, answer['id'])
        with pytest.raises(ValueError, match='bereits'):
            await bridge.offer('a' * 64, {'type': 'offer', 'sdp': peer.localDescription.sdp})
        assert (await bridge.status('a' * 64))['owned']
        assert not (await bridge.status('b' * 64))['owned']
    finally:
        await bridge.close()
        await peer.close()
    assert bridge.current is None and route.disconnected == 1


async def test_service_requires_private_key_and_session_owner(audio):
    module, _, _ = audio
    class FakeBridge:
        async def status(self, owner):
            return {'ready': False}
        async def close(self):
            pass
    app = module.application('a' * 64, FakeBridge())
    async with TestClient(TestServer(app)) as client:
        assert (await client.get('/status')).status == 403
        assert (await client.get('/status', headers={'Authorization': 'Bearer ' + 'a' * 64})).status == 403
        headers = {'Authorization': 'Bearer ' + 'a' * 64, 'X-Audio-Owner': 'b' * 64}
        response = await client.get('/status', headers=headers)
        assert response.status == 200 and await response.json() == {'ready': False}


@pytest.mark.parametrize('sdp', ['v=0\r\nm=video 9 UDP/TLS/RTP/SAVPF 96\r\n',
    'v=0\r\nm=audio 9 UDP/TLS/RTP/SAVPF 111\r\nm=application 9 UDP/DTLS/SCTP webrtc-datachannel\r\n'])
async def test_rejects_video_and_datachannels_before_connecting_phone(audio, sdp):
    bridge = audio[0].Bridge()
    with pytest.raises(ValueError):
        await bridge.offer('a' * 64, {'type': 'offer', 'sdp': sdp})
    assert bridge.current is None


async def test_expired_lease_releases_connection(audio, monkeypatch):
    module, _, _ = audio
    bridge = module.Bridge()
    row = {'lease': time.monotonic() - 10}
    bridge.current = row
    closed = []
    async def close(item):
        closed.append(item)
        bridge.current = None
    original_sleep = asyncio.sleep
    async def quick_sleep(delay):
        await original_sleep(0)
    monkeypatch.setattr(bridge, 'close', close)
    monkeypatch.setattr(module.asyncio, 'sleep', quick_sleep)
    await bridge.watchdog(row)
    assert closed == [row]
