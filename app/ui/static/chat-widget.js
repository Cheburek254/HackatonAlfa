(function () {
    const trigger = document.getElementById("alfa-chat-trigger");
    const windowEl = document.getElementById("alfa-chat-window");
    const closeBtn = document.getElementById("alfa-chat-close-btn");
    const form = document.getElementById("alfa-chat-form");
    const input = document.getElementById("alfa-chat-input");
    const messagesEl = document.getElementById("alfa-chat-messages");
    const quickReplies = document.getElementById("chat-quick-replies");
    const typingIndicator = document.getElementById("alfa-chat-typing");
    const iconOpen = trigger ? trigger.querySelector(".chat-icon-open") : null;
    const iconClose = trigger ? trigger.querySelector(".chat-icon-close") : null;

    if (!trigger || !windowEl || !form || !input || !messagesEl) {
        return;
    }

    const history = [];

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

    function scrollToBottom() {
        messagesEl.scrollTop = messagesEl.scrollHeight;
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

    function toggleChat(open) {
        const isOpen = open !== undefined ? open : windowEl.hidden;
        windowEl.hidden = !isOpen;
        trigger.setAttribute("aria-expanded", String(isOpen));
        if (iconOpen && iconClose) {
            iconOpen.hidden = isOpen;
            iconClose.hidden = !isOpen;
        }
        if (isOpen) {
            input.focus();
            scrollToBottom();
        }
    }

    trigger.addEventListener("click", () => toggleChat());
    if (closeBtn) {
        closeBtn.addEventListener("click", () => toggleChat(false));
    }

    document.addEventListener("keydown", (e) => {
        if (e.key === "Escape" && !windowEl.hidden) {
            toggleChat(false);
        }
    });

    if (quickReplies) {
        quickReplies.addEventListener("click", (e) => {
            const pill = e.target.closest(".quick-reply-pill");
            if (!pill) return;
            const prompt = pill.dataset.prompt || pill.textContent.trim();
            quickReplies.hidden = true;
            sendMessage(prompt);
        });
    }

    async function sendMessage(text) {
        const query = (text || "").trim();
        if (!query) return;

        appendMessage(query, "user");
        history.push({ role: "user", text: query });
        if (quickReplies) quickReplies.hidden = true;

        input.value = "";
        input.disabled = true;
        if (typingIndicator) typingIndicator.hidden = false;
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
                "Извините, сервис временно недоступен. Попробуйте сформулировать вопрос иначе или обновить страницу.",
                "assistant"
            );
        } finally {
            if (typingIndicator) typingIndicator.hidden = true;
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
