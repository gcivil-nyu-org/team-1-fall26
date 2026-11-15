// Household chat: live messages over the workspace WebSocket.
// Loaded as a file (not inline) so the Content-Security-Policy can forbid inline scripts.
(function () {
    const workspaceId = JSON.parse(
        document.getElementById("chat-workspace-id").textContent
    );

    const currentUserId = JSON.parse(
        document.getElementById("chat-user-id").textContent
    );

    // Names and paint colours by user id, so new messages look like the ones the server drew.
    const members = JSON.parse(
        document.getElementById("chat-members").textContent
    );

    const messages = document.getElementById("chat-messages");
    const form = document.getElementById("chat-form");
    const input = document.getElementById("chat-input");
    const priority = document.getElementById("chat-priority");
    const sendButton = document.getElementById("chat-send");
    const status = document.getElementById("chat-status");
    const error = document.getElementById("chat-error");
    const olderButton = document.getElementById("chat-older");

    const protocol = location.protocol === "https:" ? "wss:" : "ws:";
    const socketUrl = `${protocol}//${location.host}/ws/chat/${workspaceId}/`;

    let socket;
    let reconnectTimer;
    let attempts = 0;
    let intentionallyClosed = false;
    let pendingText = null;
    let offlineTimer;

    const historyUrl = `/chat/${workspaceId}/messages/`;
    const seen = new Set(
        Array.from(messages.querySelectorAll(".msg[data-id]")).map((el) => el.dataset.id)
    );

    function ids() {
        return Array.from(seen).map(Number);
    }

    function localDay(date) {
        return `${date.getFullYear()}-${pad(date.getMonth() + 1)}-${pad(date.getDate())}`;
    }

    function dayLabel(date) {
        return localDay(date) === localDay(new Date())
            ? "Today"
            : date.toLocaleDateString(undefined, { weekday: "short", day: "numeric", month: "short" });
    }

    function scrollToBottom() {
        messages.scrollTop = messages.scrollHeight;
    }

    function pad(number) {
        return String(number).padStart(2, "0");
    }

    function buildMessage(data) {
        const mine = String(data.sender_id) === String(currentUserId);
        const member = members[String(data.sender_id)] || {
            name: data.sender, initial: (data.sender || "?").charAt(0).toUpperCase(), hue: "", color: "",
        };
        const sent = new Date(data.created_at);
        const valid = !Number.isNaN(sent.getTime());

        const wrapper = document.createElement("div");
        wrapper.className = mine ? "msg mine" : "msg";
        wrapper.dataset.id = String(data.id);
        if (member.hue) wrapper.dataset.hue = member.hue;
        if (member.color) wrapper.style.setProperty("--hue", member.color);

        const dot = document.createElement("span");
        dot.className = member.hue ? "dot" : "dot dot-quiet";
        dot.setAttribute("aria-hidden", "true");
        dot.textContent = member.initial;

        const body = document.createElement("div");
        body.className = "msg-body";
        const meta = document.createElement("p");
        meta.className = "msg-meta";
        const name = document.createElement("b");
        name.translate = false;
        name.textContent = mine ? "You" : member.name;
        meta.append(name, " ");
        if (valid) {
            const time = document.createElement("time");
            time.dateTime = data.created_at;
            time.dataset.clock = `${pad(sent.getHours())}:${pad(sent.getMinutes())}`;
            time.textContent = time.dataset.clock;
            meta.appendChild(time);
        }
        if (data.priority === "high") {
            const badge = document.createElement("span");
            badge.className = "msg-priority";
            badge.textContent = "High priority";
            meta.append(" ", badge);
        }
        const bubble = document.createElement("p");
        bubble.className = "bubble";
        bubble.textContent = data.content;

        body.append(meta, bubble);
        wrapper.append(dot, body);
        return { wrapper, sent: valid ? sent : null };
    }

    function daySeparator(sent) {
        const separator = document.createElement("p");
        separator.className = "day-sep";
        separator.dataset.day = localDay(sent);
        separator.textContent = dayLabel(sent);
        return separator;
    }

    function appendMessage(data) {
        if (seen.has(String(data.id))) return;
        seen.add(String(data.id));

        const { wrapper, sent } = buildMessage(data);

        document.getElementById("chat-empty")?.remove();
        const separators = messages.querySelectorAll(".day-sep");
        const lastDay = separators.length ? separators[separators.length - 1].dataset.day : "";
        if (sent && localDay(sent) !== lastDay) {
            messages.appendChild(daySeparator(sent));
        }

        messages.appendChild(wrapper);
        if (window.syncmilyClocks) window.syncmilyClocks(wrapper);

        scrollToBottom();
    }

    // Older messages go above the current ones; text is set with textContent only.
    function prependMessages(list) {
        const fresh = list.filter((data) => !seen.has(String(data.id)));
        if (!fresh.length) return;

        const fragment = document.createDocumentFragment();
        let lastDay = "";
        fresh.forEach((data) => {
            seen.add(String(data.id));
            const { wrapper, sent } = buildMessage(data);
            const day = sent ? localDay(sent) : "";
            if (day && day !== lastDay) {
                fragment.appendChild(daySeparator(sent));
                lastDay = day;
            }
            fragment.appendChild(wrapper);
        });

        const first = messages.firstElementChild;
        if (first && first.classList.contains("day-sep") && first.dataset.day === lastDay) {
            first.remove();
        }
        const before = messages.scrollHeight;
        messages.insertBefore(fragment, messages.firstChild);
        messages.scrollTop += messages.scrollHeight - before;
        if (window.syncmilyClocks) window.syncmilyClocks(messages);
    }

    async function fetchPage(query) {
        const response = await fetch(`${historyUrl}?${query}`, {
            credentials: "same-origin",
            headers: { Accept: "application/json" },
        });
        if (!response.ok) throw new Error("History request failed");
        return response.json();
    }

    async function loadOlder() {
        const oldest = ids().length ? Math.min(...ids()) : null;
        if (oldest === null) return;
        olderButton.disabled = true;
        try {
            const page = await fetchPage(`before=${oldest}`);
            prependMessages(page.messages);
            if (!page.has_more) olderButton.remove();
            else olderButton.disabled = false;
        } catch (problem) {
            error.textContent = "Couldn’t load older messages. Try again.";
            olderButton.disabled = false;
        }
    }

    // After a drop, pick up whatever was said while the socket was down.
    async function fetchMissed() {
        try {
            let more = true;
            while (more && ids().length) {
                const page = await fetchPage(`after=${Math.max(...ids())}`);
                page.messages.forEach(appendMessage);
                more = page.has_more && page.messages.length > 0;
            }
        } catch (problem) {
            // The live socket keeps working; the next reconnect tries again.
        }
    }

    if (olderButton) olderButton.addEventListener("click", loadOlder);

    function connect() {
        status.textContent = "Connecting…";
        socket = new WebSocket(socketUrl);

        socket.onopen = function () {
            attempts = 0;
            clearTimeout(offlineTimer);
            fetchMissed();
            status.textContent = "";
            error.textContent = "";
            input.disabled = false;
            priority.disabled = false;
            sendButton.disabled = false;
            input.focus();
        };

        socket.onmessage = function (event) {
            const data = JSON.parse(event.data);

            if (data.type === "chat_message") {
                appendMessage(data);
                // The server echoed what we sent, so it is safe to clear the box.
                if (pendingText !== null && String(data.sender_id) === String(currentUserId)
                        && data.content === pendingText) {
                    if (input.value.trim() === pendingText) input.value = "";
                    priority.value = "normal";
                    pendingText = null;
                }
            } else if (data.type === "error") {
                // Keep what was typed so nothing is lost.
                pendingText = null;
                error.textContent = data.message;
            }
        };

        socket.onclose = function (event) {
            input.disabled = true;
            priority.disabled = true;
            sendButton.disabled = true;

            if (intentionallyClosed) {
                return;
            }

            if (event.code === 4403) {
                status.textContent = "Access denied";
                error.textContent = "You no longer have access to this chat.";
                return;
            }

            status.textContent = "Reconnecting…";
            clearTimeout(offlineTimer);
            offlineTimer = setTimeout(function () {
                status.textContent = "Can’t reach chat right now. It will reconnect on its own when you’re back online.";
            }, 15000);

            attempts += 1;
            const delay = Math.min(1000 * 2 ** attempts, 15000);

            reconnectTimer = setTimeout(connect, delay);
        };

        socket.onerror = function () {
            status.textContent = "Can’t connect. Trying again…";
        };
    }

    form.addEventListener("submit", function (event) {
        event.preventDefault();

        const content = input.value.trim();

        if (!content || !socket || socket.readyState !== WebSocket.OPEN) {
            return;
        }

        error.textContent = "";

        pendingText = content;
        socket.send(JSON.stringify({
            type: "chat_message",
            content: content,
            priority: priority.value,
        }));

        input.focus();
    });

    window.addEventListener("beforeunload", function () {
        intentionallyClosed = true;
        clearTimeout(reconnectTimer);
        clearTimeout(offlineTimer);

        if (socket) {
            socket.close();
        }
    });

    scrollToBottom();
    connect();
})();
