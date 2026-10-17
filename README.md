# vid2sys1 (video to system 1 models (image understanding))

converts video into a chart of any condition being present.

- yourvideo.mp4 > "is sensitive data displayed?" > get timescale of probability

video in → one yes/no `noul` question per sampled frame → one JSON out.
no chat, no memory, no cross-frame brain. each frame is isolated request.

## need

- linux (wsl2 ok), python 3.10+, [`uv`](https://docs.astral.sh/uv/)
- openrouter sys1 model key (like jev)
- ffmpeg: bundled via `imageio-ffmpeg`, don't install one

## setup

```bash
uv venv && uv sync
export OPENROUTER_SYS1="sk-or-..."
```

or drop `auth_sys1.env` next to `vid2sys1.py`:

```
OPENROUTER_SYS1=sk-or-...
```

## run

```bash
.venv/bin/python vid2sys1.py
```

full (flags skip *asking):

```bash
.venv/bin/python vid2sys1.py _temp_/vid.mp4 -H 720 -f 24 -q "YOUR_REQUEST" --workers 48 && .venv/bin/python vid2sys1.py --ui
```

| flag | what | default |
| --- | --- | --- |
| `video` | `.mkv`/`.mp4`/`.mov` | * |
| `-H, --height` | vertical px, capped at source | `720` |
| `-f, --fps` | sample rate, capped at source | `1` |
| `-q, --question` | the yes/no thing | * |
| `--workers` | parallel requests | `48` |
| `--model` | sys1 model | `perplexity/pplx-decider-flash-v1.2` |
| `--out` | json path | `_temp_/run_results.json` |
| `--ui` | serve viewer + json, no run | off |
| `--port` | ui port | `8000` |

width follows aspect ratio, rounded even. no upscaling, no re-encode, no audio.

## output

```json
{
  "header": {
    "tool": "vid2sys1",
    "created_at": "2026-10-17T19:56:59+00:00",
    "video": "/abs/path/video.mp4",
    "question": "Is a clash happening?",
    "model": "perplexity/pplx-decider-flash-v1.2",
    "source_fps": 10.0,
    "fps": 1.0,
    "resolution": {"width": 320, "height": 180},
    "duration_seconds": 3.0,
    "frame_count": 3
  },
  "frames": [
    {"frame": 1, "timestamp": "00:00:00:000", "noul": 0.9987},
    {"frame": 2, "timestamp": "00:00:01:000", "noul": 0.9988},
    {"frame": 3, "timestamp": "00:00:02:000", "noul": 0.9987}
  ]
}
```

- `frame`: 1-based
- `timestamp`: `hh:mm:ss:ms3` = `(frame-1)/fps`. constant-rate assumption, VFR drifts
- `noul`: P(yes), 0–1
- `confidence`: only if the API feels like it

frames stay in `_temp_/frames/` for eyeballing. json gets overwritten each run.

## viewer

```bash
.venv/bin/python vid2sys1.py --ui
```

→ <http://localhost:8000/>. plots `noul` over time, drag-drop any `run_results.json`, click to copy a timestamp, `copy ↑thr` for crosses.

loopback only, three routes (`/`, `/index.html`, `/run_results.json`), no key, no pipeline. json read per request → refresh after a run. `?src=` or `#<url>` overrides the file. shift + click sets vertical thresh.

## issue stuff

| scream | move |
| --- | --- |
| `OPENROUTER_SYS1 is not set` | export it or add `auth_sys1.env` |
| `No endpoints found for ...` | bad model id; use the default |
| `ffmpeg extraction failed` | corrupt/weird codec; poke it with `ffmpeg -i` |
| `HTTP 429` after retries | lower cli:`--workers` / constants:`CONCURRENT` |
| `Invalid image dimensions` | raise `--height` (some models want ≥280 px/side) |
| `video not found` | use correct absolute/relative path |
| `cannot bind 127.0.0.1:8000` | port taken; `--port` |
| `404 run_results.json not found` | run it first, or point `--out` at a real file |
