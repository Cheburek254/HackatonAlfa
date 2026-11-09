const authDemoNotice = document.querySelector("#auth-demo-notice");

for (const button of document.querySelectorAll("[data-demo-alfa]")) {
    button.addEventListener("click", () => authDemoNotice.showModal());
}
