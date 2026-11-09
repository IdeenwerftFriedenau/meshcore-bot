import asyncio
import importlib.util
import json
from pathlib import Path

import pytest

ROOT = Path(__file__).parents[1]


def load(name, path):
    spec = importlib.util.spec_from_file_location(name, ROOT / path)
    module = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(module)
    return module


@pytest.fixture
def jarvis():
    return load('jarvis_test', 'deploy/jarvis.py')


def test_numbers_normalize_german_formats(jarvis):
    assert jarvis.normalize_number('+49 152 1234567') == '01521234567'
    assert jarvis.normalize_number('0049-152-1234567') == '01521234567'
    assert jarvis.normalize_number('01521234567') == '01521234567'
    assert jarvis.normalize_number('+43 660 1234567') == '00436601234567'
    assert jarvis.normalize_number('') == ''
    assert jarvis.normalize_number(None) == ''
    assert jarvis.normalize_number('112') == ''
    assert jarvis.allowed_numbers(' +491521234567, ,0301234567 ') == {'01521234567', '0301234567'}


def test_call_state_reading_matches_dashboard(jarvis):
    assert jarvis.parse_call_state('mCallState=1\n  mCallIncomingNumber=+491521234567\n') == (
        'ringing', '+491521234567')
    two = 'mCallState=1\nmCallIncomingNumber=0151\nmCallState=0\nmCallIncomingNumber=0152\n'
    assert jarvis.parse_call_state(two) == ('ringing', None)
    assert jarvis.parse_call_state('mCallState=2\nmCallIncomingNumber=0151\n') == ('active', None)
    assert jarvis.parse_call_state('') == ('unknown', None)


def test_speech_text_is_cleaned_and_limited(jarvis):
    assert jarvis.speakable('**Hallo** [Link](https://example.org) # Test') == 'Hallo Link Test'
    long = 'Satz eins ist hier. ' * 60
    out = jarvis.speakable(long)
    assert len(out) <= 600 and out.endswith('.')
    assert jarvis.wants_goodbye('okay danke tschüss')
    assert jarvis.wants_goodbye('Bitte leg auf')
    assert not jarvis.wants_goodbye('wie wird das wetter morgen')


class FakePhone:
    def __init__(self, states):
        self.states = list(states)
        self.answered = 0
        self.hung_up = 0

    async def call_state(self):
        return self.states.pop(0) if len(self.states) > 1 else self.states[0]

    async def answer(self):
        self.answered += 1
        return True

    async def hang_up(self):
        self.hung_up += 1


class FakeRoute:
    def __init__(self):
        self.events = []

    async def connect(self):
        self.events.append('connect')

    async def disconnect(self):
        self.events.append('disconnect')

    async def wait_nodes(self):
        return {'Audio/Source': 'in', 'Audio/Sink': 'out'}


class FakeLock:
    def __init__(self, free=True):
        self.free, self.held = free, False

    def acquire(self):
        self.held = self.free
        return self.free

    def release(self):
        self.held = False


class FakeCall:
    def __init__(self):
        self.runs = 0

    async def run(self):
        self.runs += 1
        return 'goodbye'


async def test_unapproved_or_hidden_numbers_are_never_answered(jarvis):
    for number in ('01701111111', None):
        phone, route, call = FakePhone([('ringing', number)]), FakeRoute(), FakeCall()
        bot = jarvis.Jarvis({'01521234567'}, phone, route, FakeLock(), lambda nodes, call=call: call)
        assert await bot.step() == 'ignored'
        assert phone.answered == 0 and call.runs == 0 and route.events == []


async def test_approved_caller_is_answered_and_everything_released(jarvis):
    phone, route, call, lock = FakePhone([('ringing', '+491521234567')]), FakeRoute(), FakeCall(), FakeLock()
    bot = jarvis.Jarvis({'01521234567'}, phone, route, lock, lambda nodes: call)
    assert await bot.step() == 'goodbye'
    assert phone.answered == 1 and call.runs == 1 and phone.hung_up == 1
    assert route.events == ['connect', 'disconnect'] and not lock.held


async def test_browser_bridge_holding_audio_blocks_jarvis(jarvis):
    phone, route, call = FakePhone([('ringing', '01521234567')]), FakeRoute(), FakeCall()
    bot = jarvis.Jarvis({'01521234567'}, phone, route, FakeLock(free=False), lambda nodes: call)
    assert await bot.step() == 'busy'
    assert phone.answered == 0 and route.events == []


def test_empty_allow_list_refuses_to_start(jarvis):
    with pytest.raises(ValueError):
        jarvis.Jarvis(frozenset(), None, None, None, None)


def test_lock_is_shared_with_browser_bridge(jarvis, tmp_path):
    audio = load('phone_audio_lock_test', 'deploy/phone_audio.py')
    path = tmp_path / 'audio.lock'
    assert audio.claim_audio(path) is None  # Jarvis not installed: browser works as before
    lock = jarvis.AudioLock(path)
    assert lock.acquire()
    with pytest.raises(ValueError):
        audio.claim_audio(path)
    lock.release()
    handle = audio.claim_audio(path)
    assert handle is not None
    assert not jarvis.AudioLock(path).acquire()
    audio.release_audio(handle)
    assert jarvis.AudioLock(path).acquire()


class FakeRecognizer:
    def __init__(self, texts):
        self.texts = texts

    def AcceptWaveform(self, chunk):  # noqa: N802 - Vosk API
        return chunk == b'end'

    def Result(self):  # noqa: N802 - Vosk API
        return json.dumps({'text': self.texts.pop(0)})

    def FinalResult(self):  # noqa: N802 - Vosk API
        return json.dumps({'text': ''})


class FakeEars:
    def __init__(self, texts):
        self.texts = texts

    def recognizer(self):
        return FakeRecognizer(self.texts)


class FakeBrain:
    def __init__(self):
        self.questions = []

    async def ask(self, history):
        self.questions.append(history[-1]['content'])
        return '**Morgen** wird es sonnig.'


class FakeVoice:
    rate = 22050

    def __init__(self):
        self.spoken = []

    async def synthesize(self, text):
        self.spoken.append(text)
        return b''


class FakePlayback:
    class stdin:  # noqa: N801
        @staticmethod
        def write(data):
            pass

        @staticmethod
        async def drain():
            pass


async def test_conversation_answers_until_goodbye(jarvis):
    voice, brain = FakeVoice(), FakeBrain()
    call = jarvis.Call(FakePhone([('active', None)]), {}, FakeEars(['wie wird das wetter', 'danke tschüss']),
                       brain, voice)
    call.playback = FakePlayback()

    async def feed():
        await asyncio.sleep(0.4)  # after the greeting
        for chunk in (b'x', b'end', b'x', b'end'):
            await call.heard.put(chunk)
            await asyncio.sleep(0.4)

    feeder = asyncio.create_task(feed())
    assert await asyncio.wait_for(call.converse(), 10) == 'goodbye'
    await feeder
    assert brain.questions == ['wie wird das wetter']
    assert voice.spoken == [jarvis.GREETING, 'Morgen wird es sonnig.', jarvis.GOODBYE]


async def test_silence_ends_call(jarvis):
    now = [0.0]
    voice = FakeVoice()
    call = jarvis.Call(FakePhone([('active', None)]), {}, FakeEars([]), FakeBrain(), voice, clock=lambda: now[0])
    call.playback = FakePlayback()

    async def tick():
        while True:
            now[0] += 30
            await asyncio.sleep(0.05)

    ticker = asyncio.create_task(tick())
    try:
        assert await asyncio.wait_for(call.converse(), 10) == 'silence'
    finally:
        ticker.cancel()
    assert voice.spoken[-1] == jarvis.TIMEOUT_TEXT


async def test_ai_failure_is_spoken_not_fatal(jarvis):
    class Broken:
        async def ask(self, history):
            raise OSError('down')

    voice = FakeVoice()
    call = jarvis.Call(FakePhone([('active', None)]), {}, FakeEars(['hallo', 'tschüss']), Broken(), voice)
    call.playback = FakePlayback()
    async def later():
        for _ in range(2):
            await asyncio.sleep(0.5)
            await call.heard.put(b'end')

    task = asyncio.create_task(later())
    assert await asyncio.wait_for(call.converse(), 10) == 'goodbye'
    await task
    assert jarvis.AI_ERROR in voice.spoken and call.history == []


def test_brain_requires_https_and_credentials(jarvis):
    with pytest.raises(ValueError):
        jarvis.Brain('http://example.org/v1', 'm', 'k')
    with pytest.raises(ValueError):
        jarvis.Brain('https://example.org/v1', '', 'k')
    brain = jarvis.Brain('https://example.org/v1/', 'm', 'k')
    assert brain.url == 'https://example.org/v1/chat/completions'
    assert jarvis.Brain('https://example.org/gateway', 'm', 'k', '').url == 'https://example.org/gateway'
