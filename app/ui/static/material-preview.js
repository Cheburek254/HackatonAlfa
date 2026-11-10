const materialPreview = document.querySelector("#material-preview");
const seenCards = new WeakSet();
const dailyTrack = document.querySelector("#daily-track");

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

const impressionObserver = "IntersectionObserver" in window ? new IntersectionObserver((entries, observer) => {
    const grid = document.querySelector("#recommendations .card-grid");
    if (!grid || document.visibilityState !== "visible") {
        return;
    }
    const tickets = [];
    for (const entry of entries) {
        if (entry.intersectionRatio < 0.5 || !grid.contains(entry.target) || seenCards.has(entry.target)) {
            continue;
        }
        seenCards.add(entry.target);
        observer.unobserve(entry.target);
        const link = entry.target.querySelector(".material-link");
        tickets.push(new URL(link.href).searchParams.get("impression"));
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
    if (typeof materialPreview?.showModal === "function") {
        grid?.classList.add("has-material-preview");
    }
    impressionObserver?.disconnect();
    if (!impressionObserver || !grid || document.visibilityState !== "visible") {
        return;
    }
    for (const card of grid.querySelectorAll(".card")) {
        if (!seenCards.has(card)) {
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
    const loadMore = submitter?.name === "show_page";
    const data = new FormData(form);
    if (submitter?.name) {
        data.append(submitter.name, submitter.value);
    }
    status.textContent = "";
    section.setAttribute("aria-busy", "true");
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

        const previousCards = [...section.querySelectorAll(".card")];
        const nextCards = [...next.querySelectorAll(".card")];
        const scrollTop = window.scrollY;
        if (loadMore && section.dataset.impressionBatch === next.dataset.impressionBatch &&
            previousCards.length < nextCards.length &&
            previousCards.every((card, index) => card.dataset.materialId === nextCards[index].dataset.materialId)) {
            const appendedCards = nextCards.slice(previousCards.length);
            section.querySelector(".card-grid").append(...appendedCards);
            section.querySelector(".result-pagination").replaceWith(next.querySelector(".result-pagination"));
            section.querySelector('input[name="impression_batch"]').value = next.querySelector('input[name="impression_batch"]').value;
            prepareCards();
            window.scrollTo({ top: scrollTop, behavior: "instant" });
            if (document.activeElement === document.body) {
                appendedCards[0].focus({ preventScroll: true });
            }
            return;
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
        } else if (loadMore) {
            window.scrollTo({ top: scrollTop, behavior: "instant" });
            (nextCards[previousCards.length] || next.querySelector("#results-title"))?.focus({ preventScroll: true });
        } else {
            const matching = [...next.querySelectorAll("button")].find(
                (button) => button.name === submitter?.name && button.value === submitter?.value,
            );
            matching?.focus({ preventScroll: true });
        }
    } catch {
        if (!update.signal.aborted) {
            status.textContent = "Не удалось обновить подборку. Попробуйте ещё раз.";
            if (!loadMore) {
                status.scrollIntoView({ block: "nearest" });
            }
        }
    } finally {
        if (activeUpdate === update) {
            section.removeAttribute("aria-busy");
            activeUpdate = undefined;
        }
    }
});

if (typeof materialPreview?.showModal === "function") {
    const previewTitle = materialPreview.querySelector("#preview-title");
    const previewAuthors = materialPreview.querySelector("#preview-authors");
    const previewSubscription = materialPreview.querySelector("#preview-subscription");
    const previewAccess = materialPreview.querySelector("#preview-access-note");
    const previewDescription = materialPreview.querySelector("#preview-description");
    const previewProgram = materialPreview.querySelector("#preview-program");
    const materialLink = materialPreview.querySelector(".material-preview-link");
    let currentPreview;
    let openingLink;
    let isLoading = false;

    function renderProgram(lessons) {
        previewProgram.hidden = lessons.length === 0;
        previewProgram.open = lessons.length > 0;
        previewProgram.querySelector("summary").textContent = `Программа курса · ${lessons.length}`;
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

    document.addEventListener("click", async (event) => {
        if (event.defaultPrevented || event.button !== 0 || event.metaKey || event.ctrlKey || event.shiftKey || event.altKey) {
            return;
        }
        const link = event.target.closest("#recommendations .card, #daily-picks .card")?.querySelector(".material-link");
        if (!link || (!event.target.closest(".material-link") && window.getSelection()?.toString())) {
            return;
        }
        event.preventDefault();
        if (isLoading || materialPreview.open) {
            return;
        }
        isLoading = true;
        try {
            const response = await fetch(link.href, { headers: { Accept: "application/json" } });
            if (!response.ok) {
                throw new Error("Не удалось загрузить материал");
            }
            currentPreview = await response.json();
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
            materialLink.href = currentPreview.url;
            openingLink = link;
            materialPreview.showModal();
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
    materialPreview.addEventListener("close", () => openingLink?.focus({ preventScroll: true }));
}
