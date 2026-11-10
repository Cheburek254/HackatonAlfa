(function () {
    const trigger = document.getElementById("alfa-chat-trigger");
    const windowEl = document.getElementById("alfa-chat-window");
    const closeBtn = document.getElementById("alfa-chat-close-btn");
    const form = document.getElementById("alfa-chat-form");
    const input = document.getElementById("alfa-chat-input");
    const messagesEl = document.getElementById("alfa-chat-messages");
    const quickReplies = document.getElementById("chat-quick-replies");
    const typingIndicator = document.getElementById("alfa-chat-typing");

    if (!trigger || !windowEl || !form || !input || !messagesEl) {
        return;
    }

    // Default state: strictly closed
    windowEl.classList.remove("is-open");
    trigger.classList.remove("is-active");
    trigger.setAttribute("aria-expanded", "false");
    if (typingIndicator) {
        typingIndicator.style.display = "none";
    }
    input.style.overflowY = "hidden";

    const history = [];

    function autoResize() {
        input.style.height = "auto";
        const maxHeight = 120;
        if (input.scrollHeight > maxHeight) {
            input.style.height = maxHeight + "px";
            input.style.overflowY = "auto";
        } else {
            const newHeight = Math.max(input.scrollHeight, 38);
            input.style.height = newHeight + "px";
            input.style.overflowY = "hidden";
        }
    }

    input.addEventListener("input", autoResize);

    input.addEventListener("keydown", (e) => {
        if (e.key === "Enter" && !e.shiftKey) {
            e.preventDefault();
            form.requestSubmit ? form.requestSubmit() : form.dispatchEvent(new Event("submit", { cancelable: true }));
        }
    });

    function scrollToBottom() {
        messagesEl.scrollTop = messagesEl.scrollHeight;
    }

    function formatReplyText(text) {
        if (!text) return "";
        let safe = text
            .replace(/&/g, "&amp;")
            .replace(/</g, "&lt;")
            .replace(/>/g, "&gt;");

        // Bold **text**
        safe = safe.replace(/\*\*(.+?)\*\*/g, "<strong>$1</strong>");

        // Lists
        const lines = safe.split("\n");
        let inList = false;
        const result = [];

        for (const line of lines) {
            const trimmed = line.trim();
            if (/^[-•*]\s+/.test(trimmed)) {
                if (!inList) {
                    result.push("<ul class='chat-list'>");
                    inList = true;
                }
                result.push(`<li>${trimmed.replace(/^[-•*]\s+/, "")}</li>`);
            } else if (/^\d+\.\s+/.test(trimmed)) {
                if (!inList) {
                    result.push("<ol class='chat-list'>");
                    inList = true;
                }
                result.push(`<li>${trimmed.replace(/^\d+\.\s+/, "")}</li>`);
            } else {
                if (inList) {
                    result.push(inList === "ol" ? "</ol>" : "</ul>");
                    inList = false;
                }
                if (trimmed) {
                    result.push(`<p>${trimmed}</p>`);
                }
            }
        }
        if (inList) {
            result.push("</ul>");
        }
        return result.join("");
    }

    function appendMessage(text, role) {
        const msgDiv = document.createElement("div");
        msgDiv.className = `chat-message chat-message-${role}`;
        const bubble = document.createElement("div");
        bubble.className = "message-bubble";

        if (role === "assistant") {
            bubble.innerHTML = formatReplyText(text);
        } else {
            bubble.textContent = text;
        }

        msgDiv.appendChild(bubble);
        messagesEl.appendChild(msgDiv);
        scrollToBottom();
    }

    function toggleChat(forceState) {
        const isOpen = forceState !== undefined ? forceState : !windowEl.classList.contains("is-open");
        if (isOpen) {
            windowEl.classList.add("is-open");
            trigger.classList.add("is-active");
            trigger.setAttribute("aria-expanded", "true");
            setTimeout(() => {
                input.focus();
                scrollToBottom();
            }, 60);
        } else {
            windowEl.classList.remove("is-open");
            trigger.classList.remove("is-active");
            trigger.setAttribute("aria-expanded", "false");
        }
    }

    trigger.addEventListener("click", (e) => {
        e.preventDefault();
        e.stopPropagation();
        toggleChat();
    });

    if (closeBtn) {
        closeBtn.addEventListener("click", (e) => {
            e.preventDefault();
            e.stopPropagation();
            toggleChat(false);
        });
    }

    document.addEventListener("keydown", (e) => {
        if (e.key === "Escape" && windowEl.classList.contains("is-open")) {
            toggleChat(false);
        }
    });

    if (quickReplies) {
        quickReplies.addEventListener("click", (e) => {
            const pill = e.target.closest(".quick-reply-pill");
            if (!pill) return;
            const prompt = pill.dataset.prompt || pill.textContent.trim();
            hideQuickReplies();
            sendMessage(prompt);
        });
    }

    function hideQuickReplies() {
        if (quickReplies) {
            quickReplies.style.display = "none";
        }
    }

    async function sendMessage(text) {
        const query = (text || "").trim();
        if (!query) return;

        // Hide suggestions immediately when any request is sent
        hideQuickReplies();

        appendMessage(query, "user");
        history.push({ role: "user", text: query });

        input.value = "";
        input.style.height = "38px";
        input.style.overflowY = "hidden";
        input.disabled = true;

        if (typingIndicator) {
            typingIndicator.style.display = "flex";
        }
        scrollToBottom();

        try {
            const res = await fetch("/api/chat", {
                method: "POST",
                headers: {
                    "Content-Type": "application/json",
                    "Accept": "application/json",
                },
                body: JSON.stringify({ message: query, history: history.slice(-6) }),
            });

            if (!res.ok) {
                throw new Error("Ошибка связи с сервером");
            }

            const data = await res.json();
            const reply = data.reply || "Извините, не удалось сформировать ответ. Попробуйте еще раз.";
            appendMessage(reply, "assistant");
            history.push({ role: "model", text: reply });
        } catch (err) {
            appendMessage(
                "Извините, сейчас сервер обновляется. Попробуйте повторить запрос через пару секунд.",
                "assistant"
            );
        } finally {
            if (typingIndicator) {
                typingIndicator.style.display = "none";
            }
            input.disabled = false;
            input.focus();
            scrollToBottom();
        }
    }

    form.addEventListener("submit", (e) => {
        e.preventDefault();
        sendMessage(input.value);
    });
})();
