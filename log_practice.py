import csv
import os
from datetime import datetime

def log_interaction(command, category, worked):
    file_exists = os.path.exists("milo_log.csv")

    with open("milo_log.csv", "a", newline="") as f:
        writer = csv.writer(f)
        if not file_exists:
            writer.writerow(["time", "command", "category", "worked"])
        writer.writerow([datetime.now(), command, category, worked])

    print("Logged a row to milo_log.csv")

log_interaction("Hey MILO, set a timer for 20 minutes", "timer", "yes")
log_interaction("Hey MILO, what is the weather like today?", "weather", "yes")
log_interaction("Hey MILO, play some music from my Spotify playlist", "music", "no")