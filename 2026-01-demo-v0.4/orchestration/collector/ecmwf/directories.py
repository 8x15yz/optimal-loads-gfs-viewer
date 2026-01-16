from datetime import datetime, timezone

UTC = timezone.utc


def upsert_directories(
    *,
    dir_collection,
    inventory_directory: str,
):
    """
    ecmwf/ifs/2024/2024-12/2024-12-31/00Z/original/10u/
    """
    if not inventory_directory.endswith("/"):
        inventory_directory += "/"

    now = datetime.now(UTC)
    parts = inventory_directory.strip("/").split("/")

    cur = ""
    for part in parts:
        cur = f"{cur}{part}/"
        parent = "/".join(cur.strip("/").split("/")[:-1])
        parent = f"{parent}/" if parent else None

        dir_collection.update_one(
            {"_id": cur},
            {
                "$setOnInsert": {
                    "dir": cur,
                    "parent": parent,
                    "name": part,
                    "children_dirs": [],
                    "created_at": now,
                },
                "$set": {"updated_at": now},
            },
            upsert=True,
        )

        if parent:
            dir_collection.update_one(
                {"_id": parent},
                {"$addToSet": {"children_dirs": f"{part}/"}},
            )
