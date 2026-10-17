#!/usr/bin/env python3

import argparse
import base64
import json
import math
import os
import random
import shutil
import subprocess
import sys
import time
from concurrent.futures import ThreadPoolExecutor, as_completed
from datetime import datetime, timezone
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from pathlib import Path
from urllib.parse import urlsplit

import imageio_ffmpeg
import requests

HERE = Path(__file__).resolve().parent
API_URL = "https://openrouter.ai/api/alpha/decisions"
DEFAULT_MODEL = "perplexity/pplx-decider-flash-v1.2" # alternatives: "typesafe/jev-1.14" | "liquid/d1-ultra"
VIDEO_EXTS = {".mkv", ".mp4", ".mov"}
TEMP_DIR = Path("_temp_")
FRAMES_DIR = TEMP_DIR / "frames"
RESULTS_PATH = TEMP_DIR / "run_results.json"
RETRY_STATUS = {429, 500, 502, 503, 504}
RETRIES = 4 # sec
BACKOFF_CAP = 5.0 # sec
JITTER = 2.0 # random sec
CONCURRENT = 48  # in-flight requests; --workers overrides
UI_PORT = 8000


class FrameError(RuntimeError):
    pass


def die(msg: str) -> None:
    print(f"ERROR: {msg}", file=sys.stderr)
    raise SystemExit(1)


def abspath(path: str | Path) -> Path:
    path = Path(path).expanduser()
    return path if path.is_absolute() else Path.cwd() / path


def num(value, name: str, cast):
    try:
        return cast(value)
    except (TypeError, ValueError):
        die(f"invalid {name}: {value!r}")


def load_key() -> str:
    key = os.environ.get("OPENROUTER_SYS1", "").strip()
    if key:
        return key
    for candidate in (Path.cwd() / "auth_sys1.env", HERE / "auth_sys1.env"):
        if candidate.is_file():
            for line in candidate.read_text().splitlines():
                if line.startswith("OPENROUTER_SYS1="):
                    return line.split("=", 1)[1].strip()
    die("OPENROUTER_SYS1 is not set and auth_sys1.env was not found")


def ask(label: str, default: str | None = None, required: bool = False) -> str:
    suffix = f" [{default}]" if default not in (None, "") else ""
    while True:
        try:
            value = input(f"{label}{suffix}: ").strip()
        except EOFError:
            if required:
                die(f"missing required input: {label}")
            return default or ""
        if not value and default is not None:
            return default
        if value or not required:
            return value
        print("  value required", file=sys.stderr)


def probe(path: Path) -> dict:
    gen = imageio_ffmpeg.read_frames(str(path))
    try:
        return next(gen)
    except Exception as exc:  # noqa: BLE001 - surface exact ffmpeg failure
        die(f"cannot probe video {path}: {exc}")
    finally:
        gen.close()


def even(value: int) -> int:
    return value - (value % 2)


def extract(video: Path, fps: float, height: int) -> list[Path]:
    if FRAMES_DIR.exists():
        shutil.rmtree(FRAMES_DIR)
    FRAMES_DIR.mkdir(parents=True)
    exe = imageio_ffmpeg.get_ffmpeg_exe()
    vf = f"fps={fps:g},scale=-2:{height}"
    cmd = [
        exe, "-hide_banner", "-loglevel", "error", "-y",
        "-i", str(video),
        "-vf", vf,
        "-q:v", "2",
        "-start_number", "1",
        str(FRAMES_DIR / "%06d.jpg"),
    ]
    proc = subprocess.run(cmd, capture_output=True, text=True)
    if proc.returncode != 0:
        die(f"ffmpeg extraction failed (exit {proc.returncode}): {proc.stderr.strip()[:800]}")
    frames = sorted(FRAMES_DIR.glob("*.jpg"))
    if not frames:
        die("ffmpeg produced no frames")
    return frames


def timestamp(seconds: float) -> str:
    total_ms = int(round(seconds * 1000))
    hours, rem = divmod(total_ms, 3_600_000)
    minutes, rem = divmod(rem, 60_000)
    secs, millis = divmod(rem, 1000)
    return f"{hours:02d}:{minutes:02d}:{secs:02d}:{millis:03d}"


def ask_frame(
    key: str,
    model: str,
    frame: Path,
    question: str,
    stamp: str,
    retries: int = RETRIES,
) -> dict:
    payload = base64.b64encode(frame.read_bytes()).decode()
    body = {
        "model": model,
        "state": [
            {"type": "image_url", "image_url": {"url": f"data:image/jpeg;base64,{payload}"}},
            {"type": "text", "text": f"Video frame at {stamp}."},
        ],
        "questions": {"q": {"type": "noul", "instructions": question}},
    }
    last = ""
    for attempt in range(retries):
        try:
            resp = requests.post(
                API_URL,
                headers={"Authorization": f"Bearer {key}"},
                json=body,
                timeout=120,
            )
        except requests.RequestException as exc:
            last = str(exc)
        else:
            if resp.status_code == 200:
                try:
                    answer = resp.json()["answers"]["q"]
                except (ValueError, KeyError, TypeError) as exc:
                    raise FrameError(f"{frame.name}: malformed response: {exc}: {resp.text[:300]}")
                out = {"noul": answer.get("noul")}
                if "confidence" in answer:
                    out["confidence"] = answer["confidence"]
                return out
            if resp.status_code not in RETRY_STATUS:
                raise FrameError(f"{frame.name}: HTTP {resp.status_code}: {resp.text[:400]}")
            last = f"HTTP {resp.status_code}: {resp.text[:200]}"
        time.sleep(min(2 ** attempt, BACKOFF_CAP) + random.uniform(0, JITTER))
    raise FrameError(f"{frame.name}: failed after {retries} attempts: {last}")


def serve_ui(out_path: Path, port: int) -> None:
    index = HERE / "index.html"
    if not index.is_file():
        die(f"index.html not found next to {Path(__file__).name}")
    out_path = abspath(out_path).resolve()

    class Handler(BaseHTTPRequestHandler):
        def do_GET(self) -> None:
            route = urlsplit(self.path).path
            if route in ("/", "/index.html"):
                self.reply(index.read_bytes(), "text/html; charset=utf-8")
            elif route == "/run_results.json":
                if out_path.is_file():
                    self.reply(out_path.read_bytes(), "application/json")
                else:
                    self.reply(
                        f"run_results.json not found - run vid2sys1 first ({out_path})\n".encode(),
                        "text/plain; charset=utf-8",
                        404,
                    )
            else:
                self.reply(b"not found\n", "text/plain; charset=utf-8", 404)

        def reply(self, body: bytes, ctype: str, status: int = 200) -> None:
            self.send_response(status)
            self.send_header("Content-Type", ctype)
            self.send_header("Content-Length", str(len(body)))
            self.send_header("Cache-Control", "no-store")
            self.end_headers()
            self.wfile.write(body)

        def log_message(self, fmt: str, *args) -> None:
            pass

    try:
        server = ThreadingHTTPServer(("127.0.0.1", port), Handler)
    except OSError as exc:
        die(f"cannot bind 127.0.0.1:{port} ({exc}); pass --port")
    bound = server.server_address[1]
    print(f"serving {index.name} on http://localhost:{bound}/", file=sys.stderr)
    print(f"data: {out_path}", file=sys.stderr)
    print("ctrl-c to stop", file=sys.stderr)
    try:
        server.serve_forever()
    except KeyboardInterrupt:
        print("\nstopped", file=sys.stderr)
    finally:
        server.server_close()


def main() -> None:
    parser = argparse.ArgumentParser(
        description="Sample video frames and ask one sys1 noul question per frame."
    )
    parser.add_argument("video", nargs="?", help="path to .mkv/.mp4/.mov")
    parser.add_argument("-H", "--height", type=int, help="vertical resolution guide (2..source height)")
    parser.add_argument("-f", "--fps", type=float, help="sampling fps (1..source fps)")
    parser.add_argument("-q", "--question", help="yes/no question per frame")
    parser.add_argument(
        "--workers", type=int, default=CONCURRENT, help=f"parallel requests (default {CONCURRENT})"
    )
    parser.add_argument("--model", default=DEFAULT_MODEL, help=f"sys1 model (default {DEFAULT_MODEL})")
    parser.add_argument("--out", default=str(RESULTS_PATH), help=f"output JSON (default {RESULTS_PATH})")
    parser.add_argument(
        "--ui", action="store_true", help="serve index.html + the output JSON instead of running"
    )
    parser.add_argument("--port", type=int, default=UI_PORT, help=f"UI server port (default {UI_PORT})")
    args = parser.parse_args()

    if args.ui:
        serve_ui(Path(args.out), args.port)
        return

    key = load_key()

    video_arg = args.video or ask("Video path (.mkv/.mp4/.mov)", required=True)
    video = abspath(video_arg).resolve()
    if not video.is_file():
        die(f"video not found: {video}")
    if video.suffix.lower() not in VIDEO_EXTS:
        die(f"unsupported extension {video.suffix!r}; expected one of {sorted(VIDEO_EXTS)}")

    meta = probe(video)
    src_fps = float(meta.get("fps") or 0)
    if src_fps <= 0:
        die(f"cannot determine source fps for {video}")
    src_w, src_h = meta.get("size") or meta.get("source_size") or (0, 0)
    if not src_w or not src_h:
        die(f"cannot determine source resolution for {video}")
    duration = meta.get("duration")

    height_arg = args.height if args.height is not None else ask(f"Vertical resolution (2-{src_h})", "720")
    height = num(height_arg, "vertical resolution", int)
    if height < 2:
        die("vertical resolution must be >= 2")
    if height > src_h:
        print(f"note: capping height {height} to source height {src_h}", file=sys.stderr)
        height = src_h
    height = even(height)
    width = max(2, even(round(src_w * height / src_h)))

    fps_arg = args.fps if args.fps is not None else ask(f"FPS (1-{src_fps:g})", "1")
    fps = num(fps_arg, "fps", float)
    if fps <= 0:
        die("fps must be > 0")
    if fps > src_fps:
        print(f"note: capping fps {fps:g} to source fps {src_fps:g}", file=sys.stderr)
        fps = src_fps

    question = args.question if args.question is not None else ask("Question (yes/no)", required=True)
    if not question.strip():
        die("question must not be empty")

    estimate = math.ceil(duration * fps) if duration else "?"
    print(
        f"video={video.name} src={src_w}x{src_h}@{src_fps:g}fps "
        f"-> {width}x{height}@{fps:g}fps, ~{estimate} frames",
        file=sys.stderr,
    )

    frames = extract(video, fps, height)
    total = len(frames)
    print(f"extracted {total} frames -> {FRAMES_DIR}", file=sys.stderr)

    results: list[dict] = []
    step = max(1, total // 10)
    try:
        with ThreadPoolExecutor(max_workers=max(1, args.workers)) as pool:
            futures = {}
            for index, frame in enumerate(frames, start=1):
                stamp = timestamp((index - 1) / fps)
                futures[pool.submit(ask_frame, key, args.model, frame, question, stamp)] = (index, stamp)
            for done, future in enumerate(as_completed(futures), start=1):
                index, stamp = futures[future]
                try:
                    answer = future.result()
                except FrameError as exc:
                    pool.shutdown(wait=False, cancel_futures=True)
                    die(str(exc))
                results.append({"frame": index, "timestamp": stamp, **answer})
                if done % step == 0 or done == total:
                    print(f"  {done}/{total}", file=sys.stderr)
    except KeyboardInterrupt:
        print("\ninterrupted", file=sys.stderr)
        raise SystemExit(130)

    results.sort(key=lambda item: item["frame"])

    output = {
        "header": {
            "tool": "vid2sys1",
            "created_at": datetime.now(timezone.utc).isoformat(),
            "video": str(video),
            "question": question,
            "model": args.model,
            "source_fps": src_fps,
            "fps": fps,
            "resolution": {"width": width, "height": height},
            "duration_seconds": duration,
            "frame_count": len(results),
        },
        "frames": results,
    }

    out_path = abspath(args.out)
    out_path.parent.mkdir(parents=True, exist_ok=True)
    tmp_path = out_path.with_suffix(out_path.suffix + ".tmp")
    tmp_path.write_text(json.dumps(output, indent=2) + "\n")
    os.replace(tmp_path, out_path)
    print(f"wrote {out_path} ({len(results)} frames)", file=sys.stderr)


if __name__ == "__main__":
    main()
