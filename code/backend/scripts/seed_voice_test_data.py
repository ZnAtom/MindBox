"""Add idempotent inventory fixtures for local voice and reminder testing."""

import argparse
import json
from pathlib import Path
import sys

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))

from app.database import Database, utcnow
from app.store import Store


DEFAULT_DATABASE = Path(__file__).resolve().parents[1] / "var/mind-box.sqlite3"
SEED_TAG = "MindBox voice-test-seed"

ITEMS = (
    {
        "name": "坚果",
        "category": "食品",
        "description": "混合坚果测试礼盒",
        "brand": "测试品牌",
        "quantity": 1,
        "unit": "盒",
        "location_id": "cabinet-01:drawer-1",
        "production_on": "2026-10-01",
        "expires_on": "2027-04-01",
        "shelf_life_raw": "6个月",
        "notes": SEED_TAG,
    },
    {
        "name": "牛奶",
        "category": "乳制品",
        "description": "纯牛奶测试盒装",
        "brand": "测试品牌",
        "quantity": 1,
        "unit": "盒",
        "location_id": "cabinet-01:drawer-2",
        "production_on": "2026-10-17",
        "expires_on": "2026-10-24",
        "shelf_life_raw": "7天",
        "notes": SEED_TAG,
    },
    {
        "name": "鱼油",
        "category": "保健品",
        "description": "鱼油软胶囊测试瓶",
        "brand": "测试品牌",
        "quantity": 1,
        "unit": "瓶",
        "location_id": "cabinet-01:drawer-2",
        "production_on": "2026-10-17",
        "expires_on": "2027-10-17",
        "shelf_life_raw": "12个月",
        "notes": SEED_TAG,
    },
    {
        "name": "巧克力",
        "category": "零食",
        "description": "黑巧克力测试盒",
        "brand": "测试品牌",
        "quantity": 1,
        "unit": "盒",
        "location_id": "cabinet-01:drawer-3",
        "notes": SEED_TAG,
    },
)


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--database", type=Path, default=DEFAULT_DATABASE)
    args = parser.parse_args()

    database = args.database.resolve()
    store = Store(Database(database))
    store.initialize("cabinet-01")
    with store.db.connect() as connection:
        existing_seed = set()
        existing_names = set()
        for row in connection.execute("SELECT name, data_json FROM items"):
            existing_names.add(row["name"])
            if SEED_TAG in json.loads(row["data_json"]).get("notes", ""):
                existing_seed.add(row["name"])

    added = []
    skipped = []
    conflicts = []
    for template in ITEMS:
        if template["name"] in existing_seed:
            skipped.append(template["name"])
            continue
        if template["name"] in existing_names:
            conflicts.append(template["name"])
            continue
        snapshot = store.get_inventory_snapshot("cabinet-01")
        values = {
            "name_kind": "specific",
            "identity_status": "confirmed",
            "action_status": "confirmed",
            "product_match_status": "category_only",
            "presence_status": "present",
            "stored_at": utcnow(),
            **template,
        }
        receipt = store.create_item(
            "cabinet-01",
            values,
            snapshot["inventory_revision"],
            "voice-test-seed:" + template["name"],
            "voice-test-seed",
        )
        added.append({"name": template["name"], "receipt": receipt})
        existing_names.add(template["name"])

    # These rows are an explicit known test baseline, not a physical-camera result.
    with store.db.transaction() as connection:
        connection.execute("UPDATE cabinets SET inventory_known=1 WHERE id='cabinet-01'")

    snapshot = store.get_inventory_snapshot("cabinet-01")
    print(json.dumps({
        "database": str(database),
        "seed_tag": SEED_TAG,
        "added": added,
        "skipped": skipped,
        "conflicts": conflicts,
        "inventory_revision": snapshot["inventory_revision"],
        "inventory_known": snapshot["inventory_known"],
        "items": [
            {key: item[key] for key in ("id", "name", "category", "location_id", "production_on", "expires_on")}
            for item in snapshot["items"]
        ],
    }, ensure_ascii=False, indent=2))


if __name__ == "__main__":
    main()
