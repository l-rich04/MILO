# MILO Dev Log

A running journal of what I built, what broke, what I learned, and why I made each decision.
Newest entries at the top. Format: **What I did / What I learned / Decisions & why**

---

## Oct 6, 2026: Project kickoff

**What I did**
- Built a first prototype voice assistant (originally called "Jarvis"): hold F9 to talk, Whisper transcribes, Claude decides what to do and can call tools (shell commands, open apps, notes, web search), and edge-tts speaks the reply.
- Explored turning it into a physical desk robot (ESP32-S3 with mic, speaker, round display, servos). Decided to finish the software first.
- Renamed the project **MILO** (Machine Intelligence that Learns Over time).
- Designed the first version of MILO's interaction log in Excel (`docs/data_log_design_v2.xlsx`).

**What I learned**
- The prototype *uses* machine learning (Whisper, Claude, neural TTS) but doesn't *learn*. All those models are frozen. To make MILO learn, I need to train my own small models on my own interaction data.
- Machine learning starts with data. Before any model, MILO needs to keep a diary of every interaction.
- Data should be organized as **one row per conversation, one column per fact**. My first draft used one column per feature category (Smart Home, Spotify, Timers), which wouldn't scale: every new feature would need new columns, and repeated commands had nowhere to go.

**Decisions & why**
- **Log both "what I said" and "what MILO heard."** That way, when something fails, I can tell whether the speech recognition misheard me or the AI picked the wrong action.
- **Every row gets a "did it work?" label** plus "how you could tell." The label is what models learn from; the evidence lets me automate labeling later (e.g., detecting when I repeat a command).
- **Feature order:** timers/reminders → wake word → Spotify → smart home. Simplest first, so I learn the basics before harder integrations.
- **Learning gets no extra permissions.** Anything risky (shell commands, file writes) always needs my confirmation, no matter how confident a model is.

**Next:** write a Python script that appends rows to a CSV log.
