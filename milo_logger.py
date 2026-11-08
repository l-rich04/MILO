import csv
import os
from datetime import datetime

def log_interaction(command, tools_used, reply, response_time, category, worked):
    file_exists = os.path.exists("milo_log.csv")

    # encoding="utf-8" so any character (like "ﬁ" or "°") can be saved
    with open("milo_log.csv", "a", newline="", encoding="utf-8") as f:
        writer = csv.writer(f)
        if not file_exists:
            writer.writerow(["time", "command", "category", "worked", "tools_used", "reply", "response_time"])
        writer.writerow([datetime.now(), command, category, worked, tools_used, reply, response_time])

    print("Logged a row to milo_log.csv")