"""
scripts/convert_pos.py
Run: python scripts/convert_pos.py
Converts the real Purplle POS CSV to the format pos_loader.py expects.
"""
import csv
from datetime import datetime
import os

INPUT  = "data/POS_-_sample_transactionsb1e826f.csv"
OUTPUT = "data/pos_transactions.csv"

# Try alternate path if file not in expected location
if not os.path.exists(INPUT):
    INPUT = "data/pos_transactions_raw.csv"

rows_out = []
seen_orders = {}

with open(INPUT, newline="", encoding="utf-8") as f:
    reader = csv.DictReader(f)
    for row in reader:
        try:
            date_str = row["order_date"].strip()
            time_str = row["order_time"].strip()
            dt = datetime.strptime(date_str + " " + time_str, "%d-%m-%Y %H:%M:%S")
            ts = dt.strftime("%Y-%m-%dT%H:%M:%SZ")
        except Exception as e:
            print(f"  Skipping row {row.get('order_id','?')}: {e}")
            continue

        order_id = row["order_id"].strip()
        tid = f"TXN_{order_id}"

        if tid not in seen_orders:
            seen_orders[tid] = True
            # Map ST1008 -> STORE_BLR_002
            raw_store = row.get("store_id", "").strip()
            store_id = "STORE_BLR_002" if raw_store == "ST1008" else raw_store

            rows_out.append({
                "store_id":         store_id,
                "transaction_id":   tid,
                "timestamp":        ts,
                "basket_value_inr": row.get("total_amount", "0").strip(),
            })

with open(OUTPUT, "w", newline="", encoding="utf-8") as f:
    writer = csv.DictWriter(
        f, fieldnames=["store_id", "transaction_id", "timestamp", "basket_value_inr"]
    )
    writer.writeheader()
    writer.writerows(rows_out)

print(f"Done: {len(rows_out)} unique transactions written to {OUTPUT}")
print(f"Sample:")
for r in rows_out[:3]:
    print(f"  {r}")
