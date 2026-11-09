const materialPreview = document.querySelector("#material-preview");
const seenCards = new WeakSet();

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
        const response = await fetch(submitter?.hasAttribute("formaction") ? submitter.formAction : form.action, {
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
        if (loadMore && section.dataset.impressionBatch === next.dataset.impressionBatch &&
            previousCards.length < nextCards.length &&
            previousCards.every((card, index) => card.dataset.materialId === nextCards[index].dataset.materialId)) {
            previousCards.forEach((card, index) => nextCards[index].replaceWith(card));
        }
        section.replaceWith(next);
        prepareCards();

        const query = document.querySelector("#recommendation-query");
        const controls = next.querySelector("#result-orders");
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
            status.scrollIntoView({ block: "nearest" });
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
    const previewDescription = materialPreview.querySelector("#preview-description");
    const materialLink = materialPreview.querySelector(".material-preview-link");
    let currentPreview;
    let openingLink;
    let isLoading = false;

    document.addEventListener("click", async (event) => {
        if (event.defaultPrevented || event.button !== 0 || event.metaKey || event.ctrlKey || event.shiftKey || event.altKey) {
            return;
        }
        const link = event.target.closest("#recommendations .card")?.querySelector(".material-link");
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
            previewDescription.innerHTML = currentPreview.description_html;
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
