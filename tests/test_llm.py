import json
import tempfile
import threading
import unittest
from pathlib import Path
from typing import Any

import httpx

from warden.llm import (
    LLMError,
    LLMReplayMiss,
    LLMRequest,
    LLMResponse,
    LLMTransientError,
    RecordReplayClient,
    RetryingClient,
    ScriptedClient,
    read_env_value,
)
from warden.llm.gemini import GeminiClient

FAKE_KEY = "test-key-" + "0123456789abcdef"


def gemini_ok(text: str = "hello") -> dict[str, Any]:
    return {
        "candidates": [{"content": {"role": "model", "parts": [{"text": text}]}, "finishReason": "STOP"}],
        "usageMetadata": {"promptTokenCount": 7, "candidatesTokenCount": 3},
    }


class FakeGemini:
    """httpx.MockTransport handler that records requests and replies from a queue."""

    def __init__(self, *replies: httpx.Response | Exception) -> None:
        self.replies = list(replies)
        self.requests: list[httpx.Request] = []

    def __call__(self, request: httpx.Request) -> httpx.Response:
        self.requests.append(request)
        reply = self.replies.pop(0)
        if isinstance(reply, Exception):
            raise reply
        return reply

    def client(self, **kw: Any) -> GeminiClient:
        return GeminiClient(FAKE_KEY, transport=httpx.MockTransport(self), **kw)


class RequestTests(unittest.TestCase):
    def test_key_is_stable_and_sensitive_to_every_field(self) -> None:
        base = LLMRequest("prompt", system="sys", temperature=0.0, json_mode=True)
        self.assertEqual(base.key(), LLMRequest("prompt", system="sys", temperature=0.0, json_mode=True).key())
        variants = [LLMRequest("prompt!", system="sys", json_mode=True),
                    LLMRequest("prompt", system="sys2", json_mode=True),
                    LLMRequest("prompt", system="sys", temperature=0.5, json_mode=True),
                    LLMRequest("prompt", system="sys", json_mode=False)]
        self.assertEqual(len({v.key() for v in variants} | {base.key()}), 5)


class ScriptedClientTests(unittest.TestCase):
    def test_replays_script_in_order_then_default(self) -> None:
        client = ScriptedClient(["one", "two"], default="rest")
        texts = [client.complete(LLMRequest(f"p{i}")).text for i in range(4)]
        self.assertEqual(texts, ["one", "two", "rest", "rest"])
        self.assertEqual([r.prompt for r in client.calls], ["p0", "p1", "p2", "p3"])

    def test_scripted_exceptions_are_raised_for_failure_demos(self) -> None:
        client = ScriptedClient([LLMTransientError("rate limited"), "ok"])
        with self.assertRaises(LLMTransientError):
            client.complete(LLMRequest("p"))
        self.assertEqual(client.complete(LLMRequest("p")).text, "ok")

    def test_exhausted_script_without_default_fails(self) -> None:
        with self.assertRaises(LLMError):
            ScriptedClient([]).complete(LLMRequest("p"))

    def test_is_thread_safe(self) -> None:
        client = ScriptedClient([str(i) for i in range(50)])
        out: list[str] = []
        threads = [threading.Thread(target=lambda: out.append(client.complete(LLMRequest("p")).text))
                   for _ in range(50)]
        for t in threads:
            t.start()
        for t in threads:
            t.join()
        self.assertEqual(sorted(out, key=int), [str(i) for i in range(50)])


class RecordReplayTests(unittest.TestCase):
    def setUp(self) -> None:
        self._tmp = tempfile.TemporaryDirectory()
        self.addCleanup(self._tmp.cleanup)
        self.dir = Path(self._tmp.name) / "recordings"

    def test_record_then_replay_offline(self) -> None:
        inner = ScriptedClient(["recorded answer"])
        RecordReplayClient(self.dir, inner, mode="record").complete(LLMRequest("q"))
        replayed = RecordReplayClient(self.dir, mode="replay").complete(LLMRequest("q"))
        self.assertEqual(replayed.text, "recorded answer")
        self.assertEqual(replayed.backend, "replay")
        self.assertEqual(replayed.model, "scripted")

    def test_replay_miss_fails_closed(self) -> None:
        with self.assertRaises(LLMReplayMiss):
            RecordReplayClient(self.dir, mode="replay").complete(LLMRequest("never recorded"))

    def test_auto_mode_records_only_on_miss(self) -> None:
        inner = ScriptedClient(["first"], default="second")
        client = RecordReplayClient(self.dir, inner, mode="auto")
        self.assertEqual(client.complete(LLMRequest("q")).text, "first")
        self.assertEqual(client.complete(LLMRequest("q")).text, "first")
        self.assertEqual(len(inner.calls), 1)

    def test_recording_is_one_readable_file_per_request(self) -> None:
        RecordReplayClient(self.dir, ScriptedClient(["a", "b"]), mode="record").complete(LLMRequest("q", system="s"))
        files = list(self.dir.glob("*.json"))
        self.assertEqual([f.stem for f in files], [LLMRequest("q", system="s").key()])
        data = json.loads(files[0].read_text("utf-8"))
        self.assertEqual((data["request"]["prompt"], data["response"]["text"]), ("q", "a"))

    def test_errors_are_not_recorded(self) -> None:
        inner = ScriptedClient([LLMTransientError("busy")])
        with self.assertRaises(LLMTransientError):
            RecordReplayClient(self.dir, inner, mode="auto").complete(LLMRequest("q"))
        self.assertEqual(list(self.dir.glob("*.json")), [])

    def test_record_mode_requires_an_inner_client(self) -> None:
        with self.assertRaises(ValueError):
            RecordReplayClient(self.dir, mode="record")


class GeminiClientTests(unittest.TestCase):
    def test_sends_request_with_key_in_header_not_url(self) -> None:
        fake = FakeGemini(httpx.Response(200, json=gemini_ok("hi there")))
        resp = fake.client(model="gemini-test").complete(
            LLMRequest("Say hi", system="Be brief", temperature=0.2, json_mode=True))
        self.assertEqual(resp, LLMResponse("hi there", "gemini-test", "gemini", 7, 3))
        sent = fake.requests[0]
        self.assertEqual(sent.headers["x-goog-api-key"], FAKE_KEY)
        self.assertNotIn(FAKE_KEY, str(sent.url))
        self.assertTrue(str(sent.url).endswith("/models/gemini-test:generateContent"))
        body = json.loads(sent.content)
        self.assertEqual(body["contents"], [{"role": "user", "parts": [{"text": "Say hi"}]}])
        self.assertEqual(body["systemInstruction"], {"parts": [{"text": "Be brief"}]})
        self.assertEqual(body["generationConfig"],
                         {"temperature": 0.2, "responseMimeType": "application/json"})

    def test_rate_limit_and_server_errors_are_transient(self) -> None:
        for status in (429, 500, 503):
            with self.subTest(status=status):
                fake = FakeGemini(httpx.Response(status, json={"error": {"message": "try later"}}))
                with self.assertRaises(LLMTransientError):
                    fake.client().complete(LLMRequest("p"))

    def test_timeouts_and_connection_errors_are_transient(self) -> None:
        for exc in (httpx.ReadTimeout("slow"), httpx.ConnectError("down")):
            with self.subTest(exc=type(exc).__name__), self.assertRaises(LLMTransientError):
                FakeGemini(exc).client().complete(LLMRequest("p"))

    def test_client_errors_are_permanent(self) -> None:
        fake = FakeGemini(httpx.Response(400, json={"error": {"message": "bad request"}}))
        with self.assertRaises(LLMError) as cm:
            fake.client().complete(LLMRequest("p"))
        self.assertNotIsInstance(cm.exception, LLMTransientError)
        self.assertIn("bad request", str(cm.exception))

    def test_blocked_or_empty_response_is_an_error(self) -> None:
        fake = FakeGemini(httpx.Response(200, json={"promptFeedback": {"blockReason": "SAFETY"}}))
        with self.assertRaisesRegex(LLMError, "SAFETY"):
            fake.client().complete(LLMRequest("p"))

    def test_key_never_leaks_into_errors_or_repr(self) -> None:
        fake = FakeGemini(httpx.Response(400, json={"error": {"message": f"API key {FAKE_KEY} not valid"}}))
        client = fake.client()
        with self.assertRaises(LLMError) as cm:
            client.complete(LLMRequest("p"))
        self.assertNotIn(FAKE_KEY, str(cm.exception))
        self.assertIsNone(cm.exception.__cause__)  # no chained httpx objects carrying the request
        self.assertNotIn(FAKE_KEY, repr(client))

    def test_missing_key_is_rejected(self) -> None:
        with self.assertRaises(ValueError):
            GeminiClient("")

    def test_recordings_of_gemini_calls_hold_no_key(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            fake = FakeGemini(httpx.Response(200, json=gemini_ok()))
            RecordReplayClient(tmp, fake.client(), mode="record").complete(LLMRequest("p"))
            text = "".join(p.read_text("utf-8") for p in Path(tmp).glob("*.json"))
            self.assertNotIn(FAKE_KEY, text)
            self.assertIn("hello", text)


class RetryingClientTests(unittest.TestCase):
    def setUp(self) -> None:
        self.sleeps: list[float] = []

    def client(self, *script: str | Exception, attempts: int = 3) -> tuple[RetryingClient, ScriptedClient]:
        inner = ScriptedClient(list(script))
        return RetryingClient(inner, attempts=attempts, base_delay=1.0, sleep=self.sleeps.append), inner

    def test_transient_errors_back_off_then_succeed(self) -> None:
        client, inner = self.client(LLMTransientError("503"), LLMTransientError("503"), "ok")
        self.assertEqual(client.complete(LLMRequest("p")).text, "ok")
        self.assertEqual(self.sleeps, [1.0, 2.0])  # exponential backoff
        self.assertEqual(len(inner.calls), 3)

    def test_gives_up_after_bounded_attempts(self) -> None:
        client, inner = self.client(*[LLMTransientError("503")] * 5, attempts=3)
        with self.assertRaises(LLMTransientError):
            client.complete(LLMRequest("p"))
        self.assertEqual(len(inner.calls), 3)

    def test_permanent_errors_are_not_retried(self) -> None:
        client, inner = self.client(LLMError("bad request"), "never")
        with self.assertRaises(LLMError):
            client.complete(LLMRequest("p"))
        self.assertEqual((len(inner.calls), self.sleeps), (1, []))


class EnvTests(unittest.TestCase):
    def test_environment_wins_over_env_file(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            env = Path(tmp) / ".env"
            env.write_text("# comment\nexport GEMINI_API_KEY = 'from-file'\nOTHER=x\n", encoding="utf-8")
            self.assertEqual(read_env_value("GEMINI_API_KEY", env, environ={}), "from-file")
            self.assertEqual(read_env_value("GEMINI_API_KEY", env, environ={"GEMINI_API_KEY": "from-env"}),
                             "from-env")
            self.assertIsNone(read_env_value("MISSING", env, environ={}))
            self.assertIsNone(read_env_value("GEMINI_API_KEY", Path(tmp) / "nope.env", environ={}))

    def test_from_env_builds_client_or_fails_clearly(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            env = Path(tmp) / ".env"
            with self.assertRaisesRegex(LLMError, "GEMINI_API_KEY"):
                GeminiClient.from_env(env, environ={})
            env.write_text(f"GEMINI_API_KEY={FAKE_KEY}\n", encoding="utf-8")
            offline = httpx.MockTransport(FakeGemini())  # skips building a real SSL context (slow)
            client = GeminiClient.from_env(env, environ={}, transport=offline)
            self.assertEqual(client.model, GeminiClient.DEFAULT_MODEL)
