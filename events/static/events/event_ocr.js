(function () {
    "use strict";

    const ALLOWED_MIME_TYPES = new Set([
        "image/jpeg",
        "image/jpg",
        "image/png",
        "image/webp",
    ]);
    const ALLOWED_EXTENSIONS = /\.(jpe?g|png|webp)$/i;
    const MAX_FILE_SIZE_BYTES = 10 * 1024 * 1024;

    const FIELD_SELECTORS = {
        title: { wrap: ".field-title", input: '[name="title"]', label: "event title" },
        date: { wrap: ".field-date", input: '[name="date"]', label: "start date" },
        start_time: {
            wrap: ".field-start-time",
            input: '[name="start_time"]',
            label: "start time",
        },
        end_date: {
            wrap: ".field-end-date",
            input: '[name="end_date"]',
            label: "end date",
        },
        end_time: {
            wrap: ".field-end-time",
            input: '[name="end_time"]',
            label: "end time",
        },
        location: {
            wrap: ".field-location",
            input: '[name="location"]',
            label: "location",
        },
    };

    function formatFileSize(bytes) {
        if (!bytes || bytes < 1024) return (bytes || 0) + " B";
        if (bytes < 1024 * 1024) return Math.round(bytes / 1024) + " KB";
        return (bytes / (1024 * 1024)).toFixed(1) + " MB";
    }

    function readCookie(name) {
        const prefix = name + "=";
        const cookies = document.cookie ? document.cookie.split(";") : [];
        for (let i = 0; i < cookies.length; i += 1) {
            const cookie = cookies[i].trim();
            if (cookie.startsWith(prefix)) {
                return decodeURIComponent(cookie.slice(prefix.length));
            }
        }
        return "";
    }

    function validateClientFile(file) {
        if (!file) return "Choose an image file to scan.";
        if (file.size === 0) {
            return (
                "This image file appears to be empty. " +
                "Choose a different photo or enter the event manually."
            );
        }
        if (file.size > MAX_FILE_SIZE_BYTES) {
            return (
                "This image is too large to scan (max 10 MB). " +
                "Choose a smaller photo or enter the event manually."
            );
        }

        const mime = (file.type || "").toLowerCase();
        const extOk = file.name ? ALLOWED_EXTENSIONS.test(file.name) : false;
        const mimeOk = mime ? ALLOWED_MIME_TYPES.has(mime) : false;

        if ((mime && !mimeOk) || (!mime && !extOk)) {
            return (
                "Unsupported file type. Please choose a JPG, JPEG, PNG, or WebP " +
                "image (max 10 MB)."
            );
        }
        return null;
    }

    function clearFormOcrHints(form) {
        if (!form) return;
        form.querySelectorAll(".ocr-field-hint").forEach((el) => el.remove());
        form.querySelectorAll(".is-ocr-missing, .is-ocr-warning").forEach((el) => {
            el.classList.remove("is-ocr-missing", "is-ocr-warning");
        });
        form.querySelectorAll("[data-ocr-flagged='true']").forEach((input) => {
            delete input.dataset.ocrFlagged;
            if (input.dataset.ocrAriaInvalid === "true") {
                input.removeAttribute("aria-invalid");
                delete input.dataset.ocrAriaInvalid;
            }
        });
        const banner = form.querySelector("[data-ocr-form-banner]");
        if (banner) {
            banner.textContent = "";
            banner.hidden = true;
        }
    }

    function attachHintClearOnEdit(wrap, input) {
        if (!input || input.dataset.ocrClearBound === "true") return;
        input.dataset.ocrClearBound = "true";

        function clearThisField() {
            if (wrap) {
                wrap.classList.remove("is-ocr-missing", "is-ocr-warning");
                wrap.querySelectorAll(".ocr-field-hint").forEach((el) => el.remove());
            }
            if (input.dataset.ocrAriaInvalid === "true" && input.value.trim()) {
                input.removeAttribute("aria-invalid");
                delete input.dataset.ocrAriaInvalid;
            }
            delete input.dataset.ocrFlagged;
        }

        input.addEventListener("input", clearThisField);
        input.addEventListener("change", clearThisField);
    }

    function addFieldHint(form, fieldKey, message, kind) {
        const meta = FIELD_SELECTORS[fieldKey];
        if (!meta || !form || !message) return null;
        const input = form.querySelector(meta.input);
        const wrap =
            form.querySelector(meta.wrap) || (input ? input.closest(".field") : null);
        if (!wrap || !input) return null;

        wrap.querySelectorAll(".ocr-field-hint").forEach((el) => el.remove());
        wrap.classList.remove("is-ocr-missing", "is-ocr-warning");
        wrap.classList.add(kind === "missing" ? "is-ocr-missing" : "is-ocr-warning");

        const hint = document.createElement("small");
        hint.className =
            "ocr-field-hint " + (kind === "missing" ? "is-missing" : "is-warning");
        if (kind === "missing") {
            hint.setAttribute("data-ocr-missing", "true");
            if (!input.hasAttribute("aria-invalid")) {
                input.setAttribute("aria-invalid", "true");
                input.dataset.ocrAriaInvalid = "true";
            }
        }
        hint.textContent = message;
        wrap.appendChild(hint);

        input.dataset.ocrFlagged = "true";
        attachHintClearOnEdit(wrap, input);
        return input;
    }

    function initializeEventModeShell(shell) {
        if (!shell || shell.dataset.ocrReady === "true") return;
        shell.dataset.ocrReady = "true";

        const tabs = Array.from(shell.querySelectorAll("[data-event-tab]"));
        const panels = Array.from(shell.querySelectorAll("[data-event-tabpanel]"));
        const form = shell.querySelector("[data-event-form]");
        const ocrPanel = shell.querySelector("[data-ocr-panel]");

        function activateTab(mode, options) {
            const opts = options || {};
            let activeTabEl = null;
            tabs.forEach((tab) => {
                const selected = tab.dataset.eventTab === mode;
                tab.classList.toggle("is-active", selected);
                tab.setAttribute("aria-selected", selected ? "true" : "false");
                tab.setAttribute("tabindex", selected ? "0" : "-1");
                if (selected) activeTabEl = tab;
            });
            panels.forEach((panel) => {
                panel.hidden = panel.dataset.eventTabpanel !== mode;
            });
            if (opts.focusTab && activeTabEl) {
                activeTabEl.focus();
            }
        }

        tabs.forEach((tab, index) => {
            tab.addEventListener("click", () => {
                activateTab(tab.dataset.eventTab);
            });
            tab.addEventListener("keydown", (event) => {
                let targetIndex = null;
                if (event.key === "ArrowRight") {
                    targetIndex = (index + 1) % tabs.length;
                } else if (event.key === "ArrowLeft") {
                    targetIndex = (index - 1 + tabs.length) % tabs.length;
                } else if (event.key === "Home") {
                    targetIndex = 0;
                } else if (event.key === "End") {
                    targetIndex = tabs.length - 1;
                }
                if (targetIndex !== null) {
                    event.preventDefault();
                    activateTab(tabs[targetIndex].dataset.eventTab, { focusTab: true });
                }
            });
        });

        shell.querySelectorAll("[data-ocr-switch-manual]").forEach((btn) => {
            btn.addEventListener("click", () => {
                activateTab("manual");
                if (form) {
                    const titleInput = form.querySelector('[name="title"]');
                    if (titleInput) titleInput.focus();
                }
            });
        });

        if (!ocrPanel) return;

        const ocrUrl = ocrPanel.dataset.ocrUrl || "";
        const fileInput = ocrPanel.querySelector("[data-ocr-file-input]");
        const cameraInput = ocrPanel.querySelector("[data-ocr-camera-input]");
        const dropzone = ocrPanel.querySelector("[data-ocr-dropzone]");
        const browseBtn = ocrPanel.querySelector("[data-ocr-browse]");
        const cameraBtn = ocrPanel.querySelector("[data-ocr-camera]");
        const previewCard = ocrPanel.querySelector("[data-ocr-preview-card]");
        const previewImg = ocrPanel.querySelector("[data-ocr-preview-img]");
        const fileMeta = ocrPanel.querySelector("[data-ocr-file-meta]");
        const replaceBtn = ocrPanel.querySelector("[data-ocr-replace]");
        const removeBtn = ocrPanel.querySelector("[data-ocr-remove]");
        const extractBtn = ocrPanel.querySelector("[data-ocr-extract]");
        const loadingEl = ocrPanel.querySelector("[data-ocr-loading]");
        const errorEl = ocrPanel.querySelector("[data-ocr-error]");
        const errorMsgEl = ocrPanel.querySelector("[data-ocr-error-message]");
        const resultEl = ocrPanel.querySelector("[data-ocr-result]");
        const candidatesEl = ocrPanel.querySelector("[data-ocr-candidates]");
        const candidateListEl = ocrPanel.querySelector("[data-ocr-candidate-list]");
        const warningsEl = ocrPanel.querySelector("[data-ocr-draft-warnings]");
        const endDateRow = ocrPanel.querySelector('[data-ocr-row="end_date"]');
        const applyBtn = ocrPanel.querySelector("[data-ocr-apply]");

        let selectedFile = null;
        let activeObjectUrl = null;
        let activeController = null;
        let lastScanResult = null;
        let selectedCandidateIndex = 0;

        function revokePreviewUrl() {
            if (activeObjectUrl) {
                URL.revokeObjectURL(activeObjectUrl);
                activeObjectUrl = null;
            }
        }

        function showError(message) {
            if (errorMsgEl) errorMsgEl.textContent = message;
            if (errorEl) errorEl.hidden = false;
        }

        function hideError() {
            if (errorEl) errorEl.hidden = true;
            if (errorMsgEl) errorMsgEl.textContent = "";
        }

        function setBusy(isBusy) {
            if (loadingEl) loadingEl.hidden = !isBusy;
            if (extractBtn) extractBtn.disabled = isBusy;
            if (replaceBtn) replaceBtn.disabled = isBusy;
            if (removeBtn) removeBtn.disabled = isBusy;
            if (browseBtn) browseBtn.disabled = isBusy;
            if (cameraBtn) cameraBtn.disabled = isBusy;
        }

        function clearSelectedFile(keepError) {
            if (activeController) {
                activeController.abort();
                activeController = null;
            }
            selectedFile = null;
            lastScanResult = null;
            selectedCandidateIndex = 0;
            revokePreviewUrl();
            if (previewImg) previewImg.removeAttribute("src");
            if (fileMeta) fileMeta.textContent = "";
            if (fileInput) fileInput.value = "";
            if (cameraInput) cameraInput.value = "";
            if (previewCard) previewCard.hidden = true;
            if (dropzone) dropzone.hidden = false;
            if (resultEl) resultEl.hidden = true;
            setBusy(false);
            if (!keepError) hideError();
        }

        function setSelectedFile(file) {
            hideError();
            if (resultEl) resultEl.hidden = true;
            lastScanResult = null;
            selectedCandidateIndex = 0;

            const clientError = validateClientFile(file);
            if (clientError) {
                clearSelectedFile(true);
                showError(clientError);
                return;
            }

            if (activeController) {
                activeController.abort();
                activeController = null;
            }

            selectedFile = file;
            revokePreviewUrl();
            activeObjectUrl = URL.createObjectURL(file);

            if (previewImg) {
                previewImg.src = activeObjectUrl;
            }
            if (fileMeta) {
                fileMeta.textContent =
                    (file.name || "Selected image") +
                    " (" +
                    formatFileSize(file.size) +
                    ")";
            }
            if (dropzone) dropzone.hidden = true;
            if (previewCard) previewCard.hidden = false;
            setBusy(false);
        }

        if (previewImg) {
            previewImg.addEventListener("error", () => {
                if (activeObjectUrl) {
                    showError(
                        "We couldn't display a preview of this image. " +
                            "Try another photo or click Extract Event Details to test it."
                    );
                }
            });
        }

        if (browseBtn && fileInput) {
            browseBtn.addEventListener("click", (event) => {
                event.stopPropagation();
                fileInput.click();
            });
        }
        if (replaceBtn && fileInput) {
            replaceBtn.addEventListener("click", () => {
                fileInput.click();
            });
        }
        if (cameraBtn && cameraInput) {
            cameraBtn.addEventListener("click", (event) => {
                event.stopPropagation();
                cameraInput.click();
            });
        }
        if (removeBtn) {
            removeBtn.addEventListener("click", () => {
                clearSelectedFile(false);
                if (dropzone) dropzone.focus();
            });
        }

        if (fileInput) {
            fileInput.addEventListener("change", () => {
                const file = fileInput.files && fileInput.files[0];
                if (file) setSelectedFile(file);
            });
        }
        if (cameraInput) {
            cameraInput.addEventListener("change", () => {
                const file = cameraInput.files && cameraInput.files[0];
                if (file) setSelectedFile(file);
            });
        }

        function bindDragTarget(target) {
            if (!target) return;
            ["dragenter", "dragover"].forEach((evtName) => {
                target.addEventListener(evtName, (event) => {
                    event.preventDefault();
                    event.stopPropagation();
                    target.classList.add("is-dragover");
                });
            });
            ["dragleave", "drop"].forEach((evtName) => {
                target.addEventListener(evtName, (event) => {
                    event.preventDefault();
                    event.stopPropagation();
                    target.classList.remove("is-dragover");
                });
            });
            target.addEventListener("drop", (event) => {
                const files = event.dataTransfer && event.dataTransfer.files;
                if (files && files[0]) {
                    setSelectedFile(files[0]);
                }
            });
        }

        bindDragTarget(dropzone);
        bindDragTarget(previewCard);

        if (dropzone && fileInput) {
            dropzone.addEventListener("click", (event) => {
                if (!event.target.closest("button")) {
                    fileInput.click();
                }
            });
            dropzone.addEventListener("keydown", (event) => {
                if (
                    (event.key === "Enter" || event.key === " ") &&
                    event.target === dropzone
                ) {
                    event.preventDefault();
                    fileInput.click();
                }
            });
        }

        function setPreviewValue(fieldKey, value) {
            const el = ocrPanel.querySelector('[data-ocr-val="' + fieldKey + '"]');
            if (!el) return;
            const hasValue = Boolean(value && String(value).trim());
            el.textContent = hasValue ? String(value).trim() : "Not found";
            el.classList.toggle("is-missing-val", !hasValue);
        }

        function renderCandidate(candidate) {
            if (!candidate || !candidate.event) return;
            const ev = candidate.event;
            const disp = candidate.display || {};

            setPreviewValue("title", disp.title || ev.title);
            setPreviewValue("date", disp.date || ev.date);
            setPreviewValue("start_time", disp.start_time || ev.start_time);
            setPreviewValue("end_date", disp.end_date || ev.end_date);
            setPreviewValue("end_time", disp.end_time || ev.end_time);
            setPreviewValue("location", disp.location || ev.location);

            if (endDateRow) {
                endDateRow.hidden = !(
                    ev.end_date && String(ev.end_date).trim() !== String(ev.date || "")
                );
            }

            if (warningsEl) {
                warningsEl.textContent = "";
                const notes = [];
                const seen = new Set();

                (candidate.warnings || []).forEach((w) => {
                    if (w && w.message && !seen.has(w.message)) {
                        seen.add(w.message);
                        notes.push(w.message);
                    }
                });
                (candidate.missing_fields || []).forEach((f) => {
                    const msg =
                        candidate.missing_field_messages &&
                        candidate.missing_field_messages[f];
                    if (msg && !seen.has(msg)) {
                        seen.add(msg);
                        notes.push(msg);
                    }
                });

                if (notes.length > 0) {
                    const box = document.createElement("div");
                    box.className = "ocr-warning-box";
                    const list = document.createElement("ul");
                    list.className = "ocr-warning-list";
                    notes.forEach((msg) => {
                        const li = document.createElement("li");
                        li.textContent = msg;
                        list.appendChild(li);
                    });
                    box.appendChild(list);
                    warningsEl.appendChild(box);
                    warningsEl.hidden = false;
                } else {
                    warningsEl.hidden = true;
                }
            }
        }

        function renderScanResult(data) {
            lastScanResult = data;
            selectedCandidateIndex = 0;

            const candidates =
                Array.isArray(data.candidates) && data.candidates.length > 0
                    ? data.candidates
                    : [data];

            if (candidatesEl && candidateListEl) {
                candidateListEl.textContent = "";
                if (data.multiple_events && candidates.length > 1) {
                    candidates.forEach((cand, idx) => {
                        const btn = document.createElement("button");
                        btn.type = "button";
                        btn.className =
                            "ocr-candidate-chip" + (idx === 0 ? " is-active" : "");
                        btn.setAttribute("aria-pressed", idx === 0 ? "true" : "false");

                        const disp = cand.display || {};
                        const ev = cand.event || {};
                        const parts = [
                            "Event " + (idx + 1),
                            disp.title || ev.title || "Untitled",
                        ];
                        if (disp.date || ev.date) parts.push(disp.date || ev.date);
                        if (disp.start_time || ev.start_time) {
                            parts.push(disp.start_time || ev.start_time);
                        }
                        btn.textContent = parts.join(" — ");

                        btn.addEventListener("click", () => {
                            selectedCandidateIndex = idx;
                            candidateListEl
                                .querySelectorAll(".ocr-candidate-chip")
                                .forEach((chip, chipIdx) => {
                                    const active = chipIdx === idx;
                                    chip.classList.toggle("is-active", active);
                                    chip.setAttribute(
                                        "aria-pressed",
                                        active ? "true" : "false"
                                    );
                                });
                            renderCandidate(candidates[idx]);
                        });
                        candidateListEl.appendChild(btn);
                    });
                    candidatesEl.hidden = false;
                } else {
                    candidatesEl.hidden = true;
                }
            }

            renderCandidate(candidates[0]);
            if (resultEl) {
                resultEl.hidden = false;
                resultEl.scrollIntoView({ behavior: "smooth", block: "nearest" });
            }
        }

        if (extractBtn) {
            extractBtn.addEventListener("click", () => {
                if (!selectedFile) {
                    showError("Choose an image to scan before extracting details.");
                    return;
                }
                const clientError = validateClientFile(selectedFile);
                if (clientError) {
                    showError(clientError);
                    return;
                }
                if (!ocrUrl) {
                    showError(
                        "Scanning is unavailable right now. " +
                            "Please enter your event manually."
                    );
                    return;
                }

                if (activeController) activeController.abort();
                const controller = new AbortController();
                activeController = controller;

                hideError();
                if (resultEl) resultEl.hidden = true;
                setBusy(true);

                const formData = new FormData();
                formData.append("document", selectedFile);
                const tzInput = form ? form.querySelector('input[name="timezone"]') : null;
                if (tzInput && tzInput.value) {
                    formData.append("timezone", tzInput.value);
                }

                const csrfInput = form
                    ? form.querySelector('input[name="csrfmiddlewaretoken"]')
                    : null;
                const csrfToken =
                    (csrfInput && csrfInput.value) || readCookie("csrftoken");

                fetch(ocrUrl, {
                    method: "POST",
                    body: formData,
                    credentials: "same-origin",
                    signal: controller.signal,
                    headers: {
                        "X-CSRFToken": csrfToken,
                        "X-Requested-With": "XMLHttpRequest",
                    },
                })
                    .then((response) => {
                        if (controller.signal.aborted) return null;
                        if (
                            response.redirected &&
                            new URL(response.url, window.location.href).pathname ===
                                "/accounts/login/"
                        ) {
                            throw new Error(
                                "Your session expired. Reload the page and sign in to scan documents."
                            );
                        }
                        return response.text().then((bodyText) => {
                            let data = null;
                            try {
                                data = bodyText ? JSON.parse(bodyText) : null;
                            } catch (e) {
                                data = null;
                            }
                            if (!data) {
                                throw new Error(
                                    "We couldn't read this image right now. " +
                                        "Try again or enter the event manually."
                                );
                            }
                            if (!response.ok || data.status === "error") {
                                throw new Error(
                                    data.error ||
                                        "We couldn't read enough information from this image. " +
                                            "Try taking a clearer photo or enter the event manually."
                                );
                            }
                            return data;
                        });
                    })
                    .then((data) => {
                        if (data && !controller.signal.aborted) {
                            renderScanResult(data);
                        }
                    })
                    .catch((error) => {
                        if (error && error.name === "AbortError") return;
                        showError(
                            (error && error.message) ||
                                "We couldn't reach the server to read this image. " +
                                    "Check your connection or enter the event manually."
                        );
                    })
                    .finally(() => {
                        if (activeController === controller) {
                            activeController = null;
                            setBusy(false);
                        }
                    });
            });
        }

        if (applyBtn) {
            applyBtn.addEventListener("click", () => {
                if (!lastScanResult || !form) return;
                const candidates =
                    Array.isArray(lastScanResult.candidates) &&
                    lastScanResult.candidates.length > 0
                        ? lastScanResult.candidates
                        : [lastScanResult];
                const candidate = candidates[selectedCandidateIndex] || candidates[0];
                if (!candidate || !candidate.event) return;

                const ev = candidate.event;
                clearFormOcrHints(form);

                const titleInput = form.querySelector('[name="title"]');
                const dateInput = form.querySelector('[name="date"]');
                const startTimeInput = form.querySelector('[name="start_time"]');
                const endTimeInput = form.querySelector('[name="end_time"]');
                const endDateInput = form.querySelector('[name="end_date"]');
                const toggleEndDate = form.querySelector("[data-toggle-end-date]");
                const locationInput = form.querySelector('[name="location"]');
                const latInput = form.querySelector('[name="latitude"]');
                const lonInput = form.querySelector('[name="longitude"]');
                const timesContainer = form.querySelector("[data-progressive-times]");

                form.dataset.ocrSuppressSuggest = "true";
                try {
                    if (titleInput) titleInput.value = ev.title || "";
                    if (dateInput) {
                        dateInput.value = ev.date || "";
                        dateInput.dispatchEvent(new Event("input", { bubbles: true }));
                        dateInput.dispatchEvent(new Event("change", { bubbles: true }));
                    }
                    if (startTimeInput) {
                        startTimeInput.value = ev.start_time || "";
                        startTimeInput.dispatchEvent(
                            new Event("change", { bubbles: true })
                        );
                    }
                    if (endTimeInput) {
                        delete endTimeInput.dataset.autoSuggested;
                        endTimeInput.value = ev.end_time || "";
                        endTimeInput.dispatchEvent(
                            new Event("change", { bubbles: true })
                        );
                    }
                    const hasSeparateEndDate = Boolean(
                        ev.end_date && ev.end_date !== ev.date
                    );
                    if (toggleEndDate) {
                        toggleEndDate.checked = hasSeparateEndDate;
                        toggleEndDate.dispatchEvent(
                            new Event("change", { bubbles: true })
                        );
                    }
                    if (endDateInput) {
                        endDateInput.value = hasSeparateEndDate ? ev.end_date : "";
                        if (hasSeparateEndDate) {
                            endDateInput.dispatchEvent(
                                new Event("change", { bubbles: true })
                            );
                        }
                    }
                    if (latInput) latInput.value = "";
                    if (lonInput) lonInput.value = "";
                    if (locationInput) {
                        locationInput.value = ev.location || "";
                        // Trigger progressive details without opening Photon search menu.
                        locationInput.dispatchEvent(
                            new Event("change", { bubbles: true })
                        );
                    }
                } finally {
                    delete form.dataset.ocrSuppressSuggest;
                }

                const flaggedInputs = [];
                const missingFields = candidate.missing_fields || [];
                const missingMsgs = candidate.missing_field_messages || {};
                missingFields.forEach((fieldKey) => {
                    const meta = FIELD_SELECTORS[fieldKey];
                    const msg =
                        missingMsgs[fieldKey] ||
                        "Couldn't identify the " +
                            ((meta && meta.label) || fieldKey) +
                            ". Please enter it manually.";
                    const inputEl = addFieldHint(form, fieldKey, msg, "missing");
                    if (inputEl) flaggedInputs.push(inputEl);
                });

                const fieldWarnings = candidate.field_warnings || {};
                Object.keys(fieldWarnings).forEach((fieldKey) => {
                    if (missingFields.indexOf(fieldKey) !== -1) return;
                    const rawMsg = fieldWarnings[fieldKey];
                    const msg = Array.isArray(rawMsg)
                        ? rawMsg.join(" ").trim()
                        : rawMsg;
                    if (msg) {
                        const inputEl = addFieldHint(form, fieldKey, msg, "warning");
                        if (inputEl && flaggedInputs.length === 0) {
                            flaggedInputs.push(inputEl);
                        }
                    }
                });

                if (timesContainer) {
                    if (
                        timesContainer.querySelector("[data-ocr-missing]") ||
                        (startTimeInput && startTimeInput.value.trim()) ||
                        (endTimeInput && endTimeInput.value.trim())
                    ) {
                        timesContainer.hidden = false;
                    }
                }

                const banner = form.querySelector("[data-ocr-form-banner]");
                if (banner) {
                    banner.textContent = "";
                    const strong = document.createElement("strong");
                    strong.textContent = "Scanned details added. ";
                    const copy = document.createElement("span");
                    if (missingFields.length > 0) {
                        copy.textContent =
                            "Please enter the missing highlighted fields, pick who’s going, " +
                            "and click Check details before saving.";
                    } else if (Object.keys(fieldWarnings).length > 0) {
                        copy.textContent =
                            "Please double-check any highlighted dates or times, pick who’s going, " +
                            "and review before saving.";
                    } else {
                        copy.textContent =
                            "Review and edit anything below, pick who’s going, and continue.";
                    }
                    banner.appendChild(strong);
                    banner.appendChild(copy);
                    banner.hidden = false;
                }

                activateTab("manual");
                const focusTarget = flaggedInputs[0] || titleInput;
                if (focusTarget) {
                    focusTarget.scrollIntoView({ behavior: "smooth", block: "center" });
                    focusTarget.focus({ preventScroll: true });
                }
            });
        }
    }

    function initializeAllModeShells() {
        document.querySelectorAll("[data-event-mode-shell]").forEach((shell) => {
            initializeEventModeShell(shell);
        });
    }

    if (document.readyState === "loading") {
        document.addEventListener("DOMContentLoaded", initializeAllModeShells);
    } else {
        initializeAllModeShells();
    }

    const observer = new MutationObserver(() => {
        initializeAllModeShells();
    });
    observer.observe(document.documentElement, {
        childList: true,
        subtree: true,
    });
})();
