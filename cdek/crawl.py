"""The crawler.

Work is a flat list of listings: (category or landing) x (optional brand).
Each listing is paged through with is_sale=1 and sort-by=sale_desc, so the
biggest discounts arrive first and a min_discount threshold can stop a listing
early. Progress is checkpointed after every page, so an interrupted run
resumes instead of starting over.
"""
from __future__ import annotations

import json
import re
import logging
import time
from dataclasses import dataclass, field
from pathlib import Path

from . import catalog, parse
from .config import sleep_span

log = logging.getLogger(__name__)


@dataclass
class Job:
    """One listing to page through."""
    root: str
    path: str
    title: str
    category_id: int | None = None
    brand_id: int | None = None
    brand: str | None = None
    pages: int = 0          # discovered on the first page

    @property
    def key(self) -> str:
        return f"{self.path}|{self.brand_id or ''}"


@dataclass
class Progress:
    pages: int = 0
    products: int = 0
    new_products: int = 0
    jobs_done: int = 0
    jobs_total: int = 0
    current: str = ""
    started: float = field(default_factory=time.time)

    def as_dict(self) -> dict:
        elapsed = time.time() - self.started
        return {
            "pages": self.pages,
            "products": self.products,
            "new_products": self.new_products,
            "jobs_done": self.jobs_done,
            "jobs_total": self.jobs_total,
            "current": self.current,
            "elapsed_s": round(elapsed),
            "pages_per_min": round(self.pages / elapsed * 60, 1) if elapsed > 1 else 0,
        }


TOKEN_RE = re.compile(r"[a-zA-Zа-яА-ЯёЁ0-9&+']+")
APOSTROPHES = str.maketrans({"’": "'", "‘": "'", "`": "'", "´": "'"})


def _tokens(text: str) -> list[str]:
    """Lowercase word tokens, with ampersands split off.

    Both spellings of "Dolce&Gabbana" have to tokenise identically, or the
    brand in a title never matches the brand in the directory.
    """
    return TOKEN_RE.findall(text.translate(APOSTROPHES).replace("&", " & ").lower())


class BrandResolver:
    """Maps a product title onto a known brand.

    Listing cards carry no brand, so a listing not filtered by brand has to
    recognise the brand inside the title. Candidates are indexed by their first
    word, then ranked: more words matched wins first ("Air Jordan" over
    "Jordan"), and on a tie the bigger catalogue presence wins ("Nike Air Zoom
    Pegasus" resolves to Nike, not to the much smaller brand Pegasus).
    """

    def __init__(self, directory: list[tuple[int, str, int]]):
        self.index: dict[str, list[tuple[list[str], int, str, int]]] = {}
        for bid, name, count in directory:
            words = _tokens(name)
            if not words or len(name) < 3:
                continue
            self.index.setdefault(words[0], []).append((words, bid, name, count))

    def resolve(self, title: str) -> tuple[int | None, str | None]:
        words = _tokens(title)
        best = None
        for i, word in enumerate(words):
            for brand_words, bid, name, count in self.index.get(word, ()):
                span = len(brand_words)
                if words[i:i + span] != brand_words:
                    continue
                score = (span, count)
                if best is None or score > best[0]:
                    best = (score, bid, name)
        return (best[1], best[2]) if best else (None, None)


def build_jobs(leaves: list[dict], brands_by_root: dict[str, list[dict]], wanted_brand_ids: list[int]) -> list[Job]:
    """Turn discovered leaves into crawl jobs.

    With no brand filter configured, each leaf is one job. With brands
    configured, each leaf is split per brand, which also labels every product
    with its brand for free.
    """
    jobs: list[Job] = []
    for leaf in leaves:
        if not wanted_brand_ids or not leaf.get("brand_filter"):
            jobs.append(Job(root=leaf["root"], path=leaf["path"], title=leaf["title"], category_id=leaf.get("id")))
            continue
        names = {b["id"]: b["name"] for b in brands_by_root.get(leaf["root"], [])}
        for brand_id in wanted_brand_ids:
            jobs.append(
                Job(
                    root=leaf["root"],
                    path=leaf["path"],
                    title=leaf["title"],
                    category_id=leaf.get("id"),
                    brand_id=brand_id,
                    brand=names.get(brand_id),
                )
            )
    return jobs


class Checkpoint:
    """Remembers finished jobs and the page a job stopped on."""

    def __init__(self, path: str | Path):
        self.path = Path(path)
        self.data = {"done": [], "partial": {}}
        if self.path.exists():
            try:
                self.data = json.loads(self.path.read_text(encoding="utf-8"))
            except Exception:
                log.warning("unreadable checkpoint, starting fresh")
        self.done = set(self.data.get("done", []))
        self.partial = self.data.get("partial", {})

    def save(self) -> None:
        self.path.parent.mkdir(parents=True, exist_ok=True)
        self.path.write_text(
            json.dumps({"done": sorted(self.done), "partial": self.partial}, ensure_ascii=False),
            encoding="utf-8",
        )

    def reset(self) -> None:
        self.done, self.partial = set(), {}
        self.save()


class Crawler:
    def __init__(self, fetcher, store, cfg, on_progress=None):
        self.f = fetcher
        self.store = store
        self.cfg = cfg
        self.on_progress = on_progress
        self.progress = Progress()
        self.checkpoint = Checkpoint(cfg.paths.state)
        self.resolver = BrandResolver(store.brand_directory())
        self.stop_requested = False

    # -- discovery ---------------------------------------------------------
    def discover(self) -> tuple[list[dict], dict[str, list[dict]]]:
        leaves, brands_by_root = [], {}
        for root in self.cfg.roots:
            found = catalog.discover(self.f, root, self.cfg.crawl.only_sale,
                                     self.cfg.crawl.get("brand_sample_leaves", 0))
            brands_by_root[root["key"]] = found["brands"]
            for leaf in found["leaves"]:
                self.store.save_category({**leaf, "total": leaf.get("count"), "brands": found["brands"]})
                leaves.append(leaf)
        # Titles are matched against every brand seen anywhere, not just this
        # root's, so a perfume label still resolves inside a landing listing.
        self.resolver = BrandResolver(self.store.brand_directory())
        return leaves, brands_by_root

    def measure(self, jobs: list[Job], workers: int | None = None) -> list[Job]:
        """Fetch page 1 of every job to learn how deep each listing goes.

        Cheap relative to the crawl itself, and it turns "how big is this?"
        from a guess into a number before any long run starts.
        """
        workers = workers or self.cfg.crawl.workers
        for start in range(0, len(jobs), workers):
            batch = jobs[start:start + workers]
            urls = [catalog.listing_url(j.path, 1, self.cfg.crawl.only_sale, self.cfg.crawl.sort_by, j.brand_id) for j in batch]
            for job, html in zip(batch, self.f.get_many(urls, workers)):
                job.pages = parse.max_page(html) if html else 0
            time.sleep(sleep_span(self.cfg.crawl.delay_ms))
        return jobs

    # -- crawling ----------------------------------------------------------
    def run(self, jobs: list[Job], resume: bool = True) -> dict:
        if not resume:
            self.checkpoint.reset()
        pending = [j for j in jobs if j.key not in self.checkpoint.done]
        self.progress = Progress(jobs_total=len(jobs), jobs_done=len(jobs) - len(pending))
        run_id = self.store.start_run()
        log.info("crawling %s listings (%s already done)", len(pending), self.progress.jobs_done)

        try:
            for job in pending:
                if self.stop_requested:
                    break
                self._crawl_job(job)
                self.checkpoint.done.add(job.key)
                self.checkpoint.partial.pop(job.key, None)
                self.checkpoint.save()
                self.progress.jobs_done += 1
                self._report(job.title)
            status = "stopped" if self.stop_requested else "ok"
        except Exception as exc:
            log.exception("crawl failed")
            self.store.finish_run(run_id, "failed", self.progress.pages, self.progress.products,
                                  self.progress.new_products, str(exc)[:400])
            raise

        # The directory grows during a run, so titles skipped early can resolve now.
        relabelled = self.relabel_unbranded()
        if relabelled:
            log.info("brand resolved for %s products after the fact", relabelled)

        self.store.finish_run(run_id, status, self.progress.pages, self.progress.products, self.progress.new_products)
        self.store.set_meta("last_success", self.progress.as_dict())
        return self.progress.as_dict()

    def relabel_unbranded(self) -> int:
        """Re-run brand recognition over products still missing a brand."""
        resolver = BrandResolver(self.store.brand_directory())
        rows = self.store.db.execute(
            "SELECT product_id, title FROM products WHERE brand IS NULL"
        ).fetchall()
        fixed = 0
        for row in rows:
            brand_id, brand = resolver.resolve(row["title"])
            if brand:
                self.store.db.execute(
                    "UPDATE products SET brand_id=?, brand=? WHERE product_id=?",
                    (brand_id, brand, row["product_id"]),
                )
                fixed += 1
        self.store.db.commit()
        return fixed

    def _crawl_job(self, job: Job) -> None:
        cfg = self.cfg.crawl
        workers = cfg.workers
        page = self.checkpoint.partial.get(job.key, 1)
        last_page = job.pages or 10**6
        if cfg.max_pages_per_listing:
            last_page = min(last_page, cfg.max_pages_per_listing)

        while page <= last_page and not self.stop_requested:
            batch = list(range(page, min(page + workers, last_page + 1)))
            urls = [catalog.listing_url(job.path, p, cfg.only_sale, cfg.sort_by, job.brand_id) for p in batch]
            pages = self.f.get_many(urls, workers)

            batch_items, exhausted = [], False
            for html in pages:
                if not html:
                    continue
                self.progress.pages += 1
                if not job.pages:
                    job.pages = parse.max_page(html)
                    last_page = min(job.pages, cfg.max_pages_per_listing or job.pages)
                items = parse.parse_listing(html)
                if not items:
                    exhausted = True
                    break
                for item in items:
                    if cfg.min_discount and item["discount"] < cfg.min_discount:
                        exhausted = True   # sorted by discount: the rest are smaller
                        break
                    batch_items.append(self._label(item, job))
                if exhausted:
                    break

            if batch_items:
                written, fresh = self.store.upsert_products(batch_items)
                self.progress.products += written
                self.progress.new_products += fresh

            page = batch[-1] + 1
            self.checkpoint.partial[job.key] = page
            self._report(f"{job.title} p{page-1}/{job.pages or '?'}")
            if exhausted:
                break
            time.sleep(sleep_span(cfg.delay_ms))

    def _label(self, item: dict, job: Job) -> dict:
        """Attach root/brand to a parsed card."""
        item["root"] = job.root
        if job.brand_id:
            item["brand_id"], item["brand"] = job.brand_id, job.brand
        else:
            item["brand_id"], item["brand"] = self.resolver.resolve(item["title"])
        return item

    def _report(self, current: str) -> None:
        self.progress.current = current
        if self.on_progress:
            self.on_progress(self.progress.as_dict())
