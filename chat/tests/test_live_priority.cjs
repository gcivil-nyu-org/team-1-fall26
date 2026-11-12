const assert = require("node:assert/strict");
const fs = require("node:fs");
const path = require("node:path");
const test = require("node:test");
const vm = require("node:vm");

const script = fs.readFileSync(
    path.join(__dirname, "../static/chat/chat_home.js"), "utf8"
);

function element() {
    return {
        children: [],
        dataset: {},
        append(...children) { this.children.push(...children); },
        appendChild(child) { this.append(child); },
        querySelectorAll() { return []; },
        setAttribute() {},
        addEventListener() {},
    };
}

test("live messages use the same priority badge as history", () => {
    const elements = {
        "chat-workspace-id": {textContent: '"household"'},
        "chat-user-id": {textContent: "1"},
        "chat-members": {textContent: "{}"},
        "chat-messages": element(),
        "chat-form": element(),
        "chat-status": element(),
    };
    let socket;
    vm.runInNewContext(script, {
        document: {
            getElementById: id => elements[id] ?? null,
            createElement: element,
        },
        window: {addEventListener() {}},
        location: {protocol: "http:", host: "localhost"},
        WebSocket: class { constructor() { socket = this; } },
    });

    for (const priority of ["high", "normal", undefined]) {
        socket.onmessage({data: JSON.stringify({
            type: "chat_message",
            sender_id: 2,
            sender: "child",
            created_at: "2026-11-11T12:00:00Z",
            content: "Pickup update",
            priority,
        })});

        const message = elements["chat-messages"].children.at(-1);
        const [meta, bubble] = message.children[1].children;
        const badges = meta.children.filter(child => child.className === "msg-priority");
        assert.deepEqual(
            badges.map(badge => badge.textContent),
            priority === "high" ? ["High priority"] : [],
            `badge for priority ${priority}`
        );
        assert.equal(bubble.textContent, "Pickup update");
    }
});
