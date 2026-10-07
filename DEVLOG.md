# MILO Dev Log

What I did, what I learned, and why I made each decision. Newest entries at the top.

---

## Oct 6, 2026: Project kickoff

**What I did**
- Got a starter voice assistant script (`jarvis.py`) from Claude as a prototype: push-to-talk, speech-to-text, Claude API with tools, text-to-speech. Not yet tested on my machine.
- Named the project MILO (Machine Intelligence that Learns Over time).
- Designed the first version of MILO's interaction log in Excel (`docs/`).
- Moved the project out of OneDrive to `C:\MILO` and set up the GitHub repo.

**What I learned**
- The prototype uses ML models but doesn't learn. To make MILO learn, I need to log my own interaction data and train my own small models on it.
- Data should be one row per conversation and one column per fact. My first draft used one column per feature, which wouldn't scale.
- The PowerShell prompt shows which folder you're in. I accidentally ran `git init` in my user folder, caught it with `git status`, and deleted that `.git` folder before committing anything.

**Decisions & why**
- Log both "what I said" and "what MILO heard," so I can tell whether a failure was a hearing mistake or a wrong action.
- Keep the project out of OneDrive, since syncing can conflict with Git and SQLite.

**Next:** write a Python script that appends rows to a CSV log.
