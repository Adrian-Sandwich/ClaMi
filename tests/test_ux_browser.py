"""Optional browser checks with intercepted requests: never contact the live board."""
import json
import os
from pathlib import Path

import pytest

ROOT = Path(__file__).resolve().parents[1]


@pytest.fixture
def page(allow_real_processes):
    executable = os.environ.get("CLAMI_BROWSER_PATH")
    if not executable:
        pytest.skip("Set CLAMI_BROWSER_PATH to run the optional browser checks")
    api = pytest.importorskip("playwright.sync_api")
    with api.sync_playwright() as playwright:
        browser = playwright.chromium.launch(executable_path=executable, headless=True)
        page = browser.new_page(viewport={"width": 1280, "height": 1000})
        page.add_init_script("window.EventSource = class {constructor() {window.feed = this;}}")

        def route(request):
            filename = request.request.url.rsplit("/", 1)[-1] or "index.html"
            path = ROOT / "debate-mcp" / "ui" / filename
            if filename not in ("index.html", "app.js", "sound.js", "style.css"):
                request.abort()
                return
            types = {".html": "text/html", ".js": "text/javascript", ".css": "text/css"}
            request.fulfill(body=path.read_bytes(), content_type=types[path.suffix])

        page.route("**/*", route)
        page.goto("http://magi.test/")
        yield page
        browser.close()


def snapshot(status="open"):
    return {"chat": [], "decisions": [{"id": 4, "status": status, "round": 1,
        "protocol": "adaptive", "title": "Make the council easier to use", "confidence": None,
        "badge": {"text": "DELIBERATING", "color": "#ff8d00", "flicker": False},
        "seats": [{"seat": seat, "voted": False, "body": ""}
                  for seat in ("melchior", "balthasar", "casper")], "journal": []}]}


def feed(page, value):
    page.evaluate("data => window.feed.onmessage({data: JSON.stringify(data)})", value)


def test_composer_targets_actions_and_preserves_failed_drafts(page):
    errors = []
    page.on("pageerror", lambda error: errors.append(str(error)))
    feed(page, snapshot("split"))
    page.locator("#c-input").fill("Keep this context")
    page.locator("#sa-ruling").click()
    assert page.locator("#c-send").inner_text() == "Close with my ruling"
    sent = []

    def fail(route):
        sent.append(route.request.post_data_json)
        route.fulfill(status=409, content_type="application/json", body='{"error":"Decision changed"}')

    page.route("**/message", fail)
    page.locator("#c-send").click()
    page.wait_for_function("document.getElementById('c-status').textContent.includes('Decision changed')")
    assert sent[-1] == {"mode": "council", "body": "Keep this context", "decision_id": 4, "action": "arbitrate"}
    assert page.locator("#c-input").input_value() == "Keep this context"
    page.locator("#sa-segui").click()
    page.locator("#c-send").click()
    page.wait_for_function("!document.getElementById('c-send').disabled")
    assert sent[-1]["action"] == "resume"
    page.locator("#c-new").click()
    assert len(sent) == 2  # New question prepares a draft; it never sends.
    assert page.locator("#c-send").inner_text() == "Ask council"
    page.locator("#c-repo").fill("C:/work/example")
    assert page.locator("#c-send").inner_text() == "Start production"
    page.locator("#c-send").click()
    page.wait_for_function("!document.getElementById('c-send').disabled")
    assert sent[-1]["force_new"] is True
    assert "decision_id" not in sent[-1]
    assert sent[-1]["artifact"] == "C:/work/example"
    page.evaluate("window.feed.onerror()")
    assert page.locator("#c-send").is_disabled()
    assert not errors


def test_sound_transition_dedup_keyboard_and_mobile(page):
    assert page.locator("#sound-toggle").get_attribute("aria-pressed") == "false"
    assert page.locator(".wise-man").count() == 3
    page.locator("#sound-toggle").click()
    assert page.locator("#sound-toggle").get_attribute("aria-pressed") == "true"
    page.evaluate("() => { window.cues = []; MagiSound.play = kind => window.cues.push(kind); }")
    data = snapshot()
    feed(page, data)
    assert page.evaluate("window.cues") == []
    data["decisions"][0]["seats"][0].update(voted=True, position="yes")
    feed(page, data)
    feed(page, data)
    assert page.evaluate("window.cues") == ["vote"]
    page.evaluate("window.feed.onerror()")
    data["decisions"][0]["status"] = "split"
    feed(page, data)
    assert page.evaluate("window.cues") == ["vote"]
    page.locator(".wise-man").first.focus()
    page.keyboard.press("Enter")
    assert page.locator("#modal").is_visible()
    page.keyboard.press("Escape")
    assert not page.locator("#modal").is_visible()
    page.screenshot(path=str(ROOT / "experiments" / "ux-desktop.png"), full_page=True)
    page.set_viewport_size({"width": 390, "height": 844})
    assert page.evaluate("document.documentElement.scrollWidth <= window.innerWidth")
    page.screenshot(path=str(ROOT / "experiments" / "ux-mobile.png"), full_page=True)
