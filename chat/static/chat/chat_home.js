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
    const sendButton = document.getElementById("chat-send");
    const status = document.getElementById("chat-status");
    const error = document.getElementById("chat-error");

    const protocol = location.protocol === "https:" ? "wss:" : "ws:";
    const socketUrl = `${protocol}//${location.host}/ws/chat/${workspaceId}/`;

    let socket;
    let reconnectTimer;
    let attempts = 0;
    let intentionallyClosed = false;

    function scrollToBottom() {
        messages.scrollTop = messages.scrollHeight;
    }

    function pad(number) {
        return String(number).padStart(2, "0");
    }

    function appendMessage(data) {
        const mine = String(data.sender_id) === String(currentUserId);
        const member = members[String(data.sender_id)] || {
            name: data.sender, initial: (data.sender || "?").charAt(0).toUpperCase(), hue: "", color: "",
        };
        const sent = new Date(data.created_at);
        const valid = !Number.isNaN(sent.getTime());

        document.getElementById("chat-empty")?.remove();
        const day = valid ? `${sent.getFullYear()}-${pad(sent.getMonth() + 1)}-${pad(sent.getDate())}` : "";
        const separators = messages.querySelectorAll(".day-sep");
        const lastDay = separators.length ? separators[separators.length - 1].dataset.day : "";
        if (day && day !== lastDay) {
            const separator = document.createElement("p");
            separator.className = "day-sep";
            separator.dataset.day = day;
            separator.textContent = "Today";
            messages.appendChild(separator);
        }

        const wrapper = document.createElement("div");
        wrapper.className = mine ? "msg mine" : "msg";
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
        const bubble = document.createElement("p");
        bubble.className = "bubble";
        bubble.textContent = data.content;

        body.append(meta, bubble);
        wrapper.append(dot, body);
        messages.appendChild(wrapper);
        if (window.syncmilyClocks) window.syncmilyClocks(wrapper);

        scrollToBottom();
    }

    function connect() {
        status.textContent = "Connecting…";
        socket = new WebSocket(socketUrl);

        socket.onopen = function () {
            attempts = 0;
            status.textContent = "";
            error.textContent = "";
            input.disabled = false;
            sendButton.disabled = false;
            input.focus();
        };

        socket.onmessage = function (event) {
            const data = JSON.parse(event.data);

            if (data.type === "chat_message") {
                appendMessage(data);
            } else if (data.type === "error") {
                error.textContent = data.message;
            }
        };

        socket.onclose = function (event) {
            input.disabled = true;
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

        socket.send(JSON.stringify({
            type: "chat_message",
            content: content
        }));

        input.value = "";
        input.focus();
    });

    window.addEventListener("beforeunload", function () {
        intentionallyClosed = true;
        clearTimeout(reconnectTimer);

        if (socket) {
            socket.close();
        }
    });

    scrollToBottom();
    connect();
})();
