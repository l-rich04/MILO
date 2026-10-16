import csv
from datetime import datetime

with open("milo_log.csv", "a", newline="") as f:
    writer = csv.writer(f)
    writer.writerow([datetime.now(), "Hey MILO, set a timer for 20 minutes", "timer", "yes"])