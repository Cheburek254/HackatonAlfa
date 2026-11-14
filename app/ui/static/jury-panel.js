const JURY_COLLAPSE_KEY = "alfa_jury_panel_collapsed";
const PROFILE_FIELD_NAMES = [
    "stage",
    "industry",
    "experience",
    "team_size",
    "learning_topic",
    "preferred_format",
];

let activeJuryRequest = null;

function currentVisibleTopIds(root = document, limit = 6) {
    return [...root.querySelectorAll("#recommendations .card[data-material-id]")]
        .slice(0, limit)
        .map((card) => card.dataset.materialId)
        .filter(Boolean);
}

function setPanelCollapsed(collapsed) {
    const panel = document.querySelector("#jury-panel");
    const collapseBtn = document.querySelector("#jury-collapse-btn");
    const reopenBtn = document.querySelector("#jury-reopen-btn");
    if (!panel) return;
    panel.hidden = collapsed;
    if (collapseBtn) collapseBtn.setAttribute("aria-expanded", collapsed ? "false" : "true");
    if (reopenBtn) reopenBtn.hidden = !collapsed;
    try {
        sessionStorage.setItem(JURY_COLLAPSE_KEY, collapsed ? "1" : "0");
    } catch {}
}

function restorePanelCollapsedState() {
    let saved = false;
    try {
        saved = sessionStorage.getItem(JURY_COLLAPSE_KEY) === "1";
    } catch {}
    setPanelCollapsed(saved);
}

function buildDeltaDescriptor(materialId, currIdx, prevIdxMap) {
    if (!prevIdxMap.has(materialId)) {
        return { label: "★ Новый", cls: "is-new" };
    }
    const prevIdx = prevIdxMap.get(materialId);
    if (currIdx < prevIdx) {
        return { label: `↑ +${prevIdx - currIdx}`, cls: "is-up" };
    }
    if (currIdx > prevIdx) {
        return { label: `↓ -${currIdx - prevIdx}`, cls: "is-down" };
    }
    return null;
}

function applyRankDiffIndicators(previousTopIds) {
    const banner = document.querySelector("#jury-diff-banner");
    const currentCards = [...document.querySelectorAll("#recommendations .card[data-material-id]")];
    const currentTopIds = currentCards.slice(0, 6).map((card) => card.dataset.materialId);

    document.querySelectorAll("#recommendations .jury-card-delta").forEach((el) => el.remove());

    if (!previousTopIds?.length || !currentTopIds.length) {
        if (banner) banner.hidden = true;
        return;
    }

    const prevIndex = new Map(previousTopIds.map((id, idx) => [id, idx]));
    let changedCount = 0;

    currentTopIds.forEach((id, idx) => {
        if (previousTopIds[idx] !== id) {
            changedCount += 1;
        }
        const delta = buildDeltaDescriptor(id, idx, prevIndex);
        if (!delta) return;

        const cardMeta = currentCards[idx]?.querySelector(".card-meta");
        if (cardMeta) {
            const chip = document.createElement("span");
            chip.className = `jury-card-delta ${delta.cls}`;
            chip.textContent = delta.label;
            cardMeta.append(chip);
        }

        const panelBadge = document.querySelector(`#jury-panel .jury-rank-badge[data-top-id="${CSS.escape(id)}"]`);
        if (panelBadge) {
            panelBadge.textContent = delta.label;
            panelBadge.className = `jury-rank-badge ${delta.cls}`;
            panelBadge.hidden = false;
        }
    });

    if (banner) {
        banner.hidden = false;
        banner.classList.toggle("has-changes", changedCount > 0);
        banner.textContent = changedCount > 0
            ? `После изменения сдвинулись позиции топ-6: ${changedCount} из ${currentTopIds.length}`
            : "Состав и порядок топ-6 не изменились";
    }
}

function syncHeroInputsFromDoc(parsedDoc) {
    const srcForm = parsedDoc.querySelector("#recommendation-query");
    const dstForm = document.querySelector("#recommendation-query");
    if (!srcForm || !dstForm) return;

    const srcGoal = srcForm.elements.namedItem("goal");
    const dstGoal = dstForm.elements.namedItem("goal");
    if (srcGoal && dstGoal) {
        dstGoal.value = srcGoal.value;
    }

    for (const fieldName of PROFILE_FIELD_NAMES) {
        const srcField = srcForm.elements.namedItem(fieldName);
        const dstField = dstForm.elements.namedItem(fieldName);
        if (srcField && dstField) {
            dstField.value = srcField.value;
        }
    }

    const details = dstForm.querySelector("details.profile");
    if (details) {
        details.open = true;
    }

    const goalVal = dstGoal?.value || "";
    dstForm.querySelectorAll('button[name="quick_goal"]').forEach((btn) => {
        btn.classList.toggle("chip-selected", btn.value === goalVal);
    });
}

function appendHeroAndFilterState(data) {
    const heroForm = document.querySelector("#recommendation-query");
    if (heroForm) {
        const goalInput = heroForm.elements.namedItem("goal");
        if (goalInput) data.set("goal", goalInput.value);
        for (const fieldName of PROFILE_FIELD_NAMES) {
            const field = heroForm.elements.namedItem(fieldName);
            if (field) data.set(fieldName, field.value);
        }
    }
    const ordersForm = document.querySelector("#result-orders");
    if (ordersForm) {
        const orderInput = ordersForm.elements.namedItem("current_order");
        const formatInput = ordersForm.elements.namedItem("format_filter");
        const pageSizeInput = ordersForm.elements.namedItem("page_size");
        if (orderInput) data.set("result_order", orderInput.value);
        if (formatInput) data.set("format_filter", formatInput.value);
        if (pageSizeInput) data.set("page_size", pageSizeInput.value);
        ordersForm.querySelectorAll('input[name="access_filter"]:checked').forEach((cb) => {
            if (!data.has("access_filters_set")) data.set("access_filters_set", "1");
            data.append("access_filter", cb.value);
        });
    }
}

async function runLabUpdate(actionUrl, data, {
    syncHero = false, updateResults = true, updateShelves = true, previousTopIds = null,
} = {}) {
    activeJuryRequest?.abort();
    const controller = new AbortController();
    activeJuryRequest = controller;

    const panel = document.querySelector("#jury-panel");
    const prevScroll = panel?.scrollTop || 0;
    const prevTop = previousTopIds ?? currentVisibleTopIds();
    panel?.setAttribute("aria-busy", "true");

    try {
        const response = await fetch(actionUrl, {
            method: "POST",
            headers: { "X-Jury-Panel": "1" },
            body: new URLSearchParams(data),
            signal: controller.signal,
        });
        if (!response.ok) {
            throw new Error("Не удалось обновить параметры проверки");
        }
        const html = await response.text();
        if (controller.signal.aborted) return;

        const parsed = new DOMParser().parseFromString(html, "text/html");
        const nextPanelInner = parsed.querySelector("#jury-panel .jury-panel-inner");
        const curPanelInner = document.querySelector("#jury-panel .jury-panel-inner");
        if (nextPanelInner && curPanelInner) {
            curPanelInner.replaceWith(nextPanelInner);
            const nextPanel = parsed.querySelector("#jury-panel");
            if (panel && nextPanel) {
                panel.classList.toggle("is-session-active", nextPanel.classList.contains("is-session-active"));
            }
        }

        if (updateShelves) {
            for (const slotId of ["jury-daily-slot", "jury-next-slot"]) {
                const curSlot = document.querySelector(`#${slotId}`);
                const nextSlot = parsed.querySelector(`#${slotId}`);
                if (curSlot && nextSlot) {
                    curSlot.replaceWith(nextSlot);
                }
            }
        }

        if (updateResults) {
            const curResults = document.querySelector("#recommendations");
            const nextResults = parsed.querySelector("#recommendations");
            if (curResults && nextResults) {
                curResults.replaceWith(nextResults);
            }
            const status = document.querySelector("#recommendation-status");
            if (status) status.textContent = "";
        }

        if (syncHero) {
            syncHeroInputsFromDoc(parsed);
        }

        if (panel) {
            panel.scrollTop = prevScroll;
        }
        restorePanelCollapsedState();
        applyRankDiffIndicators(prevTop);
        document.dispatchEvent(new CustomEvent("alfa:dom-updated"));
    } catch {
        if (!controller.signal.aborted) {
            const status = document.querySelector("#recommendation-status");
            if (status) {
                status.textContent = "Не удалось применить настройку проверки. Попробуйте ещё раз.";
            }
        }
    } finally {
        if (activeJuryRequest === controller) {
            activeJuryRequest = null;
        }
        document.querySelector("#jury-panel")?.removeAttribute("aria-busy");
    }
}

function submitLabRefresh({ updateResults = true, updateShelves = true, previousTopIds = null } = {}) {
    const anyLabForm = document.querySelector("#jury-panel form");
    const csrfInput = anyLabForm?.querySelector('input[name="csrf_token"]');
    if (!anyLabForm || !csrfInput) return;

    const data = new FormData();
    data.set("csrf_token", csrfInput.value);
    data.set("lab_action", "sync_view");
    appendHeroAndFilterState(data);
    runLabUpdate(anyLabForm.getAttribute("action") || "/demo-lab", data, {
        syncHero: false,
        updateResults,
        updateShelves,
        previousTopIds,
    });
}

document.addEventListener("submit", (event) => {
    const form = event.target;
    if (!(form instanceof HTMLFormElement) || !form.closest("#jury-panel")) {
        return;
    }
    event.preventDefault();
    const actionName = form.dataset.juryAction || "";
    const shouldReplaceHero = actionName === "apply_preset" || actionName === "reset_all";
    const data = new FormData(form);
    if (form.dataset.syncHero === "1") {
        data.set("hero_sync", "1");
        appendHeroAndFilterState(data);
    }
    runLabUpdate(form.getAttribute("action") || "/demo-lab", data, {
        syncHero: shouldReplaceHero,
        updateResults: true,
    });
});

document.addEventListener("click", (event) => {
    const target = event.target;
    if (!(target instanceof Element)) return;
    if (target.closest("#jury-collapse-btn")) {
        setPanelCollapsed(true);
    } else if (target.closest("#jury-reopen-btn")) {
        setPanelCollapsed(false);
    }
});

document.addEventListener("keydown", (event) => {
    if (event.key !== "Escape") return;
    if (document.querySelector("dialog[open]")) return;
    const panel = document.querySelector("#jury-panel");
    if (panel && !panel.hidden) {
        setPanelCollapsed(true);
    }
});

document.addEventListener("alfa:jury-sync", (event) => {
    submitLabRefresh({
        updateResults: false,
        updateShelves: Boolean(event.detail?.isQuery),
        previousTopIds: event.detail?.previousTopIds || [],
    });
});

document.addEventListener("alfa:jury-behavior", () => {
    submitLabRefresh({
        updateResults: true,
        previousTopIds: currentVisibleTopIds(),
    });
});

restorePanelCollapsedState();
