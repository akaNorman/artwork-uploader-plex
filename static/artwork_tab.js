// Fork (ranked mode): the Artwork tab - every ranked user's poster options per film, show and
// season, best-ranked first. Picking one pins it for the item (every library holding it) and
// applies it straight away; unpinning hands the slot back to the ranking.

(function () {
    const PAGE = 150;
    const $ = (id) => document.getElementById(id);
    const tab = $("artwork-tab");
    if (!tab) return;

    let items = [];
    let shown = PAGE;
    let selected = null;   // {kind, tmdb_id}
    let loading = false;

    const esc = (s) => String(s ?? "").replace(/[&<>"']/g, (c) => ({"&": "&amp;", "<": "&lt;", ">": "&gt;", '"': "&quot;", "'": "&#39;"}[c]));

    function notice(message, kind = "warning") {
        const box = $("artwork_notice");
        if (!message) { box.classList.add("d-none"); return; }
        box.className = `alert alert-${kind} small mt-3 mb-0`;
        box.textContent = message;
    }

    async function getJSON(url, options) {
        const response = await fetch(url, options);
        const body = await response.json().catch(() => ({}));
        if (!response.ok) throw new Error(body.error || `HTTP ${response.status}`);
        return body;
    }

    // --- the list ---------------------------------------------------------------------------
    async function loadItems(refresh = false) {
        if (loading) return;
        loading = true;
        $("artwork_count").textContent = "Listing your libraries - this takes a moment...";
        try {
            const data = await getJSON(`/api/artwork/items${refresh ? "?refresh=1" : ""}`);
            items = data.items;
            const libraries = [...new Set(items.flatMap((i) => i.libraries))].sort();
            const select = $("artwork_library");
            const keep = select.value;
            select.innerHTML = `<option value="">All libraries</option>` +
                libraries.map((l) => `<option value="${esc(l)}">${esc(l)}</option>`).join("");
            select.value = libraries.includes(keep) ? keep : "";
            notice(data.dry_run ? "Ranked mode dry run is on: picks are saved, but nothing is changed until the dry run is off." : "", "info");
            renderList();
        } catch (e) {
            items = [];
            renderList();
            notice(e.message);
        } finally {
            loading = false;
        }
    }

    function filtered() {
        const q = $("artwork_search").value.trim().toLowerCase();
        const kind = $("artwork_kind").value;
        const library = $("artwork_library").value;
        const choiceOnly = $("artwork_choice_only").checked;
        const pinnedOnly = $("artwork_pinned_only").checked;
        return items.filter((i) => (!q || (i.title || "").toLowerCase().includes(q))
            && (!kind || i.kind === kind)
            && (!library || i.libraries.includes(library))
            && (!choiceOnly || i.choice)
            && (!pinnedOnly || i.pinned));
    }

    function renderList() {
        const list = filtered();
        $("artwork_count").textContent = `${list.length.toLocaleString()} of ${items.length.toLocaleString()}`;
        $("artwork_list").innerHTML = list.slice(0, shown).map((i) => {
            const active = selected && selected.kind === i.kind && selected.tmdb_id === i.tmdb_id;
            return `<button type="button" class="list-group-item list-group-item-action d-flex justify-content-between align-items-start gap-2${active ? " active" : ""}"
                        data-kind="${esc(i.kind)}" data-tmdb="${i.tmdb_id}">
                    <span><span class="fw-semibold">${esc(i.title)}</span>${i.year ? ` <span class="opacity-75">(${i.year})</span>` : ""}
                        <br><span class="small opacity-75">${esc(i.libraries.join(" • "))}</span></span>
                    <span class="text-nowrap small">${i.pinned ? `<i class="bi bi-pin-angle-fill" title="${i.pinned} pinned"></i> ` : ""}${i.options} option${i.options === 1 ? "" : "s"}</span>
                </button>`;
        }).join("") || `<div class="text-muted small p-2">Nothing matches.</div>`;
        $("artwork_more").classList.toggle("d-none", list.length <= shown);
    }

    // --- one item ---------------------------------------------------------------------------
    async function openItem(kind, tmdbId, keepScroll = false) {
        selected = {kind, tmdb_id: tmdbId};
        renderList();
        const detail = $("artwork_detail");
        if (!keepScroll) detail.innerHTML = `<div class="text-muted small">Loading...</div>`;
        try {
            renderItem(await getJSON(`/api/artwork/item?kind=${encodeURIComponent(kind)}&tmdb_id=${tmdbId}`));
            // On a narrow screen the options sit below the list
            if (!keepScroll && window.innerWidth < 992) detail.scrollIntoView({behavior: "smooth", block: "start"});
        } catch (e) {
            detail.innerHTML = `<div class="alert alert-warning small">${esc(e.message)}</div>`;
        }
    }

    function renderItem(item) {
        const listed = items.find((i) => i.kind === item.kind && i.tmdb_id === item.tmdb_id);
        if (listed) {
            listed.pinned = item.slots.filter((s) => s.pinned).length;
            renderList();
        }
        const first = item.libraries[0];
        const bust = Date.now();
        const slots = item.slots.map((s) => {
            const options = s.options.map((o) => {
                const pinned = s.pinned === o.asset_id;
                const current = o.current_in.length > 0;
                const badges = [
                    pinned ? `<span class="badge text-bg-warning"><i class="bi bi-pin-angle-fill"></i> Pinned</span>` : "",
                    current ? `<span class="badge text-bg-success" title="${esc(o.current_in.join(", "))}">In use</span>` : "",
                ].join(" ");
                return `<div class="artwork-option${pinned ? " pinned" : ""}${current ? " current" : ""}">
                        <button type="button" class="artwork-pick" data-slot="${esc(s.slot)}" data-asset="${o.asset_id}" data-user="${esc(o.user)}" data-label="${esc(s.label)}"
                                title="Use ${esc(o.user)}'s poster">
                            <img loading="lazy" alt="${esc(o.user)} poster" src="/api/artwork/thumb/${o.asset_id}"
                                 onerror="this.replaceWith(Object.assign(document.createElement('div'), {className: 'artwork-thumb-missing', textContent: 'No preview'}))">
                        </button>
                        <div class="small mt-1"><span class="fw-semibold">${esc(o.user)}</span> <span class="opacity-75">#${o.rank}</span></div>
                        <div class="small">${badges} <a href="${esc(o.poster_page)}" target="_blank" rel="noopener" class="small">poster ${o.asset_id}</a></div>
                    </div>`;
            }).join("");
            const now = first ? `<div class="artwork-option artwork-now">
                    <img loading="lazy" alt="Current file" src="/api/artwork/current?library=${encodeURIComponent(first.library)}&rating_key=${first.rating_key}&slot=${encodeURIComponent(s.slot)}&_=${bust}"
                         onerror="this.replaceWith(Object.assign(document.createElement('div'), {className: 'artwork-thumb-missing', textContent: 'No file yet'}))">
                    <div class="small mt-1 fw-semibold">In your Kometa folder</div>
                    <div class="small opacity-75">${esc(first.library)}</div>
                </div>` : "";
            return `<section class="mb-4">
                    <div class="d-flex align-items-center gap-2 mb-2">
                        <h6 class="mb-0">${esc(s.label)}</h6>
                        ${s.pinned ? `<button type="button" class="btn btn-sm btn-outline-secondary py-0 artwork-unpin" data-slot="${esc(s.slot)}" data-label="${esc(s.label)}">
                            <i class="bi bi-pin-angle"></i> Unpin (back to the ranking)</button>` : `<span class="small text-muted">following the ranking</span>`}
                    </div>
                    <div class="artwork-options">${now}${options}</div>
                </section>`;
        }).join("");
        $("artwork_detail").innerHTML = `
            <div class="d-flex flex-wrap align-items-baseline gap-2 mb-3">
                <h5 class="mb-0">${esc(item.title)}${item.year ? ` (${item.year})` : ""}</h5>
                <span class="small text-muted">${esc(item.libraries.map((l) => l.library).join(" • "))}</span>
            </div>
            ${item.dry_run ? `<div class="alert alert-info small">Ranked mode dry run is on: a pick is saved but nothing changes until the dry run is off.</div>` : ""}
            <p class="small text-muted">Click a poster to use it. It's pinned for every library above and applied straight away; the file it replaces is moved to the backup folder.</p>
            ${slots || `<div class="text-muted small">No posters from your ranked users.</div>`}`;
    }

    async function send(path, body, what) {
        notice(`${what}...`, "info");
        try {
            const result = await getJSON(path, {method: "POST", headers: {"Content-Type": "application/json"}, body: JSON.stringify(body)});
            notice(result.message, result.applied ? "success" : "warning");
            await openItem(body.kind, body.tmdb_id, true);
        } catch (e) {
            notice(e.message);
        }
    }

    // --- wiring -----------------------------------------------------------------------------
    tab.addEventListener("shown.bs.tab", () => { if (!items.length) loadItems(); });
    $("artwork_refresh").addEventListener("click", () => loadItems(true));
    for (const id of ["artwork_kind", "artwork_library", "artwork_choice_only", "artwork_pinned_only"]) {
        $(id).addEventListener("change", () => { shown = PAGE; renderList(); });
    }
    $("artwork_search").addEventListener("input", () => { shown = PAGE; renderList(); });
    $("artwork_more").addEventListener("click", () => { shown += PAGE; renderList(); });
    $("artwork_list").addEventListener("click", (event) => {
        const row = event.target.closest("[data-tmdb]");
        if (row) openItem(row.dataset.kind, Number(row.dataset.tmdb));
    });
    $("artwork_detail").addEventListener("click", (event) => {
        if (!selected) return;
        const pick = event.target.closest(".artwork-pick");
        const unpin = event.target.closest(".artwork-unpin");
        if (pick) {
            if (pick.closest(".artwork-option").classList.contains("pinned")) return;
            if (!confirm(`Use ${pick.dataset.user}'s poster for ${pick.dataset.label}?\n\nIt's pinned for every library holding this item and applied now. The file it replaces is moved to the backup folder.`)) return;
            send("/api/artwork/pin", {...selected, slot: pick.dataset.slot, asset_id: Number(pick.dataset.asset)}, "Applying");
        } else if (unpin) {
            if (!confirm(`Unpin ${unpin.dataset.label} and go back to the ranking's choice?`)) return;
            send("/api/artwork/unpin", {...selected, slot: unpin.dataset.slot}, "Unpinning");
        }
    });
})();
