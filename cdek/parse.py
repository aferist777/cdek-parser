"""Listing HTML -> product rows.

Everything we need is already in the listing card, so no product page visits.
The brand is NOT in the card; it is resolved by the caller (either from the
brands[] filter used for the crawl, or by matching the title against the brand
directory).
"""
from __future__ import annotations

import re

from selectolax.parser import HTMLParser

CARD = "div.ps-product-item"
THUMB_RE = re.compile(r"/fw/\d+/\d+/")


def thumb_url(image_url: str, size: int = 64) -> str:
    """The CDN renders any size on demand: /fw/300/300/ -> /fw/64/64/."""
    if not image_url:
        return ""
    return THUMB_RE.sub(f"/fw/{size}/{size}/", image_url, count=1)


def _money(text: str | None) -> int | None:
    if not text:
        return None
    digits = re.sub(r"[^\d]", "", text)
    return int(digits) if digits else None


def parse_listing(html: str) -> list[dict]:
    """Extract every product card on a listing page."""
    tree = HTMLParser(html)
    products = []
    for card in tree.css(CARD):
        marker = card.css_first("input.product-item__id")
        if marker is None:
            continue
        attrs = marker.attributes
        product_id = attrs.get("value")
        if not product_id:
            continue

        link = card.css_first("a.ps-product-item__name-url")
        img = card.css_first("img.ps-product-item__image-product__block-image")
        old = card.css_first(".old-price .value")
        badge = card.css_first(".ps-product-item__badge-list__message-status__text")

        image = img.attributes.get("src", "") if img else ""
        title = link.text(strip=True) if link else (img.attributes.get("alt", "") if img else "")

        products.append(
            {
                "product_id": int(product_id),
                "category_id": int(attrs.get("data-category-id") or 0) or None,
                "title": title,
                "url": link.attributes.get("href", "") if link else "",
                "image_url": image,
                "thumb_url": thumb_url(image),
                "price": int(attrs.get("data-price") or 0) or None,
                "old_price": _money(old.text(strip=True)) if old else None,
                "discount": int(attrs.get("data-discount") or 0),
                "badge": badge.text(strip=True) if badge else None,
            }
        )
    return products


def max_page(html: str) -> int:
    """Highest page number the pagination links to (1 if unpaginated)."""
    pages = [int(m) for m in re.findall(r"[?&]page=(\d+)", html)]
    return max(pages) if pages else 1


def subcategories(html: str) -> list[dict]:
    """Child categories shown on a parent category page.

    A page with children lists no products; a page without them is a leaf.
    """
    tree = HTMLParser(html)
    out = []
    for a in tree.css(".root-category-item a"):
        href = a.attributes.get("href", "")
        m = re.search(r"/c/(\d+)/([\w-]+)", href)
        if not m:
            continue
        out.append({"id": int(m.group(1)), "slug": m.group(2), "title": a.text(strip=True), "path": f"/c/{m.group(1)}/{m.group(2)}"})
    return out
