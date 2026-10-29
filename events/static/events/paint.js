// Paint-chip pages: the sheet (event details and the add-event form open over the page, as the
// product brief asks), confirmations before deleting, the plan finder on the list, and the
// colour editor on the day page. Everything here is an enhancement: without it, links and forms
// still work as ordinary pages.
(function () {
    "use strict";

    function ready(fn) {
        if (document.readyState === "loading") document.addEventListener("DOMContentLoaded", fn);
        else fn();
    }

    // ---- Confirm before a destructive form submits (inline onsubmit is blocked by the CSP) ----
    function askConfirm(form, submitter) {
        var confirmDialog = document.getElementById("confirm");
        if (!confirmDialog || !confirmDialog.showModal) return false;
        if (!confirmDialog.dataset.ready) {
            confirmDialog.dataset.ready = "yes";
            confirmDialog.querySelectorAll("[data-close]").forEach(function (button) {
                button.addEventListener("click", function () { confirmDialog.close(); });
            });
        }
        confirmDialog.querySelector("#confirm-title").textContent = form.dataset.confirmTitle || "Are you sure?";
        confirmDialog.querySelector("#confirm-text").textContent = form.dataset.confirm;
        confirmDialog.querySelector("[data-keep]").textContent = form.dataset.confirmKeep || "Keep it";
        var go = confirmDialog.querySelector("[data-go]");
        go.textContent = (submitter && submitter.textContent.trim()) || "Delete";
        go.onclick = function () {
            confirmDialog.close();
            form.dataset.confirmed = "yes";
            if (form.requestSubmit) form.requestSubmit(submitter || undefined);
            else form.submit();
            delete form.dataset.confirmed;
        };
        confirmDialog.showModal();
        confirmDialog.querySelector("[data-keep]").focus();
        return true;
    }
    document.addEventListener("submit", function (event) {
        var form = event.target;
        if (!form.hasAttribute("data-confirm") || form.dataset.confirmed === "yes") return;
        if (askConfirm(form, event.submitter)) {
            event.preventDefault();
            event.stopImmediatePropagation();
        }
    }, true);

    // ---- The sheet ----
    ready(function () {
        var sheet = document.getElementById("sheet");
        if (!sheet || !sheet.showModal) return;
        var body = document.getElementById("sheet-body");
        var title = document.getElementById("sheet-title");
        var pageLink = document.getElementById("sheet-page");
        var status = document.getElementById("sheet-status");
        var current = "", returnFocus = null, changed = false, request = null, saving = false;
        title.tabIndex = -1;

        function setBusy(busy, posting) {
            saving = busy && posting;
            sheet.setAttribute("aria-busy", String(busy));
            body.querySelectorAll("button[type='submit']").forEach(function (button) { button.disabled = busy; });
        }

        function showError(message) {
            var note = document.createElement("p");
            note.className = "form-error sheet-error";
            note.setAttribute("role", "alert");
            note.textContent = message;
            body.querySelectorAll(".sheet-error").forEach(function (old) { old.remove(); });
            body.prepend(note);
        }

        // Ids inside the sheet get a prefix so they can't clash with the page underneath.
        function prefixIds(root) {
            var ids = new Map();
            root.querySelectorAll("[id]").forEach(function (el) {
                ids.set(el.id, "sheet-" + el.id);
                el.id = "sheet-" + el.id;
            });
            root.querySelectorAll("[for], [aria-labelledby], [aria-describedby], [aria-controls], [href^='#']").forEach(function (el) {
                ["for", "aria-labelledby", "aria-describedby", "aria-controls"].forEach(function (name) {
                    if (!el.hasAttribute(name)) return;
                    el.setAttribute(name, el.getAttribute(name).split(" ").map(function (id) { return ids.get(id) || id; }).join(" "));
                });
                var href = el.getAttribute("href");
                if (href && href.charAt(0) === "#" && ids.has(href.slice(1))) el.setAttribute("href", "#" + ids.get(href.slice(1)));
            });
        }

        function loadScript(url) {
            if (!url || document.querySelector('script[src="' + url + '"]')) return;
            var script = document.createElement("script");
            script.src = url;
            document.head.appendChild(script);
        }

        function render(html, url) {
            var page = new DOMParser().parseFromString(html, "text/html");
            var main = page.getElementById("main-content");
            if (!main || !page.body.classList.contains("paint")) {
                window.location.assign(url);
                return;
            }
            main.querySelectorAll("script").forEach(function (script) { script.remove(); });
            var heading = main.querySelector("h1");
            title.textContent = heading ? heading.textContent.trim() : page.title.split(" | ")[0];
            prefixIds(main);
            body.replaceChildren.apply(body, Array.prototype.map.call(main.childNodes, function (node) {
                return document.importNode(node, true);
            }));
            current = url;
            pageLink.href = url;
            if (window.syncmilyClocks) window.syncmilyClocks(body);
            if (body.querySelector("[data-event-form]")) loadScript(sheet.dataset.locationScript);
            sheet.scrollTop = 0;
            var focus = body.querySelector(".form-error, .errorlist, [aria-invalid='true']");
            if (focus && !focus.matches("input, select, textarea")) focus.tabIndex = -1;
            if (!focus) focus = body.querySelector("[data-autofocus]") || title;
            focus.focus({ preventScroll: true });
            if (status) status.textContent = title.textContent + " is open.";
        }

        function load(url, options) {
            if (request) request.abort();
            var mine = new AbortController();
            request = mine;
            var posting = !!(options && options.method === "POST");
            setBusy(true, posting);
            fetch(url, Object.assign({ credentials: "same-origin", signal: mine.signal }, options || {}))
                .then(function (response) {
                    if (new URL(response.url).pathname === "/accounts/login/") {
                        window.location.assign(response.url);
                        return null;
                    }
                    if (!response.ok) {
                        throw new Error(response.status === 403 || response.status === 404
                            ? "This isn’t available to you any more. Close this and reload the page."
                            : "Something went wrong. Your details are still here, so try again.");
                    }
                    if (posting && response.redirected) changed = true;
                    return response.text().then(function (html) {
                        if (request === mine && sheet.open) render(html, response.url);
                    });
                })
                .catch(function (error) {
                    if (error.name === "AbortError") return;
                    showError(error instanceof TypeError ? "Couldn’t reach Syncmily. Your details are still here, so try again." : error.message);
                })
                .finally(function () {
                    if (request === mine) {
                        request = null;
                        setBusy(false, false);
                    }
                });
        }

        function open(link) {
            returnFocus = link;
            changed = false;
            title.textContent = link.dataset.sheetTitle || "Opening…";
            body.replaceChildren();
            pageLink.href = link.href;
            sheet.showModal();
            title.focus();
            load(link.href);
        }

        document.addEventListener("click", function (event) {
            var link = event.target.closest("a[data-dialog]");
            if (!link || event.button !== 0 || event.metaKey || event.ctrlKey || event.shiftKey || event.altKey) return;
            if (new URL(link.href, window.location.href).origin !== window.location.origin) return;
            event.preventDefault();
            if (sheet.open) load(link.href);
            else open(link);
        });

        document.addEventListener("submit", function (event) {
            var form = event.target;
            if (!sheet.open || !body.contains(form) || form.hasAttribute("data-full-page")) return;
            event.preventDefault();
            if (sheet.getAttribute("aria-busy") === "true") return;
            var data = new FormData(form);
            if (event.submitter && event.submitter.name) data.append(event.submitter.name, event.submitter.value);
            var method = (form.getAttribute("method") || "get").toUpperCase();
            var url = new URL(form.getAttribute("action") || current, window.location.href);
            if (method === "GET") {
                url.search = new URLSearchParams(data).toString();
                load(url.href);
            } else {
                load(url.href, { method: "POST", body: data });
            }
        });

        sheet.querySelector("[data-close]").addEventListener("click", function () { sheet.close(); });
        sheet.addEventListener("cancel", function (event) { if (saving) event.preventDefault(); });
        sheet.addEventListener("click", function (event) {
            if (event.target !== sheet || saving) return;
            var box = sheet.getBoundingClientRect();
            if (event.clientX < box.left || event.clientX > box.right || event.clientY < box.top || event.clientY > box.bottom) sheet.close();
        });
        sheet.addEventListener("close", function () {
            if (request) request.abort();
            if (changed) {
                window.location.reload();
                return;
            }
            if (returnFocus && returnFocus.isConnected) returnFocus.focus();
        });
    });

    // ---- The kid's "Your day" pop-up: opens by itself on the first visit of the day and from the
    // "Your day" button. Closing it in any way (Got it, the X, Escape) tells the server once, so it
    // stays closed until tomorrow. Without script the dashboard still shows the same plans. ----
    ready(function () {
        var day = document.getElementById("my-day");
        if (!day || !day.showModal) return;
        var form = day.querySelector("form");
        var title = document.getElementById("my-day-title");
        var opener = document.querySelector("[data-open-my-day]");
        var returnFocus = null, told = false;
        function open(from) {
            returnFocus = from || null;
            day.showModal();
            title.focus({ preventScroll: true });
        }
        if (opener) {
            opener.hidden = false;
            opener.addEventListener("click", function () { open(opener); });
        }
        day.querySelector("[data-close]").addEventListener("click", function () { day.close(); });
        form.addEventListener("submit", function (event) {
            event.preventDefault();
            day.close();
        });
        day.addEventListener("close", function () {
            if (!told) {
                told = true;
                fetch(form.action, {
                    method: "POST", body: new FormData(form), credentials: "same-origin",
                    headers: { "X-Requested-With": "fetch" },
                }).catch(function () { told = false; });
            }
            var target = returnFocus && returnFocus.isConnected ? returnFocus : opener;
            if (target && !target.hidden) target.focus();
        });
        if (day.hasAttribute("data-auto-open")) open(null);
    });

    // ---- The plan finder on the schedule list ----
    ready(function () {
        var input = document.querySelector("[data-find-input]");
        if (!input) return;
        var empty = document.getElementById("find-empty");
        input.addEventListener("input", function () {
            var query = input.value.trim().toLocaleLowerCase();
            var shown = 0;
            document.querySelectorAll("[data-find-item]").forEach(function (row) {
                var match = !query || row.textContent.toLocaleLowerCase().indexOf(query) >= 0;
                row.hidden = !match;
                if (match) shown++;
            });
            document.querySelectorAll("[data-find-group]").forEach(function (heading) {
                var next = heading.nextElementSibling, any = false;
                while (next && !next.hasAttribute("data-find-group")) {
                    if (next.hasAttribute("data-find-item") && !next.hidden) any = true;
                    next = next.nextElementSibling;
                }
                heading.hidden = !any;
            });
            if (empty) empty.hidden = shown > 0;
        });
    });

    // ---- The colour editor: start each picker on the colour the member is drawn in now, and only
    // send the ones that changed, so untouched members keep following the family's deck. ----
    ready(function () {
        var canvas = document.createElement("canvas").getContext("2d");
        function hex(colour) {
            if (!canvas || !colour) return "";
            canvas.fillStyle = "transparent";
            canvas.fillStyle = colour;
            return /^#[0-9a-f]{6}$/i.test(canvas.fillStyle) ? canvas.fillStyle : "";
        }
        document.querySelectorAll("form[data-colours]").forEach(function (form) {
            var inputs = form.querySelectorAll('input[type="color"]');
            inputs.forEach(function (input) {
                if (input.hasAttribute("data-deck")) {
                    var deck = hex(getComputedStyle(input.closest("[data-hue]")).getPropertyValue("--s4").trim());
                    if (deck) input.value = deck;
                }
                input.dataset.initial = input.value;
            });
            form.addEventListener("submit", function () {
                inputs.forEach(function (input) { input.disabled = input.value === input.dataset.initial; });
            });
            window.addEventListener("pageshow", function () {
                inputs.forEach(function (input) { input.disabled = false; });
            });
        });
    });
})();
