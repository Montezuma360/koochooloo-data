"""Caesar's recorded voice reads the briefing and critical alerts.

Default engine: Kokoro (free, open source, Apache 2.0) with the British male voice bm_george, run right here on the GitHub runner:
no account, no key, no tokens. Set VOICE_ENGINE=elevenlabs (plus the ELEVENLABS_API_KEY secret) to use ElevenLabs instead.

The terminal (inside Claude and on the website) and the landing page play these clips; no other voice is used anywhere.
Live conversations use the same voice through the ElevenLabs agent.

Needs the GitHub secret ELEVENLABS_API_KEY (an ElevenLabs key with only Text to Speech allowed). Without it this does nothing.

What it records (UK time):
- a briefing on weekdays at each slot in VOICE_BRIEF_SLOTS (default "07:30"), and one at once if none exists yet;
- an alert when the market regime changes, a stock newly meets the research setup, a tracked stock files an SEC warning,
  or Fear & Greed reaches an extreme (once a day).
Budget: at most VOICE_MONTHLY_CHARS characters a month (default 8000); alerts come before briefings.

Files in data/voice/:
- index.json: the latest briefing and the last 8 alerts, each with its text, time and the chunk files of its MP3;
- <clip id>-<n>.bin: the MP3 split into 14,000-byte pieces (the terminal's connector reads files up to about 16 KB);
- state.json: what has already been said, and the month's character count."""
import datetime as dt
import hashlib
import json
import os
import re
import sys
import urllib.error
import urllib.request
from zoneinfo import ZoneInfo

KEY = os.environ.get("ELEVENLABS_API_KEY", "").strip()
VOICE_ID = os.environ.get("VOICE_ID", "").strip() or "nPvIP7YzPX8Whz6ZKp2R"  # Jerry B. - British RP | Posh & Refined
ENGINE = (os.environ.get("VOICE_ENGINE", "").strip() or "kokoro").lower()
KOKORO_VOICE = os.environ.get("KOKORO_VOICE", "").strip() or "bm_george"
VOICE_NAME = "Jerry B. (British RP)" if ENGINE == "elevenlabs" else "Caesar \u00b7 British (%s)" % KOKORO_VOICE
MODEL = os.environ.get("VOICE_MODEL", "").strip() or "eleven_multilingual_v2"
CAP = int(os.environ.get("VOICE_MONTHLY_CHARS", "").strip() or (8000 if ENGINE == "elevenlabs" else 200000))
SLOTS = [s.strip() for s in (os.environ.get("VOICE_BRIEF_SLOTS", "").strip() or "07:30").split(",") if s.strip()]
OUT = "data/voice"
CHUNK = 14000
KEEP_ALERTS = 8
UK = ZoneInfo("Europe/London")


def load(path, default):
    try:
        with open(path, encoding="utf-8") as f:
            return json.load(f)
    except (OSError, ValueError):
        return default


def save(path, obj):
    os.makedirs(os.path.dirname(path), exist_ok=True)
    with open(path, "w", encoding="utf-8") as f:
        json.dump(obj, f, ensure_ascii=False, separators=(",", ":"))


def greet(now):
    return "Good morning." if now.hour < 12 else "Good afternoon." if now.hour < 18 else "Good evening."


def spoken(text):
    """Make symbols read naturally."""
    return re.sub(r"\s+", " ", text).replace("Fear & Greed", "Fear and Greed").strip()


def briefing_text(core, now):
    rg = core.get("regime") or {}
    fg = core.get("fear_greed") or {}
    lv = (rg.get("level") or "unknown").replace("-", " ")
    parts = [greet(now) + " Caesar here."]
    s = "The market is in " + lv
    if rg.get("spy_vs_200d_pct") is not None:
        p = rg["spy_vs_200d_pct"]
        s += ": the S&P 500 is %.1f%% %s its 200-day average" % (abs(p), "above" if p >= 0 else "below")
    parts.append(s + ".")
    fgv = fg.get("value", rg.get("fear_greed"))
    if fgv is not None:
        parts.append("Fear and Greed reads %d, %s; the VIX is %.1f." % (round(fgv), (fg.get("label") or rg.get("fear_greed_label") or "").lower(), rg.get("vix") or 0))
    setups = core.get("setups") or []
    if setups:
        parts.append("Research setup%s today: %s. A clue to research, not a trade." % ("" if len(setups) == 1 else "s", ", ".join(setups[:4])))
    else:
        parts.append("No stock meets the research setup today; the rule waits for every condition.")
    nm = core.get("near_misses") or []
    if nm:
        parts.append("%d more %s one condition short." % (len(nm), "is" if len(nm) == 1 else "are"))
    parts.append("That is the picture.")
    return spoken(" ".join(parts))


def alerts_for(core, sec, tracked, state, today):
    """New critical events since the last run, in Caesar's words. Mirrors the terminal's own alert rules."""
    out = []
    rg = core.get("regime") or {}
    lv = rg.get("level")
    if lv and state.get("regime") and lv != state["regime"]:
        why = "; ".join((rg.get("why") or [])[:2])
        out.append(("regime", "Caesar here. The market regime has moved from %s to %s. %s." % (state["regime"].replace("-", " "), lv.replace("-", " "), why)))
    if lv:
        state["regime"] = lv
    seen = state.setdefault("setups", [])
    for sy in core.get("setups") or []:
        if sy in seen:
            continue
        seen.append(sy)
        out.append(("setup", "Caesar here. %s now meets the research setup: a smaller company where two or more officers or directors each bought "
                    "twenty-five thousand dollars or more within thirty days, with cash flow above profit. A clue to research, not a trade." % sy))
    flags = state.setdefault("flags", [])
    for f in (sec or {}).get("red_flags") or []:
        fid = f.get("accession") or ((f.get("ticker") or "") + (f.get("time") or ""))
        if fid in flags:
            continue
        flags.append(fid)
        if f.get("ticker") in tracked:
            means = ", ".join(str(x.get("meaning", "")).lower() for x in (f.get("items") or []) if x.get("item") in (f.get("red_flag") or [])) or f.get("form", "a filing")
            out.append(("sec", "Caesar here. %s has filed a warning sign with the SEC: %s. A warning in a company's own filing outranks any signal." % (f.get("ticker"), means)))
    fg = (core.get("fear_greed") or {}).get("value")
    if fg is not None and (fg < 25 or fg > 75) and state.get("fg_day") != today:
        state["fg_day"] = today
        out.append(("mood", "Caesar here. Fear and Greed reads %d: %s. Moods like this pass; we keep to the rules." % (round(fg), "extreme fear" if fg < 25 else "extreme greed")))
    state["setups"] = seen[-200:]
    state["flags"] = flags[-300:]
    return [(k, spoken(t)) for k, t in out]


_KOKORO = None


def kokoro_tts(text):
    """Free local speech: Kokoro-82M (ONNX) -> WAV -> MP3 with ffmpeg."""
    global _KOKORO
    import subprocess
    import wave
    import numpy as np
    d = os.path.expanduser("~/.cache/kokoro")
    os.makedirs(d, exist_ok=True)
    base = "https://github.com/thewh1teagle/kokoro-onnx/releases/download/model-files-v1.0/"
    for fn in ("kokoro-v1.0.int8.onnx", "voices-v1.0.bin"):
        if not os.path.exists(os.path.join(d, fn)):
            urllib.request.urlretrieve(base + fn, os.path.join(d, fn))
    if _KOKORO is None:
        from kokoro_onnx import Kokoro
        _KOKORO = Kokoro(os.path.join(d, "kokoro-v1.0.int8.onnx"), os.path.join(d, "voices-v1.0.bin"))
    samples, sr = _KOKORO.create(text, voice=KOKORO_VOICE, speed=0.92, lang="en-gb")
    pcm = (np.clip(samples, -1, 1) * 32767).astype("<i2")
    wav = os.path.join(d, "out.wav")
    with wave.open(wav, "wb") as w:
        w.setnchannels(1)
        w.setsampwidth(2)
        w.setframerate(sr)
        w.writeframes(pcm.tobytes())
    return subprocess.run(["ffmpeg", "-loglevel", "error", "-y", "-i", wav, "-ac", "1", "-ar", "24000", "-b:a", "32k", "-f", "mp3", "-"], check=True, capture_output=True).stdout


def tts(text):
    if ENGINE != "elevenlabs":
        return kokoro_tts(text)
    req = urllib.request.Request(
        "https://api.elevenlabs.io/v1/text-to-speech/%s?output_format=mp3_22050_32" % VOICE_ID,
        data=json.dumps({"text": text, "model_id": MODEL, "voice_settings": {"stability": 0.55, "similarity_boost": 0.8, "style": 0.15, "use_speaker_boost": True}}).encode(),
        headers={"xi-api-key": KEY, "Content-Type": "application/json", "Accept": "audio/mpeg"},
        method="POST",
    )
    with urllib.request.urlopen(req, timeout=60) as r:
        return r.read()


def store(clip_id, audio):
    names = []
    for i in range(0, len(audio), CHUNK):
        name = "%s-%d.bin" % (clip_id, i // CHUNK)
        with open(os.path.join(OUT, name), "wb") as f:
            f.write(audio[i:i + CHUNK])
        names.append(name)
    return names


def main():
    now = dt.datetime.now(UK)
    today = now.date().isoformat()
    month = now.strftime("%Y-%m")
    core = load("data/brain/core.json", None)
    if not core:
        print("voice: no brain yet")
        return 0
    sec = load("data/brain/sec.json", {})
    idx = load("data/brain/index.json", {})
    tracked = set()
    for i in range(int(idx.get("list_parts") or 0)):
        for row in load("data/brain/list_%d.json" % i, []):
            if row:
                tracked.add(row[0])
    os.makedirs(OUT, exist_ok=True)
    state = load(OUT + "/state.json", {})
    index = load(OUT + "/index.json", {"alerts": []})
    if state.get("month") != month:
        state["month"], state["chars"] = month, 0
    first = not state.get("init")
    pending = alerts_for(core, sec, tracked, state, today)
    if first:
        pending = []  # the first run only learns what is already true; it does not announce the backlog
        state["init"] = True
    due = None
    if not index.get("briefing"):
        due = "first"
    elif now.weekday() < 5:
        for s in SLOTS:
            hh, mm = (int(x) for x in s.split(":"))
            if (now.hour, now.minute) >= (hh, mm) and (today + " " + s) not in state.get("slots_done", []):
                due = s
    index.update({"voice": VOICE_NAME, "voice_id": VOICE_ID if ENGINE == "elevenlabs" else KOKORO_VOICE, "model": MODEL if ENGINE == "elevenlabs" else "kokoro-82m", "brain_updated": core.get("updated")})
    error = None
    if ENGINE == "elevenlabs" and not KEY:
        error = "No ELEVENLABS_API_KEY secret yet, so nothing is recorded."
    else:
        jobs = [("alert", k, t) for k, t in pending]
        if due:
            jobs.append(("brief", due, briefing_text(core, now)))
        for kind, sub, text in jobs:
            if state["chars"] + len(text) > CAP:
                error = "Monthly budget of %d characters reached; recording resumes next month." % CAP
                break
            try:
                audio = tts(text)
            except urllib.error.HTTPError as e:
                error = "ElevenLabs answered %d: %s" % (e.code, e.read()[:200].decode("utf-8", "replace"))
                break
            except Exception as e:  # network trouble: try again next run
                error = "ElevenLabs could not be reached: %s" % e
                break
            state["chars"] += len(text)
            cid = "%s-%s-%s" % (kind, now.strftime("%Y%m%d%H%M"), hashlib.sha1(text.encode()).hexdigest()[:6])
            clip = {"id": cid, "kind": sub, "text": text, "at": now.astimezone(dt.timezone.utc).isoformat(timespec="seconds").replace("+00:00", "Z"), "chunks": store(cid, audio), "bytes": len(audio)}
            if kind == "brief":
                index["briefing"] = clip
                if due != "first":
                    state.setdefault("slots_done", []).append(today + " " + due)
                    state["slots_done"] = state["slots_done"][-30:]
            else:
                index["alerts"] = ([clip] + (index.get("alerts") or []))[:KEEP_ALERTS]
            print("voice: recorded", cid, len(text), "chars,", len(audio), "bytes")
    index["budget"] = {"month": month, "used": state.get("chars", 0), "cap": CAP}
    index["error"] = error
    index["updated"] = now.astimezone(dt.timezone.utc).isoformat(timespec="seconds").replace("+00:00", "Z")
    # remove chunk files no clip points at any more
    keep = set(c for clip in [index.get("briefing")] + (index.get("alerts") or []) if clip for c in clip["chunks"])
    for name in os.listdir(OUT):
        if name.endswith(".bin") and name not in keep:
            os.remove(os.path.join(OUT, name))
    old = load(OUT + "/index.json", {})
    save(OUT + "/state.json", state)
    if {k: v for k, v in old.items() if k != "updated"} != {k: v for k, v in index.items() if k != "updated"}:
        save(OUT + "/index.json", index)
    if error:
        print("voice:", error)
    return 0


if __name__ == "__main__":
    sys.exit(main())
