"""Wizard of Oz: lines the operator types on the spot.

The show script (show/cues.json) covers what was planned. On stage the
operator also needs to make Reachy say something nobody planned, and say it
now. A TTS call takes a few seconds, so the hub records the line while the
operator is still typing (`POST /api/show/prepare`, debounced on the hub side)
and Say (`POST /api/show/say`) then plays a clip that is already on disk.
When the clip is not ready yet, Say waits for it (or records it there and
then) and plays it the moment it lands.

Same voice as the scripted cues: the voice, model and delivery style come from
show/cues.json, and the synthesis is build_show.synthesise(), so a typed line
sounds exactly like a scripted one. Clips are cached by (text, voice, style,
model) under show/audio/said/ (git-ignored, like the cue audio) and the cache
is capped; the least recently used clips go first.

Safe to call repeatedly while someone types: a clip already on disk is a hit,
and a second request for a line that is being recorded waits on the same
recording instead of paying for another one.
"""

from __future__ import annotations

import hashlib
import json
import logging
import os
import re
import threading
import time
from collections import deque
from concurrent.futures import Future, ThreadPoolExecutor
from concurrent.futures import TimeoutError as FutureTimeout
from pathlib import Path
from typing import Callable, Optional

import soundfile as sf

SCRIPT_DIR = Path(__file__).resolve().parent
SHOW_DIR = SCRIPT_DIR / "show"
CUES_PATH = SHOW_DIR / "cues.json"
SAID_DIR = SHOW_DIR / "audio" / "said"

TTS_RATE = 24000                 # what the TTS model emits and the player expects
MAX_TEXT_CHARS = 600             # same limit as a scripted cue
MAX_FILES = 100                  # cached clips kept on the robot's SD card
HISTORY = 50                     # "Said" lines kept for a dashboard that reconnects
DEFAULT_VOICE = "Aoede"
DEFAULT_MODEL = "gemini-3.1-flash-tts-preview"
# cues.json's style tells the TTS model to read Hebrew. A typed English line
# gets the same delivery without the language instruction.
DEFAULT_STYLE_EN = ("Say this in English, in a warm, friendly and natural "
                    "tone, at a normal speaking pace.")

_HEBREW = re.compile(r"[֐-׿]")

log = logging.getLogger("reachy.show.lines")


class LineError(Exception):
    """A line could not be prepared or played. `code` is a short machine code
    for the hub (EMPTY, TOO_LONG, NO_KEY, RATE_LIMITED, TTS_FAILED, NOT_READY),
    the message is a sentence for the operator."""

    def __init__(self, message: str, code: str = "TTS_FAILED", status: int = 400):
        super().__init__(message)
        self.code = code
        self.status = status


def clean_text(text) -> str:
    text = " ".join(str(text or "").split())
    if not text:
        raise LineError("type a line first", "EMPTY")
    if len(text) > MAX_TEXT_CHARS:
        raise LineError(f"keep it under {MAX_TEXT_CHARS} characters", "TOO_LONG")
    return text


def _gemini_synth(text: str, voice: str, style: str, model: str, api_key: str):
    """The real recorder: build_show's synthesise(), unpaced (one attempt)."""
    import sys
    tools = str(SCRIPT_DIR / "tools")
    if tools not in sys.path:
        sys.path.insert(0, tools)
    import build_show                   # noqa: E402  (tools/ on sys.path)
    from google import genai

    client = genai.Client(api_key=api_key)
    return build_show.synthesise(client, model, voice, style, text, pace=False)


class LineStore:
    """Records, caches and remembers typed lines. One per SystemManager."""

    def __init__(self, key_getter: Callable[[], str],
                 broadcast: Optional[Callable[[dict], None]] = None,
                 cache_dir: Path = SAID_DIR,
                 cues_path: Path = CUES_PATH,
                 max_files: int = MAX_FILES,
                 synth: Optional[Callable] = None,
                 workers: int = 2):
        self._key_getter = key_getter
        self._broadcast = broadcast
        self.cache_dir = Path(cache_dir)
        self.cues_path = Path(cues_path)
        self.max_files = max(1, int(max_files))
        self._synth = synth or _gemini_synth
        self._lock = threading.Lock()
        self._inflight: dict[str, Future] = {}
        self._ms: dict[str, int] = {}
        self._pool = ThreadPoolExecutor(max_workers=workers,
                                        thread_name_prefix="said-tts")
        self.history: deque = deque(maxlen=HISTORY)

    # ----- what a line is -----

    def _voice(self) -> tuple[str, str, str]:
        """(voice, style, model) from cues.json, read each time so a voice
        changed in the script applies to the next typed line."""
        try:
            data = json.loads(self.cues_path.read_text(encoding="utf-8"))
        except Exception:
            data = {}
        return (str(data.get("voice") or DEFAULT_VOICE),
                str(data.get("style") or ""),
                str(data.get("tts_model") or DEFAULT_MODEL))

    def describe(self, text) -> dict:
        """The cache identity of one line, without recording anything."""
        text = clean_text(text)
        voice, style_he, model = self._voice()
        style = style_he if _HEBREW.search(text) else DEFAULT_STYLE_EN
        h = hashlib.sha256(
            f"{model}\x00{voice}\x00{style}\x00{text}".encode("utf-8")).hexdigest()[:16]
        return {"id": h, "text": text, "voice": voice, "style": style, "model": model}

    def path_for(self, line_id: str) -> Path:
        if not re.fullmatch(r"[0-9a-f]{16}", line_id or ""):
            raise LineError("unknown line", "NOT_FOUND", 404)
        return self.cache_dir / f"{line_id}.wav"

    def _clip_ms(self, line_id: str, path: Path) -> int:
        ms = self._ms.get(line_id)
        if ms is None:
            try:
                info = sf.info(str(path))
                ms = int(round(info.frames * 1000 / (info.samplerate or TTS_RATE)))
            except Exception:
                ms = 0
            self._ms[line_id] = ms
        return ms

    # ----- recording -----

    def _start(self, line: dict) -> tuple[Optional[Future], bool]:
        """(future, cached). A clip on disk is a hit (and is touched, so the
        cache drops the least recently *used* clip); a line already being
        recorded shares that recording."""
        path = self.path_for(line["id"])
        with self._lock:
            if path.exists():
                try:
                    os.utime(path, None)
                except OSError:
                    pass
                return None, True
            fut = self._inflight.get(line["id"])
            if fut is None:
                fut = self._pool.submit(self._record, line)
                self._inflight[line["id"]] = fut
            return fut, False

    def _record(self, line: dict) -> int:
        lid = line["id"]
        t0 = time.monotonic()
        try:
            try:
                api_key = self._key_getter()
            except (Exception, SystemExit) as e:
                raise LineError(f"no Gemini key for the show voice ({e})",
                                "NO_KEY") from None
            if not api_key:
                raise LineError("no Gemini key for the show voice", "NO_KEY")
            try:
                samples = self._synth(line["text"], line["voice"], line["style"],
                                      line["model"], api_key)
            except LineError:
                raise
            except Exception as e:
                msg = str(e)
                if "RESOURCE_EXHAUSTED" in msg or "429" in msg:
                    raise LineError("the voice service is busy, try again in a "
                                    "moment", "RATE_LIMITED") from None
                raise LineError(f"could not record the line: {msg[:200]}",
                                "TTS_FAILED") from None
            if samples is None or len(samples) == 0:
                raise LineError("the voice service sent no audio", "TTS_FAILED")
            self.cache_dir.mkdir(parents=True, exist_ok=True)
            path = self.path_for(lid)
            # Write beside, then rename: a half-written file must never look
            # like a ready clip to the next request.
            tmp = path.with_suffix(".tmp.wav")
            sf.write(str(tmp), samples, TTS_RATE, subtype="PCM_16")
            os.replace(tmp, path)
            ms = int(round(len(samples) * 1000 / TTS_RATE))
            self._ms[lid] = ms
            self._evict(keep=lid)
            log.info("said line %s recorded: %d ms of audio in %.1fs", lid, ms,
                     time.monotonic() - t0)
            self._emit({"event": "show.line.ready", "id": lid,
                        "text": line["text"], "ms": ms})
            return ms
        finally:
            with self._lock:
                self._inflight.pop(lid, None)

    def _evict(self, keep: str) -> None:
        try:
            files = sorted(self.cache_dir.glob("*.wav"),
                           key=lambda p: p.stat().st_mtime)
        except OSError:
            return
        extra = len(files) - self.max_files
        for p in files:
            if extra <= 0:
                break
            if p.stem == keep:
                continue
            try:
                p.unlink()
                self._ms.pop(p.stem, None)
                extra -= 1
            except OSError:
                pass

    # ----- the two calls the routes make -----

    def prepare(self, text, wait: bool = True, timeout: float = 30.0) -> dict:
        """Record a line in the show voice unless it is already on disk.

        {id, ready, ms, text, voice}: ms is the clip's length (0 while not
        ready). With wait (the default) this returns once the clip is ready or
        `timeout` has passed; the recording carries on either way."""
        line = self.describe(text)
        fut, cached = self._start(line)
        out = {"id": line["id"], "text": line["text"], "voice": line["voice"]}
        if cached:
            return {**out, "ready": True,
                    "ms": self._clip_ms(line["id"], self.path_for(line["id"]))}
        if wait:
            try:
                ms = fut.result(timeout=timeout)
                return {**out, "ready": True, "ms": ms}
            except FutureTimeout:
                pass
        return {**out, "ready": False, "ms": 0}

    def ensure(self, text, timeout: float = 30.0) -> dict:
        """For Say: the clip, recording it now when it is not ready.
        {id, text, path, ms, prepared, waited_ms}. Raises LineError."""
        line = self.describe(text)
        t0 = time.monotonic()
        fut, cached = self._start(line)
        if not cached:
            try:
                fut.result(timeout=timeout)
            except FutureTimeout:
                raise LineError("the line is still being recorded, try again",
                                "NOT_READY", 504) from None
        path = self.path_for(line["id"])
        return {"id": line["id"], "text": line["text"], "path": path,
                "ms": self._clip_ms(line["id"], path), "prepared": cached,
                "waited_ms": int((time.monotonic() - t0) * 1000)}

    def record_said(self, line: dict) -> dict:
        """Remember a played line and tell every dashboard."""
        entry = {"id": line["id"], "text": line["text"], "ms": line["ms"],
                 "prepared": line["prepared"], "at": time.time()}
        self.history.append(entry)
        self._emit({"event": "show.said", **entry})
        return entry

    def said(self) -> list:
        return list(self.history)

    def shutdown(self) -> None:
        self._pool.shutdown(wait=False, cancel_futures=True)

    def _emit(self, payload: dict) -> None:
        if self._broadcast is None:
            return
        try:
            self._broadcast(payload)
        except Exception:
            log.exception("broadcast failed for %s", payload.get("event"))
