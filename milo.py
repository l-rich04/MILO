"""
MILO - push-to-talk voice assistant
  Hold F9 -> speak -> release -> MILO transcribes, thinks, acts, and talks back.
  Press F9 while MILO is talking to interrupt.
  Run with --text to type instead of talk (good for debugging tools).

Stack: faster-whisper (STT) -> local LLM via Ollama w/ tools (brain) -> edge-tts + pygame (TTS)
"""

import spotipy
from spotipy.oauth2 import SpotifyOAuth
import asyncio
import datetime
import os
import platform
import re
import sqlite3
import subprocess
import sys
import tempfile
import threading
import time
import unicodedata
import math
import ollama
import pymupdf
import requests
from milo_logger import log_interaction
from simpleeval import simple_eval

# ─────────────────────────── Config ───────────────────────────
MODEL = os.getenv("MILO_MODEL", "gemma4:e4b")              # any Ollama model with tool support
WHISPER_MODEL = os.getenv("MILO_WHISPER", "small.en")   # tiny.en / base.en / small.en / medium.en
VOICE = os.getenv("MILO_VOICE", "en-GB-RyanNeural")      # `edge-tts --list-voices` for more
USER_NAME = os.getenv("MILO_USER", "Layne")
SAMPLE_RATE = 16000
MAX_HISTORY = 30          # messages kept in context
MAX_TOOL_ROUNDS = 5       # stop if the model keeps calling tools without answering
CONTEXT_SIZE = 8192       # tokens the model can "see" at once
TEXTBOOK_PATH = os.path.join(os.path.dirname(os.path.abspath(__file__)), "textbooks", "electromagnetics.pdf")
PAGE_OFFSET = 13          # PDF index = book page + 13 (14 front pages, minus 1 for counting from 0)
DIRECT_REPLY_TOOLS = {"read_textbook_page"}   # tools whose output IS the reply
DB_PATH = os.path.join(os.path.dirname(os.path.abspath(__file__)), "milo.db")
IS_WINDOWS = platform.system() == "Windows"

SYSTEM_PROMPT = f"""You are MILO, a voice assistant running on {USER_NAME}'s computer ({platform.system()}).
Your replies are spoken aloud, so:
- Keep answers short: 1-3 sentences unless asked for detail.
- No markdown, bullet points, or code blocks in speech. If you must show code or long output, say it's printed in the console.
- Be direct, friendly, and a little witty, but never at the expense of being useful.
Use tools when they help. For shell commands prefer simple, safe, read-only commands unless the user clearly asks for a change.
Current date/time is available via the get_datetime tool."""


# ─────────────────────────── Memory (SQLite) ───────────────────────────
def db():
    conn = sqlite3.connect(DB_PATH)
    conn.execute("""CREATE TABLE IF NOT EXISTS notes (
        id INTEGER PRIMARY KEY AUTOINCREMENT,
        created TEXT NOT NULL,
        text TEXT NOT NULL)""")
    return conn

# ─────────────────────────── Tools ───────────────────────────
_spotify = None

def spotify():
    """Connect to Spotify the first time it's needed, then reuse the connection."""
    global _spotify
    if _spotify is None:
        _spotify = spotipy.Spotify(auth_manager=SpotifyOAuth(
            scope="user-read-playback-state user-modify-playback-state",
            cache_path=os.path.join(os.path.dirname(os.path.abspath(__file__)), ".cache"),
        ))
    return _spotify

def pick_device(sp):
    """Use the device that's already playing, or any open Spotify device."""
    devices = sp.devices()["devices"]
    if not devices:
        return None
    active = [d for d in devices if d["is_active"]]
    return (active or devices)[0]["id"]

def tool_play_music(query: str, kind: str = "track") -> str:
    sp = spotify()
    device = pick_device(sp)
    if device is None:
        return "Spotify isn't open on any device. Open the Spotify app first."
    if kind not in ("track", "artist", "album", "playlist"):
        kind = "track"
    results = sp.search(q=query, type=kind, limit=1)
    items = [i for i in results[kind + "s"]["items"] if i]
    if not items:
        return f"Couldn't find a {kind} matching '{query}'."
    item = items[0]
    if kind == "track":
        sp.start_playback(device_id=device, uris=[item["uri"]])
        return f"Playing {item['name']} by {item['artists'][0]['name']}."
    sp.start_playback(device_id=device, context_uri=item["uri"])
    return f"Playing the {kind} {item['name']}."

def tool_spotify_control(action: str) -> str:
    sp = spotify()
    action = action.lower().strip()
    if action == "now_playing":
        current = sp.current_playback()
        if current and current.get("item"):
            song = current["item"]
            return f"Now playing {song['name']} by {song['artists'][0]['name']}."
        return "Nothing is playing right now."
    device = pick_device(sp)
    if device is None:
        return "Spotify isn't open on any device. Open the Spotify app first."
    if action == "pause":
        sp.pause_playback(device_id=device)
    elif action == "resume":
        sp.start_playback(device_id=device)
    elif action == "next":
        sp.next_track(device_id=device)
    elif action == "previous":
        sp.previous_track(device_id=device)
    else:
        return f"Unknown Spotify action: {action}"
    return f"Done: {action}."

def confirm(action: str) -> bool:
    print(f"\n⚠️  MILO wants to: {action}")
    return input("   Allow? [y/N]: ").strip().lower() == "y"

def tool_run_shell_command(command: str) -> str:
    if not confirm(f"run `{command}`"):
        return "User denied permission to run this command."
    try:
        r = subprocess.run(command, shell=True, capture_output=True, text=True, timeout=60)
        out = (r.stdout + r.stderr).strip() or "(no output)"
        return out[:4000]
    except subprocess.TimeoutExpired:
        return "Command timed out after 60 seconds."

APP_ALIASES = {"vs code": "code", "vscode": "code", "terminal": "wt" if IS_WINDOWS else "gnome-terminal",
               "file explorer": "explorer", "explorer": "explorer", "notepad": "notepad",
               "chrome": "chrome", "spotify": "spotify:", "excel": "excel", "word": "winword"}

def tool_open_app(target: str) -> str:
    t = APP_ALIASES.get(target.lower().strip(), target)
    try:
        if IS_WINDOWS:
            subprocess.Popen(f'start "" "{t}"', shell=True)
        elif platform.system() == "Darwin":
            subprocess.Popen(["open", "-a", t] if not t.startswith(("http", "/")) else ["open", t])
        else:
            subprocess.Popen(["xdg-open", t])
        return f"Opened {t}."
    except Exception as e:
        return f"Failed to open {t}: {e}"

def tool_read_file(path: str) -> str:
    path = os.path.expanduser(path)
    if not os.path.isfile(path):
        return f"No file at {path}"
    with open(path, "r", encoding="utf-8", errors="replace") as f:
        data = f.read(20000)
    return data + ("\n...(truncated)" if os.path.getsize(path) > 20000 else "")

def tool_write_file(path: str, content: str) -> str:
    path = os.path.expanduser(path)
    if not confirm(f"write {len(content)} chars to {path}"):
        return "User denied permission to write this file."
    os.makedirs(os.path.dirname(os.path.abspath(path)), exist_ok=True)
    with open(path, "w", encoding="utf-8") as f:
        f.write(content)
    return f"Wrote {path}"

WEATHER_CODES = {
    0: "clear skies", 1: "mostly clear", 2: "partly cloudy", 3: "overcast",
    45: "foggy", 48: "foggy",
    51: "light drizzle", 53: "drizzle", 55: "heavy drizzle",
    61: "light rain", 63: "rain", 65: "heavy rain", 66: "freezing rain", 67: "freezing rain",
    71: "light snow", 73: "snow", 75: "heavy snow", 77: "snow grains",
    80: "light showers", 81: "showers", 82: "heavy showers",
    85: "snow showers", 86: "heavy snow showers",
    95: "thunderstorms", 96: "thunderstorms with hail", 99: "thunderstorms with hail",
}
HOME = ("Johnstown, PA", 40.33, -78.92)


def get_current_location():
    """Guess the computer's location from its internet connection.
    Falls back to HOME if the lookup fails."""
    try:
        info = requests.get("https://ipinfo.io/json", timeout=5).json()
        lat, lon = map(float, info["loc"].split(","))
        return f"{info['city']}, {info['region']}", lat, lon
    except Exception:
        return HOME
    
def tool_get_weather(city: str = "") -> str:
    try:
        if city.strip():
            geo = requests.get("https://geocoding-api.open-meteo.com/v1/search",
                               params={"name": city.split(",")[0].strip(), "count": 1},
                               timeout=10).json()
            if not geo.get("results"):
                return f"Couldn't find a place called {city}."
            place = geo["results"][0]
            name = f"{place['name']}, {place.get('admin1', place.get('country', ''))}"
            lat, lon = place["latitude"], place["longitude"]
        else:
            name, lat, lon = get_current_location()

        data = requests.get("https://api.open-meteo.com/v1/forecast", params={
            "latitude": lat, "longitude": lon,
            "current": "temperature_2m,weather_code",
            "daily": "temperature_2m_max,temperature_2m_min,precipitation_probability_max",
            "temperature_unit": "fahrenheit",
            "timezone": "auto", "forecast_days": 1,
        }, timeout=10).json()

        now = data["current"]
        today = data["daily"]
        sky = WEATHER_CODES.get(now["weather_code"], "unknown conditions")
        return (f"Weather in {name}: {sky}, {now['temperature_2m']:.0f}°F right now. "
                f"Today's high is {today['temperature_2m_max'][0]:.0f}°F, the low is "
                f"{today['temperature_2m_min'][0]:.0f}°F, with a "
                f"{today['precipitation_probability_max'][0]}% chance of precipitation.")
    except requests.RequestException as e:
        return f"Couldn't reach the weather service: {e}"

def tool_add_note(text: str) -> str:
    with db() as conn:
        conn.execute("INSERT INTO notes (created, text) VALUES (?, ?)",
                     (datetime.datetime.now().isoformat(timespec="minutes"), text))
    return "Note saved."

def tool_calculate(expression: str) -> str:
    functions = {"sqrt": math.sqrt, "sin": math.sin, "cos": math.cos,
                 "tan": math.tan, "log": math.log10, "ln": math.log}
    names = {"pi": math.pi, "e": math.e}
    try:
        result = simple_eval(expression, functions=functions, names=names)
        return f"{expression} = {result:g}"
    except Exception as e:
        return f"Couldn't calculate that: {e}"

def tool_set_timer(minutes: int) -> str:
    minutes = float(minutes)
    if minutes <= 0:
        return "Timer must be longer than 0 minutes."
    def timer_thread():
        time.sleep(minutes * 60)
        print(f"\n⏰ Timer for {minutes:g} minutes is up!")
        try:
            speak(f"Timer for {minutes:g} minutes is up!")
        except Exception as e:
            print(f"   (TTS failed: {e})")
    threading.Thread(target=timer_thread, daemon=True).start()
    return f"Timer set for {minutes:g} minutes."

def tool_get_notes(query: str = "") -> str:
    with db() as conn:
        rows = conn.execute("SELECT id, created, text FROM notes WHERE text LIKE ? ORDER BY id DESC LIMIT 20",
                            (f"%{query}%",)).fetchall()
    return "\n".join(f"[{r[0]}] {r[1]}: {r[2]}" for r in rows) or "No notes found."

def tool_delete_note(note_id: int) -> str:
    note_id = int(note_id)   # small models sometimes send numbers as text
    with db() as conn:
        n = conn.execute("DELETE FROM notes WHERE id = ?", (note_id,)).rowcount
    return "Deleted." if n else f"No note with id {note_id}."

def tool_get_datetime() -> str:
    return datetime.datetime.now().strftime("%A, %B %d, %Y, %I:%M %p")

CLEANUP_PROMPT = """You turn textbook pages into text that can be read aloud. Rules:
- Keep every normal sentence word for word. Do not summarize, explain, or add anything.
- Say equations in words, for example "z one equals x one plus j y one". Mention equation numbers, like "equation 1.44".
- Skip page headers, page numbers, and scattered figure labels. For each figure, just say its name and caption, like "Figure 1-18: relation between rectangular and polar representations of a complex number."
- Output only the text to be read aloud."""

def tool_read_textbook_page(page: int) -> str:
    page = int(page)
    with pymupdf.open(TEXTBOOK_PATH) as doc:
        index = page + PAGE_OFFSET
        if not 0 <= index < doc.page_count:
            return f"Page {page} isn't in the textbook."
        raw = doc[index].get_text()
        raw = unicodedata.normalize("NFKC", raw)   # turn special characters like "ﬁ" into plain "fi"

    print(f"\n----- Original text of page {page} -----\n{raw}\n----- end of page {page} -----\n")

    response = ollama.chat(
        model=MODEL,
        messages=[{"role": "system", "content": CLEANUP_PROMPT},
                  {"role": "user", "content": raw}],
        think=False,
        keep_alive="30m",
        options={"num_ctx": CONTEXT_SIZE},
    )
    return f"Page {page}. " + (response.message.content or "").strip()

TOOL_FUNCS = {
    "run_shell_command": tool_run_shell_command,
    "open_app": tool_open_app,
    "read_file": tool_read_file,
    "write_file": tool_write_file,
    "add_note": tool_add_note,
    "get_notes": tool_get_notes,
    "delete_note": tool_delete_note,
    "get_datetime": tool_get_datetime,
    "set_timer": tool_set_timer,
    "calculate": tool_calculate,
    "get_weather": tool_get_weather,
    "read_textbook_page": tool_read_textbook_page,
    "play_music": tool_play_music,
    "spotify_control": tool_spotify_control,
    
}

def schema(name, desc, props=None, required=None):
    """Build one tool description in the format Ollama expects."""
    return {
        "type": "function",
        "function": {
            "name": name,
            "description": desc,
            "parameters": {"type": "object", "properties": props or {}, "required": required or []},
        },
    }

TOOLS = [
    schema("run_shell_command", "Run a command in the system shell (cmd.exe on Windows) and return its output. "
           "The user must approve each command.", {"command": {"type": "string"}}, ["command"]),
    schema("open_app", "Open an application, file, folder, or URL. Accepts common names like 'vs code', "
           "'chrome', 'spotify', or a full path/URL.", {"target": {"type": "string"}}, ["target"]),
    schema("read_file", "Read a text file from disk.", {"path": {"type": "string"}}, ["path"]),
    schema("write_file", "Write text to a file (overwrites). The user must approve.",
           {"path": {"type": "string"}, "content": {"type": "string"}}, ["path", "content"]),
    schema("add_note", "Save a note or reminder to persistent memory.", {"text": {"type": "string"}}, ["text"]),
    schema("get_notes", "Search saved notes. Empty query returns the most recent.",
           {"query": {"type": "string"}}),
    schema("delete_note", "Delete a saved note by id.", {"note_id": {"type": "integer"}}, ["note_id"]),
    schema("get_datetime", "Get the current local date and time."),
    schema("set_timer", "Set a timer for a specified number of minutes.", {"minutes": {"type": "number"}}, ["minutes"]),
    schema("calculate", "Evaluate a mathematical expression.", {"expression": {"type": "string"}}, ["expression"]),
    schema("get_weather", "Get the current weather and today's forecast for a city. Leave the city empty for home.",
           {"city": {"type": "string"}}),
    schema("read_textbook_page", "Read a page of the user's Electromagnetics textbook aloud. "
           "Use the page number printed in the book, exactly as the user says it.",
           {"page": {"type": "integer"}}, ["page"]),
    schema("play_music", "Search Spotify and play the best matching track, artist, album, or playlist.",
           {"query": {"type": "string"}, "kind": {"type": "string"}}, ["query"]),
    schema("spotify_control", "Control Spotify playback. Actions: now_playing, pause, resume, next, previous.",
           {"action": {"type": "string"}}, ["action"]),
]

# ─────────────────────────── Brain ───────────────────────────
history = []

def role_of(m):
    """Messages can be plain dicts (ours) or Message objects (from Ollama)."""
    return m["role"] if isinstance(m, dict) else m.role

def trim_history():
    """Keep history bounded, cutting only at one of the user's own messages
    so a tool request is never separated from its result."""
    global history
    if len(history) <= MAX_HISTORY:
        return
    for i in range(len(history) - MAX_HISTORY, len(history)):
        if role_of(history[i]) == "user":
            history = history[i:]
            return

def think(user_text: str):
    """Returns two things: MILO's reply, and a list of the tools it used."""
    history.append({"role": "user", "content": user_text})
    trim_history()
    tools_used = []

    for _ in range(MAX_TOOL_ROUNDS):
        response = ollama.chat(
            model=MODEL,
            messages=[{"role": "system", "content": SYSTEM_PROMPT}] + history,
            tools=TOOLS,
            think=False,                       # skip "thinking mode" for faster replies
            keep_alive="30m",                  # keep the model loaded on the GPU
            options={"num_ctx": CONTEXT_SIZE},
        )
        message = response.message
        history.append(message)

        if not message.tool_calls:             # no tools requested: this is the answer
            return (message.content or "").strip(), tools_used

        for call in message.tool_calls:
            name = call.function.name
            tools_used.append(name)
            args = call.function.arguments or {}
            print(f"   🔧 {name}({args})")
            fn = TOOL_FUNCS.get(name)
            try:
                out = fn(**args) if fn else f"Unknown tool {name}"
            except Exception as e:
                out = f"Tool error: {e}"
            if name in DIRECT_REPLY_TOOLS:
                history.append({"role": "tool", "content": "(The result was given to the user directly.)", "tool_name": name})
                history.append({"role": "assistant", "content": "I read that to you."})
                return str(out), tools_used
            history.append({"role": "tool", "content": str(out), "tool_name": name})

    return "Sorry, I got stuck trying to use my tools. Could you rephrase that?", tools_used

# ─────────────────────────── Voice out (TTS) ───────────────────────────
_speaking = threading.Event()

def clean_for_speech(text: str) -> str:
    text = re.sub(r"```.*?```", " I've put the code in the console. ", text, flags=re.S)
    text = re.sub(r"[*_#`>]", "", text)
    text = re.sub(r"\[(.*?)\]\(.*?\)", r"\1", text)   # markdown links -> just the label
    return text.strip()

def speak(text: str):
    import edge_tts
    import pygame
    spoken = clean_for_speech(text)
    if not spoken:
        return
    path = os.path.join(tempfile.gettempdir(), f"milo_{int(time.time()*1000)}.mp3")
    asyncio.run(edge_tts.Communicate(spoken, VOICE).save(path))
    if not pygame.mixer.get_init():
        pygame.mixer.init()
    pygame.mixer.music.load(path)
    pygame.mixer.music.play()
    _speaking.set()
    while pygame.mixer.music.get_busy() and _speaking.is_set():
        time.sleep(0.05)
    pygame.mixer.music.stop()
    pygame.mixer.music.unload()
    _speaking.clear()
    try:
        os.remove(path)
    except OSError:
        pass

# ─────────────────────────── Voice in (push-to-talk + STT) ───────────────────────────
def load_whisper():
    from faster_whisper import WhisperModel
    device = os.getenv("MILO_WHISPER_DEVICE", "cpu")   # set to "cuda" once NVIDIA's libraries are installed
    compute = "float16" if device == "cuda" else "int8"
    print(f"Loading Whisper '{WHISPER_MODEL}' on {device}...")
    
    return WhisperModel(WHISPER_MODEL, device=device, compute_type=compute)

WAKE_ENABLED = os.getenv("MILO_WAKE", "on") == "on"    # set to "off" for push-to-talk only
WAKE_PATTERN = re.compile(r"^\s*(hey[\s,]+)?(milo|mylo|my\s?lo|mi\s?lo|mila)\b[\s,.!?]*", re.IGNORECASE)
BLOCK_SECONDS = 0.1        # the mic delivers audio in 0.1-second blocks
SILENCE_SECONDS = 0.8      # this much quiet means you stopped talking
MAX_PHRASE_SECONDS = 10    # longer than this is probably music or TV, so ignore it
FOLLOW_UP_SECONDS = 8      # after "MILO?" alone, how long to wait for the command

def voice_loop():
    import collections
    import queue
    import numpy as np
    import sounddevice as sd
    from pynput import keyboard

    whisper = load_whisper()
    block_size = int(SAMPLE_RATE * BLOCK_SECONDS)
    jobs = queue.Queue()               # recordings waiting to be transcribed: (audio, source)
    recording = threading.Event()      # True while F9 is held
    ptt_frames = []
    wake = {"phrase": [], "quiet_blocks": 0, "talking": False,
            "preroll": collections.deque(maxlen=3), "noise": [], "threshold": None}

    def reset_wake():
        wake["phrase"], wake["quiet_blocks"], wake["talking"] = [], 0, False

    def audio_cb(indata, n, t, status):
        block = indata[:, 0].copy()
        if recording.is_set():                         # push-to-talk takes priority
            ptt_frames.append(block)
            return
        if not WAKE_ENABLED or _speaking.is_set():     # don't listen to MILO's own voice
            reset_wake()
            return

        level = float(np.sqrt(np.mean(block ** 2)))    # loudness of this block (RMS)
        if wake["threshold"] is None:                  # first second: measure room noise
            wake["noise"].append(level)
            if len(wake["noise"]) >= 10:
                wake["threshold"] = max(0.01, 3 * float(np.mean(wake["noise"])))
            return

        if level > wake["threshold"]:
            if not wake["talking"]:                    # speech just started:
                wake["phrase"] = list(wake["preroll"]) # keep the moment before it, too
                wake["talking"] = True
            wake["phrase"].append(block)
            wake["quiet_blocks"] = 0
        elif wake["talking"]:
            wake["phrase"].append(block)
            wake["quiet_blocks"] += 1
            if wake["quiet_blocks"] * BLOCK_SECONDS >= SILENCE_SECONDS:   # speech ended
                audio = np.concatenate(wake["phrase"])
                if 0.5 <= len(audio) / SAMPLE_RATE <= MAX_PHRASE_SECONDS:
                    jobs.put((audio, "wake"))
                reset_wake()
        else:
            wake["preroll"].append(block)

        if len(wake["phrase"]) * BLOCK_SECONDS > MAX_PHRASE_SECONDS:     # never-ending sound
            reset_wake()

    def on_press(key):
        if key == keyboard.Key.f9:
            if _speaking.is_set():          # interrupt MILO mid-sentence
                _speaking.clear()
                return
            if not recording.is_set():
                ptt_frames.clear()
                reset_wake()
                recording.set()
                print("🎙️  Listening...", end="", flush=True)

    def on_release(key):
        if key == keyboard.Key.f9 and recording.is_set():
            recording.clear()
            if ptt_frames:
                jobs.put((np.concatenate(ptt_frames), "ptt"))

    stream = sd.InputStream(samplerate=SAMPLE_RATE, channels=1, dtype="float32",
                            blocksize=block_size, callback=audio_cb)
    stream.start()
    keyboard.Listener(on_press=on_press, on_release=on_release, daemon=True).start()
    how = "Hold F9 or say \"MILO\" to talk" if WAKE_ENABLED else "Hold F9 to talk"
    print(f"\nMILO online. {how}. Ctrl+C to quit.\n")
    speak(f"Online and ready, {USER_NAME}.")

    follow_up_until = 0
    while True:
        audio, source = jobs.get()
        if source == "ptt" and len(audio) < SAMPLE_RATE * 0.4:     # ignore accidental taps
            print(" (too short)")
            continue

        segments, _ = whisper.transcribe(audio, language="en", vad_filter=True, beam_size=5,
                                         initial_prompt="Hey MILO.")
        text = " ".join(s.text for s in segments).strip()
        if not text:
            continue

        if source == "ptt":
            print(f"\rYou: {text}           ")
            handle(text)
            continue

        # Heard through the always-on mic: only act if it's meant for MILO.
        match = WAKE_PATTERN.match(text)
        if match:
            command = text[match.end():].strip()
        elif time.time() < follow_up_until:
            command = text                 # answering MILO's "Yes?"
        else:
            continue                       # someone talking, but not to MILO

        print(f"You: {text}")
        if not command:                    # just "MILO" on its own
            follow_up_until = time.time() + FOLLOW_UP_SECONDS
            speak("Yes?")
            continue
        follow_up_until = 0
        handle(command)

def handle(text: str, speak_reply: bool = True):
    start = time.time()
    try:
        reply, tools_used = think(text)
    except Exception as e:
        reply = f"I couldn't reach my brain. Is Ollama running? ({e})"
        tools_used = []
    response_time = round(time.time() - start, 2)

    # Log every interaction before speaking, so a speech error can't lose it.
    # Category and whether it worked are unknown for now; MILO will learn to fill these in.
    tools_text = ", ".join(tools_used) if tools_used else "none"
    log_interaction(text, tools_text, reply, response_time, "unknown", "unknown")

    print(f"MILO: {reply}\n")
    if speak_reply:
        try:
            speak(reply)
        except Exception as e:
            print(f"   (TTS failed: {e})")

def text_loop():
    print("MILO text mode. Type 'quit' to exit.\n")
    while True:
        text = input("You: ").strip()
        if text.lower() in ("quit", "exit"):
            break
        if text:
            handle(text, speak_reply="--speak" in sys.argv)

if __name__ == "__main__":
    try:
        ollama.show(MODEL)                     # check Ollama is running and the model is downloaded
    except Exception:
        sys.exit(f"Can't use '{MODEL}'. Make sure Ollama is running, then try: ollama pull {MODEL}")
    try:
        text_loop() if "--text" in sys.argv else voice_loop()
    except KeyboardInterrupt:
        print("\nMILO offline.")