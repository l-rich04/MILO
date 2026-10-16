import csv
import os
from datetime import datetime

file_exists = os.path.exists("milo_log.csv")

with open("milo_log.csv", "a", newline="") as f:
    writer = csv.writer(f)
    if not file_exists:
        writer.writerow(["time", "command", "category", "worked"])
    writer.writerow([datetime.now(), "Hey MILO, set a timer for 20 minutes", "timer", "yes"])

print("Logged a row to milo_log.csv")