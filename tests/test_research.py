"""Free online research tools: Wikipedia, news, local SearXNG and bounded page reading."""

import json

import httpx
import pytest

from meshcore_ai_bot import research
from meshcore_ai_bot.assistant import Assistant
from meshcore_ai_bot.config import Config
from meshcore_ai_bot.store import Store
from meshcore_ai_bot.tools import Tools, schemas

RSS = b"""<?xml version="1.0" encoding="UTF-8"?><rss><channel>
<item><title>Neue Radwege in Friedenau</title><link>https://www.rbb24.de/radwege.html</link>
<description>Der Bezirk plant mehr Radwege.</description><pubDate>Sat, 07 Nov 2026 10:00:00 +0100</pubDate></item>
<item><title>Wetter am Wochenende</title><link>https://www.rbb24.de/wetter.html</link>
<description>Grau.</description><pubDate>Sat, 07 Nov 2026 09:00:00 +0100</pubDate></item>
</channel></rss>"""


def public_dns(monkeypatch, address="93.184.216.34"):
    async def fake(host):
        return [address]

    monkeypatch.setattr(research, "resolve", fake)


async def test_wikipedia_search_and_summary():
    seen = []

    def respond(request):
        seen.append(request)
        if request.url.path.endswith("/search/page"):
            return httpx.Response(200, json={"pages": [{"key": "Berlin-Friedenau", "title": "Berlin-Friedenau"},
                                                       {"key": "Friedenau", "title": "Friedenau (Begriffsklärung)"}]})
        return httpx.Response(200, json={
            "title": "Berlin-Friedenau",
            "extract": "Friedenau ist ein Ortsteil im Bezirk Tempelhof-Schöneberg.",
            "content_urls": {"desktop": {"page": "https://de.wikipedia.org/wiki/Berlin-Friedenau"}},
        })

    async with httpx.AsyncClient(transport=httpx.MockTransport(respond)) as client:
        result = await Tools(Config(), Store(":memory:"), client).run("search_wikipedia", {"query": "Friedenau"})
    assert "Ortsteil im Bezirk Tempelhof-Schöneberg" in result["text"]
    assert "Begriffsklärung" in result["text"]
    assert result["sources"] == ["https://de.wikipedia.org/wiki/Berlin-Friedenau"]
    assert all(r.headers["User-Agent"].startswith("MeshCoreAIBot") for r in seen)


async def test_news_combines_tagesschau_and_filtered_rss():
    def respond(request):
        if request.url.host == "www.tagesschau.de":
            assert request.url.path == "/api2u/search/"
            assert request.url.params["searchText"] == "radwege"
            return httpx.Response(200, json={"searchResults": [{
                "title": "Mehr Radwege geplant", "topline": "Verkehr", "date": "2026-11-07T08:00:00.000+01:00",
                "firstSentence": "Berlin will das Radnetz ausbauen.",
                "shareURL": "https://www.tagesschau.de/inland/radwege-100.html",
            }]})
        return httpx.Response(200, content=RSS)

    async with httpx.AsyncClient(transport=httpx.MockTransport(respond)) as client:
        result = await Tools(Config(), Store(":memory:"), client).run("search_news", {"query": "Radwege Berlin"})
    assert "tagesschau: Verkehr: Mehr Radwege geplant" in result["text"]
    assert "rbb24.de: Neue Radwege in Friedenau" in result["text"]
    assert "Wetter am Wochenende" not in result["text"]
    assert "https://www.rbb24.de/radwege.html" in result["sources"]


async def test_regional_headlines_without_topic_use_region_feed():
    def respond(request):
        if request.url.host == "www.tagesschau.de":
            assert request.url.path == "/api2u/news/" and request.url.params["regions"] == "3"
            return httpx.Response(200, json={"news": [{"title": "S-Bahn-Störung", "shareURL": "https://x.de/a"}]})
        return httpx.Response(503)

    async with httpx.AsyncClient(transport=httpx.MockTransport(respond)) as client:
        result = await Tools(Config(), Store(":memory:"), client).run("search_news", {"query": "Berlin aktuell"})
    assert "S-Bahn-Störung" in result["text"]


async def test_web_search_only_with_local_searxng():
    config = Config()
    assert "web_search" not in {s["function"]["name"] for s in schemas(config)}
    async with httpx.AsyncClient(transport=httpx.MockTransport(lambda r: pytest.fail("no request"))) as client:
        with pytest.raises(ValueError):
            await Tools(config, Store(":memory:"), client).run("web_search", {"query": "x"})

    config.research.searxng_url = "http://127.0.0.1:8888"

    def respond(request):
        assert request.url.host == "127.0.0.1" and request.url.params["format"] == "json"
        return httpx.Response(200, json={"results": [
            {"title": "Ideenwerft", "url": "https://ideenwerft-friedenau.de/", "content": "Offene Werkstatt"},
            {"title": "Unsicher", "url": "http://example.org/", "content": "kein https"},
        ]})

    async with httpx.AsyncClient(transport=httpx.MockTransport(respond)) as client:
        tools = Tools(config, Store(":memory:"), client)
        tools.research.local_client = client
        result = await tools.run("web_search", {"query": "Ideenwerft"})
    assert "Offene Werkstatt" in result["text"] and "example.org" not in result["text"]
    assert result["sources"] == ["https://ideenwerft-friedenau.de/"]
    assert "web_search" in {s["function"]["name"] for s in schemas(config)}


def test_searxng_must_be_loopback():
    config = Config()
    config.research.searxng_url = "https://searx.example.org"
    with pytest.raises(ValueError):
        config.validate()


async def test_read_page_requires_known_url_and_public_host(monkeypatch):
    page = (
        "<html><head><title>Radwege</title><script>evil()</script></head><body><nav>Menü</nav>"
        "<article><p>" + "Der Bezirk baut neue Radwege. " * 20 + "</p></article><footer>Impressum</footer></body></html>"
    )

    def respond(request):
        return httpx.Response(200, html=page)

    tools_args = {"url": "https://www.rbb24.de/radwege.html"}
    async with httpx.AsyncClient(transport=httpx.MockTransport(respond)) as client:
        tools = Tools(Config(), Store(":memory:"), client)
        refused = await tools.run("read_webpage", tools_args, allowed_urls=set())
        assert "Erst suchen" in refused["text"]
        public_dns(monkeypatch)
        result = await tools.run("read_webpage", tools_args, allowed_urls={"https://www.rbb24.de/radwege.html"})
        assert result["text"].startswith("„Radwege“: Der Bezirk baut")
        assert "evil" not in result["text"] and "Menü" not in result["text"] and "Impressum" not in result["text"]
        public_dns(monkeypatch, "192.168.8.200")
        with pytest.raises(ValueError):
            await tools.run("read_webpage", tools_args, allowed_urls={"https://www.rbb24.de/radwege.html"})


async def test_redirect_to_private_address_is_rechecked(monkeypatch):
    calls = []

    async def fake_check(url):
        calls.append(url)
        if "intern" in url:
            raise ValueError("Private address")

    monkeypatch.setattr(research, "check_public_https", fake_check)

    def respond(request):
        return httpx.Response(302, headers={"Location": "https://intern.lan/admin"})

    async with httpx.AsyncClient(transport=httpx.MockTransport(respond)) as client:
        tools = Tools(Config(), Store(":memory:"), client)
        with pytest.raises(ValueError):
            await tools.run("read_webpage", {"url": "https://a.de/x"}, allowed_urls={"https://a.de/x"})
    assert calls == ["https://a.de/x", "https://intern.lan/admin"]


async def test_assistant_researches_over_several_rounds(monkeypatch):
    config = Config()
    config.ai._api_key = "synthetic-key"
    config.ai.requests_per_minute = 60
    store = Store(":memory:")
    payloads = []

    def call(name, args, ident):
        return {"choices": [{"message": {"tool_calls": [
            {"id": ident, "type": "function", "function": {"name": name, "arguments": json.dumps(args)}}
        ]}}]}

    def respond(request):
        if request.url.host == "openrouter.ai":
            payload = json.loads(request.content)
            payloads.append(payload)
            if len(payloads) == 1:
                return httpx.Response(200, json=call("search_news", {"query": "Radwege"}, "a"))
            if len(payloads) == 2:
                return httpx.Response(200, json=call("search_wikipedia", {"query": "Friedenau"}, "b"))
            return httpx.Response(200, json={"choices": [{"message": {"content": "Laut tagesschau: mehr Radwege."}}]})
        if request.url.host == "www.tagesschau.de":
            return httpx.Response(200, json={"searchResults": [{"title": "Mehr Radwege", "shareURL": "https://t.de/r"}]})
        if request.url.host == "de.wikipedia.org":
            if request.url.path.endswith("/search/page"):
                return httpx.Response(200, json={"pages": [{"key": "Friedenau"}]})
            return httpx.Response(200, json={"title": "Friedenau", "extract": "Ortsteil."})
        return httpx.Response(200, content=RSS)

    async with httpx.AsyncClient(transport=httpx.MockTransport(respond)) as client:
        answer = await Assistant(config, store, client).answer("Was gibt es Neues zu Radwegen in Friedenau?")
    assert answer.text == "Laut tagesschau: mehr Radwege."
    assert "https://t.de/r" in answer.sources
    assert "Recherche:" in payloads[0]["messages"][0]["content"]
    names = {tool["function"]["name"] for tool in payloads[0]["tools"]}
    assert {"search_news", "search_wikipedia", "read_webpage"} <= names
    assert "tools" in payloads[2]
    store.close()


CATALOG = b"""<?xml version="1.0" encoding="UTF-8"?>
<feed xmlns="http://www.w3.org/2005/Atom"><entry><title>Wikipedia</title><language>deu</language>
<name>wikipedia_de_all</name>
<link type="text/html" href="/content/wikipedia_de_all_maxi_2026-01"/></entry></feed>"""

SEARCH = b"""<?xml version="1.0" encoding="UTF-8"?><rss><channel>
<item><title>Berlin-Friedenau</title><link>/content/wikipedia_de_all_maxi_2026-01/Berlin-Friedenau</link>
<description>&lt;b&gt;Friedenau&lt;/b&gt; ist ein Ortsteil</description></item>
<item><title>Friedenau</title><link>/content/wikipedia_de_all_maxi_2026-01/Friedenau</link></item>
</channel></rss>"""

ARTICLE = (
    "<html><body><table><tr><td>Einwohner 29.406</td></tr></table>"
    "<p><b>Friedenau</b><style>.mw-parser-output .IPA a{text-decoration:none}</style> ist ein Ortsteil im Bezirk Tempelhof-Schöneberg von Berlin und galt lange als Künstlerviertel.[1]</p>"
    "</body></html>"
)


async def test_wikipedia_falls_back_to_local_kiwix():
    config = Config()
    config.research.kiwix_url = "http://127.0.0.1:8081"
    config.validate()
    seen = []

    def respond(request):
        seen.append(request.url.path)
        if request.url.host == "de.wikipedia.org":
            raise httpx.ConnectError("offline")
        if request.url.path == "/catalog/v2/entries":
            return httpx.Response(200, content=CATALOG)
        if request.url.path == "/search":
            assert request.url.params["books.name"] == "wikipedia_de_all_maxi_2026-01"
            return httpx.Response(200, content=SEARCH)
        assert request.url.path == "/content/wikipedia_de_all_maxi_2026-01/Berlin-Friedenau"
        return httpx.Response(200, html=ARTICLE)

    async with httpx.AsyncClient(transport=httpx.MockTransport(respond)) as client:
        tools = Tools(config, Store(":memory:"), client)
        tools.research.local_client = client
        result = await tools.run("search_wikipedia", {"query": "Friedenau"})
        again = await tools.run("search_wikipedia", {"query": "Friedenau"})
    assert result["text"].startswith("Wikipedia offline (Stand 2026-01) „Berlin-Friedenau“: Friedenau ist ein Ortsteil")
    assert "[1]" not in result["text"] and "Einwohner" not in result["text"]
    assert "Weitere Treffer: Friedenau." in result["text"]
    assert result["sources"] == ["https://de.wikipedia.org/wiki/Berlin-Friedenau"]
    assert again == result and seen.count("/catalog/v2/entries") == 1


async def test_without_kiwix_offline_wikipedia_error_propagates():
    def respond(request):
        raise httpx.ConnectError("offline")

    async with httpx.AsyncClient(transport=httpx.MockTransport(respond)) as client:
        with pytest.raises(httpx.HTTPError):
            await Tools(Config(), Store(":memory:"), client).run("search_wikipedia", {"query": "x"})


def test_kiwix_must_be_loopback():
    config = Config()
    config.research.kiwix_url = "http://192.168.8.200:8081"
    with pytest.raises(ValueError):
        config.validate()
