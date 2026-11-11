import base64
import io
import json
import os

import engine
import app


def test_groq_key_calls_vision_endpoint_and_returns_reviewable_alt_text(monkeypatch):
    monkeypatch.setenv("GROQ_API_KEY", "gsk_test_value")
    monkeypatch.delenv("ANTHROPIC_API_KEY", raising=False)
    monkeypatch.delenv("GROQ_ALT_MODEL", raising=False)
    monkeypatch.delenv("ALT_MODEL", raising=False)
    captured = {}

    def fake_urlopen(request, timeout):
        captured["url"] = request.full_url
        captured["auth"] = request.get_header("Authorization")
        captured["body"] = json.loads(request.data)
        captured["timeout"] = timeout
        return io.BytesIO(json.dumps({"choices": [{"message": {"content": "A blue circle beside a short heading."}}]}).encode())

    monkeypatch.setattr(engine.urllib.request, "urlopen", fake_urlopen)
    image = b"synthetic png bytes"
    assert engine.describe_image(image, "en") == "A blue circle beside a short heading."
    assert captured["url"] == "https://api.groq.com/openai/v1/chat/completions"
    assert captured["auth"] == "Bearer gsk_test_value"
    assert captured["body"]["model"] == "qwen/qwen3.8-27b"
    assert captured["body"]["messages"][0]["content"][1]["image_url"]["url"] == "data:image/png;base64," + base64.b64encode(image).decode()
    assert captured["timeout"] == 40


def test_ai_provider_failure_does_not_leak_key_or_crash(monkeypatch):
    monkeypatch.setenv("GROQ_API_KEY", "gsk_test_secret")
    monkeypatch.delenv("ANTHROPIC_API_KEY", raising=False)

    def fail(*args, **kwargs):
        raise OSError("simulated network failure")

    monkeypatch.setattr(engine.urllib.request, "urlopen", fail)
    assert engine.describe_image(b"image", "en") is None


def test_local_env_loader_reads_key_but_preserves_shell_override(tmp_path, monkeypatch):
    monkeypatch.setattr(app, "HERE", str(tmp_path))
    monkeypatch.delenv("GROQ_API_KEY", raising=False)
    monkeypatch.delenv("ANTHROPIC_API_KEY", raising=False)
    (tmp_path / ".env").write_text("GROQ_API_KEY=local-test\nANTHROPIC_API_KEY= # optional\n", encoding="utf-8")
    app._load_local_env()
    assert os.environ["GROQ_API_KEY"] == "local-test"
    assert os.environ["ANTHROPIC_API_KEY"] == ""
    monkeypatch.setenv("GROQ_API_KEY", "shell-test")
    app._load_local_env()
    assert os.environ["GROQ_API_KEY"] == "shell-test"
