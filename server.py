
import asyncio
import json
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from pathlib import Path

import edge_tts

ROOT = Path(__file__).resolve().parent
VOICES = {
    "th-TH-NiwatNeural",
    "th-TH-PremwadeeNeural",
    "en-US-GuyNeural",
    "en-US-AriaNeural",
}


async def generate_audio(text, voice):
    data = bytearray()
    tts = edge_tts.Communicate(text, voice)

    async for chunk in tts.stream():
        if chunk["type"] == "audio":
            data.extend(chunk["data"])

    return bytes(data)


class Handler(BaseHTTPRequestHandler):
    def do_GET(self):
        if self.path != "/":
            self.send_error(404)
            return

        content = (ROOT / "index.html").read_bytes()
        self.send_response(200)
        self.send_header("Content-Type", "text/html; charset=utf-8")
        self.send_header("Content-Length", str(len(content)))
        self.end_headers()
        self.wfile.write(content)

    def do_POST(self):
        if self.path != "/tts":
            self.send_error(404)
            return

        try:
            length = int(self.headers.get("Content-Length", "0"))
            if length <= 0 or length > 20000:
                self.send_error(413, "Invalid request size")
                return

            body = json.loads(
                self.rfile.read(length).decode("utf-8")
            )
            text = body.get("text", "").strip()
            voice = body.get("voice", "th-TH-NiwatNeural")

            if not isinstance(text, str) or not 0 < len(text) <= 2000:
                self.send_error(400, "Invalid text")
                return
            if voice not in VOICES:
                self.send_error(400, "Invalid voice")
                return

            audio = asyncio.run(generate_audio(text, voice))
            if not audio:
                self.send_error(502, "TTS returned no audio")
                return

            self.send_response(200)
            self.send_header("Content-Type", "audio/mpeg")
            self.send_header("Content-Length", str(len(audio)))
            self.send_header("Cache-Control", "no-store")
            self.end_headers()
            self.wfile.write(audio)

        except Exception as exc:
            print("TTS error:", exc)
            self.send_error(502, "TTS generation failed")


if __name__ == "__main__":
    server = ThreadingHTTPServer(
        ("127.0.0.1", 8765), Handler
    )
    print("Open http://localhost:8765")
    server.serve_forever()