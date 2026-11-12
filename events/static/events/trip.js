// Live leave-by times: the card on event pages ([data-trip-url]) and the "Leave by"
// markers on timelines ([data-trip-marker]). Both refresh every five minutes, the same
// rate the server re-reads the MTA, while the page is visible. Works in the sheet too:
// paint.js loads this file when an event with a trip card opens there.
(function () {
    "use strict";

    var REFRESH_MS = 5 * 60 * 1000;

    function loadLocationSearch(box) {
        var url = box.dataset.locationScript;
        if (url && !document.querySelector('script[src="' + url + '"]')) {
            var script = document.createElement("script");
            script.src = url;
            document.head.appendChild(script);
        }
    }

    // ---- The card ---------------------------------------------------------------------

    function showCard(box, html) {
        box.innerHTML = html;
        // An empty card means the event has started: the trip is over, stop asking.
        if (!box.querySelector(".trip-card")) box.dataset.tripDone = "yes";
        if (window.syncmilyClocks) window.syncmilyClocks(box);
        if (box.querySelector("[data-location-form]")) loadLocationSearch(box);
    }

    function fail(box, message) {
        var error = box.querySelector(".trip-error");
        if (error) {
            error.textContent = message;
            error.hidden = false;
        } else {
            box.innerHTML = '<p class="trip-muted"></p>';
            box.firstChild.textContent = message;
        }
    }

    function loadCard(box, body) {
        box.setAttribute("aria-busy", "true");
        var options = { credentials: "same-origin" };
        if (body) {
            options.method = "POST";
            options.body = body;
            options.headers = { "X-CSRFToken": box.dataset.csrf };
        }
        return fetch(box.dataset.tripUrl, options)
            .then(function (response) {
                if (!response.ok) throw new Error("status " + response.status);
                return response.text();
            })
            .then(function (html) {
                showCard(box, html);
                // A new start point moves this event's marker too.
                if (body) refreshMarkers(document);
            })
            .catch(function () { fail(box, "Couldn’t update the trip just now. Try again in a minute."); })
            .finally(function () { box.removeAttribute("aria-busy"); });
    }

    function startFrom(box, fields) {
        var body = new FormData();
        Object.keys(fields).forEach(function (key) { body.append(key, fields[key]); });
        loadCard(box, body);
    }

    document.addEventListener("click", function (event) {
        var box = event.target.closest("[data-trip-url]");
        if (!box) return;
        var modeButton = event.target.closest("[data-trip-mode]");
        if (modeButton && !modeButton.disabled) {
            startFrom(box, { mode: modeButton.dataset.tripMode });
            return;
        }
        var locate = event.target.closest("[data-trip-locate]");
        if (!locate) return;
        if (!navigator.geolocation) {
            fail(box, "This browser can’t share your location. Search a place instead.");
            return;
        }
        locate.disabled = true;
        locate.textContent = "Finding you…";
        navigator.geolocation.getCurrentPosition(
            function (position) {
                startFrom(box, {
                    mode: "here",
                    latitude: position.coords.latitude,
                    longitude: position.coords.longitude,
                    label: "Your current location",
                });
            },
            function (error) {
                locate.disabled = false;
                locate.textContent = "Use my current location";
                fail(box, error.code === error.PERMISSION_DENIED
                    ? "Location is off for Syncmily. Allow it in your browser, or search a place."
                    : "Couldn’t find your location. Try again, or search a place.");
            },
            { enableHighAccuracy: false, timeout: 15000, maximumAge: 5 * 60 * 1000 }
        );
    });

    // Capture phase, so paint.js doesn't turn this into a sheet navigation.
    document.addEventListener("submit", function (event) {
        var form = event.target.closest("form[data-trip-start]");
        if (!form) return;
        event.preventDefault();
        event.stopPropagation();
        var box = form.closest("[data-trip-url]");
        var latitude = form.elements.latitude.value;
        var longitude = form.elements.longitude.value;
        if (!latitude || !longitude) {
            fail(box, "Pick a place from the suggestions first.");
            return;
        }
        startFrom(box, {
            mode: "here",
            latitude: latitude,
            longitude: longitude,
            label: form.elements.location.value,
        });
    }, true);

    // ---- Timeline markers -------------------------------------------------------------

    function percent(value) {
        return parseFloat(value) || 0;
    }

    // Where a moment sits on a timeline, from its hour marks ([data-at] with a top %).
    // Interpolating between marks keeps daylight-saving days right.
    function topFor(container, moment) {
        var marks = Array.prototype.map.call(container.querySelectorAll("[data-at]"), function (mark) {
            return { at: Date.parse(mark.dataset.at), top: percent(mark.style.top) };
        }).filter(function (mark) { return !isNaN(mark.at); }).sort(function (a, b) { return a.at - b.at; });
        if (marks.length < 2) return null;
        var time = moment.getTime();
        var before = marks[0], after = marks[1];
        for (var i = 1; i < marks.length; i++) {
            before = marks[i - 1];
            after = marks[i];
            if (time <= after.at) break;
        }
        var top = before.top + (time - before.at) / (after.at - before.at) * (after.top - before.top);
        return Math.max(0, Math.min(100, top));
    }

    // The event block right after a marker holds the "Leave 21:43 · B" label.
    function chipFor(marker) {
        var next = marker.nextElementSibling;
        return next && next.querySelector("[data-trip-chip]");
    }

    function drawMarker(marker, data) {
        var chip = chipFor(marker);
        if (data.status !== "ok") {
            marker.hidden = true;
            if (chip) chip.hidden = true;
            if (data.status === "unavailable") marker.dataset.tripDone = "yes";
            return;
        }
        var text = "Leave " + (document.documentElement.dataset.clock === "24" ? data.clock : data.label)
            + (data.line ? " · " + data.line : "")
            + (data.late_minutes ? " · ~" + data.late_minutes + " min late" : "")
            + (data.alerts ? " · ⚠" : "");
        var target = chip || marker;
        target.replaceChildren();
        if (data.live) {
            var dot = document.createElement("span");
            dot.className = "trip-live-dot";
            dot.setAttribute("aria-hidden", "true");
            target.appendChild(dot);
        }
        target.appendChild(document.createTextNode(text));
        target.classList.toggle("is-late", !!data.late_minutes || !!data.alerts);
        target.title = data.live ? "Live from the MTA, updated every 5 minutes" : "Planned time";
        target.hidden = false;
        // The bar shows the trip from leave time to the event's start.
        var container = marker.closest("[data-rail]");
        if (container) {
            var top = topFor(container, new Date(data.leave_by));
            var end = topFor(container, new Date(marker.dataset.eventStart));
            if (top !== null && end !== null) {
                marker.style.setProperty("--top", top + "%");
                marker.style.setProperty("--h", Math.max(end - top, 0) + "%");
            }
        }
        marker.hidden = false;
    }

    function loadMarker(marker) {
        fetch(marker.dataset.tripMarker, { credentials: "same-origin" })
            .then(function (response) { return response.ok ? response.json() : { status: "error" }; })
            .then(function (data) { drawMarker(marker, data); })
            .catch(function () { marker.hidden = true; });
    }

    function refreshMarkers(root) {
        (root || document).querySelectorAll("[data-trip-marker]:not([data-trip-done])").forEach(loadMarker);
    }

    // ---- Start up and refresh -----------------------------------------------------------

    function setup() {
        document.querySelectorAll("[data-trip-url]").forEach(function (box) {
            if (box.dataset.tripReady) return;
            box.dataset.tripReady = "yes";
            loadCard(box);
        });
        document.querySelectorAll("[data-trip-marker]").forEach(function (marker) {
            if (marker.dataset.tripReady) return;
            marker.dataset.tripReady = "yes";
            loadMarker(marker);
        });
    }

    setInterval(function () {
        if (document.hidden) return;
        document.querySelectorAll("[data-trip-url]:not([data-trip-done])").forEach(function (box) {
            // Don't reload while someone is typing a place.
            if (!box.contains(document.activeElement)) loadCard(box);
        });
        refreshMarkers(document);
    }, REFRESH_MS);

    // Event pages that open in the sheet arrive after this file has run.
    new MutationObserver(setup).observe(document.documentElement, { childList: true, subtree: true });
    if (document.readyState === "loading") document.addEventListener("DOMContentLoaded", setup);
    else setup();
})();
