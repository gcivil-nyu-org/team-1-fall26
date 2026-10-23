// Household chat: live messages over the workspace WebSocket.
// Loaded as a file (not inline) so the Content-Security-Policy can forbid inline scripts.
(function () {
    const workspaceId = JSON.parse(
        document.getElementById("chat-workspace-id").textContent
    );

    const currentUserId = JSON.parse(
        document.getElementById("chat-user-id").textContent
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

    function appendMessage(data) {
        const wrapper = document.createElement("div");
        wrapper.className = "message";

        if (String(data.sender_id) === String(currentUserId)) {
            wrapper.classList.add("own");
        }

        const sender = document.createElement("div");
        sender.className = "message-sender";
        sender.textContent = data.sender;

        const bubble = document.createElement("div");
        bubble.className = "message-bubble";
        bubble.textContent = data.content;

        const time = document.createElement("div");
        time.className = "message-time";

        const parsedDate = new Date(data.created_at);
        time.textContent = Number.isNaN(parsedDate.getTime())
            ? ""
            : parsedDate.toLocaleString();

        wrapper.append(sender, bubble, time);
        messages.appendChild(wrapper);

        scrollToBottom();
    }

    function connect() {
        status.textContent = "Connecting...";
        socket = new WebSocket(socketUrl);

        socket.onopen = function () {
            attempts = 0;
            status.textContent = "Connected";
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

            status.textContent = "Disconnected. Reconnecting...";

            attempts += 1;
            const delay = Math.min(1000 * 2 ** attempts, 15000);

            reconnectTimer = setTimeout(connect, delay);
        };

        socket.onerror = function () {
            status.textContent = "Connection error";
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
