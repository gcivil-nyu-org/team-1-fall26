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
            if (window.syncmilyClocks) window.syncmilyClocks(body);
            if (body.querySelector("[data-event-form]")) {
                loadScript(sheet.dataset.locationScript);
                if (sheet.dataset.progressiveScript) loadScript(sheet.dataset.progressiveScript);
            }
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
        var form = day.querySelector(".my-day-actions");
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
        // Parents change how a plan is reached right here. Picking a mode shows Confirm; nothing is
        // saved until they press it.
        day.querySelectorAll("form[data-transport-mode]").forEach(function (modeForm) {
            var select = modeForm.querySelector("select");
            var confirmButton = modeForm.querySelector("[data-mode-confirm]");
            var status = modeForm.querySelector("[role='status']");
            var saved = select.value;
            confirmButton.hidden = true;
            select.addEventListener("change", function () {
                confirmButton.hidden = select.value === saved;
                status.textContent = "";
            });
            modeForm.addEventListener("submit", function (event) {
                event.preventDefault();
                if (confirmButton.disabled) return;
                confirmButton.disabled = true;
                status.textContent = "Saving…";
                fetch(modeForm.action, {
                    method: "POST", body: new FormData(modeForm), credentials: "same-origin",
                    headers: { "X-Requested-With": "fetch" },
                }).then(function (response) {
                    if (!response.ok) throw new Error(String(response.status));
                    saved = select.value;
                    confirmButton.hidden = true;
                    status.textContent = "Saved";
                }).catch(function () {
                    status.textContent = "Couldn’t save. Try again.";
                }).finally(function () {
                    confirmButton.disabled = false;
                });
            });
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
            // Hex codes beside the pickers: typing one moves the picker, moving the picker
            // rewrites the code. Only the picker is sent, so the server check stays the same.
            form.querySelectorAll("[data-hex]").forEach(function (field) {
                var picker = field.closest(".colour-item, .member-colour").querySelector('input[type="color"]');
                if (!picker) return;
                function show() { field.value = picker.value.toUpperCase(); field.setCustomValidity(""); }
                function typed() {
                    var value = field.value.trim();
                    if (value && value.charAt(0) !== "#") value = "#" + value;
                    return /^#[0-9a-f]{6}$/i.test(value) ? value.toLowerCase() : "";
                }
                function apply(eventName) {
                    var value = typed();
                    if (!value) return false;
                    picker.value = value;
                    picker.dispatchEvent(new Event(eventName, { bubbles: true }));
                    return true;
                }
                function follow() { if (document.activeElement !== field) show(); }
                field.hidden = false;
                show();
                picker.addEventListener("input", follow);
                picker.addEventListener("change", follow);
                field.addEventListener("input", function () { apply("input"); });
                field.addEventListener("change", function () {
                    if (apply("change")) show();
                    else field.setCustomValidity("Use a hex code like #4A6FD8.");
                });
                field.addEventListener("keydown", function (event) {
                    if (event.key !== "Enter") return;
                    event.preventDefault();
                    field.dispatchEvent(new Event("change", { bubbles: true }));
                });
            });
            form.addEventListener("submit", function () {
                inputs.forEach(function (input) { input.disabled = input.value === input.dataset.initial; });
            });
            // The header's Individual colors save as soon as the picker closes: only that
            // member's colour is sent, then the page reloads so every chip shows it.
            if (form.hasAttribute("data-autosave")) {
                inputs.forEach(function (input) {
                    input.addEventListener("change", function () {
                        if (input.value === input.dataset.initial) return;
                        var data = new FormData();
                        data.append("csrfmiddlewaretoken", form.querySelector("[name=csrfmiddlewaretoken]").value);
                        data.append("next", form.querySelector("[name=next]").value);
                        data.append(input.name, input.value);
                        fetch(form.action, {
                            method: "POST", body: data, credentials: "same-origin",
                            headers: { "X-Requested-With": "fetch" },
                        }).then(function (response) {
                            if (!response.ok) throw new Error(String(response.status));
                            location.reload();
                        }).catch(function () {
                            input.value = input.dataset.initial;
                        });
                    });
                });
            }
            window.addEventListener("pageshow", function () {
                inputs.forEach(function (input) { input.disabled = false; });
            });
        });
    });

    // ---- Day view: right-click an empty time to add an event that starts there. The menu
    // follows the WAI-ARIA menu pattern: arrows move, Enter opens, Esc closes and returns
    // focus. Shift+F10 or the context-menu key opens it on a focused member's day. ----
    ready(function () {
        var grid = document.querySelector(".daygrid[data-add-at]");
        if (!grid) return;
        var marks = Array.prototype.map.call(grid.querySelectorAll(".axis [data-axis]"), function (mark) {
            var parts = mark.dataset.clock.split(":");
            return { top: parseFloat(mark.style.top), minutes: (+parts[0] || 24) * 60 + (+parts[1] || 0) };
        }).sort(function (a, b) { return a.top - b.top; });
        if (marks.length < 2) return;
        // The last mark is midnight at the bottom edge: count it as 24:00, not 00:00.
        marks.forEach(function (mark, i) { if (i < marks.length - 1 && mark.minutes === 24 * 60) mark.minutes = 0; });

        var menu = document.createElement("div");
        menu.className = "slot-menu";
        menu.setAttribute("role", "menu");
        menu.hidden = true;
        var item = document.createElement("a");
        item.setAttribute("role", "menuitem");
        item.setAttribute("data-dialog", "");
        item.setAttribute("tabindex", "-1");
        menu.appendChild(item);
        // Inside the page's main landmark, so screen readers find it with the day.
        (grid.closest("main") || document.body).appendChild(menu);
        var returnTo = null;

        function minutesAt(percent) {
            for (var i = 0; i < marks.length - 1; i++) {
                var a = marks[i], b = marks[i + 1];
                if (percent < b.top || i === marks.length - 2) {
                    var share = Math.max(0, Math.min(1, (percent - a.top) / (b.top - a.top)));
                    var minutes = a.minutes + share * (b.minutes - a.minutes);
                    return Math.min(Math.floor(minutes / 30) * 30, 23 * 60 + 30);
                }
            }
            return marks[0].minutes;
        }
        function label(minutes) {
            var h = Math.floor(minutes / 60), m = minutes % 60, mm = (m < 10 ? "0" : "") + m;
            if (document.documentElement.dataset.clock === "24") return (h < 10 ? "0" : "") + h + ":" + mm;
            return ((h + 11) % 12 + 1) + ":" + mm + " " + (h < 12 ? "AM" : "PM");
        }
        function close(restore) {
            if (menu.hidden) return;
            menu.hidden = true;
            if (restore && returnTo) returnTo.focus();
        }
        function open(minutes, x, y, from) {
            var h = Math.floor(minutes / 60), m = minutes % 60;
            var value = (h < 10 ? "0" : "") + h + ":" + (m < 10 ? "0" : "") + m;
            item.href = grid.dataset.addAt + "&time=" + value;
            item.textContent = "Add event at " + label(minutes);
            returnTo = from;
            menu.hidden = false;
            var box = menu.getBoundingClientRect();
            menu.style.left = Math.min(x, window.innerWidth - box.width - 8) + "px";
            menu.style.top = Math.min(y, window.innerHeight - box.height - 8) + "px";
            item.focus();
        }

        grid.addEventListener("contextmenu", function (event) {
            var track = event.target.closest(".track");
            // Plans keep the browser's own menu (open in a new tab, copy link…).
            if (!track || event.target.closest("a, .plan")) return;
            event.preventDefault();
            var box = track.getBoundingClientRect();
            var percent = (event.clientY - box.top) / box.height * 100;
            open(minutesAt(percent), event.clientX, event.clientY, track);
        });
        grid.addEventListener("keydown", function (event) {
            var track = event.target.closest(".track");
            if (!track || event.target !== track) return;
            if (event.key !== "ContextMenu" && !(event.key === "F10" && event.shiftKey)) return;
            event.preventDefault();
            // From the keyboard: the next half hour from now if the day shows it, else its first hour.
            var now = new Date(), minutes = now.getHours() * 60 + now.getMinutes();
            var first = marks[0].minutes, start = Math.ceil(minutes / 30) * 30;
            var target = grid.querySelector(".now") && start < 24 * 60 ? start : first;
            var box = track.getBoundingClientRect();
            open(Math.max(target, first), box.left + 12, box.top + 12, track);
        });
        menu.addEventListener("keydown", function (event) {
            if (event.key === "Escape") { event.preventDefault(); close(true); }
            else if (event.key === "Tab") { close(false); }
            else if (["ArrowDown", "ArrowUp", "Home", "End"].indexOf(event.key) >= 0) { event.preventDefault(); item.focus(); }
            else if (event.key === " ") { event.preventDefault(); item.click(); }
        });
        item.addEventListener("click", function () { close(false); });
        document.addEventListener("pointerdown", function (event) { if (!menu.contains(event.target)) close(false); });
        window.addEventListener("scroll", function () { close(false); }, { passive: true });
        window.addEventListener("resize", function () { close(false); });
    });
})();
