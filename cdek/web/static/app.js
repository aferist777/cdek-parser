"use strict";

const $ = (id) => document.getElementById(id);
const PAGE = 100;

let offset = 0;
let total = 0;
let loading = false;
let crawlBrands = [];      // brand ids chosen for the next crawl
let brandNameById = {};
let rootTitles = {};       // luxury -> Люкс
let pollTimer = null;

const money = (v) => (v == null ? "" : String(v).replace(/\B(?=(\d{3})+(?!\d))/g, " ") + "₽");

function filters() {
    return {
        root: $("f-root").value,
        brands: $("f-brand").value,
        q: $("f-q").value.trim(),
        min_discount: +$("f-discount").value || 0,
        min_price: +$("f-pmin").value || 0,
        max_price: +$("f-pmax").value || 0,
        sort: $("f-sort").value,
    };
}

function queryString(extra) {
    const params = new URLSearchParams({ ...filters(), ...extra });
    return params.toString();
}

// -- list -----------------------------------------------------------------
async function load(reset) {
    if (loading) return;
    loading = true;
    if (reset) { offset = 0; $("rows").innerHTML = ""; }

    const res = await fetch("/api/products?" + queryString({ offset, limit: PAGE }));
    const data = await res.json();
    total = data.total;
    render(data.items);
    offset += data.items.length;

    $("count").textContent = total
        ? `${total.toLocaleString("ru")} товаров, показано ${offset.toLocaleString("ru")}`
        : "ничего не найдено";
    $("more").innerHTML = "";
    if (offset < total) {
        const btn = document.createElement("button");
        btn.textContent = `Показать ещё ${Math.min(PAGE, total - offset)}`;
        btn.onclick = () => load(false);
        $("more").appendChild(btn);
    }
    loading = false;
}

function render(items) {
    const body = $("rows");
    const frag = document.createDocumentFragment();
    for (const p of items) {
        const tr = document.createElement("tr");
        tr.ondblclick = () => window.open(p.url, "_blank");

        const img = document.createElement("td");
        img.className = "c-img";
        if (p.thumb_url) {
            const el = document.createElement("img");
            el.src = p.thumb_url;
            el.loading = "lazy";
            el.alt = "";
            img.appendChild(el);
        }

        const name = document.createElement("td");
        name.className = "name";
        const link = document.createElement("a");
        link.href = p.url;
        link.target = "_blank";
        link.textContent = p.title;
        const meta = document.createElement("div");
        meta.className = "meta";
        meta.textContent = [p.brand, rootTitles[p.root] || p.root, p.badge].filter(Boolean).join(" · ");
        name.append(link, meta);

        const disc = document.createElement("td");
        disc.className = "c-disc disc";
        disc.textContent = p.discount ? "−" + p.discount + "%" : "";

        const price = document.createElement("td");
        price.className = "c-price price";
        price.textContent = money(p.price);

        const old = document.createElement("td");
        old.className = "c-old old";
        old.textContent = p.old_price ? money(p.old_price) : "";

        tr.append(img, name, disc, price, old);
        frag.appendChild(tr);
    }
    body.appendChild(frag);
}

// -- facets ---------------------------------------------------------------
async function loadFacets() {
    const data = await fetch("/api/facets").then((r) => r.json());
    const titles = {};
    for (const r of data.roots_config) titles[r.key] = r.title;
    rootTitles = titles;

    const rootSel = $("f-root");
    const keepRoot = rootSel.value;
    rootSel.innerHTML = "<option value=''>все</option>";
    for (const r of data.roots) {
        rootSel.insertAdjacentHTML("beforeend",
            `<option value="${r.key}">${titles[r.key] || r.key} (${r.count.toLocaleString("ru")})</option>`);
    }
    rootSel.value = keepRoot;

    const brandSel = $("f-brand");
    const keepBrand = brandSel.value;
    brandSel.innerHTML = "<option value=''>все</option>";
    for (const b of data.brands) {
        brandSel.insertAdjacentHTML("beforeend",
            `<option value="${b.name}">${b.name} (${b.count.toLocaleString("ru")})</option>`);
    }
    brandSel.value = keepBrand;

    if (!$("r-roots").dataset.built) {
        for (const r of data.roots_config) {
            const id = "root-" + r.key;
            $("r-roots").insertAdjacentHTML("beforeend",
                `<label><input type="checkbox" class="r-root" id="${id}" value="${r.key}" checked> ${r.title}</label>`);
        }
        $("r-roots").dataset.built = "1";
        $("r-discount").value = data.defaults.min_discount;
        $("r-workers").value = data.defaults.workers;
    }

    const run = data.last_run;
    $("lastrun").textContent = run && run.finished_at
        ? `последний парсинг: ${run.finished_at.slice(0, 16).replace("T", " ")} · ${run.products} товаров · ${run.status}`
        : "парсинг ещё не запускался";
}

async function loadBrandList(term) {
    const data = await fetch("/api/brands?limit=60&q=" + encodeURIComponent(term || "")).then((r) => r.json());
    const list = $("brand-list");
    list.innerHTML = "";
    for (const b of data.items) {
        brandNameById[b.id] = b.name;
        list.insertAdjacentHTML("beforeend", `<option value="${b.name}" data-id="${b.id}"></option>`);
    }
    return data.items;
}

function renderChips() {
    const box = $("r-brand-chips");
    box.innerHTML = "";
    for (const id of crawlBrands) {
        const chip = document.createElement("span");
        chip.className = "chip";
        chip.textContent = brandNameById[id] || id;
        const kill = document.createElement("a");
        kill.href = "#";
        kill.textContent = "×";
        kill.onclick = (e) => { e.preventDefault(); crawlBrands = crawlBrands.filter((x) => x !== id); renderChips(); };
        chip.appendChild(kill);
        box.appendChild(chip);
    }
}

// -- crawl ----------------------------------------------------------------
async function startRun() {
    const options = {
        min_discount: +$("r-discount").value || 0,
        workers: +$("r-workers").value || 4,
        max_pages: +$("r-maxpages").value || 0,
        roots: [...document.querySelectorAll(".r-root:checked")].map((c) => c.value),
        brand_ids: crawlBrands,
        resume: $("r-resume").checked,
    };
    if (!options.roots.length) { setStatus("выберите хотя бы один раздел", true); return; }

    const res = await fetch("/api/run", {
        method: "POST",
        headers: { "Content-Type": "application/json" },
        body: JSON.stringify(options),
    });
    if (!res.ok) { setStatus((await res.json()).error || "не удалось запустить", true); return; }
    poll();
}

function setStatus(text, isError) {
    $("r-status").textContent = text;
    $("r-status").className = isError ? "err" : "";
}

function lockControls(busy) {
    $("btn-run").disabled = busy;
    $("btn-stop").disabled = !busy;
    for (const el of document.querySelectorAll("#cell-crawl input")) el.disabled = busy;
}

async function poll() {
    const s = await fetch("/api/run").then((r) => r.json());
    lockControls(s.busy);

    const p = s.progress || {};
    if (s.busy) {
        const jobs = p.jobs_total ? ` · ${p.jobs_done}/${p.jobs_total} листингов` : "";
        const speed = p.pages_per_min ? ` · ${p.pages_per_min} стр/мин` : "";
        const eta = p.jobs_total && p.jobs_done
            ? ` · осталось ~${Math.round((p.elapsed_s / p.jobs_done) * (p.jobs_total - p.jobs_done) / 60)} мин`
            : "";
        setStatus(`${s.message || s.status}${jobs} · ${p.pages || 0} стр · ${p.products || 0} товаров${speed}${eta} · ${p.current || ""}`);
        if (!pollTimer) pollTimer = setInterval(poll, 2000);
        if ((p.pages || 0) % 20 < 5) load(true);
    } else {
        clearInterval(pollTimer);
        pollTimer = null;
        if (s.status !== "idle") {
            setStatus(`${s.status}: ${s.message}`, s.status === "failed");
            loadFacets();
            load(true);
        }
    }
}

// -- wiring ---------------------------------------------------------------
function wire() {
    // Filters apply immediately; no Apply button.
    for (const id of ["f-root", "f-brand", "f-sort"]) $(id).onchange = () => load(true);
    let debounce;
    for (const id of ["f-discount", "f-pmin", "f-pmax", "f-q"]) {
        $(id).oninput = () => { clearTimeout(debounce); debounce = setTimeout(() => load(true), 300); };
    }
    $("btn-reset").onclick = () => {
        $("f-root").value = ""; $("f-brand").value = ""; $("f-q").value = "";
        $("f-discount").value = 0; $("f-pmin").value = ""; $("f-pmax").value = "";
        $("f-sort").value = "discount";
        load(true);
    };

    $("btn-run").onclick = startRun;
    $("btn-stop").onclick = () => fetch("/api/run/stop", { method: "POST" }).then(poll);

    const brandInput = $("r-brand-input");
    let brandDebounce;
    brandInput.oninput = async () => {
        clearTimeout(brandDebounce);
        const term = brandInput.value.trim();
        const items = await loadBrandList(term);
        const exact = items.find((b) => b.name.toLowerCase() === term.toLowerCase());
        if (exact) {
            if (!crawlBrands.includes(exact.id)) crawlBrands.push(exact.id);
            brandNameById[exact.id] = exact.name;
            brandInput.value = "";
            renderChips();
        } else {
            brandDebounce = setTimeout(() => loadBrandList(term), 250);
        }
    };

    for (const [id, fmt] of [["exp-csv", "csv"], ["exp-xlsx", "xlsx"]]) {
        $(id).onclick = (e) => { e.preventDefault(); window.location = "/api/export?" + queryString({ fmt }); };
    }
}

wire();
loadFacets();
loadBrandList("");
load(true);
poll();
