# What the hub needs to do differently

**Written 2026-09-17. Nothing in `robot-hub/` has been changed — this is the
spec for when it is.** `reachy_chat` now has the receiving end of all of it;
every flag below exists and works today.

The hub's current Reachy adapter was written when one `reachy_chat` meant one
robot, running on the laptop, listening through a K11 lavalier. Two of those
three are no longer true.

---

## 1. The big one: run it *on* the robot, not on the laptop

`hub/adapters/reachy_wireless.py` `launch()` starts `laptop_chat.py` as a local
child process and opens `http://127.0.0.1:<allocated-port>/`.

That worked because the laptop held the microphone. It cannot work now. The
K11 is gone and every robot listens through its own built-in mic, which is
physically on the robot — and `robot_streaming_player.py` is one-directional
(audio and motion *to* the robot; nothing comes back). There is no path for
the robot's microphone to reach a laptop-hosted process.

So the launch becomes an SSH launch:

```
ssh pollen@<robot-ip>
cd /home/pollen/reachy_chat && /venvs/mini_daemon/bin/python laptop_chat.py \
    --local-robot --host 0.0.0.0 --no-browser \
    --robot-id <unit_id> --robot-name <assigned name> \
    --provider gemini --api-key-id <key id> \
    --port 8765
```

and the tab the hub opens becomes `http://<robot-ip>:8765/`.

**Consequences that make the hub's job easier, not harder:**

- **Port allocation stops mattering for this robot type.** Each robot binds
  8765 on its own address. There is no collision to arbitrate, so the 9100–9199
  allocator is not needed here (it stays for everything else).
- **`ensure_zero_instances()` gets safe.** Today it calls
  `supervisor.kill_matching(("laptop_chat.py", "robot_streaming_player.py"))`,
  which matches *every* such process on the laptop regardless of which robot it
  serves — so launching robot #2 would kill robot #1's dashboard. With nothing
  running on the laptop there is nothing for it to hit. **Drop the laptop-side
  sweep for this robot type.**
- **`_clear_robot_side()` was right all along.** It SSHes to one specific
  robot's address and clears only that robot's leftovers. Keep it exactly as
  it is; it becomes the whole of `ensure_zero_instances` for this type.

The measured facts that made the old design correct are all in `PLAN.md` §2.6
and are still true — they just no longer apply to a laptop-hosted process.

---

## 2. Per-robot config, not per-type

`hub/config.py` `config.robot(type_id).settings` returns one dict shared by
every robot of that type. With ten Reachys that means one name, one announce
sentence, one key for all of them.

Needs per-unit overrides keyed by `unit_id`, falling back to the type defaults:

```toml
[robots.reachy_wireless.units."unit-a1b2"]
display_name = "Rina"
api_key_id   = "gemini-lab-1"

[robots.reachy_wireless.units."unit-c3d4"]
display_name = "Dvir"
api_key_id   = "gemini-lab-2"
```

`hub/core.py` `_on_found` currently does
`name = cfg.display_name or DISPLAY_NAMES[found.type_id]`, so all ten cards
read "Reachy-Mini". It should prefer the per-unit name.

The announcement comes free once names do: `hub/announce.py` derives the WAV
filename from a hash of the *text*, so "I am Rina" gets its own recording
automatically.

---

## 3. The name registry

Decided: **the hub is the authority.** It discovers `unit_id` over mDNS before
anything launches, holds the `unit_id → name` map, and passes both in.

`reachy_chat` caches what it is given in `robot_identity.json` on the robot, so
a hand-started instance still knows who it is, and it **logs a warning if a
launch renames a robot that already had a different name** — a robot that
quietly becomes a different robot is how two transcripts end up attributed to
the wrong body.

A name is assigned once and then permanent. Renaming is possible; it should be
deliberate.

---

## 4. The keys registry

Decided: **the hub owns the robots-to-keys map.** You draw "robots 1 and 2 on
key A, 3 and 4 on key B, 5–7 on key C" in one place, and the hub passes each
robot its assignment at launch.

`reachy_chat` reads `keys.json`, deployed to each robot:

```json
{
  "keys": [
    {"id": "gemini-lab-1", "provider": "gemini",   "label": "Lab Gemini #1", "key_env": "GEMINI_API_KEY_1"},
    {"id": "gemini-lab-2", "provider": "gemini",   "label": "Lab Gemini #2", "key": "AIza..."},
    {"id": "openai-demo",  "provider": "gpt_live", "label": "OpenAI demo",   "key_env": "OPENAI_API_KEY"}
  ]
}
```

`key_env` names an environment variable instead of inlining the secret, which
is how one registry file gets deployed to ten robots without ten copies of a
key on ten disks.

The hub then passes `--provider <name> --api-key-id <id>`. Mismatches are
refused with a sentence: a key id that is not in the registry, or a key whose
provider disagrees with `--provider`, fails at startup rather than when
somebody presses Start in front of an audience.

`--api-key <literal>` also exists, and should be a last resort — a key on a
command line is a key in every process list on that machine.

**Nothing about a key reaches the dashboard** except its label and last four
characters.

---

## 5. Providers

`--provider gemini` (the default) or `--provider gpt_live`.

`gpt_live` is a seam, not a backend. A robot launched with it will refuse at
startup with an explanation. `GPT-LIVE-MIGRATION-PLAN.md` §6 gates it behind a
one-day Hebrew spike that has not been run — OpenAI publishes no language list
for `gpt-live-1` and all twelve of its voices are English or Brazilian
Portuguese. The hub can offer the option; it should expect the refusal until
that spike passes.

---

## 6. The microphone

`mic_mode` in the adapter is currently advisory: it decides whether to claim a
laptop audio device, but nothing is ever passed to `laptop_chat.py`.

With on-robot execution there is no laptop capture device to claim at all for
this robot type, so **the `audio_in` claim should go away here** — it can only
produce false conflicts with NAO now.

Two flags exist for when the built-in mic has actually been measured:
`--mic-match <substring>` and `--mic-rate <hz>`. Neither has a verified value
yet. `tools/probe_internal_mic.py`, run on a robot, prints both — and also
measures the echo floor, which is the number that decides whether barge-in is
ever possible.

---

## 7. Health and the card

Unchanged. The daemon still serves `/api/daemon/status` on port 8000 on the
robot's own address, which is what the hub already probes. What changes is that
the dashboard URL is now on the robot too, so a card whose robot is unreachable
means the dashboard is unreachable as well — worth saying on the card rather
than leaving a dead link.

---

## 8. Wizard of Oz lines, live captions, deploying without the venv (2026-11-05)

**Typed lines.** The operator types a line nobody scripted; it is recorded in
the show voice (voice, model and style from `show/cues.json`, Gemini TTS on the
Gemini key) while they type, so Say is instant. Code: `show_lines.py`.

| Route | Body | Answer |
|---|---|---|
| `POST /api/show/prepare` | `{text, wait?: true}` | `{id, ready, ms, text, voice}`. `ms` = clip length. Blocks until recorded (30 s cap) unless `wait: false`. Safe to call on every debounced keystroke: a recorded line is a cache hit, a line being recorded is shared. |
| `POST /api/show/say` | `{text}` | `{played: true, prepared, ms, id, text, waited_ms}`. Plays the prepared clip at once (`prepared: true`), or records it first (`prepared: false`, `waited_ms` = the wait). Same rules as firing an audio cue: idle or in a conversation (else 409 `invalid_state`), cuts what is playing, `POST /api/show/stop` silences it. |
| `GET /api/show/said` | — | `{said: [{id, text, ms, prepared, at}]}`, the last 50, oldest first |

Errors: `{error: "<sentence>", code}` with `code` one of `EMPTY`, `TOO_LONG`
(over 600 characters), `NO_KEY`, `RATE_LIMITED`, `TTS_FAILED`, `NOT_READY`
(504, still recording after 30 s), `INVALID_STATE` (409), `SHOW_FAILED`
(e.g. robot not connected).

Events on `/ws`:
- `show.line.ready` `{id, text, ms}` when a typed line finishes recording.
- `show.said` `{id, text, ms, prepared, at}` when a typed line is played (the "Said" history).
- The clip itself plays like a cue: `show.cue.started` / `show.cue.ended` with
  `cue_id: "said:<id>"`, `label: "Said"`, plus `said: true` and `text`.

Cache: `show/audio/said/<id>.wav` on the robot (git-ignored, not shipped by
`--with-show`), keyed by (text, voice, style, model), capped at 100 clips,
least recently used first out.

**Live captions.** `conversation.partial` `{turn, turn_id, who: "robot"|"user",
text, delta}` is sent every time a piece of transcription arrives in a turn.
`text` is everything so far in that turn (replace, don't append); the turn's
`transcript.robot` / `transcript.user` stays the final. All three backends send
it (Gemini Live output transcription, GPT-Live `output_transcript.delta`, the
ElevenLabs layer, where the text runs slightly ahead of the voice).

**Deploy.** `tools/deploy_robot_app.py` and `tools/deploy_robot_player.py`
read their constants from `deploy_config.py`, not `conversation.py`, so they
run under any Python 3 with `paramiko` (the hub's own). SSH login from
`ROBOT_USER` / `ROBOT_PASSWORD`, default the robot's factory login. Flags and
behaviour unchanged.

---

## Quick reference — every flag the hub passes

| Flag | Value | Required |
|---|---|---|
| `--local-robot` | — | yes, on the robot |
| `--host` | `0.0.0.0` | yes, so the laptop can reach it |
| `--no-browser` | — | yes; the hub opens the tab |
| `--robot-id` | the mDNS `unit_id` | strongly |
| `--robot-name` | the assigned name | strongly |
| `--provider` | `gemini` | recommended |
| `--api-key-id` | an id from `keys.json` | when >1 key |
| `--port` | `8765` | optional on-robot |
| `--mic-match` | measured device substring | once measured |
| `--mic-rate` | measured rate | if not 48000 |

All of them are optional. A robot started by hand with none of them comes up on
the base persona, the default provider, and whatever key the environment holds.
