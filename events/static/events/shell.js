// The light switch and the 12h/24h clock switch in the top bar.
// Times inside [data-clock] are formatted here from the 24-hour value in the attribute (`H:i`).
// A value with brackets is a template for a whole label: data-clock="[23:00] – Oct 24, [01:00]".
// Pages that add content later (dialogs, chat) call window.syncmilyClocks(root) to format it.
(function () {
    var root = document.documentElement;
    function load(key) { try { return localStorage.getItem(key); } catch (e) { return null; } }
    function save(key, value) { try { localStorage.setItem(key, value); } catch (e) {} }

    document.addEventListener("DOMContentLoaded", function () {
        // Light switch: follows the OS until flipped, then remembers the choice.
        var themeSwitch = document.getElementById("theme-switch");
        var themeColor = document.querySelector('meta[name="theme-color"]');
        function syncTheme() {
            if (themeSwitch) themeSwitch.setAttribute("aria-checked", String(root.dataset.theme === "dark"));
            if (themeColor) themeColor.content = root.dataset.theme === "dark" && document.body.classList.contains("paint") ? "#1b1e1c" : "#ffffff";
        }
        syncTheme();
        if (themeSwitch) themeSwitch.addEventListener("click", function () {
            root.dataset.theme = root.dataset.theme === "dark" ? "light" : "dark";
            save("syncmily-theme", root.dataset.theme);
            syncTheme();
        });
        var os = window.matchMedia("(prefers-color-scheme: dark)");
        os.addEventListener("change", function (e) {
            if (load("syncmily-theme")) return;
            root.dataset.theme = e.matches ? "dark" : "light";
            syncTheme();
        });

        // Clock switch: 12-hour (US) or 24-hour, defaulting to the browser's locale.
        var clockSwitch = document.getElementById("clock-switch");
        var f12 = new Intl.DateTimeFormat("en-US", { hour: "numeric", minute: "2-digit", hour12: true });
        var f12hour = new Intl.DateTimeFormat("en-US", { hour: "numeric", hour12: true });
        var f24 = new Intl.DateTimeFormat("en-GB", { hour: "2-digit", minute: "2-digit", hourCycle: "h23" });
        function formatTime(value, use24, axis) {
            var parts = value.split(":");
            var moment = new Date(2026, 0, 1, +parts[0], +parts[1]);
            return use24 ? f24.format(moment) : (axis ? f12hour : f12).format(moment);
        }
        // <html data-clock> holds the chosen mode, so only look inside the body.
        function formatClocks(scope) {
            var use24 = root.dataset.clock === "24";
            (scope || document.body).querySelectorAll("[data-clock]").forEach(function (el) {
                var spec = el.dataset.clock;
                if (/^\d{1,2}:\d{2}$/.test(spec)) el.textContent = formatTime(spec, use24, "axis" in el.dataset);
                else if (spec.indexOf("[") >= 0) {
                    el.textContent = spec.replace(/\[(\d{1,2}:\d{2})\]/g, function (match, time) { return formatTime(time, use24, false); });
                }
            });
        }
        window.syncmilyClocks = formatClocks;
        function setClock(mode, remember) {
            root.dataset.clock = mode;
            formatClocks(document.body);
            if (clockSwitch) clockSwitch.setAttribute("aria-checked", String(mode === "24"));
            if (remember) save("syncmily-clock", mode);
        }
        var localeHour12 = new Intl.DateTimeFormat(navigator.language, { hour: "numeric" }).resolvedOptions().hour12;
        setClock(load("syncmily-clock") || (localeHour12 === false ? "24" : "12"), false);
        if (clockSwitch) clockSwitch.addEventListener("click", function () {
            setClock(root.dataset.clock === "24" ? "12" : "24", true);
        });

        // Header menus (date, Colors, account) are <details>: one open at a time, and Escape or a
        // click anywhere else closes them, like every other menu on the web.
        var menus = Array.prototype.slice.call(document.querySelectorAll(".bar details, details[data-menu]"));
        menus.forEach(function (menu) {
            menu.addEventListener("toggle", function () {
                if (menu.open) menus.forEach(function (other) { if (other !== menu) other.open = false; });
            });
        });
        document.addEventListener("click", function (event) {
            menus.forEach(function (menu) { if (menu.open && !menu.contains(event.target)) menu.open = false; });
        });
        document.addEventListener("keydown", function (event) {
            if (event.key !== "Escape") return;
            menus.forEach(function (menu) {
                if (!menu.open) return;
                menu.open = false;
                menu.querySelector("summary").focus();
            });
        });

        // View shortcuts, as in Google Calendar: D day, W week, M month, L list. Ignored while
        // typing, with a modifier held, or with a dialog open.
        document.addEventListener("keydown", function (event) {
            if (event.metaKey || event.ctrlKey || event.altKey || event.defaultPrevented) return;
            var target = event.target;
            if (target.closest && target.closest("input, textarea, select, [contenteditable], dialog[open]")) return;
            if (document.querySelector("dialog[open]")) return;
            var link = document.querySelector('.view-pop a[data-shortcut="' + event.key.toUpperCase() + '"]');
            if (!link || event.key.length !== 1) return;
            event.preventDefault();
            window.location.href = link.href;
        });

        // On phones the 12h/24h and light/dark switches move into the account menu, so the top
        // bar keeps only the date menu, Add event and the account button.
        var switches = document.querySelector(".bar-switches");
        var accountMenu = document.querySelector(".bar .acct-menu");
        if (switches && accountMenu) {
            var home = switches.parentNode, after = switches.nextSibling;
            var phone = window.matchMedia("(max-width: 560px)");
            var place = function () {
                if (phone.matches) accountMenu.insertBefore(switches, accountMenu.querySelector(".sign-out"));
                else home.insertBefore(switches, after);
                switches.classList.toggle("in-menu", phone.matches);
            };
            place();
            phone.addEventListener("change", place);
        }
    });

    // "Show all time zones": a time-zone menu lists common zones; this swaps in every zone.
    // Delegated, because the Add event form loads into a dialog after the page.
    document.addEventListener("click", function (event) {
        var button = event.target.closest && event.target.closest("[data-all-timezones]");
        if (!button) return;
        var select = document.getElementById(button.dataset.for);
        if (!select) return;
        button.disabled = true;
        fetch(button.dataset.allTimezones, { credentials: "same-origin" })
            .then(function (response) { if (!response.ok) throw new Error(); return response.json(); })
            .then(function (names) {
                var chosen = select.value, first = select.options[0];
                var keepFirst = first && first.value === "" ? first : null;
                select.textContent = "";
                if (keepFirst) select.appendChild(keepFirst);
                names.forEach(function (name) { select.appendChild(new Option(name, name)); });
                select.value = chosen;
                button.remove();
                select.focus();
            })
            .catch(function () {
                button.disabled = false;
                button.textContent = "Couldn’t load the list. Try again";
            });
    });
})();
