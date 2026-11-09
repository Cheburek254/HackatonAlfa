const materialGrid = document.querySelector(".card-grid");
const materialPreview = document.querySelector("#material-preview");

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
