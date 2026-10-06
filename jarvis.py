"""
Jarvis - push-to-talk voice assistant
  Hold F9 -> speak -> release -> Jarvis transcribes, thinks, acts, and talks back.
  Press F9 while Jarvis is talking to interrupt.
  Run with --text to type instead of talk (good for debugging tools).

Stack: faster-whisper (STT) -> Claude w/ tools (brain) -> edge-tts + pygame (TTS)
"""

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

import anthropic

# ─────────────────────────── Config ───────────────────────────
MODEL = os.getenv("JARVIS_MODEL", "claude-sonnet-5-5")
WHISPER_MODEL = os.getenv("JARVIS_WHISPER", "small.en")   # tiny.en / base.en / small.en / medium.en
VOICE = os.getenv("JARVIS_VOICE", "en-GB-RyanNeural")      # `edge-tts --list-voices` for more
USER_NAME = os.getenv("JARVIS_USER", "Layne")
SAMPLE_RATE = 16000
MAX_HISTORY = 30          # messages kept in context
DB_PATH = os.path.join(os.path.dirname(os.path.abspath(__file__)), "jarvis.db")
IS_WINDOWS = platform.system() == "Windows"

SYSTEM_PROMPT = f"""You are Jarvis, a voice assistant running on {USER_NAME}'s computer ({platform.system()}).
Your replies are spoken aloud, so:
- Keep answers short: 1-3 sentences unless asked for detail.
- No markdown, bullet points, or code blocks in speech. If you must show code or long output, say it's printed in the console.
- Be direct and a little dry-witted, like the movie Jarvis, but never at the expense of being useful.
Use tools when they help. For shell commands prefer simple, safe, read-only commands unless the user clearly asks for a change.
Current date/time is available via the get_datetime tool."""

client = anthropic.Anthropic()  # reads ANTHROPIC_API_KEY from the environment

# ─────────────────────────── Memory (SQLite) ───────────────────────────
def db():
    conn = sqlite3.connect(DB_PATH)
    conn.execute("""CREATE TABLE IF NOT EXISTS notes (
        id INTEGER PRIMARY KEY AUTOINCREMENT,
        created TEXT NOT NULL,
        text TEXT NOT NULL)""")
    return conn

# ─────────────────────────── Tools ───────────────────────────
def confirm(action: str) -> bool:
    print(f"\n⚠️  Jarvis wants to: {action}")
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

def tool_add_note(text: str) -> str:
    with db() as conn:
        conn.execute("INSERT INTO notes (created, text) VALUES (?, ?)",
                     (datetime.datetime.now().isoformat(timespec="minutes"), text))
    return "Note saved."

def tool_get_notes(query: str = "") -> str:
    with db() as conn:
        rows = conn.execute("SELECT id, created, text FROM notes WHERE text LIKE ? ORDER BY id DESC LIMIT 20",
                            (f"%{query}%",)).fetchall()
    return "\n".join(f"[{r[0]}] {r[1]}: {r[2]}" for r in rows) or "No notes found."

def tool_delete_note(note_id: int) -> str:
    with db() as conn:
        n = conn.execute("DELETE FROM notes WHERE id = ?", (note_id,)).rowcount
    return "Deleted." if n else f"No note with id {note_id}."

def tool_get_datetime() -> str:
    return datetime.datetime.now().strftime("%A, %B %d, %Y, %I:%M %p")

TOOL_FUNCS = {
    "run_shell_command": tool_run_shell_command,
    "open_app": tool_open_app,
    "read_file": tool_read_file,
    "write_file": tool_write_file,
    "add_note": tool_add_note,
    "get_notes": tool_get_notes,
    "delete_note": tool_delete_note,
    "get_datetime": tool_get_datetime,
}

def schema(name, desc, props=None, required=None):
    return {"name": name, "description": desc,
            "input_schema": {"type": "object", "properties": props or {}, "required": required or []}}

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
    # Server-side tool: Anthropic runs the search, no local code needed
    {"type": "web_search_20250305", "name": "web_search", "max_uses": 3},
]

# ─────────────────────────── Brain ───────────────────────────
history = []

def trim_history():
    """Keep history bounded without splitting a tool_use / tool_result pair."""
    global history
    if len(history) <= MAX_HISTORY:
        return
    for i in range(len(history) - MAX_HISTORY, len(history)):
        m = history[i]
        if m["role"] == "user" and isinstance(m["content"], str):
            history = history[i:]
            return

def think(user_text: str) -> str:
    history.append({"role": "user", "content": user_text})
    trim_history()
    while True:
        resp = client.messages.create(model=MODEL, max_tokens=1024, system=SYSTEM_PROMPT,
                                      tools=TOOLS, messages=history)
        history.append({"role": "assistant", "content": resp.content})

        if resp.stop_reason == "pause_turn":      # long server-side tool turn; let it continue
            continue
        if resp.stop_reason != "tool_use":
            return " ".join(b.text for b in resp.content if b.type == "text").strip()

        results = []
        for block in resp.content:
            if block.type == "tool_use":
                print(f"   🔧 {block.name}({block.input})")
                fn = TOOL_FUNCS.get(block.name)
                try:
                    out = fn(**block.input) if fn else f"Unknown tool {block.name}"
                except Exception as e:
                    out = f"Tool error: {e}"
                results.append({"type": "tool_result", "tool_use_id": block.id, "content": str(out)})
        history.append({"role": "user", "content": results})

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
    path = os.path.join(tempfile.gettempdir(), f"jarvis_{int(time.time()*1000)}.mp3")
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
    try:
        import ctranslate2
        device = "cuda" if ctranslate2.get_cuda_device_count() > 0 else "cpu"
    except Exception:
        device = "cpu"
    compute = "float16" if device == "cuda" else "int8"
    print(f"Loading Whisper '{WHISPER_MODEL}' on {device}...")
    return WhisperModel(WHISPER_MODEL, device=device, compute_type=compute)

def voice_loop():
    import numpy as np
    import sounddevice as sd
    from pynput import keyboard

    whisper = load_whisper()
    frames, recording, done = [], threading.Event(), threading.Event()

    def audio_cb(indata, n, t, status):
        if recording.is_set():
            frames.append(indata.copy())

    def on_press(key):
        if key == keyboard.Key.f9:
            if _speaking.is_set():          # interrupt Jarvis mid-sentence
                _speaking.clear()
                return
            if not recording.is_set():
                frames.clear()
                recording.set()
                print("🎙️  Listening...", end="", flush=True)

    def on_release(key):
        if key == keyboard.Key.f9 and recording.is_set():
            recording.clear()
            done.set()

    stream = sd.InputStream(samplerate=SAMPLE_RATE, channels=1, dtype="float32", callback=audio_cb)
    stream.start()
    keyboard.Listener(on_press=on_press, on_release=on_release, daemon=True).start()
    print(f"\nJarvis online. Hold F9 to talk, Ctrl+C to quit.\n")
    speak(f"Online and ready, {USER_NAME}.")

    while True:
        done.wait()
        done.clear()
        if not frames:
            continue
        audio = np.concatenate(frames).flatten()
        if len(audio) < SAMPLE_RATE * 0.4:     # ignore accidental taps
            print(" (too short)")
            continue
        segments, _ = whisper.transcribe(audio, language="en", vad_filter=True, beam_size=5)
        text = " ".join(s.text for s in segments).strip()
        print(f"\rYou: {text}           ")
        if not text:
            continue
        handle(text)

def handle(text: str):
    try:
        reply = think(text)
    except anthropic.APIError as e:
        reply = f"I hit an API error: {e}"
    print(f"Jarvis: {reply}\n")
    try:
        speak(reply)
    except Exception as e:
        print(f"   (TTS failed: {e})")

def text_loop():
    print("Jarvis text mode. Type 'quit' to exit.\n")
    while True:
        text = input("You: ").strip()
        if text.lower() in ("quit", "exit"):
            break
        if text:
            handle(text) if "--speak" in sys.argv else print(f"Jarvis: {think(text)}\n")

if __name__ == "__main__":
    if not os.getenv("ANTHROPIC_API_KEY"):
        sys.exit("Set ANTHROPIC_API_KEY first.")
    try:
        text_loop() if "--text" in sys.argv else voice_loop()
    except KeyboardInterrupt:
        print("\nJarvis offline.")
