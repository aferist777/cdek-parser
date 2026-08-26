"""Measure real crawl volume: pages per listing, before any long run."""
import logging, time, json
from cdek import config, crawl
from cdek.fetch import Fetcher
from cdek.store import Store

logging.basicConfig(level=logging.WARNING, format="%(levelname)s %(message)s")
cfg = config.load()
store = Store(config.resolve(cfg.paths.db))

with Fetcher(cfg.site.base_url, cfg.crawl.timeout_s, cfg.crawl.retries) as f:
    c = crawl.Crawler(f, store, cfg)
    leaves, brands = c.discover()
    jobs = crawl.build_jobs(leaves, brands, [])
    print(f"listings to measure: {len(jobs)}")
    t = time.time()
    jobs = c.measure(jobs)
    print(f"measured in {time.time()-t:.0f}s\n")

by_root = {}
for j in jobs:
    by_root.setdefault(j.root, []).append(j)

grand = 0
for root, js in by_root.items():
    pages = sum(x.pages for x in js)
    grand += pages
    empty = sum(1 for x in js if x.pages == 0)
    print(f"{root:<10} listings={len(js):>4} empty={empty:>3} pages={pages:>6} products~{pages*60:>9,}")
    for j in sorted(js, key=lambda x: -x.pages)[:5]:
        print(f"    {j.pages:>5}p  {j.title[:34]:<34} {j.path}")

print(f"\nTOTAL pages={grand:,}  products~{grand*60:,}")
print(f"at 0.7s/page with 4 workers: ~{grand*0.7/60:.0f} min")
json.dump([{"root": j.root, "path": j.path, "title": j.title, "pages": j.pages} for j in jobs],
          open("data/volume.json", "w", encoding="utf-8"), ensure_ascii=False, indent=1)
store.close()
