from datetime import timedelta
DB_PATH = "setuhaul.sqlite3"
HOLD_TTL = timedelta(minutes=10)      # a hold is a promise to keep, not to show
ETA_BUFFER = timedelta(minutes=15)    # truck must arrive this long before slot start
MAX_TOOL_ROUNDS = 6                   # S2: round 7 raises
MAX_DELAY_HOURS = 24                  # beyond this a human decides
HISTORY_LIMIT = 20                    # S4: cap what goes back to the model
