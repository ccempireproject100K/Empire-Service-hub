"""Voice standard (2026-11-01): dictation on the embeddable widget.

The widget is a static JS file with no build step, so the harness
pins its dictation contract by reading the served source: browser-
native recognition, the guided "Fill it in by talking" walk, spoken
prompts, per-language strings + recognition tags, the honest privacy
line, and — critically — no path from dictation to a submit.
"""
from pathlib import Path

WIDGET_JS = (
    Path(__file__).resolve().parents[2] / "frontend" / "widget" / "widget.js"
)


def _source() -> str:
    return WIDGET_JS.read_text(encoding="utf-8")


def test_widget_js_served_with_dictation(client):
    resp = client.get("/widget.js")
    assert resp.status_code == 200
    assert "Fill it in by talking" in resp.text
    assert "SpeechRecognition" in resp.text


def test_dictation_uses_browser_native_recognition_only():
    src = _source()
    assert "window.SpeechRecognition || window.webkitSpeechRecognition" in src
    assert "continuous = true" in src
    assert "interimResults = true" in src
    # No audio upload path: dictation never posts to the hub API.
    assert "MediaRecorder" not in src
    assert "getUserMedia" not in src


def test_guided_walk_and_spoken_prompts_present():
    src = _source()
    assert "Fill it in by talking" in src  # the ONE guided control
    assert "speechSynthesis" in src  # prompts spoken where available
    assert "SpeechSynthesisUtterance" in src
    # Spoken commands + graceful pause handling live in the source.
    for marker in ('"done"', '"stop"', '"back"', '"skip"', '"next"'):
        assert marker in src
    assert "advances gracefully" in src


def test_dictation_strings_and_recognition_langs_for_all_tenant_languages():
    src = _source()
    for code in ('en:', 'es:', 'fr:', 'zh:'):
        assert code in src
    for tag in ('"en-US"', '"es-ES"', '"fr-FR"', '"zh-CN"'):
        assert tag in src
    # The honest privacy line, Trade Circle pattern, product-adapted.
    assert "turns speech into text" in src
    assert "only receives the text you keep" in src


def test_dictation_never_auto_submits():
    src = _source()
    # The only submit listener is the composer's own manual one;
    # dictation code fills input.value and never dispatches it.
    assert "requestSubmit" not in src
    assert src.count('addEventListener("submit"') == 1
    assert "appendDictated" in src
