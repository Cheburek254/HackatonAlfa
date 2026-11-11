(function () {
    const widget = document.getElementById("alfa-chat-widget");
    const trigger = document.getElementById("alfa-chat-trigger");
    const windowEl = document.getElementById("alfa-chat-window");
    const closeBtn = document.getElementById("alfa-chat-close-btn");
    const resetBtn = document.getElementById("alfa-chat-reset-btn");
    const form = document.getElementById("alfa-chat-form");
    const input = document.getElementById("alfa-chat-input");
    const messagesEl = document.getElementById("alfa-chat-messages");
    const typingIndicator = document.getElementById("alfa-chat-typing");

    if (!trigger || !windowEl || !form || !input || !messagesEl) {
        return;
    }

    const userId = (widget && widget.dataset && widget.dataset.userId) || "guest";
    const STORAGE_KEY = "alfa_chat_history_" + userId;
    const STATE_KEY = "alfa_chat_open_" + userId;

    // Default state: closed until checked
    windowEl.classList.remove("is-open");
    trigger.classList.remove("is-active");
    trigger.setAttribute("aria-expanded", "false");
    if (typingIndicator) {
        typingIndicator.style.display = "none";
    }
    input.style.overflowY = "hidden";

    const history = [];
    let cardContext = null;
    let activeRequest = null;

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

        // Ссылки на карточки каталога, профиль, настройки, авторизацию.
        safe = safe.replace(/\[([^\]]+)\]\((\/(?:materials\/[a-zA-Z0-9_-]+|profile(?:\/settings)?(?:#[a-zA-Z0-9_-]+)?|login|register|forgot-password))\)/g, '<a href="$2" class="chat-material-link">$1</a>');

        // Lists and paragraphs
        const lines = safe.split("\n");
        let inList = false;
        let listType = "ul";
        const result = [];

        for (const line of lines) {
            const trimmed = line.trim();
            if (/^[-•*]\s+/.test(trimmed)) {
                if (!inList || listType !== "ul") {
                    if (inList) result.push(listType === "ol" ? "</ol>" : "</ul>");
                    result.push("<ul class='chat-list'>");
                    inList = true;
                    listType = "ul";
                }
                result.push(`<li>${trimmed.replace(/^[-•*]\s+/, "")}</li>`);
            } else if (/^\d+\.\s+/.test(trimmed)) {
                if (!inList || listType !== "ol") {
                    if (inList) result.push(listType === "ol" ? "</ol>" : "</ul>");
                    result.push("<ol class='chat-list'>");
                    inList = true;
                    listType = "ol";
                }
                result.push(`<li>${trimmed.replace(/^\d+\.\s+/, "")}</li>`);
            } else {
                if (inList) {
                    result.push(listType === "ol" ? "</ol>" : "</ul>");
                    inList = false;
                }
                if (trimmed) {
                    result.push(`<p>${trimmed}</p>`);
                }
            }
        }
        if (inList) {
            result.push(listType === "ol" ? "</ol>" : "</ul>");
        }
        return result.join("");
    }

    function appendMessage(text, role, shouldSave = true, { cards = [], clickContext = null } = {}) {
        const msgDiv = document.createElement("div");
        msgDiv.className = `chat-message chat-message-${role}`;
        const bubble = document.createElement("div");
        bubble.className = "message-bubble";

        if (role === "assistant" || role === "model") {
            const validCards = Array.isArray(cards) ? cards.filter(card => card &&
                typeof card.id === "string" && /^[a-zA-Z0-9_-]+$/.test(card.id) && typeof card.title === "string") : [];
            const cardIds = new Set(validCards.map(card => card.id));
            const prose = text.split("\n").filter(line => {
                const match = line.trim().match(/^(?:[•*-]\s+|\d+\.\s+)?\[([^\]]+)\]\(\/materials\/([a-zA-Z0-9_-]+)\)(?:\s*\([^)]*\))?\s*$/);
                return !match || !cardIds.has(match[2]);
            }).join("\n");
            bubble.innerHTML = formatReplyText(prose);
            if (typeof clickContext === "string") bubble.dataset.chatClickContext = clickContext;
            if (validCards.length) {
                const list = document.createElement("ol");
                list.className = "chat-result-cards";
                list.setAttribute("aria-label", "Подобранные материалы");
                for (const card of validCards) {
                    const item = document.createElement("li");
                    const link = document.createElement("a");
                    link.className = "chat-material-link";
                    link.href = `/materials/${card.id}`;
                    link.textContent = card.title;
                    item.append(link);
                    if (card.format) {
                        const format = document.createElement("small");
                        format.textContent = card.format;
                        item.append(format);
                    }
                    list.append(item);
                }
                bubble.append(list);
            }
        } else {
            bubble.textContent = text;
        }

        msgDiv.appendChild(bubble);
        messagesEl.appendChild(msgDiv);
        scrollToBottom();

        if (shouldSave) {
            saveState();
        }
    }

    function saveState() {
        try {
            const quick = document.getElementById("chat-quick-replies");
            sessionStorage.setItem(STORAGE_KEY, JSON.stringify({
                history: history.slice(-24),
                cardContext,
                quickHidden: !quick || quick.style.display === "none",
            }));
        } catch (e) {
            // Storage quota or disabled
        }
    }

    function hideQuickReplies() {
        const quick = document.getElementById("chat-quick-replies");
        if (quick) {
            quick.style.display = "none";
        }
    }

    function loadState() {
        try {
            const raw = sessionStorage.getItem(STORAGE_KEY);
            if (!raw) return false;
            const data = JSON.parse(raw);
            if (!data || !Array.isArray(data.history) || data.history.length === 0) {
                return false;
            }

            // Keep initial greeting, remove subsequent messages if any
            const existingMessages = messagesEl.querySelectorAll(".chat-message");
            if (existingMessages.length > 1) {
                for (let i = 1; i < existingMessages.length; i++) {
                    existingMessages[i].remove();
                }
            }

            history.length = 0;
            cardContext = typeof data.cardContext === "string" ? data.cardContext : null;
            for (const item of data.history) {
                if (!item || typeof item.text !== "string") continue;
                const role = item.role === "user" ? "user" : "assistant";
                appendMessage(item.text, role, false, item);
                history.push(item);
            }

            if (data.quickHidden) {
                hideQuickReplies();
            }
            return true;
        } catch (e) {
            return false;
        }
    }

    function resetChat() {
        activeRequest?.abort();
        activeRequest = null;
        cardContext = null;
        try {
            sessionStorage.removeItem(STORAGE_KEY);
        } catch (e) {}

        history.length = 0;
        messagesEl.innerHTML = `
            <div class="chat-message chat-message-assistant">
                <div class="message-bubble">
                    Здравствуйте! Я ИИ-помощник образовательной платформы <strong>«Альфа-Курс»</strong>.<br><br>
                    Помогу разобраться с бизнес-задачей, подберу практические курсы, статьи или шаблоны документов. С чего хотите начать?
                </div>
            </div>
            <div id="chat-quick-replies" class="chat-quick-replies" aria-label="Быстрые вопросы">
                <button type="button" class="quick-reply-pill" data-prompt="Моё обучение на Альфа-Курсе">Моё обучение на Альфа-Курсе</button>
                <button type="button" class="quick-reply-pill" data-prompt="Хочу начать учиться">Хочу начать учиться</button>
                <button type="button" class="quick-reply-pill" data-prompt="Как поднять продажи и привлечь клиентов?">Как поднять продажи</button>
                <button type="button" class="quick-reply-pill" data-prompt="Посоветуй готовые шаблоны и таблицы для учета">Шаблоны для учета</button>
                <button type="button" class="quick-reply-pill" data-prompt="Техническая проблема">Техническая проблема</button>
            </div>
        `;

        bindQuickReplies();
        if (typingIndicator) {
            typingIndicator.style.display = "none";
        }
        input.value = "";
        input.style.height = "38px";
        input.disabled = false;
        input.focus();
        scrollToBottom();
    }

    function toggleChat(forceState) {
        const isOpen = forceState !== undefined ? forceState : !windowEl.classList.contains("is-open");
        if (isOpen) {
            windowEl.classList.add("is-open");
            trigger.classList.add("is-active");
            trigger.setAttribute("aria-expanded", "true");
            try {
                sessionStorage.setItem(STATE_KEY, "1");
            } catch (e) {}
            setTimeout(() => {
                input.focus();
                scrollToBottom();
            }, 60);
        } else {
            windowEl.classList.remove("is-open");
            trigger.classList.remove("is-active");
            trigger.setAttribute("aria-expanded", "false");
            try {
                sessionStorage.setItem(STATE_KEY, "0");
            } catch (e) {}
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

    if (resetBtn) {
        resetBtn.addEventListener("click", (e) => {
            e.preventDefault();
            e.stopPropagation();
            resetChat();
        });
    }

    document.addEventListener("keydown", (e) => {
        if (e.key === "Escape" && windowEl.classList.contains("is-open")) {
            toggleChat(false);
        }
    });

    function onQuickRepliesClick(e) {
        const pill = e.target.closest(".quick-reply-pill");
        if (!pill) return;
        const prompt = pill.dataset.prompt || pill.textContent.trim();
        hideQuickReplies();
        sendMessage(prompt);
    }

    function bindQuickReplies() {
        const quick = document.getElementById("chat-quick-replies");
        if (quick) {
            quick.removeEventListener("click", onQuickRepliesClick);
            quick.addEventListener("click", onQuickRepliesClick);
        }
    }

    bindQuickReplies();

    messagesEl.addEventListener("click", (event) => {
        const link = event.target.closest?.("a.chat-material-link");
        const token = link?.closest("[data-chat-click-context]")?.dataset.chatClickContext;
        const material = link?.getAttribute("href")?.match(/^\/materials\/([a-zA-Z0-9_-]+)$/);
        if (!token || !material) return;
        fetch("/api/chat/card-open", {
            method: "POST", keepalive: true,
            headers: { "Content-Type": "application/json" },
            body: JSON.stringify({ click_context: token, material_id: material[1] }),
        }).catch(() => {});
    });

    async function sendMessage(text) {
        const query = (text || "").trim();
        if (!query || activeRequest) return;

        const request = new AbortController();
        activeRequest = request;
        const timeout = setTimeout(() => request.abort(), 12000);
        const previous = history.slice(-6).map(({ role, text }) => ({ role, text }));

        hideQuickReplies();

        appendMessage(query, "user", false);
        history.push({ role: "user", text: query });
        saveState();

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
                signal: request.signal,
                headers: {
                    "Content-Type": "application/json",
                    "Accept": "application/json",
                },
                body: JSON.stringify({ message: query, history: previous, card_context: cardContext }),
            });

            const data = await res.json();
            if (activeRequest !== request) return;
            if (!res.ok) {
                appendMessage(data.error || "Не удалось отправить сообщение. Попробуйте ещё раз.", "assistant", false);
                return;
            }

            const reply = data.reply || "Извините, не удалось сформировать ответ. Попробуйте еще раз.";
            cardContext = typeof data.card_context === "string" ? data.card_context : null;
            const entry = { role: "model", text: reply, cards: data.cards, clickContext: data.click_context };
            appendMessage(reply, "assistant", false, entry);
            history.push(entry);
            saveState();
        } catch (err) {
            if (activeRequest !== request) return;
            appendMessage(
                "Не удалось получить ответ вовремя. Попробуйте отправить сообщение ещё раз.",
                "assistant",
                false
            );
        } finally {
            clearTimeout(timeout);
            if (activeRequest !== request) return;
            activeRequest = null;
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

    // Initialize state from sessionStorage
    loadState();
    try {
        if (sessionStorage.getItem(STATE_KEY) === "1") {
            toggleChat(true);
        }
    } catch (e) {}
})();
