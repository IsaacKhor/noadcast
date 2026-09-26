from __future__ import annotations

import dataclasses
import json
import tempfile
import unittest
from pathlib import Path

import httpx

from noadcast.classify.base import ClassifierError, ClassifyRequest
from noadcast.classify.gemini_audio import GeminiAudioClassifier, file_name_from_uri
from noadcast.classify.prompts import AUDIO_SEGMENTS_PROMPT, RESPONSE_SCHEMA
from noadcast.classify.retry import ClassifyFailed
from tests.test_classify_helpers import RecordingSleep, episode, gemini_body, gemini_error, no_jitter, segments_json

KEY = "AIza-audio-key"
UPLOAD_URL = "https://generativelanguage.googleapis.com/upload/v1beta/files?upload_id=session-123&upload_protocol=resumable"
FILE_URI = "https://generativelanguage.googleapis.com/v1beta/files/abc123"
OUTRO = {"startSeconds": 160, "endSeconds": 185, "summary": "Credits", "kind": "outro"}


class FilesApi:
    """In-memory Files API + generateContent; ``generate`` scripts the model's replies."""

    def __init__(self, *generate: tuple[int, dict]) -> None:
        self.generate = list(generate)
        self.requests: list[httpx.Request] = []

    def __call__(self, request: httpx.Request) -> httpx.Response:
        self.requests.append(request)
        path = request.url.path
        if request.method == "POST" and path == "/upload/v1beta/files" and "upload_id" not in str(request.url):
            return httpx.Response(200, headers={"X-Goog-Upload-URL": UPLOAD_URL}, json={})
        if request.method == "POST" and "upload_id=session-123" in str(request.url):
            return httpx.Response(200, json={"file": {
                "name": "files/abc123", "uri": FILE_URI, "mimeType": "audio/mpeg", "state": "ACTIVE"}})
        if request.method == "POST" and path.endswith(":generateContent"):
            status, body = self.generate.pop(0) if len(self.generate) > 1 else self.generate[0]
            return httpx.Response(status, json=body)
        if request.method == "DELETE" and path == "/v1beta/files/abc123":
            return httpx.Response(200, json={})
        return httpx.Response(404, json={"error": {"code": 404, "status": "NOT_FOUND", "message": path}})

    def of(self, method: str, marker: str) -> list[httpx.Request]:
        return [r for r in self.requests if r.method == method and marker in str(r.url)]


class GeminiAudioTests(unittest.IsolatedAsyncioTestCase):
    def setUp(self) -> None:
        directory = tempfile.TemporaryDirectory()
        self.addCleanup(directory.cleanup)
        self.audio = Path(directory.name) / "episode.mp3"
        self.audio_bytes = bytes(range(256)) * 9000  # > 2 upload chunks
        self.audio.write_bytes(self.audio_bytes)

    def classifier(self, api: FilesApi) -> GeminiAudioClassifier:
        client = httpx.AsyncClient(transport=httpx.MockTransport(api))
        self.addAsyncCleanup(client.aclose)
        return GeminiAudioClassifier(
            model="gemini-3.5-flash", api_key=KEY, client=client, sleep=RecordingSleep(), rand=no_jitter
        )

    def request(self) -> ClassifyRequest:
        return dataclasses.replace(episode(), audio_path=str(self.audio), audio_content_type="audio/mpeg")

    async def test_uploads_classifies_and_deletes(self) -> None:
        api = FilesApi((200, gemini_body(segments_json(OUTRO), prompt=42000, thoughts=900, candidates=150)))
        result = await self.classifier(api).classify(self.request())

        start, upload, generate, delete = api.requests
        self.assertEqual(start.headers["x-goog-api-key"], KEY)
        self.assertEqual(start.headers["x-goog-upload-protocol"], "resumable")
        self.assertEqual(start.headers["x-goog-upload-command"], "start")
        self.assertEqual(start.headers["x-goog-upload-header-content-length"], str(len(self.audio_bytes)))
        self.assertEqual(start.headers["x-goog-upload-header-content-type"], "audio/mpeg")
        self.assertEqual(json.loads(start.content), {"file": {"display_name": "episode.mp3"}})

        self.assertEqual(str(upload.url), UPLOAD_URL)
        self.assertEqual(upload.headers["x-goog-upload-command"], "upload, finalize")
        self.assertEqual(upload.headers["x-goog-upload-offset"], "0")
        self.assertEqual(upload.headers["content-length"], str(len(self.audio_bytes)))
        self.assertNotIn("transfer-encoding", upload.headers)
        self.assertNotIn("x-goog-api-key", upload.headers)
        self.assertEqual(upload.content, self.audio_bytes)

        body = json.loads(generate.content)
        self.assertEqual(generate.headers["x-goog-api-key"], KEY)
        self.assertNotIn(KEY, str(generate.url))
        self.assertEqual(body["systemInstruction"], {"parts": [{"text": AUDIO_SEGMENTS_PROMPT}]})
        self.assertEqual(body["generationConfig"]["responseSchema"], RESPONSE_SCHEMA)
        file_part, text_part = body["contents"][0]["parts"]
        self.assertEqual(file_part, {"file_data": {"mime_type": "audio/mpeg", "file_uri": FILE_URI}})
        self.assertEqual(
            text_part["text"],
            "Produce the JSON object as specified.\n\nThe complete episode duration is 185.00 seconds. Treat 185.00 "
            "seconds as the physical audio endpoint and deliberately inspect the final portion. If an outro exists, "
            "its endSeconds must be 185.00, including any trailing music, silence, or postroll audio.",
        )

        self.assertEqual(delete.url.path, "/v1beta/files/abc123")
        self.assertEqual(delete.headers["x-goog-api-key"], KEY)

        self.assertEqual([(s.kind, s.start_seconds, s.end_seconds) for s in result.segments], [("outro", 160.0, 185.0)])
        self.assertEqual(
            (result.provider, result.prompt_version, result.render_format, result.include_silence, result.attempts),
            ("gemini-audio", "audio-v1", "audio", False, 1),
        )
        self.assertEqual((result.usage.input_tokens, result.usage.thought_tokens), (42000, 900))
        self.assertEqual(result.raw_response["upload"]["file"], "files/abc123")

    async def test_the_upload_is_deleted_even_when_classification_fails(self) -> None:
        api = FilesApi((400, gemini_error(400, "INVALID_ARGUMENT", "bad request")))
        with self.assertRaises(ClassifyFailed) as caught:
            await self.classifier(api).classify(self.request())
        self.assertTrue(caught.exception.permanent)
        self.assertEqual(len(api.of("DELETE", "/files/abc123")), 1)

    async def test_the_request_hash_covers_the_audio_bytes(self) -> None:
        api = FilesApi((200, gemini_body(segments_json())))
        classifier = self.classifier(api)
        first = await classifier.classify(self.request())
        self.audio.write_bytes(self.audio_bytes[:-1] + b"\x00")
        second = await classifier.classify(self.request())
        self.assertNotEqual(first.request_sha256, second.request_sha256)

    async def test_missing_audio_is_permanent(self) -> None:
        api = FilesApi((200, gemini_body(segments_json())))
        with self.assertRaises(ClassifierError) as caught:
            await self.classifier(api).classify(episode())
        self.assertTrue(caught.exception.permanent)
        self.assertEqual(api.requests, [])

    def test_file_name_from_uri(self) -> None:
        self.assertEqual(file_name_from_uri(FILE_URI), "files/abc123")
        self.assertEqual(file_name_from_uri("files/xyz"), "files/xyz")
        self.assertIsNone(file_name_from_uri("https://example.com/other"))


if __name__ == "__main__":
    unittest.main()
