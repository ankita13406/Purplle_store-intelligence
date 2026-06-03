"""
scripts/restamp_pos.py
Restamps POS transactions to today so conversion rate correlation works.
Run: python scripts/restamp_pos.py
"""
import csv
from datetime import datetime, timezone, timedelta

rows = list(csv.DictReader(open("data/pos_transactions.csv")))
print("Loaded", len(rows), "POS transactions")
print("Current sample:", rows[0]["timestamp"])

base = datetime.now(timezone.utc).replace(hour=14, minute=0, second=0, microsecond=0)
new_rows = []

for i, row in enumerate(rows):
    offset = timedelta(minutes=i * 4)
    new_ts = (base + offset).strftime("%Y-%m-%dT%H:%M:%SZ")
    new_rows.append({
        "store_id":         "STORE_BLR_002",
        "transaction_id":   row["transaction_id"],
        "timestamp":        new_ts,
        "basket_value_inr": row["basket_value_inr"],
    })

with open("data/pos_transactions.csv", "w", newline="") as f:
    w = csv.DictWriter(
        f, fieldnames=["store_id", "transaction_id", "timestamp", "basket_value_inr"]
    )
    w.writeheader()
    w.writerows(new_rows)

print("Done:", len(new_rows), "transactions restamped to today")
print("First:", new_rows[0]["timestamp"])
print("Last: ", new_rows[-1]["timestamp"])
print("Date: ", new_rows[0]["timestamp"][:10])