
"use strict";

(function () {
    // Google Places (through our server, so the key stays private) when it's set up;
    // otherwise Photon. "unknown" until the first search finds out.
    let googleState = "unknown";
    const SUGGEST_URL = "/events/places/";
    const DETAILS_URL = "/events/places/details/";

    function newSessionToken() {
        if (window.crypto && window.crypto.randomUUID) return window.crypto.randomUUID();
        return String(Date.now()) + Math.random().toString(16).slice(2);
    }

    function csrfToken(form) {
        const input = form.querySelector('input[name="csrfmiddlewaretoken"]');
        if (input) return input.value;
        const holder = form.closest("[data-csrf]");
        if (holder) return holder.dataset.csrf;
        const match = document.cookie.match(/(?:^|; )csrftoken=([^;]+)/);
        return match ? decodeURIComponent(match[1]) : "";
    }

    function initializeLocationAutocomplete(form) {
        if (!form || form.dataset.locationAutocompleteInitialized === "true") {
            return;
        }

        const locationInput = form.querySelector('input[name="location"]');
        const latitudeInput = form.querySelector('input[name="latitude"]');
        const longitudeInput = form.querySelector('input[name="longitude"]');

        if (!locationInput || !latitudeInput || !longitudeInput) {
            return;
        }

        form.dataset.locationAutocompleteInitialized = "true";

        // Looks come from the page's stylesheet (paint.css), so the list follows light and dark.
        const wrapper = document.createElement("div");
        wrapper.className = "location-autocomplete";

        locationInput.parentNode.insertBefore(wrapper, locationInput);
        wrapper.appendChild(locationInput);

        const suggestions = document.createElement("div");
        suggestions.className = "location-suggestions";
        suggestions.setAttribute("role", "listbox");
        suggestions.setAttribute("aria-label", "Location suggestions");
        suggestions.hidden = true;
        suggestions.id = `${locationInput.id || "location"}-suggestions`;
        locationInput.setAttribute("aria-controls", suggestions.id);

        wrapper.appendChild(suggestions);

        const attribution = document.createElement("small");
        attribution.className = "location-attribution";
        attribution.textContent = "Place search by Photon and OpenStreetMap";
        wrapper.appendChild(attribution);

        let debounceTimer = null;
        let activeController = null;
        let requestVersion = 0;
        // One token per search, from first keystroke to the pick (Google bills it once).
        let sessionToken = newSessionToken();

        function post(url, fields, signal) {
            const body = new FormData();
            Object.keys(fields).forEach((key) => body.append(key, fields[key]));
            return fetch(url, {
                method: "POST",
                body,
                signal,
                credentials: "same-origin",
                headers: { "X-CSRFToken": csrfToken(form), Accept: "application/json" }
            });
        }

        function useGoogleAttribution() {
            attribution.textContent = "Powered by Google";
        }

        async function selectGooglePlace(place) {
            showMessage("Finding " + place.main + "…");
            try {
                const response = await post(DETAILS_URL, {
                    id: place.id,
                    session: sessionToken
                });
                if (!response.ok) throw new Error(`Places details returned ${response.status}`);
                const data = await response.json();
                if (!Number.isFinite(data.latitude) || !Number.isFinite(data.longitude)) {
                    throw new Error("No coordinates");
                }
                locationInput.value = data.label;
                latitudeInput.value = String(data.latitude);
                longitudeInput.value = String(data.longitude);
                sessionToken = newSessionToken();
                clearTimeout(debounceTimer);
                activeController?.abort();
                requestVersion += 1;
                closeSuggestions();
                locationInput.focus();
                locationInput.dispatchEvent(new Event("change", { bubbles: true }));
            } catch (error) {
                showMessage("Couldn’t get that place. Try another suggestion.");
                console.error("Google place details failed:", error);
            }
        }

        function renderGoogleSuggestions(places) {
            suggestions.replaceChildren();
            if (!places.length) {
                showMessage("No matching locations found.");
                return;
            }
            places.forEach((place) => {
                const option = document.createElement("button");
                option.type = "button";
                option.setAttribute("role", "option");
                const main = document.createElement("span");
                main.textContent = place.main;
                option.appendChild(main);
                if (place.secondary) {
                    const secondary = document.createElement("small");
                    secondary.textContent = ", " + place.secondary;
                    option.appendChild(secondary);
                }
                option.addEventListener("click", () => selectGooglePlace(place));
                suggestions.appendChild(option);
            });
            suggestions.hidden = false;
            locationInput.setAttribute("aria-expanded", "true");
        }

        // True when Google answered (with or without results); false to fall back.
        async function searchGoogle(query, controller, currentVersion) {
            const response = await post(
                SUGGEST_URL,
                { q: query, session: sessionToken },
                controller.signal
            );
            if (response.status === 503) {
                googleState = "off";
                return false;
            }
            if (!response.ok) throw new Error(`Places returned ${response.status}`);
            const data = await response.json();
            googleState = "on";
            useGoogleAttribution();
            if (currentVersion === requestVersion && locationInput.value.trim() === query) {
                renderGoogleSuggestions(Array.isArray(data.suggestions) ? data.suggestions : []);
            }
            return true;
        }

        function clearCoordinates() {
            latitudeInput.value = "";
            longitudeInput.value = "";
        }

        function closeSuggestions() {
            suggestions.hidden = true;
            suggestions.replaceChildren();
            locationInput.setAttribute("aria-expanded", "false");
        }

        function showMessage(message) {
            suggestions.replaceChildren();

            const item = document.createElement("div");
            item.className = "location-message";
            item.textContent = message;

            suggestions.appendChild(item);
            suggestions.hidden = false;
            locationInput.setAttribute("aria-expanded", "true");
        }

        function formatLocation(properties) {
            const parts = [
                properties.name,
                [properties.housenumber, properties.street].filter(Boolean).join(" "),
                properties.city,
                properties.state,
                properties.country
            ].filter(Boolean);

            return [...new Set(parts)].join(", ");
        }

        function selectLocation(feature) {
            const coordinates = feature.geometry?.coordinates;

            if (
                !Array.isArray(coordinates) ||
                coordinates.length < 2 ||
                !Number.isFinite(coordinates[0]) ||
                !Number.isFinite(coordinates[1])
            ) {
                return;
            }

            locationInput.value = formatLocation(feature.properties || {});
            longitudeInput.value = String(coordinates[0]);
            latitudeInput.value = String(coordinates[1]);

            clearTimeout(debounceTimer);
            activeController?.abort();
            requestVersion += 1;

            closeSuggestions();
            locationInput.focus();
            locationInput.dispatchEvent(new Event("change", { bubbles: true }));
        }

        function renderSuggestions(features) {
            suggestions.replaceChildren();

            if (!features.length) {
                showMessage("No matching locations found.");
                return;
            }

            features.forEach((feature) => {
                const coordinates = feature.geometry?.coordinates;
                const label = formatLocation(feature.properties || {});

                if (
                    !label ||
                    !Array.isArray(coordinates) ||
                    coordinates.length < 2
                ) {
                    return;
                }

                const option = document.createElement("button");
                option.type = "button";
                option.textContent = label;
                option.setAttribute("role", "option");

                option.addEventListener("click", () => {
                    selectLocation(feature);
                });

                suggestions.appendChild(option);
            });

            suggestions.hidden = suggestions.childElementCount === 0;
            locationInput.setAttribute(
                "aria-expanded",
                String(!suggestions.hidden)
            );
        }

        async function searchLocations(query) {
            activeController?.abort();

            const controller = new AbortController();
            activeController = controller;

            const currentVersion = ++requestVersion;

            const params = new URLSearchParams({
                q: query,
                limit: "5"
            });

            try {
                if (googleState !== "off" && await searchGoogle(query, controller, currentVersion)) {
                    return;
                }
                const response = await fetch(
                    `https://photon.komoot.io/api/?${params.toString()}`,
                    {
                        signal: controller.signal,
                        headers: {
                            Accept: "application/json"
                        }
                    }
                );

                if (!response.ok) {
                    throw new Error(`Photon returned ${response.status}`);
                }

                const data = await response.json();

                if (
                    currentVersion !== requestVersion ||
                    locationInput.value.trim() !== query
                ) {
                    return;
                }

                renderSuggestions(
                    Array.isArray(data.features) ? data.features : []
                );
            } catch (error) {
                if (error.name === "AbortError") {
                    return;
                }

                if (currentVersion === requestVersion) {
                    showMessage(
                        "Location search is temporarily unavailable. " +
                        "You can still enter an address manually."
                    );
                }

                console.error("Location search failed:", error);
            }
        }

        locationInput.setAttribute("role", "combobox");
        locationInput.setAttribute("aria-autocomplete", "list");
        locationInput.setAttribute("aria-expanded", "false");

        locationInput.addEventListener("input", () => {
            clearCoordinates();
            closeSuggestions();

            clearTimeout(debounceTimer);
            activeController?.abort();
            requestVersion += 1;

            const query = locationInput.value.trim();

            if (query.length < 3) {
                return;
            }

            debounceTimer = setTimeout(() => {
                searchLocations(query);
            }, 400);
        });

        locationInput.addEventListener("keydown", (event) => {
            if (event.key === "Escape") {
                closeSuggestions();
            }
        });

        document.addEventListener("click", (event) => {
            if (!wrapper.contains(event.target)) {
                closeSuggestions();
            }
        });
    }

    function initializeAllLocationForms() {
        // Event forms, plus small place pickers (family home, trip start).
        document.querySelectorAll("[data-event-form], [data-location-form]").forEach((form) => {
            initializeLocationAutocomplete(form);
        });
    }

    if (document.readyState === "loading") {
        document.addEventListener(
            "DOMContentLoaded",
            initializeAllLocationForms
        );
    } else {
        initializeAllLocationForms();
    }

    const observer = new MutationObserver(() => {
        initializeAllLocationForms();
    });

    observer.observe(document.documentElement, {
        childList: true,
        subtree: true
    });
})();
