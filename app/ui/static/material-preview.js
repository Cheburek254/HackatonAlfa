const materialGrid = document.querySelector(".card-grid");
const materialPreview = document.querySelector("#material-preview");

if (materialGrid && "IntersectionObserver" in window) {
    const pendingCards = new Set(materialGrid.querySelectorAll(".card"));
    const impressions = new IntersectionObserver((entries, observer) => {
        if (document.visibilityState !== "visible") {
            return;
        }
        const tickets = [];
        for (const entry of entries) {
            if (entry.intersectionRatio >= 0.5 && pendingCards.delete(entry.target)) {
                const link = entry.target.querySelector(".material-link");
                tickets.push(new URL(link.href).searchParams.get("impression"));
                observer.unobserve(entry.target);
            }
        }
        for (let start = 0; start < tickets.length; start += 16) {
            const body = new URLSearchParams({ csrf_token: materialGrid.dataset.impressionCsrf });
            for (const ticket of tickets.slice(start, start + 16)) {
                body.append("impression", ticket);
            }
            fetch(materialGrid.dataset.impressionUrl, { method: "POST", body, keepalive: true }).catch(() => {});
        }
    }, { threshold: 0.5 });
    function observeVisibleTab() {
        if (document.visibilityState === "visible") {
            for (const card of pendingCards) {
                impressions.observe(card);
            }
        } else {
            impressions.disconnect();
        }
    }
    document.addEventListener("visibilitychange", observeVisibleTab);
    observeVisibleTab();
}

if (materialGrid && typeof materialPreview?.showModal === "function") {
    materialGrid.classList.add("has-material-preview");
    const previewTitle = materialPreview.querySelector("#preview-title");
    const previewDescription = materialPreview.querySelector("#preview-description");
    const materialLink = materialPreview.querySelector(".material-preview-link");
    let currentPreview;
    let openingLink;
    let isLoading = false;

    materialGrid.addEventListener("click", async (event) => {
        if (event.defaultPrevented || event.button !== 0 || event.metaKey || event.ctrlKey || event.shiftKey || event.altKey) {
            return;
        }
        const link = event.target.closest(".card")?.querySelector(".material-link");
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
            previewDescription.textContent = currentPreview.description || "Краткое описание пока недоступно.";
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
