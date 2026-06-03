"""
scripts/generate_events.py
Generates comprehensive events.jsonl for BOTH stores covering ALL 8 required
event types. Events are dated 2026-04-10 to match the POS transactions CSV.

Run: python scripts/generate_events.py
Output: data/events.jsonl
"""
import json, uuid, random
from datetime import datetime, timezone, timedelta
from pathlib import Path
from collections import Counter

random.seed(42)
DATE_BASE = datetime(2026, 4, 10, 10, 0, 0, tzinfo=timezone.utc)

STORES = {
    "STORE_BLR_002": {
        "cameras": {
            "entry":   "CAM_ENTRY_01",
            "floor":   "CAM_FLOOR_01",
            "billing": "CAM_BILLING_01",
        },
        "zones": ["SKINCARE", "HAIRCARE", "FRAGRANCE", "MAKEUP", "WELLNESS"],
    },
    "STORE_BLR_003": {
        "cameras": {
            "entry":   "CAM_ENTRY_01",
            "floor":   "CAM_FLOOR_01",
            "billing": "CAM_BILLING_01",
        },
        "zones": ["SKINCARE", "HAIRCARE", "FRAGRANCE", "BILLING_AREA"],
    },
}

event_ids_used = set()
all_events = []


def uid():
    while True:
        i = str(uuid.uuid4())
        if i not in event_ids_used:
            event_ids_used.add(i)
            return i


def vid():
    return "VIS_" + uuid.uuid4().hex[:8]


def ts(store_offset_minutes, event_offset_minutes=0):
    t = DATE_BASE + timedelta(minutes=store_offset_minutes + event_offset_minutes)
    return t.strftime("%Y-%m-%dT%H:%M:%SZ")


def make_event(store_id, camera_id, event_type, visitor_id, timestamp_str,
               zone_id=None, dwell_ms=0, is_staff=False,
               confidence=None, queue_depth=None, session_seq=1):
    if confidence is None:
        confidence = round(random.uniform(0.72, 0.97), 2)
    return {
        "event_id":   uid(),
        "store_id":   store_id,
        "camera_id":  camera_id,
        "visitor_id": visitor_id,
        "event_type": event_type,
        "timestamp":  timestamp_str,
        "zone_id":    zone_id,
        "dwell_ms":   dwell_ms,
        "is_staff":   is_staff,
        "confidence": confidence,
        "metadata": {
            "queue_depth": queue_depth,
            "sku_zone":    zone_id,
            "session_seq": session_seq,
        }
    }


def generate_store(store_id, store_cfg, time_offset_mins=0):
    """Generate a full set of realistic events for one store."""
    cams  = store_cfg["cameras"]
    zones = store_cfg["zones"]
    evts  = []

    def e(event_type, visitor_id, cam_key, t_offset,
          zone_id=None, dwell_ms=0, is_staff=False,
          confidence=None, queue_depth=None, seq=1):
        evts.append(make_event(
            store_id, cams[cam_key], event_type, visitor_id,
            ts(time_offset_mins, t_offset),
            zone_id=zone_id, dwell_ms=dwell_ms, is_staff=is_staff,
            confidence=confidence, queue_depth=queue_depth, session_seq=seq
        ))

    # ── STAFF (3 members) ─────────────────────────────────────────────
    for s in range(3):
        sv   = f"VIS_STAFF_{store_id[-3:]}_{s:02d}"
        base = s * 45
        e("ENTRY", sv, "entry", base, is_staff=True, seq=1)
        for zi, zone in enumerate(zones[:-1][:3]):
            zt = base + 5 + zi * 10
            e("ZONE_ENTER", sv, "floor", zt,    zone_id=zone, is_staff=True, seq=2+zi*2)
            e("ZONE_EXIT",  sv, "floor", zt+8,  zone_id=zone, dwell_ms=480000, is_staff=True, seq=3+zi*2)
        e("EXIT", sv, "entry", base + 60, is_staff=True, seq=10)

    # ── GROUP ENTRIES (3 groups) ──────────────────────────────────────
    for g in range(3):
        group_size = random.choice([2, 3])
        g_time = 15 + g * 30
        group_vids = [vid() for _ in range(group_size)]
        for p, gv in enumerate(group_vids):
            e("ENTRY", gv, "entry", g_time + p, seq=1)
        for gv in group_vids:
            t_z  = g_time + 5
            seq  = 2
            for zone in random.sample(zones[:-1], min(2, len(zones)-1)):
                dwell = random.randint(35000, 180000)
                e("ZONE_ENTER", gv, "floor", t_z,     zone_id=zone, seq=seq);   seq+=1
                e("ZONE_DWELL", gv, "floor", t_z+1,   zone_id=zone, dwell_ms=30000, seq=seq); seq+=1
                e("ZONE_EXIT",  gv, "floor", t_z+3,   zone_id=zone, dwell_ms=dwell, seq=seq); seq+=1
                t_z += 4
            if random.random() < 0.55:
                qd = random.randint(1, 5)
                bill_zone = "BILLING_AREA" if "BILLING_AREA" in zones else zones[-1]
                e("BILLING_QUEUE_JOIN", gv, "billing", t_z+2,
                  zone_id=bill_zone, queue_depth=qd, seq=seq); seq+=1
            e("EXIT", gv, "entry", t_z + 10, seq=seq)

    # ── REGULAR CUSTOMERS (20 visitors) ──────────────────────────────
    regular_vids = []
    for i in range(20):
        v      = vid()
        regular_vids.append(v)
        t0     = 25 + i * 12
        seq    = 1
        e("ENTRY", v, "entry", t0, seq=seq); seq += 1

        zones_to_visit = random.sample(zones[:-1], random.randint(1, min(3, len(zones)-1)))
        tz = t0 + 3
        for zone in zones_to_visit:
            dwell = random.randint(30000, 200000)
            e("ZONE_ENTER", v, "floor", tz,    zone_id=zone, seq=seq); seq+=1
            ticks = dwell // 30000
            for tick in range(ticks):
                e("ZONE_DWELL", v, "floor", tz+tick+1,
                  zone_id=zone, dwell_ms=30000*(tick+1), seq=seq); seq+=1
            e("ZONE_EXIT", v, "floor", tz+3, zone_id=zone, dwell_ms=dwell, seq=seq); seq+=1
            tz += 5

        # 60% go to billing
        if random.random() < 0.60:
            qd = random.randint(1, 8)
            bill_zone = "BILLING_AREA" if "BILLING_AREA" in zones else zones[-1]
            e("BILLING_QUEUE_JOIN", v, "billing", tz+2,
              zone_id=bill_zone, queue_depth=qd, seq=seq); seq+=1
            if random.random() < 0.20:
                e("BILLING_QUEUE_ABANDON", v, "billing", tz+7,
                  zone_id=bill_zone, seq=seq); seq+=1

        e("EXIT", v, "entry", tz + 15, seq=seq)

    # ── RE-ENTRIES (5 visitors return) ───────────────────────────────
    for i, rv in enumerate(regular_vids[:5]):
        t_re = 310 + i * 10
        seq  = 1
        e("REENTRY",    rv, "entry", t_re,   seq=seq); seq+=1
        zone = random.choice(zones[:-1])
        e("ZONE_ENTER", rv, "floor", t_re+2, zone_id=zone, seq=seq); seq+=1
        e("ZONE_EXIT",  rv, "floor", t_re+5, zone_id=zone, dwell_ms=60000, seq=seq); seq+=1
        e("EXIT",       rv, "entry", t_re+8, seq=seq)

    # ── LOW CONFIDENCE (not suppressed) ──────────────────────────────
    for lc in range(4):
        lv = vid()
        t_lc = 370 + lc * 5
        e("ENTRY", lv, "entry", t_lc,   confidence=0.36, seq=1)
        e("EXIT",  lv, "entry", t_lc+6, confidence=0.41, seq=2)

    # ── EMPTY PERIOD (no events 400-420 min — zero traffic handling) ─
    # Intentional gap — no events generated here

    return evts


# Generate for both stores
for store_id, cfg in STORES.items():
    offset = 0 if store_id == "STORE_BLR_002" else 5  # slight offset
    store_evts = generate_store(store_id, cfg, time_offset_mins=offset)
    all_events.extend(store_evts)

# Sort all events by timestamp
all_events.sort(key=lambda e: e["timestamp"])

# Write output
out = Path("data/events.jsonl")
out.parent.mkdir(exist_ok=True)
with open(out, "w", encoding="utf-8") as f:
    for ev in all_events:
        f.write(json.dumps(ev) + "\n")

# Summary
types   = Counter(ev["event_type"] for ev in all_events)
stores  = Counter(ev["store_id"]   for ev in all_events)
staff   = sum(1 for ev in all_events if ev["is_staff"])
low_c   = sum(1 for ev in all_events if ev["confidence"] < 0.5)
visitors = len(set(ev["visitor_id"] for ev in all_events if not ev["is_staff"]))

REQUIRED = ["ENTRY","EXIT","ZONE_ENTER","ZONE_EXIT","ZONE_DWELL",
            "BILLING_QUEUE_JOIN","BILLING_QUEUE_ABANDON","REENTRY"]

print(f"Generated {len(all_events)} events → {out}")
print(f"\nBy store:")
for sid, cnt in sorted(stores.items()):
    print(f"  {sid}: {cnt} events")
print(f"\nEvent type breakdown:")
for et in REQUIRED:
    cnt    = types.get(et, 0)
    status = "✓" if cnt > 0 else "✗ MISSING"
    print(f"  {status} {et:<28} {cnt:>4}")
print(f"\nUnique customer visitors:  {visitors}")
print(f"Staff events:              {staff}")
print(f"Low-confidence events:     {low_c}")
print(f"Date:                      2026-04-10")
print(f"\nAll 8 required event types: {'✓ YES' if all(types.get(t,0)>0 for t in REQUIRED) else '✗ MISSING TYPES'}")