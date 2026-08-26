"""Product queries shared by the web API and the exporter.

One place builds the WHERE clause, so the list on screen and the exported file
always mean the same thing.
"""
from __future__ import annotations

SORTS = {
    "discount": "discount DESC, price ASC",
    "price_asc": "price ASC",
    "price_desc": "price DESC",
    "saving": "(old_price - price) DESC",
    "newest": "first_seen DESC",
}

COLUMNS = [
    "product_id", "title", "brand", "root", "category_id", "price", "old_price",
    "discount", "badge", "url", "thumb_url", "image_url", "first_seen", "last_seen",
]


def build(filters: dict) -> tuple[str, list]:
    """Turn a filter dict into a WHERE clause plus its arguments."""
    where, args = ["is_active = 1"], []

    if filters.get("root"):
        where.append("root = ?")
        args.append(filters["root"])
    if filters.get("category_id"):
        where.append("category_id = ?")
        args.append(int(filters["category_id"]))
    brands = [b for b in (filters.get("brands") or []) if b]
    if brands:
        where.append(f"brand IN ({','.join('?' * len(brands))})")
        args.extend(brands)
    if filters.get("min_discount"):
        where.append("discount >= ?")
        args.append(int(filters["min_discount"]))
    if filters.get("min_price"):
        where.append("price >= ?")
        args.append(int(filters["min_price"]))
    if filters.get("max_price"):
        where.append("price <= ?")
        args.append(int(filters["max_price"]))
    if filters.get("q"):
        where.append("title LIKE ?")
        args.append(f"%{filters['q'].strip()}%")

    return " AND ".join(where), args


def select(store, filters: dict, sort: str = "discount", limit: int = 100, offset: int = 0) -> list[dict]:
    clause, args = build(filters)
    order = SORTS.get(sort, SORTS["discount"])
    rows = store.db.execute(
        f"SELECT {', '.join(COLUMNS)} FROM products WHERE {clause} ORDER BY {order} LIMIT ? OFFSET ?",
        [*args, limit, offset],
    ).fetchall()
    return [dict(r) for r in rows]


def count(store, filters: dict) -> int:
    clause, args = build(filters)
    return store.db.execute(f"SELECT COUNT(*) AS n FROM products WHERE {clause}", args).fetchone()["n"]


def iterate(store, filters: dict, sort: str = "discount"):
    """Stream every matching row, for exports that must not be paged."""
    clause, args = build(filters)
    order = SORTS.get(sort, SORTS["discount"])
    cursor = store.db.execute(
        f"SELECT {', '.join(COLUMNS)} FROM products WHERE {clause} ORDER BY {order}", args
    )
    while True:
        chunk = cursor.fetchmany(1000)
        if not chunk:
            return
        for row in chunk:
            yield dict(row)


def facets(store) -> dict:
    """What the filter controls should offer, counted from stored products."""
    roots = store.db.execute(
        "SELECT root, COUNT(*) AS n FROM products WHERE is_active = 1 AND root IS NOT NULL GROUP BY root ORDER BY n DESC"
    ).fetchall()
    brands = store.db.execute(
        """SELECT brand, COUNT(*) AS n FROM products
           WHERE is_active = 1 AND brand IS NOT NULL
           GROUP BY brand ORDER BY n DESC LIMIT 400"""
    ).fetchall()
    span = store.db.execute(
        "SELECT MIN(price) AS lo, MAX(price) AS hi FROM products WHERE is_active = 1"
    ).fetchone()
    return {
        "roots": [{"key": r["root"], "count": r["n"]} for r in roots],
        "brands": [{"name": r["brand"], "count": r["n"]} for r in brands],
        "price": {"min": span["lo"], "max": span["hi"]},
        "total": store.count_products(),
    }
