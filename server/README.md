Noadcast Ad Detection Server
============================

This is the alternate ad-detection backend for Noadcast. The iOS app uploads
episode audio here instead of directly uploading it to Gemini. The server:

1. converts the upload to 16 kHz mono WAV with `ffmpeg`;
2. transcribes it with `whisper.cpp`;
3. sends the timestamped transcript to Gemini;
4. returns skip segments as JSON.

Requirements
------------

- Python 3.11+
- `ffmpeg` on `PATH`
- a built `whisper.cpp` CLI, usually `whisper-cli`
- a whisper.cpp model file, such as `ggml-base.en.bin`
- a Gemini API key

Environment
-----------

Required:

- `WHISPER_CPP_MODEL`: path to the whisper.cpp model file
- `GEMINI_API_KEY`: Gemini key used when the app does not send one

Optional:

- `WHISPER_CPP_BINARY`: whisper.cpp executable name/path, default `whisper-cli`
- `WHISPER_CPP_LANGUAGE`: language hint passed to whisper.cpp, for example `en`
- `FFMPEG_BINARY`: ffmpeg executable name/path, default `ffmpeg`
- `HOST`: bind host, default `127.0.0.1`
- `PORT`: bind port, default `8765`
- `GEMINI_API_BASE`: Gemini API base URL, default `https://generativelanguage.googleapis.com`

Run
---

```sh
cd server
python -m venv .venv
source .venv/bin/activate
pip install -e .
WHISPER_CPP_MODEL=/path/to/ggml-base.en.bin GEMINI_API_KEY=... noadcast-ad-server
```

Then in Noadcast Settings choose `Whisper.cpp server`, set:

- Server URL: `http://127.0.0.1`
- Port: `8765`

API
---

- `GET /health`
- `POST /analyze`

`POST /analyze` accepts multipart form data:

- `audio`: audio file
- `model`: Gemini model id, such as `gemini-3.5-flash`
- `mime_type`: original audio MIME type
- `thinking_level`: optional Gemini thinking level
- `google_api_key`: optional key; overrides `GEMINI_API_KEY`

The response matches the app contract:

```json
{
  "segments": [
    {
      "startSeconds": 12.3,
      "endSeconds": 45.6,
      "summary": "Sponsor message",
      "kind": "ad"
    }
  ],
  "usage": {
    "inputTokens": 1000,
    "thoughtTokens": 0,
    "outputTokens": 100
  }
}
```
