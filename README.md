# MILO
**Machine Intelligence that Learns Over time**

MILO is a personal voice assistant, like Alexa but built around one person, that keeps learning from everyday use. The long-term goal is a physical desk buddy (ESP32-based robot with a face, speaker, and mic) powered by a continual learning pipeline running on my PC.

> 🚧 Work in progress. See [DEVLOG.md](DEVLOG.md) for the build journal.

## How it works (planned)
1. **Hear:** speech-to-text with Whisper
2. **Think:** Claude API with tool calling
3. **Act:** tools for timers, music, smart home, notes, and more
4. **Speak:** text-to-speech
5. **Learn:** every interaction is logged, and small models retrain nightly on that data. A new model only replaces the old one if it scores better on a held-out test set.

## Roadmap
- [x] Prototype voice assistant (push-to-talk, tool calling, text-to-speech)
- [x] Design the interaction log (one row per conversation)
- [ ] Phase 0: Log every interaction (CSV, then SQLite)
- [ ] Timers, alarms, and reminders
- [ ] "Hey MILO" wake word (custom-trained model)
- [ ] Intent router with nightly retraining
- [ ] Music control (Spotify)
- [ ] Smart home control
- [ ] Physical robot body (ESP32-S3)

## Results
Metrics are tracked in [docs/METRICS.md](docs/METRICS.md).

## Tech stack
Python · faster-whisper · Claude API · edge-tts · SQLite · (planned) scikit-learn, openWakeWord, ESP32-S3
