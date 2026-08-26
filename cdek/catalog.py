"""Category tree and brand directory.

Two shapes of listing exist:
  /c/{id}/{slug}  categories -- support brands[] filtering, and expose a JSON
                  endpoint carrying the whole category subtree, the exact
                  discounted total, and the full brand list with counts.
  /l/{id}/{slug}  landings   -- plain listings; no brand filter, no JSON.

Parent categories render their children instead of products, so only leaves
are worth crawling. One call to the filter endpoint per root returns that
root's entire subtree, so no page-by-page walking is needed.
"""
from __future__ import annotations

import json
import logging
import re

log = logging.getLogger(__name__)

CATEGORY_RE = re.compile(r"^/c/(\d+)/")

# Without an Accept header the filter endpoint answers with a 4MB HTML page
# instead of JSON, so both headers matter.
AJAX_HEADERS = {
    "X-Requested-With": "XMLHttpRequest",
    "Accept": "application/json, text/javascript, */*; q=0.01",
}


def is_category(path: str) -> bool:
    return bool(CATEGORY_RE.match(path))


def category_id(path: str) -> int | None:
    m = CATEGORY_RE.match(path)
    return int(m.group(1)) if m else None


def listing_url(
    path: str,
    page: int = 1,
    only_sale: bool = True,
    sort_by: str = "sale_desc",
    brand_id: int | None = None,
) -> str:
    params = []
    if only_sale:
        params.append("is_sale=1")
    if sort_by and sort_by != "default_sorting":
        params.append(f"sort-by={sort_by}")
    if brand_id:
        params.append(f"brands%5B%5D={brand_id}")
    if page > 1:
        params.append(f"page={page}")
    return path + ("?" + "&".join(params) if params else "")


def fetch_filters(fetcher, cat_id: int, only_sale: bool = True) -> dict:
    """Raw filter payload for a category: subtree, totals and brands."""
    path = f"/c/getFiltersByCategory/{cat_id}" + ("?is_sale=1" if only_sale else "")
    raw = fetcher.get(path, headers=AJAX_HEADERS)
    return json.loads(raw).get("additional") or {}


def canonical_brand(name: str) -> str:
    """Fold the site's spelling variants together.

    It ships both "Dolce&Gabbana" and "Dolce & Gabbana" as separate brands,
    which would otherwise split one brand across two filter entries.
    """
    return " ".join(name.replace("&", " & ").split())


def brands_of(extra: dict) -> list[dict]:
    brands = extra.get("filterBrands")
    if not isinstance(brands, list):
        return []
    return [
        {"id": int(b["id"]), "name": canonical_brand(b.get("name", "")), "count": int(b.get("count") or 0)}
        for b in brands
        if str(b.get("id", "")).isdigit()
    ]


def sample_brands(fetcher, leaves: list[dict], limit: int, only_sale: bool = True) -> list[dict]:
    """Widen the brand directory using the biggest leaves.

    One category answers with at most 500 brands, so a catalogue this size
    needs several vantage points before rare labels show up.
    """
    biggest = sorted((l for l in leaves if l.get("id")), key=lambda l: -(l.get("count") or 0))[:limit]
    merged: dict[int, dict] = {}
    for leaf in biggest:
        try:
            for brand in brands_of(fetch_filters(fetcher, leaf["id"], only_sale)):
                known = merged.get(brand["id"])
                if known is None or brand["count"] > known["count"]:
                    merged[brand["id"]] = brand
        except Exception as exc:
            log.warning("brand sample failed for %s: %s", leaf["path"], exc)
    return list(merged.values())


def _find(nodes: list[dict], cat_id: int) -> dict | None:
    for node in nodes:
        if node.get("id") == cat_id:
            return node
        found = _find(node.get("children") or [], cat_id)
        if found:
            return found
    return None


def _leaves(node: dict, out: list[dict]) -> list[dict]:
    children = node.get("children") or []
    if children:
        for child in children:
            _leaves(child, out)
    else:
        out.append(node)
    return out


def discover(fetcher, root: dict, only_sale: bool = True, sample_leaves: int = 0) -> dict:
    """Resolve one configured root into leaf listings plus its brand directory.

    Returns {"leaves": [...], "brands": [...], "total": int|None}. A landing
    page is its own single leaf: it has no subtree and no brand filter.
    """
    cat_id = category_id(root["path"])
    if cat_id is None:  # landing page
        leaf = {"root": root["key"], "path": root["path"], "id": None,
                "title": root["title"], "count": None, "brand_filter": False}
        brands = []
        if root.get("brands_from"):
            # A landing cannot be filtered by brand, but its products still
            # need brand labels, so borrow a related category's directory.
            try:
                brands = brands_of(fetch_filters(fetcher, int(root["brands_from"]), only_sale))
            except Exception as exc:
                log.warning("brand directory for %s failed: %s", root["title"], exc)
        log.info("%s: landing page, single listing, %s brands", root["title"], len(brands))
        return {"leaves": [leaf], "brands": brands, "total": None}

    extra = fetch_filters(fetcher, cat_id, only_sale)
    tree = extra.get("menuCategories") or []
    node = _find(tree, cat_id)

    if node is None:
        log.warning("%s (%s) missing from its own tree; treating as a leaf", root["title"], cat_id)
        leaves = [{"root": root["key"], "path": root["path"], "id": cat_id,
                   "title": root["title"], "count": extra.get("total"), "brand_filter": True}]
    else:
        leaves = [
            {
                "root": root["key"],
                "path": f"/c/{n['id']}/{n.get('slug') or ''}".rstrip("/"),
                "id": n["id"],
                "title": n.get("name", ""),
                "count": n.get("productCount"),
                "brand_filter": True,
            }
            for n in _leaves(node, [])
        ]

    brands = {b["id"]: b for b in brands_of(extra)}
    if sample_leaves and len(leaves) > 1:
        for brand in sample_brands(fetcher, leaves, sample_leaves, only_sale):
            brands.setdefault(brand["id"], brand)

    log.info("%s: %s leaves, %s brands, total=%s", root["title"], len(leaves), len(brands), extra.get("total"))
    return {"leaves": leaves, "brands": list(brands.values()), "total": extra.get("total")}
