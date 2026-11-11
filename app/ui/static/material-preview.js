const materialPreview = document.querySelector("#material-preview");
const seenCards = new WeakSet();
const dailyTrack = document.querySelector("#daily-track");
const VIEWED_STORAGE_KEY = "alfa_viewed_materials";

function getSessionViewedIds() {
    try {
        const raw = sessionStorage.getItem(VIEWED_STORAGE_KEY);
        const parsed = raw ? JSON.parse(raw) : [];
        return Array.isArray(parsed) ? parsed.filter((id) => typeof id === "string" && id) : [];
    } catch {
        return [];
    }
}

function addSessionViewedId(materialId) {
    if (!materialId) return;
    try {
        const existing = getSessionViewedIds().filter((id) => id !== materialId);
        existing.unshift(materialId);
        sessionStorage.setItem(VIEWED_STORAGE_KEY, JSON.stringify(existing.slice(0, 50)));
    } catch {}
}

if (dailyTrack) {
    const dragHint = document.querySelector("#daily-drag-hint");
    const originals = [...dailyTrack.children];
    let clones = [];
    let cycle = 0;
    let scrollTimer;
    let drag;
    let suppressClick = false;
    const modulo = (value, width) => ((value % width) + width) % width;
    const jump = (left) => dailyTrack.scrollTo({ left, behavior: "instant" });
    const inMainCycle = (left) => cycle + modulo(left - cycle, cycle);
    const cloneCards = () => originals.map((card) => {
        const copy = card.cloneNode(true);
        copy.setAttribute("aria-hidden", "true");
        copy.querySelector(".material-link").tabIndex = -1;
        return copy;
    });
    const arrange = () => {
        if (!originals.length) {
            return;
        }
        const first = originals[0];
        const last = originals.at(-1);
        const overflow = last.offsetLeft + last.offsetWidth - first.offsetLeft > dailyTrack.clientWidth - 6;
        dragHint.hidden = !overflow;
        dailyTrack.classList.toggle("daily-interactive", overflow);
        if (!overflow) {
            clones.forEach((card) => card.remove());
            clones = [];
            cycle = 0;
            jump(0);
            return;
        }
        const progress = cycle ? modulo(dailyTrack.scrollLeft - cycle, cycle) / cycle : 0;
        if (!clones.length) {
            const preceding = cloneCards();
            const following = cloneCards();
            dailyTrack.prepend(...preceding);
            dailyTrack.append(...following);
            clones = [...preceding, ...following];
        }
        cycle = first.offsetLeft - clones[0].offsetLeft;
        jump(cycle * (1 + progress));
    };
    const recenter = () => {
        const left = dailyTrack.scrollLeft;
        if (cycle && !drag?.active && (left < cycle - 1 || left >= 2 * cycle)) {
            jump(inMainCycle(left));
        }
    };

    dailyTrack.addEventListener("pointerdown", (event) => {
        if (!cycle || !event.isPrimary || event.button !== 0 || !event.target.closest(".daily-card")) {
            return;
        }
        suppressClick = false;
        drag = {
            id: event.pointerId, x: event.clientX, y: event.clientY, left: dailyTrack.scrollLeft,
            started: performance.now(), touch: event.pointerType === "touch", active: false,
        };
    });
    dailyTrack.addEventListener("pointermove", (event) => {
        if (!drag || drag.id !== event.pointerId) {
            return;
        }
        const distance = event.clientX - drag.x;
        if (!drag.active) {
            if (Math.abs(event.clientY - drag.y) > 8) {
                drag = undefined;
                return;
            }
            if (Math.abs(distance) < 6) {
                return;
            }
            if (drag.touch && performance.now() - drag.started < 180) {
                drag = undefined;
                return;
            }
            drag.active = true;
            dailyTrack.setPointerCapture(event.pointerId);
            dailyTrack.classList.add("daily-dragging");
        }
        jump(inMainCycle(drag.left - distance));
    });
    const finishDrag = (event) => {
        if (!drag || drag.id !== event.pointerId) {
            return;
        }
        suppressClick = drag.active;
        drag = undefined;
        dailyTrack.classList.remove("daily-dragging");
        recenter();
    };
    dailyTrack.addEventListener("pointerup", finishDrag);
    dailyTrack.addEventListener("pointercancel", finishDrag);
    dailyTrack.addEventListener("lostpointercapture", finishDrag);
    dailyTrack.addEventListener("pointerleave", () => {
        if (!drag?.active) {
            drag = undefined;
        }
    });
    dailyTrack.addEventListener("click", (event) => {
        if (suppressClick && event.detail) {
            event.preventDefault();
            event.stopPropagation();
            suppressClick = false;
        }
    }, { capture: true });
    dailyTrack.addEventListener("keydown", (event) => {
        if (cycle && (event.key === "ArrowLeft" || event.key === "ArrowRight")) {
            const step = originals[1].offsetLeft - originals[0].offsetLeft;
            event.preventDefault();
            jump(inMainCycle(dailyTrack.scrollLeft + (event.key === "ArrowLeft" ? -step : step)));
        }
    });
    dailyTrack.addEventListener("scroll", () => {
        clearTimeout(scrollTimer);
        scrollTimer = setTimeout(recenter, 120);
    }, { passive: true });
    window.addEventListener("resize", arrange);
    arrange();
}

const catalogPages = new Map();
const exposureTimers = new Map();
const SKIP_EXPOSURE_MS = 1500;

function rememberCatalogPage() {
    const section = document.querySelector("#recommendations");
    const scope = section?.dataset.pageScope;
    if (!scope) return null;
    if (!catalogPages.has(scope)) {
        if (catalogPages.size >= 8) catalogPages.delete(catalogPages.keys().next().value);
        catalogPages.set(scope, { pages: new Map(), exposed: new Set() });
    }
    const state = catalogPages.get(scope);
    const ids = [...section.querySelectorAll(".card-grid .card")]
        .map(card => card.dataset.materialId).filter(Boolean);
    const page = section.dataset.resultPage;
    const previous = state.pages.get(page);
    if (page === "1" && previous && previous.join(",") !== ids.join(",")) {
        state.pages.clear();
        state.exposed.clear();
    }
    if (ids.length) state.pages.set(page, ids);
    return state;
}

const impressionObserver = "IntersectionObserver" in window ? new IntersectionObserver((entries, observer) => {
    const grid = document.querySelector("#recommendations .card-grid");
    if (!grid || document.visibilityState !== "visible" || materialPreview?.open) return;
    const state = catalogPages.get(grid.closest("#recommendations").dataset.pageScope);
    const tickets = [];
    for (const entry of entries) {
        const card = entry.target;
        if (entry.intersectionRatio < 0.5 || !grid.contains(card)) {
            clearTimeout(exposureTimers.get(card));
            exposureTimers.delete(card);
            continue;
        }
        if (!seenCards.has(card)) {
            seenCards.add(card);
            const link = card.querySelector(".material-link");
            tickets.push(new URL(link.href).searchParams.get("impression"));
        }
        const id = card.dataset.materialId;
        if (!state || state.exposed.has(id)) {
            observer.unobserve(card);
        } else if (!exposureTimers.has(card)) {
            exposureTimers.set(card, setTimeout(() => {
                exposureTimers.delete(card);
                if (card.isConnected && document.visibilityState === "visible" && !materialPreview?.open) {
                    state.exposed.add(id);
                    observer.unobserve(card);
                }
            }, SKIP_EXPOSURE_MS));
        }
    }
    for (let start = 0; start < tickets.length; start += 16) {
        const body = new URLSearchParams({ csrf_token: grid.dataset.impressionCsrf });
        for (const ticket of tickets.slice(start, start + 16)) {
            body.append("impression", ticket);
        }
        fetch(grid.dataset.impressionUrl, { method: "POST", body, keepalive: true }).catch(() => {});
    }
}, { threshold: 0.5 }) : null;

function prepareCards() {
    const grid = document.querySelector("#recommendations .card-grid");
    const state = rememberCatalogPage();
    if (typeof materialPreview?.showModal === "function") {
        grid?.classList.add("has-material-preview");
    }
    impressionObserver?.disconnect();
    for (const timeout of exposureTimers.values()) clearTimeout(timeout);
    exposureTimers.clear();
    if (!impressionObserver || !grid || document.visibilityState !== "visible" || materialPreview?.open) {
        return;
    }
    for (const card of grid.querySelectorAll(".card")) {
        if (!seenCards.has(card) || (state && !state.exposed.has(card.dataset.materialId))) {
            impressionObserver.observe(card);
        }
    }
}

document.addEventListener("visibilitychange", prepareCards);
document.addEventListener("error", (event) => {
    if (event.target instanceof HTMLImageElement && event.target.classList.contains("card-cover-image")) {
        event.target.hidden = true;
    }
}, true);
prepareCards();

let activeUpdate;
document.addEventListener("submit", async (event) => {
    const form = event.target;
    const isQuery = form.id === "recommendation-query";
    if (!isQuery && form.id !== "result-orders") {
        return;
    }
    event.preventDefault();
    activeUpdate?.abort();
    const update = new AbortController();
    activeUpdate = update;
    const section = document.querySelector("#recommendations");
    const status = document.querySelector("#recommendation-status");
    const submitter = event.submitter;
    const isPageChange = submitter?.name === "show_page";
    const data = new FormData(form);
    if (submitter?.name) {
        data.append(submitter.name, submitter.value);
    }
    if (isPageChange) {
        const state = rememberCatalogPage();
        if (state) {
            data.set("page_scope", section.dataset.pageScope);
            for (const [page, ids] of state.pages) data.append("visited_page", `${page}:${ids.join(",")}`);
            data.set("exposed_ids", [...state.exposed].join(","));
            data.set("seen_ids", getSessionViewedIds().slice(0, 40).join(","));
        }
    }
    status.textContent = "";
    section.setAttribute("aria-busy", "true");
    if (submitter?.classList?.contains("format-chip")) {
        const bar = submitter.closest(".format-filters-bar");
        if (bar) {
            bar.querySelectorAll(".format-chip").forEach(btn => {
                btn.classList.remove("active");
                btn.setAttribute("aria-pressed", "false");
            });
            submitter.classList.add("active");
            submitter.setAttribute("aria-pressed", "true");
        }
    } else if (isPageChange) {
        submitter.disabled = true;
        submitter.classList.add("is-loading");
    }
    try {
        const action = submitter?.getAttribute("formaction") ?? form.getAttribute("action");
        const response = await fetch(action || window.location.href, {
            method: "POST",
            headers: { "X-Recommendation-Fragment": "1" },
            body: new URLSearchParams(data),
            signal: update.signal,
        });
        if (!response.ok) {
            throw new Error("Не удалось обновить рекомендации");
        }
        const fragment = document.createElement("template");
        fragment.innerHTML = await response.text();
        const next = fragment.content.querySelector("#recommendations");
        if (update.signal.aborted) {
            return;
        }
        if (!next) {
            throw new Error("Выдача недоступна");
        }

        section.replaceWith(next);
        prepareCards();

        const query = document.querySelector("#recommendation-query");
        const controls = next.querySelector("#result-orders");
        const queryAccess = query.querySelector("#query-access-filters");
        if (queryAccess) {
            queryAccess.querySelectorAll('input[name="access_filter"]').forEach(input => input.remove());
            for (const checkbox of controls.querySelectorAll('input[name="access_filter"]:checked')) {
                const input = document.createElement("input");
                input.type = "hidden";
                input.name = "access_filter";
                input.value = checkbox.value;
                queryAccess.append(input);
            }
        }
        const carriedFields = ["result_order", "format_filter", "page_size"];
        if (isQuery) {
            carriedFields.push("goal", "stage", "industry", "experience", "team_size", "learning_topic", "preferred_format");
        }
        for (const name of carriedFields) {
            const source = controls.elements.namedItem(name === "result_order" ? "current_order" : name);
            query.elements.namedItem(name).value = source.value;
        }
        if (isQuery) {
            const sampleHistory = query.querySelector('input[name="history"]');
            if (sampleHistory) {
                sampleHistory.checked = Boolean(controls.elements.namedItem("history"));
            }
            for (const button of query.querySelectorAll('button[name="quick_goal"]')) {
                button.classList.toggle("chip-selected", button.value === query.elements.goal.value);
            }
            next.scrollIntoView({ block: "start" });
        } else if (isPageChange) {
            next.scrollIntoView({ behavior: "smooth", block: "start" });
            (next.querySelector(".card") || next.querySelector("#results-title"))?.focus({ preventScroll: true });
        } else {
            const matching = [...next.querySelectorAll("button")].find(
                (button) => button.name === submitter?.name && button.value === submitter?.value,
            );
            matching?.focus({ preventScroll: true });
        }
    } catch {
        if (!update.signal.aborted) {
            status.textContent = "Не удалось обновить подборку. Попробуйте ещё раз.";
            if (!isPageChange) {
                status.scrollIntoView({ block: "nearest" });
            }
        }
    } finally {
        if (activeUpdate === update) {
            section.removeAttribute("aria-busy");
            activeUpdate = undefined;
        }
        if (submitter) {
            submitter.disabled = false;
            submitter.classList.remove("is-loading");
        }
    }
});

if (typeof materialPreview?.showModal === "function") {
    const previewContent = materialPreview.querySelector(".material-preview-content");
    const previewEyebrow = materialPreview.querySelector("#preview-eyebrow");
    const previewTitle = materialPreview.querySelector("#preview-title");
    const previewAuthors = materialPreview.querySelector("#preview-authors");
    const previewSubscription = materialPreview.querySelector("#preview-subscription");
    const previewAccess = materialPreview.querySelector("#preview-access-note");
    const previewDescription = materialPreview.querySelector("#preview-description");
    const previewProgram = materialPreview.querySelector("#preview-program");
    const previewNextSteps = materialPreview.querySelector("#preview-next-steps");
    const previewNextPill = previewNextSteps?.querySelector(".next-steps-pill");
    const previewNextTitle = previewNextSteps?.querySelector(".next-steps-title");
    const previewNextSub = previewNextSteps?.querySelector(".next-steps-sub");
    const previewNextGrid = materialPreview.querySelector("#preview-next-steps-grid");
    const materialLink = materialPreview.querySelector(".material-preview-link");
    let currentPreview;
    let openingLink;
    let isLoading = false;

    function formatLessons(count) {
        if (!count || count <= 0) return "";
        const rem10 = count % 10;
        const rem100 = count % 100;
        if (rem100 >= 11 && rem100 <= 19) return `${count} уроков`;
        if (rem10 === 1) return `${count} урок`;
        if (rem10 >= 2 && rem10 <= 4) return `${count} урока`;
        return `${count} уроков`;
    }

    function renderProgram(lessons) {
        previewProgram.hidden = lessons.length === 0;
        previewProgram.open = lessons.length > 0;
        previewProgram.querySelector("summary").textContent = `Программа курса · ${formatLessons(lessons.length) || lessons.length}`;
        const list = previewProgram.querySelector("ol");
        list.replaceChildren();
        let section;
        for (const lesson of lessons) {
            const item = document.createElement("li");
            if (lesson.section && lesson.section !== section) {
                const label = document.createElement("span");
                label.className = "lesson-section";
                label.textContent = lesson.section;
                item.append(label);
            }
            section = lesson.section;
            item.append(lesson.title);
            const badges = [lesson.type, lesson.trial ? "Пробный" : ""].filter(Boolean);
            if (badges.length) {
                const meta = document.createElement("small");
                meta.textContent = ` · ${badges.join(" · ")}`;
                item.append(meta);
            }
            list.append(item);
        }
    }

    function renderNextSteps(nextMaterials, allSeen = false) {
        if (!previewNextSteps || !previewNextGrid) {
            return;
        }
        const items = Array.isArray(nextMaterials) ? nextMaterials : [];
        previewNextSteps.hidden = items.length === 0;
        if (previewNextPill) {
            previewNextPill.textContent = allSeen ? "Все шаги просмотрены" : "Траектория обучения";
        }
        if (previewNextTitle) {
            previewNextTitle.textContent = allSeen
                ? "Вы уже посмотрели все материалы по этой траектории"
                : "Рекомендуем изучить следующим шагом";
        }
        if (previewNextSub) {
            previewNextSub.textContent = allSeen
                ? "Можно вернуться к ключевым материалам темы или подобрать новую задачу в поиске:"
                : "Материалы, которые логически развивают эту тему и помогут закрепить знания на практике:";
        }
        previewNextGrid.replaceChildren();
        for (const item of items) {
            const card = document.createElement("a");
            card.className = "next-material-card";
            card.href = item.url || `/materials/${item.id}`;

            const badges = document.createElement("div");
            badges.className = "next-card-badges";

            const stepBadge = document.createElement("span");
            stepBadge.className = "next-step-badge";
            const baseLabel = item.step_label || "Следующий шаг";
            stepBadge.textContent = item.viewed ? `Уже просмотрено · ${baseLabel}` : baseLabel;

            const formatBadge = document.createElement("span");
            formatBadge.className = "next-format-badge";
            const fmtParts = [(item.format || "МАТЕРИАЛ").toUpperCase()];
            if (item.duration) {
                fmtParts.push(item.duration.toUpperCase());
            }
            formatBadge.textContent = fmtParts.join(" · ");

            badges.append(stepBadge, formatBadge);
            card.append(badges);

            const title = document.createElement("h4");
            title.className = "next-card-title";
            title.textContent = item.title || "";
            card.append(title);

            if (item.description) {
                const desc = document.createElement("p");
                desc.className = "next-card-desc";
                desc.textContent = item.description.length >= 110 ? `${item.description.slice(0, 107).trimEnd()}...` : item.description;
                card.append(desc);
            }

            const bottom = document.createElement("div");
            bottom.className = "next-card-bottom";

            const tag = document.createElement("span");
            if (Array.isArray(item.subscriptions) && item.subscriptions.length > 0) {
                tag.className = "tag paid";
                tag.textContent = "По подписке Курс+";
            } else if (item.access === "paid") {
                tag.className = "tag paid";
                tag.textContent = item.cost != null ? `${Number(item.cost).toLocaleString("ru-RU").replace(/\s/g, "\u202f")} ₽` : "Платно";
            } else {
                tag.className = "tag";
                tag.textContent = "Бесплатно";
            }

            const linkText = document.createElement("span");
            linkText.className = "next-card-link-text";
            linkText.textContent = "К материалу\u00a0→";

            bottom.append(tag, linkText);
            card.append(bottom);
            previewNextGrid.append(card);
        }
    }

    document.addEventListener("click", async (event) => {
        if (event.defaultPrevented || event.button !== 0 || event.metaKey || event.ctrlKey || event.shiftKey || event.altKey) {
            return;
        }
        const directMaterialLink = event.target.closest('a[href^="/materials/"], a[href*="/materials/"]');
        const cardLink = event.target.closest("#recommendations .card, #daily-picks .card")?.querySelector(".material-link");
        const link = directMaterialLink || cardLink;
        if (!link || (!directMaterialLink && window.getSelection()?.toString())) {
            return;
        }
        event.preventDefault();
        if (isLoading) {
            return;
        }
        isLoading = true;
        try {
            const fetchUrl = new URL(link.href, window.location.origin);
            const seenList = getSessionViewedIds();
            if (seenList.length) {
                fetchUrl.searchParams.set("seen", seenList.slice(0, 30).join(","));
            }
            const response = await fetch(fetchUrl, { headers: { Accept: "application/json" } });
            if (!response.ok) {
                throw new Error("Не удалось загрузить материал");
            }
            currentPreview = await response.json();
            const materialId = currentPreview.id || fetchUrl.pathname.split("/materials/")[1]?.split("/")[0];
            addSessionViewedId(materialId);
            if (previewEyebrow) {
                const parts = [];
                if (currentPreview.format) parts.push(currentPreview.format.toUpperCase());
                if (currentPreview.duration) parts.push(currentPreview.duration.toUpperCase());
                if (currentPreview.lesson_count) {
                    const lessonsStr = formatLessons(currentPreview.lesson_count);
                    if (lessonsStr) parts.push(lessonsStr.toUpperCase());
                }
                previewEyebrow.textContent = parts.length ? parts.join(" · ") : "О МАТЕРИАЛЕ";
            }
            previewTitle.textContent = currentPreview.title;
            const authors = currentPreview.authors ?? [];
            previewAuthors.hidden = authors.length === 0;
            previewAuthors.textContent = authors.length ? `Авторы · ${authors.join(", ")}` : "";
            const subscriptions = currentPreview.subscriptions ?? [];
            previewSubscription.hidden = subscriptions.length === 0;
            previewSubscription.textContent = subscriptions.length ? `По подписке ${subscriptions.join(", ")}` : "";
            previewAccess.hidden = !currentPreview.access_required;
            previewDescription.innerHTML = currentPreview.description_html;
            renderProgram(currentPreview.lessons ?? []);
            renderNextSteps(currentPreview.next_materials ?? [], Boolean(currentPreview.next_all_seen));
            materialLink.href = currentPreview.url;
            if (!materialPreview.open) {
                openingLink = link;
                materialPreview.showModal();
                prepareCards();
            }
            if (previewContent) {
                previewContent.scrollTop = 0;
            }
        } catch {
            window.location.assign(link.href);
        } finally {
            isLoading = false;
        }
    });

    function recordClick(event) {
        if (event.defaultPrevented || !currentPreview || (event.type === "auxclick" && event.button !== 1)) {
            return;
        }
        fetch(currentPreview.open_url, {
            method: "POST",
            headers: { Accept: "application/json" },
            body: new URLSearchParams({ csrf_token: currentPreview.csrf_token }),
            keepalive: true,
        }).catch(() => {});
    }

    materialLink.addEventListener("click", recordClick);
    materialLink.addEventListener("auxclick", recordClick);
    materialPreview.addEventListener("click", (event) => {
        if (event.target === materialPreview) {
            materialPreview.close();
        }
    });
    materialPreview.addEventListener("close", () => {
        prepareCards();
        if (previewEyebrow) previewEyebrow.textContent = "О МАТЕРИАЛЕ";
        openingLink?.focus({ preventScroll: true });
    });
}
