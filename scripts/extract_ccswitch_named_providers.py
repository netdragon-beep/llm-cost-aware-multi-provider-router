import os
from pathlib import Path
import shutil
import sqlite3
import tempfile
import json


SRC = Path(
    os.environ.get(
        "CCSWITCH_WEB_DATA",
        str(Path.home() / "AppData/Local/com.ccswitch.desktop/EBWebView/Default/Web Data"),
    )
)
TMP = Path(tempfile.gettempdir()) / "ccswitch-webdata-named-providers.db"

NAME = "1108997039640870368"
WEBSITE = "80584110603409727"
BASE = "-7974198728941174057"
MODEL = "-8886929033128719565"
PROVIDER_ID = "5663282974585124475"

TARGETS = ["owl", "鹿森", "autocodex", "灵算"]


def main() -> None:
    shutil.copy2(SRC, TMP)
    conn = sqlite3.connect(str(TMP))
    conn.row_factory = sqlite3.Row
    rows = conn.execute(
        """
        SELECT rowid, field_id, value, date_created, date_last_used
        FROM autofill_edge_field_values
        ORDER BY rowid
        """
    ).fetchall()
    conn.close()
    try:
        TMP.unlink(missing_ok=True)
    except Exception:
        pass

    groups = {}
    for row in rows:
        field_id = str(row["field_id"])
        parts = field_id.split("$")
        if len(parts) < 3:
            continue
        group_id = "$".join(parts[:2])
        suffix = parts[2]
        item = groups.setdefault(group_id, {"group": group_id})
        item["rowid"] = row["rowid"]
        item["last_used"] = row["date_last_used"]
        value = str(row["value"]).strip()
        if suffix == NAME:
            item["name"] = value
        elif suffix == WEBSITE:
            item["website"] = value
        elif suffix == BASE:
            item["base"] = value
        elif suffix == MODEL:
            item["model"] = value
        elif suffix == PROVIDER_ID:
            item["provider_id"] = value

    out = []
    for item in groups.values():
        name = str(item.get("name", "")).strip()
        if name in TARGETS:
            out.append(item)

    out.sort(key=lambda x: (x.get("name", ""), x.get("last_used", 0)), reverse=True)
    print(json.dumps(out, ensure_ascii=False, indent=2))


if __name__ == "__main__":
    main()
