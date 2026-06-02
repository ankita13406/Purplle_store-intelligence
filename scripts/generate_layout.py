import json

layout = {
    "STORE_BLR_002": {
        "store_id": "STORE_BLR_002",
        "name": "Brigade Road Bangalore",
        "open_hours": {"open": "10:00", "close": "22:00"},
        "zones": [
            {
                "zone_id": "ENTRY_THRESHOLD",
                "cameras": ["CAM_ENTRY_01"],
                "polygon": [[0, 800], [1920, 800], [1920, 1080], [0, 1080]]
            },
            {
                "zone_id": "SKINCARE",
                "cameras": ["CAM_FLOOR_01"],
                "polygon": [[100, 100], [600, 100], [600, 500], [100, 500]]
            },
            {
                "zone_id": "HAIRCARE",
                "cameras": ["CAM_FLOOR_01"],
                "polygon": [[700, 100], [1200, 100], [1200, 500], [700, 500]]
            },
            {
                "zone_id": "FRAGRANCE",
                "cameras": ["CAM_FLOOR_01"],
                "polygon": [[1300, 100], [1800, 100], [1800, 500], [1300, 500]]
            },
            {
                "zone_id": "BILLING_AREA",
                "cameras": ["CAM_BILLING_01"],
                "polygon": [[400, 400], [1500, 400], [1500, 1080], [400, 1080]]
            }
        ]
    }
}

with open("data/store_layout.json", "w") as f:
    json.dump(layout, f, indent=2)
print("store_layout.json created!")