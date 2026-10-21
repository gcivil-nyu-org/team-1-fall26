/* Liquid glass for the selected pills (top nav, Upcoming/Day/Week, List/Cards).
 *
 * Ported from archisvaze/liquid-glass (MIT License, Copyright (c) 2026 Archis Vaze,
 * https://github.com/archisvaze/liquid-glass): a refraction profile from Snell's law through a
 * convex squircle bezel, painted into a displacement map for an SVG feDisplacementMap
 * backdrop-filter, plus a specular rim map.
 *
 * Chromium renders the refraction (backdrop-filter: url(#filter)). Safari and Firefox can't, so they
 * get the same specular rim, inner glow and tint with a plain blur behind it. Each pill gets maps
 * sized to it; maps are cached by size. */
(() => {
    const GLASS = ".primary-nav a[aria-current], .schedule-tabs a[aria-current], .display-switch .glass-thumb";
    const THICKNESS = 40, IOR = 3.0, SCALE_RATIO = 1.0, BLUR = 0.3, SPEC_OPACITY = 0.6, SPEC_SATURATION = 4;
    const squircle = x => Math.pow(1 - Math.pow(1 - x, 4), 0.25);
    const chromium = !!navigator.userAgentData?.brands?.some(b => /Chromium/.test(b.brand));
    const cache = new Map();
    let defs = null;

    function refractionProfile(bezel, samples = 128) {
        const eta = 1 / IOR, profile = new Float64Array(samples);
        for (let i = 0; i < samples; i++) {
            const x = i / samples, y = squircle(x);
            const dx = x < 1 ? 0.0001 : -0.0001;
            const deriv = (squircle(x + dx) - y) / dx, mag = Math.sqrt(deriv * deriv + 1);
            const nx = -deriv / mag, ny = -1 / mag;
            const k = 1 - eta * eta * (1 - ny * ny);
            if (k < 0) continue;
            const sq = Math.sqrt(k), rx = -(eta * ny + sq) * nx, ry = eta - (eta * ny + sq) * ny;
            profile[i] = rx * ((y * bezel + THICKNESS) / ry);
        }
        return profile;
    }

    // Walks the rounded-rectangle rim and calls paint(index, cos, sin, fromSide, opacity) per pixel.
    function eachRimPixel(w, h, r, bezel, paint) {
        const r1Sq = (r + 1) ** 2, rSq = r * r, rBSq = Math.max(r - bezel, 0) ** 2, wB = w - 2 * r, hB = h - 2 * r;
        for (let y1 = 0; y1 < h; y1++) {
            for (let x1 = 0; x1 < w; x1++) {
                const x = x1 < r ? x1 - r : x1 >= w - r ? x1 - r - wB : 0;
                const y = y1 < r ? y1 - r : y1 >= h - r ? y1 - r - hB : 0;
                const dSq = x * x + y * y;
                if (dSq > r1Sq || dSq < rBSq) continue;
                const dist = Math.sqrt(dSq);
                const op = dSq < rSq ? 1 : 1 - (dist - r) / (Math.sqrt(r1Sq) - r);
                if (op <= 0 || dist === 0) continue;
                paint((y1 * w + x1) * 4, x / dist, y / dist, r - dist, op);
            }
        }
    }

    function canvasMap(w, h, fill, draw) {
        const c = document.createElement("canvas");
        c.width = w; c.height = h;
        const ctx = c.getContext("2d"), img = ctx.createImageData(w, h);
        if (fill) for (let i = 0; i < img.data.length; i += 4) img.data.set(fill, i);
        draw(img.data);
        ctx.putImageData(img, 0, 0);
        return c.toDataURL();
    }

    function maps(w, h, r) {
        const key = `${w}x${h}r${r}`;
        if (cache.has(key)) return cache.get(key);
        const bezel = Math.max(1, Math.min(r - 1, Math.min(w, h) / 2 - 1));
        const profile = refractionProfile(bezel), maxDisp = Math.max(...profile.map(Math.abs)) || 1, S = profile.length;
        const displacement = canvasMap(w, h, [128, 128, 0, 255], d => eachRimPixel(w, h, r, bezel, (i, cos, sin, fromSide, op) => {
            const disp = profile[Math.min(((fromSide / bezel) * S) | 0, S - 1)] || 0;
            d[i] = (128 + (-cos * disp / maxDisp) * 127 * op + 0.5) | 0;
            d[i + 1] = (128 + (-sin * disp / maxDisp) * 127 * op + 0.5) | 0;
        }));
        const light = [Math.cos(Math.PI / 3), Math.sin(Math.PI / 3)];
        const specular = canvasMap(w, h, null, d => eachRimPixel(w, h, r, bezel * 2.5, (i, cos, sin, fromSide, op) => {
            const coeff = Math.abs(cos * light[0] - sin * light[1]) * Math.sqrt(Math.max(0, 1 - (1 - fromSide) ** 2));
            const col = (255 * coeff) | 0;
            d.set([col, col, col, (col * coeff * op * SPEC_OPACITY) | 0], i);
        }));
        const id = `lg-${key}`;
        if (chromium) {
            defs ??= document.body.appendChild(Object.assign(document.createElementNS("http://www.w3.org/2000/svg", "svg"),
                {innerHTML: "<defs></defs>"})).querySelector("defs");
            defs.parentNode.setAttribute("aria-hidden", "true");
            defs.parentNode.style.cssText = "position:absolute;width:0;height:0;overflow:hidden";
            defs.insertAdjacentHTML("beforeend", `<filter id="${id}" x="0" y="0" width="100%" height="100%" color-interpolation-filters="sRGB">
                <feGaussianBlur in="SourceGraphic" stdDeviation="${BLUR}" result="blurred"/>
                <feImage href="${displacement}" x="0" y="0" width="${w}" height="${h}" result="map"/>
                <feDisplacementMap in="blurred" in2="map" scale="${maxDisp * SCALE_RATIO}" xChannelSelector="R" yChannelSelector="G" result="displaced"/>
                <feColorMatrix in="displaced" type="saturate" values="${SPEC_SATURATION}" result="saturated"/>
                <feImage href="${specular}" x="0" y="0" width="${w}" height="${h}" result="spec"/>
                <feComposite in="saturated" in2="spec" operator="in" result="rim"/>
                <feBlend in="rim" in2="displaced" mode="normal"/>
            </filter>`);
        }
        const result = {specular, filter: chromium ? `url(#${id})` : null};
        cache.set(key, result);
        return result;
    }

    function applyLiquidGlass(root = document) {
        for (const el of root.querySelectorAll(GLASS)) {
            const w = Math.round(el.offsetWidth), h = Math.round(el.offsetHeight);
            if (w < 4 || h < 4) continue;
            const r = Math.floor(Math.min(parseFloat(getComputedStyle(el).borderTopLeftRadius) || 0, w / 2, h / 2));
            const {specular, filter} = maps(w, h, Math.max(r, 2));
            el.style.setProperty("--lg-specular", `url("${specular}")`);
            if (filter) el.style.setProperty("--lg-backdrop", filter);
        }
    }

    window.applyLiquidGlass = applyLiquidGlass;
    let resizeTimer;
    addEventListener("resize", () => { clearTimeout(resizeTimer); resizeTimer = setTimeout(applyLiquidGlass, 150); });
    // Measure again once styles, fonts and images have settled; pill sizes can change until then.
    if (document.readyState === "loading") document.addEventListener("DOMContentLoaded", () => applyLiquidGlass());
    else applyLiquidGlass();
    addEventListener("load", () => applyLiquidGlass());
    document.fonts?.ready.then(() => applyLiquidGlass());
})();
