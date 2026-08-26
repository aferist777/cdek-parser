"""SQLite storage.

Products are upserted by product_id, so the same item found under several
categories or brands stays one row. Prices are appended to price_history only
when they actually move, which keeps the table small enough to be useful.
"""
from __future__ import annotations

import json
import sqlite3
from datetime import datetime, timezone
from pathlib import Path

SCHEMA = """
CREATE TABLE IF NOT EXISTS categories (
    id          INTEGER PRIMARY KEY,
    root        TEXT NOT NULL,
    path        TEXT NOT NULL UNIQUE,
    title       TEXT NOT NULL,
    total       INTEGER,
    is_leaf     INTEGER NOT NULL DEFAULT 1,
    updated_at  TEXT NOT NULL
);

CREATE TABLE IF NOT EXISTS brands (
    id          INTEGER PRIMARY KEY,
    name        TEXT NOT NULL,
    updated_at  TEXT NOT NULL
);

CREATE TABLE IF NOT EXISTS category_brands (
    category_id INTEGER NOT NULL,
    brand_id    INTEGER NOT NULL,
    count       INTEGER NOT NULL DEFAULT 0,
    PRIMARY KEY (category_id, brand_id)
);

CREATE TABLE IF NOT EXISTS products (
    product_id  INTEGER PRIMARY KEY,
    title       TEXT NOT NULL,
    url         TEXT NOT NULL,
    image_url   TEXT,
    thumb_url   TEXT,
    price       INTEGER,
    old_price   INTEGER,
    discount    INTEGER NOT NULL DEFAULT 0,
    badge       TEXT,
    brand_id    INTEGER,
    brand       TEXT,
    category_id INTEGER,
    root        TEXT,
    first_seen  TEXT NOT NULL,
    last_seen   TEXT NOT NULL,
    is_active   INTEGER NOT NULL DEFAULT 1
);

CREATE INDEX IF NOT EXISTS ix_products_discount ON products(discount DESC);
CREATE INDEX IF NOT EXISTS ix_products_price    ON products(price);
CREATE INDEX IF NOT EXISTS ix_products_brand    ON products(brand);
CREATE INDEX IF NOT EXISTS ix_products_root     ON products(root);
CREATE INDEX IF NOT EXISTS ix_products_category ON products(category_id);
CREATE INDEX IF NOT EXISTS ix_products_seen     ON products(last_seen);

CREATE TABLE IF NOT EXISTS price_history (
    product_id  INTEGER NOT NULL,
    seen_at     TEXT NOT NULL,
    price       INTEGER,
    old_price   INTEGER,
    discount    INTEGER,
    PRIMARY KEY (product_id, seen_at)
);

CREATE TABLE IF NOT EXISTS runs (
    id          INTEGER PRIMARY KEY AUTOINCREMENT,
    started_at  TEXT NOT NULL,
    finished_at TEXT,
    status      TEXT NOT NULL,
    pages       INTEGER NOT NULL DEFAULT 0,
    products    INTEGER NOT NULL DEFAULT 0,
    new_products INTEGER NOT NULL DEFAULT 0,
    note        TEXT
);

CREATE TABLE IF NOT EXISTS meta (
    key   TEXT PRIMARY KEY,
    value TEXT NOT NULL
);
"""


def now() -> str:
    return datetime.now(timezone.utc).isoformat(timespec="seconds")


class Store:
    def __init__(self, db_path: str | Path):
        self.path = Path(db_path)
        self.path.parent.mkdir(parents=True, exist_ok=True)
        self.db = sqlite3.connect(self.path, check_same_thread=False)
        self.db.row_factory = sqlite3.Row
        self.db.executescript(SCHEMA)
        self.db.execute("PRAGMA journal_mode=WAL")
        self.db.commit()

    def close(self) -> None:
        self.db.close()

    # -- catalog -----------------------------------------------------------
    def save_category(self, leaf: dict) -> None:
        stamp = now()
        self.db.execute(
            """INSERT INTO categories (id, root, path, title, total, is_leaf, updated_at)
               VALUES (?,?,?,?,?,1,?)
               ON CONFLICT(path) DO UPDATE SET
                   root=excluded.root, title=excluded.title,
                   total=excluded.total, updated_at=excluded.updated_at""",
            (leaf.get("id"), leaf["root"], leaf["path"], leaf["title"], leaf.get("total"), stamp),
        )
        for brand in leaf.get("brands", []):
            self.db.execute(
                "INSERT INTO brands (id, name, updated_at) VALUES (?,?,?) "
                "ON CONFLICT(id) DO UPDATE SET name=excluded.name, updated_at=excluded.updated_at",
                (brand["id"], brand["name"], stamp),
            )
            if leaf.get("id"):
                self.db.execute(
                    "INSERT INTO category_brands (category_id, brand_id, count) VALUES (?,?,?) "
                    "ON CONFLICT(category_id, brand_id) DO UPDATE SET count=excluded.count",
                    (leaf["id"], brand["id"], brand["count"]),
                )
        self.db.commit()

    def categories(self, root: str | None = None) -> list[sqlite3.Row]:
        sql = "SELECT * FROM categories"
        args = []
        if root:
            sql += " WHERE root = ?"
            args.append(root)
        return self.db.execute(sql + " ORDER BY total DESC", args).fetchall()

    def brand_directory(self) -> list[tuple[int, str, int]]:
        """Every known brand as (id, name, catalogue_count).

        The count ranks candidates when a title matches more than one brand:
        "Nike Air Zoom Pegasus" should resolve to Nike, not to Pegasus.
        """
        rows = self.db.execute(
            """SELECT b.id, b.name, COALESCE(SUM(cb.count), 0) AS cnt
               FROM brands b LEFT JOIN category_brands cb ON cb.brand_id = b.id
               GROUP BY b.id, b.name"""
        ).fetchall()
        return [(r["id"], r["name"], r["cnt"]) for r in rows]

    # -- products ----------------------------------------------------------
    def upsert_products(self, items: list[dict]) -> tuple[int, int]:
        """Insert or update products. Returns (written, newly_seen)."""
        if not items:
            return 0, 0
        stamp = now()
        ids = [i["product_id"] for i in items]
        placeholders = ",".join("?" * len(ids))
        known = {
            r["product_id"]: r
            for r in self.db.execute(
                f"SELECT product_id, price, discount FROM products WHERE product_id IN ({placeholders})", ids
            ).fetchall()
        }

        fresh = 0
        for item in items:
            pid = item["product_id"]
            previous = known.get(pid)
            if previous is None:
                fresh += 1
                self.db.execute(
                    """INSERT INTO products (product_id, title, url, image_url, thumb_url, price,
                                             old_price, discount, badge, brand_id, brand, category_id,
                                             root, first_seen, last_seen, is_active)
                       VALUES (?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,1)""",
                    (pid, item["title"], item["url"], item.get("image_url"), item.get("thumb_url"),
                     item.get("price"), item.get("old_price"), item.get("discount", 0), item.get("badge"),
                     item.get("brand_id"), item.get("brand"), item.get("category_id"), item.get("root"),
                     stamp, stamp),
                )
            else:
                self.db.execute(
                    """UPDATE products SET title=?, url=?, image_url=?, thumb_url=?, price=?, old_price=?,
                              discount=?, badge=?, brand_id=COALESCE(?, brand_id), brand=COALESCE(?, brand),
                              category_id=COALESCE(?, category_id), root=COALESCE(?, root),
                              last_seen=?, is_active=1
                       WHERE product_id=?""",
                    (item["title"], item["url"], item.get("image_url"), item.get("thumb_url"),
                     item.get("price"), item.get("old_price"), item.get("discount", 0), item.get("badge"),
                     item.get("brand_id"), item.get("brand"), item.get("category_id"), item.get("root"),
                     stamp, pid),
                )
            # History only when the price actually moved.
            if previous is None or previous["price"] != item.get("price") or previous["discount"] != item.get("discount", 0):
                self.db.execute(
                    "INSERT OR REPLACE INTO price_history (product_id, seen_at, price, old_price, discount) VALUES (?,?,?,?,?)",
                    (pid, stamp, item.get("price"), item.get("old_price"), item.get("discount", 0)),
                )
        self.db.commit()
        return len(items), fresh

    def count_products(self) -> int:
        return self.db.execute("SELECT COUNT(*) AS n FROM products").fetchone()["n"]

    # -- runs & meta -------------------------------------------------------
    def start_run(self) -> int:
        cur = self.db.execute("INSERT INTO runs (started_at, status) VALUES (?, 'running')", (now(),))
        self.db.commit()
        return cur.lastrowid

    def finish_run(self, run_id: int, status: str, pages: int, products: int, new_products: int, note: str = "") -> None:
        self.db.execute(
            "UPDATE runs SET finished_at=?, status=?, pages=?, products=?, new_products=?, note=? WHERE id=?",
            (now(), status, pages, products, new_products, note, run_id),
        )
        self.db.commit()

    def last_run(self) -> sqlite3.Row | None:
        return self.db.execute("SELECT * FROM runs ORDER BY id DESC LIMIT 1").fetchone()

    def set_meta(self, key: str, value) -> None:
        self.db.execute(
            "INSERT INTO meta (key, value) VALUES (?,?) ON CONFLICT(key) DO UPDATE SET value=excluded.value",
            (key, json.dumps(value, ensure_ascii=False)),
        )
        self.db.commit()

    def get_meta(self, key: str, default=None):
        row = self.db.execute("SELECT value FROM meta WHERE key=?", (key,)).fetchone()
        return json.loads(row["value"]) if row else default
