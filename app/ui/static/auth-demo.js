const authDemoNotice = document.querySelector("#auth-demo-notice");

for (const button of document.querySelectorAll("[data-demo-alfa]")) {
    button.addEventListener("click", () => authDemoNotice.showModal());
}

for (const dialog of document.querySelectorAll("dialog[data-show-modal]")) {
    if (typeof dialog.showModal === "function") {
        dialog.close();
        dialog.showModal();
    }
}
