#!/usr/bin/env python3
"""Wizard of Oz lines, live captions and the deploy constants, with no robot,
no network and no key.

    .venv\\Scripts\\python.exe tools\\test_show_lines.py

Covers:
  * show_lines.LineStore: one recording per line however often prepare is
    called, cache hits, wait=false, the cache cap, Hebrew vs English style,
    every error code, retry after a failure;
  * ShowPlayer.play_line: plays through the cue path, carries its text in the
    events, and STOP cuts it;
  * POST /api/show/prepare, /api/show/say, GET /api/show/said through the real
    FastAPI app and the real SystemManager.say_line, against fakes;
  * conversation.partial: drain_one_turn_streaming feeds the partial hook with
    the robot's words as they arrive;
  * deploy_config: conversation re-exports it, and the deploy tool no longer
    imports conversation.

Uses temporary folders; never writes into show/.
"""

from __future__ import annotations

import asyncio
import sys
import tempfile
import threading
import time
from pathlib import Path

ROOT = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(ROOT))
try:
    sys.stdout.reconfigure(encoding="utf-8", errors="replace")
except Exception:
    pass

import numpy as np  # noqa: E402

import show_lines  # noqa: E402
from show_lines import LineError, LineStore  # noqa: E402

PASSED: list[str] = []


def check(name: str, cond: bool, detail: str = "") -> None:
    if not cond:
        raise AssertionError(f"{name} FAILED {detail}")
    PASSED.append(name)
    print(f"  ok  {name}")


class FakeSynth:
    """Stands in for Gemini TTS: 0.5 s of tone per call, slowly."""

    def __init__(self, delay: float = 0.2, seconds: float = 0.5):
        self.calls: list[tuple] = []
        self.delay = delay
        self.seconds = seconds
        self.fail_with: Exception | None = None
        self.lock = threading.Lock()

    def __call__(self, text, voice, style, model, api_key):
        with self.lock:
            self.calls.append((text, voice, style, model))
        time.sleep(self.delay)
        if self.fail_with is not None:
            raise self.fail_with
        n = int(24000 * self.seconds)
        return (np.sin(np.arange(n) / 10.0) * 8000).astype(np.int16)


def make_store(tmp: Path, **kw) -> tuple[LineStore, FakeSynth, list]:
    cues = tmp / "cues.json"
    cues.write_text('{"voice": "Kore", "style": "read in Hebrew", '
                    '"tts_model": "tts-x", "sections": []}', encoding="utf-8")
    synth = kw.pop("synth", None) or FakeSynth()
    events: list = []
    store = LineStore(key_getter=kw.pop("key_getter", lambda: "test-key"),
                      broadcast=events.append, cache_dir=tmp / "said",
                      cues_path=cues, synth=synth, **kw)
    return store, synth, events


def test_store() -> None:
    print("LineStore")
    with tempfile.TemporaryDirectory() as d:
        tmp = Path(d)
        store, synth, events = make_store(tmp)

        # Five callers ask for the same line at once: one recording.
        results = []
        threads = [threading.Thread(target=lambda: results.append(
            store.prepare("Hello there"))) for _ in range(5)]
        for t in threads:
            t.start()
        for t in threads:
            t.join()
        check("concurrent prepare records once", len(synth.calls) == 1, synth.calls)
        check("all callers get ready", all(r["ready"] for r in results), results)
        check("ms is the clip length", all(r["ms"] == 500 for r in results), results)
        check("voice from cues.json", synth.calls[0][1] == "Kore" and synth.calls[0][3] == "tts-x")
        check("English line gets the English style",
              synth.calls[0][2] == show_lines.DEFAULT_STYLE_EN)
        check("show.line.ready broadcast",
              [e["event"] for e in events] == ["show.line.ready"], events)

        again = store.prepare("  Hello   there ")
        check("cache hit (whitespace normalised)", len(synth.calls) == 1
              and again["ready"] and again["id"] == results[0]["id"])

        store.prepare("שלום לכולם")
        check("Hebrew line gets the script's style", synth.calls[-1][2] == "read in Hebrew")

        synth.delay = 0.5
        quick = store.prepare("Not yet", wait=False)
        check("wait=false returns at once, not ready", not quick["ready"] and quick["ms"] == 0)
        line = store.ensure("Not yet")
        check("ensure waits for the running recording",
              not line["prepared"] and line["ms"] == 500 and line["path"].exists()
              and sum(1 for c in synth.calls if c[0] == "Not yet") == 1)
        line2 = store.ensure("Not yet")
        check("ensure on a ready line is prepared", line2["prepared"] and line2["waited_ms"] < 200)
        synth.delay = 0.0

        for code, text in (("EMPTY", "   "), ("TOO_LONG", "x" * 601)):
            try:
                store.prepare(text)
                check(f"{code} raised", False)
            except LineError as e:
                check(f"{code} raised", e.code == code, e.code)

        synth.fail_with = RuntimeError("429 RESOURCE_EXHAUSTED")
        try:
            store.prepare("busy line")
            check("RATE_LIMITED raised", False)
        except LineError as e:
            check("RATE_LIMITED raised", e.code == "RATE_LIMITED", e.code)
        synth.fail_with = RuntimeError("boom")
        try:
            store.prepare("busy line")
            check("TTS_FAILED raised", False)
        except LineError as e:
            check("TTS_FAILED raised", e.code == "TTS_FAILED", e.code)
        synth.fail_with = None
        check("retry after a failure works", store.prepare("busy line")["ready"])
        store.shutdown()

    with tempfile.TemporaryDirectory() as d:
        def no_key():
            sys.exit("No Gemini API key found")
        store, synth, _ = make_store(Path(d), key_getter=no_key)
        try:
            store.prepare("hi")
            check("NO_KEY raised (SystemExit from the key lookup)", False)
        except LineError as e:
            check("NO_KEY raised (SystemExit from the key lookup)", e.code == "NO_KEY", e.code)
        check("no synth call without a key", not synth.calls)
        store.shutdown()

    with tempfile.TemporaryDirectory() as d:
        store, synth, _ = make_store(Path(d), max_files=3,
                                     synth=FakeSynth(delay=0.0, seconds=0.1))
        ids = []
        for i in range(5):
            ids.append(store.prepare(f"line {i}")["id"])
            time.sleep(0.02)   # distinct mtimes
        left = sorted(p.stem for p in (Path(d) / "said").glob("*.wav"))
        check("cache capped at max_files", len(left) == 3, left)
        check("newest clips kept", set(ids[-3:]) == set(left), (ids, left))
        check("no temp files left", not list((Path(d) / "said").glob("*.tmp*")))
        store.shutdown()


class FakeRobot:
    connected = True

    def __init__(self):
        self.chunks = 0
        self.cleared = 0
        self.motions = []
        self.ended = 0

    def stream_chunk(self, samples):
        self.chunks += 1
        return True

    def end_turn(self):
        self.ended += 1

    def clear_playback(self):
        self.cleared += 1

    def send_motion_command(self, cmd):
        self.motions.append(cmd)


def test_player_and_routes() -> None:
    print("ShowPlayer.play_line + routes")
    from fastapi.testclient import TestClient

    import system
    from show_player import ShowPlayer
    from web.server import create_app

    with tempfile.TemporaryDirectory() as d:
        events: list = []
        robot = FakeRobot()
        store, synth, _ = make_store(Path(d), synth=FakeSynth(delay=0.05, seconds=2.0))
        store._broadcast = events.append
        player = ShowPlayer(lambda: robot, broadcast=events.append)

        class FakeManager:
            state = system.SystemState.IDLE_BREATHING
            say_line = system.SystemManager.say_line
            _check_show_state = system.SystemManager._check_show_state

            def __init__(self):
                self.lines = store
                self.show = player

            def stop_cue(self):
                self.show.stop()
                return {"stopped": True}

        class FakeBroadcaster:
            def set_loop(self, loop):
                pass

        mgr = FakeManager()
        client = TestClient(create_app(mgr, FakeBroadcaster()))

        r = client.post("/api/show/prepare", json={"text": "Welcome, everyone"})
        body = r.json()
        check("prepare 200", r.status_code == 200, r.text)
        check("prepare shape", {"id", "ready", "ms"} <= set(body) and body["ready"]
              and body["ms"] == 2000, body)

        r = client.post("/api/show/say", json={"text": "Welcome, everyone"})
        said = r.json()
        check("say 200", r.status_code == 200, r.text)
        check("say played a prepared clip", said["played"] and said["prepared"]
              and said["ms"] == 2000, said)
        time.sleep(0.3)
        check("audio streams to the robot", robot.chunks > 0)
        started = [e for e in events if e.get("event") == "show.cue.started"]
        check("show.cue.started carries said + text",
              started and started[-1]["said"] and started[-1]["text"] == "Welcome, everyone"
              and started[-1]["cue_id"] == f"said:{said['id']}", started)
        check("show.said broadcast", any(e.get("event") == "show.said" for e in events))
        check("playing is the said line", player.playing == f"said:{said['id']}")

        r = client.post("/api/show/stop")
        check("STOP cuts the said line", r.status_code == 200 and player.playing is None
              and robot.cleared >= 1)

        n_before = len(synth.calls)
        r = client.post("/api/show/say", json={"text": "A brand new line"})
        live = r.json()
        check("say on an unprepared line records then plays",
              r.status_code == 200 and live["played"] and not live["prepared"]
              and len(synth.calls) == n_before + 1, live)
        client.post("/api/show/stop")

        r = client.get("/api/show/said")
        check("said history", [x["text"] for x in r.json()["said"]]
              == ["Welcome, everyone", "A brand new line"], r.text)

        r = client.post("/api/show/say", json={"text": ""})
        check("say empty -> 400 EMPTY", r.status_code == 400 and r.json()["code"] == "EMPTY", r.text)

        mgr.state = system.SystemState.STOPPED
        r = client.post("/api/show/say", json={"text": "Welcome, everyone"})
        check("say while stopped -> 409", r.status_code == 409
              and r.json()["code"] == "INVALID_STATE", r.text)
        mgr.state = system.SystemState.IDLE_BREATHING

        robot.connected = False
        r = client.post("/api/show/say", json={"text": "Welcome, everyone"})
        check("say with robot down -> 400 SHOW_FAILED", r.status_code == 400
              and r.json()["code"] == "SHOW_FAILED", r.text)
        store.shutdown()


def test_partials() -> None:
    print("conversation.partial")
    import conversation as conv
    from providers.base import gemini_like

    class FakeSession:
        async def receive(self):
            for msg in (gemini_like(user_text="Tell me "),
                        gemini_like(user_text="a joke"),
                        gemini_like(robot_text="Why did "),
                        gemini_like(audio=np.zeros(2400, np.int16).tobytes()),
                        gemini_like(robot_text="the robot "),
                        gemini_like(robot_text="cross the road?"),
                        gemini_like(turn_complete=True)):
                yield msg

    seen = []
    conv.set_partial_hook(lambda who, turn, text, delta: seen.append((who, turn, text, delta)))
    try:
        t = conv.TurnTimings(turn=4)
        audio, user_txt, robot_txt = asyncio.run(conv.drain_one_turn_streaming(
            FakeSession(), FakeRobot(), t, time.perf_counter()))
    finally:
        conv.set_partial_hook(None)
    robot_parts = [s for s in seen if s[0] == "robot"]
    check("robot partials arrive per piece", [p[2] for p in robot_parts] ==
          ["Why did", "Why did the robot", "Why did the robot cross the road?"], robot_parts)
    check("partials carry the turn", all(p[1] == 4 for p in seen))
    check("user partials too", [s[2] for s in seen if s[0] == "user"] == ["Tell me", "Tell me a joke"])
    check("final text unchanged", robot_txt == "Why did the robot cross the road?"
          and user_txt == "Tell me a joke")

    import system

    class B:
        def __init__(self):
            self.out = []

        def broadcast_threadsafe(self, m):
            self.out.append(m)

    fake = type("M", (), {})()
    fake.broadcaster = B()
    fake._broadcast = lambda m: system.SystemManager._broadcast(fake, m)
    system.SystemManager._on_partial(fake, "robot", 2, "Hi there", "there")
    check("conversation.partial event shape", fake.broadcaster.out == [{
        "event": "conversation.partial", "turn": 2, "turn_id": 2, "who": "robot",
        "text": "Hi there", "delta": "there"}], fake.broadcaster.out)


def test_deploy_config() -> None:
    print("deploy_config")
    import conversation as conv
    import deploy_config
    for name in ("ROBOT_HOST_DEFAULT", "ROBOT_HOST", "ROBOT_USER", "ROBOT_PASSWORD",
                 "ROBOT_PYTHON", "ROBOT_STREAMING_PLAYER"):
        check(f"conversation.{name} is deploy_config's",
              getattr(conv, name) == getattr(deploy_config, name))
    for tool in ("deploy_robot_app.py", "deploy_robot_player.py"):
        src = (ROOT / "tools" / tool).read_text(encoding="utf-8")
        check(f"{tool} does not import conversation",
              "from conversation import" not in src and "import conversation" not in src)
    app_src = (ROOT / "tools" / "deploy_robot_app.py").read_text(encoding="utf-8")
    for f in ("deploy_config.py", "show_lines.py"):
        check(f"deploy ships {f}", f'"{f}"' in app_src)


def main() -> int:
    test_store()
    test_player_and_routes()
    test_partials()
    test_deploy_config()
    print(f"\nall {len(PASSED)} checks passed")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
