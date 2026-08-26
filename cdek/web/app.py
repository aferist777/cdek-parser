"""Local web app: the product list, the crawl controls, and exports.

Crawling runs in a worker thread so the list stays usable while it works; the
UI polls /api/run for progress.
"""
from __future__ import annotations

import logging
import threading
import webbrowser
from pathlib import Path

from fastapi import FastAPI
from fastapi.responses import FileResponse, JSONResponse
from fastapi.staticfiles import StaticFiles

from .. import catalog, config, crawl, export, query
from ..fetch import Fetcher
from ..store import Store

log = logging.getLogger(__name__)

cfg = config.load()
store = Store(config.resolve(cfg.paths.db))
STATIC = Path(__file__).parent / "static"

app = FastAPI(title="CDEK Sales")


class RunState:
    """The one crawl that may be in flight, and its progress."""

    def __init__(self):
        self.lock = threading.Lock()
        self.thread: threading.Thread | None = None
        self.crawler: crawl.Crawler | None = None
        self.progress: dict = {}
        self.status = "idle"          # idle | discovering | running | done | failed | stopped
        self.message = ""

    @property
    def busy(self) -> bool:
        return self.thread is not None and self.thread.is_alive()

    def snapshot(self) -> dict:
        return {"status": self.status, "message": self.message, "progress": self.progress, "busy": self.busy}


state = RunState()


def _crawl_worker(options: dict) -> None:
    """Discover, optionally measure, then crawl. Runs off the request thread."""
    run_cfg = config.load()
    run_cfg["crawl"]["min_discount"] = int(options.get("min_discount", 50))
    run_cfg["crawl"]["workers"] = max(1, min(int(options.get("workers", 4)), 16))
    run_cfg["crawl"]["max_pages_per_listing"] = int(options.get("max_pages", 0))
    wanted_roots = options.get("roots") or [r["key"] for r in run_cfg["roots"]]
    run_cfg["roots"] = [r for r in run_cfg["roots"] if r["key"] in wanted_roots]
    brand_ids = [int(b) for b in (options.get("brand_ids") or [])]

    run_store = Store(config.resolve(run_cfg.paths.db))
    try:
        state.status, state.message = "discovering", "Каталог"
        with Fetcher(run_cfg.site.base_url, run_cfg.crawl.timeout_s, run_cfg.crawl.retries) as fetcher:
            crawler = crawl.Crawler(fetcher, run_store, run_cfg, on_progress=lambda p: setattr(state, "progress", p))
            state.crawler = crawler
            leaves, brands_by_root = crawler.discover()
            jobs = crawl.build_jobs(leaves, brands_by_root, brand_ids)

            state.status, state.message = "running", f"{len(jobs)} листингов"
            result = crawler.run(jobs, resume=bool(options.get("resume", False)))
            state.progress = result
        state.status = "stopped" if crawler.stop_requested else "done"
        state.message = f"{result['products']} товаров, из них новых {result['new_products']}"
    except Exception as exc:
        log.exception("crawl failed")
        state.status, state.message = "failed", str(exc)[:300]
    finally:
        run_store.close()
        state.crawler = None


# -- API ------------------------------------------------------------------
@app.get("/api/facets")
def api_facets():
    last = store.last_run()
    return {
        **query.facets(store),
        "roots_config": [{"key": r["key"], "title": r["title"]} for r in cfg["roots"]],
        "last_run": dict(last) if last else None,
        "defaults": {"min_discount": cfg.crawl.min_discount, "workers": cfg.crawl.workers},
    }


@app.get("/api/products")
def api_products(
    root: str = "",
    brands: str = "",
    q: str = "",
    min_discount: int = 0,
    min_price: int = 0,
    max_price: int = 0,
    sort: str = "discount",
    offset: int = 0,
    limit: int = 0,
):
    filters = {
        "root": root,
        "brands": [b for b in brands.split("|") if b],
        "q": q,
        "min_discount": min_discount,
        "min_price": min_price,
        "max_price": max_price,
    }
    limit = limit or cfg.web.page_size
    return {
        "total": query.count(store, filters),
        "items": query.select(store, filters, sort, limit, offset),
        "offset": offset,
        "limit": limit,
    }


@app.get("/api/brands")
def api_brands(q: str = "", limit: int = 60):
    """Brand directory for the crawl picker: every brand the site offers."""
    # MAX, not SUM: a brand's count is per category, and summing 386 of them
    # produces meaningless millions. The largest single category reads as
    # "how big is this brand here".
    rows = store.db.execute(
        """SELECT b.id, b.name, COALESCE(MAX(cb.count), 0) AS cnt
           FROM brands b LEFT JOIN category_brands cb ON cb.brand_id = b.id
           WHERE (? = '' OR b.name LIKE ?)
           GROUP BY b.id, b.name ORDER BY cnt DESC LIMIT ?""",
        (q, f"%{q}%", limit),
    ).fetchall()
    return {"items": [{"id": r["id"], "name": r["name"], "count": r["cnt"]} for r in rows]}


@app.post("/api/run")
def api_run(options: dict):
    with state.lock:
        if state.busy:
            return JSONResponse({"error": "уже выполняется"}, status_code=409)
        state.progress, state.status, state.message = {}, "starting", ""
        state.thread = threading.Thread(target=_crawl_worker, args=(options,), daemon=True)
        state.thread.start()
    return state.snapshot()


@app.get("/api/run")
def api_run_status():
    return state.snapshot()


@app.post("/api/run/stop")
def api_run_stop():
    if state.crawler:
        state.crawler.stop_requested = True
        state.message = "Останавливаюсь после текущей страницы"
    return state.snapshot()


@app.get("/api/export")
def api_export(
    fmt: str = "csv",
    root: str = "",
    brands: str = "",
    q: str = "",
    min_discount: int = 0,
    min_price: int = 0,
    max_price: int = 0,
    sort: str = "discount",
):
    filters = {
        "root": root,
        "brands": [b for b in brands.split("|") if b],
        "q": q,
        "min_discount": min_discount,
        "min_price": min_price,
        "max_price": max_price,
    }
    out_dir = config.resolve(cfg.paths.exports + "/x").parent
    titles = {r["key"]: r["title"] for r in cfg["roots"]}
    writer = export.to_xlsx if fmt == "xlsx" else export.to_csv
    path = writer(store, filters, sort, out_dir, titles)
    return FileResponse(path, filename=path.name)


app.mount("/", StaticFiles(directory=STATIC, html=True), name="static")


def serve(open_browser: bool = True) -> None:
    import uvicorn

    url = f"http://{cfg.web.host}:{cfg.web.port}/"
    if open_browser:
        threading.Timer(1.0, lambda: webbrowser.open(url)).start()
    print(f"CDEK Sales -> {url}")
    uvicorn.run(app, host=cfg.web.host, port=cfg.web.port, log_level="warning")
