// ── Browser upload fallback ─────────────────────────────────────────
// When My Lab runs headless/remote (no pywebview window), there is no native
// file dialog. mylabPickFile() opens a normal <input type=file> in the
// browser, uploads the chosen file to the server via /api/upload, and
// resolves with the resulting server-side path — usable everywhere a
// pywebview.api.pick_file() path was used before (flash, scripts, scenari...).
//
// kind   : sub-folder under uploads/ on the server (e.g. "firmware", "script")
// accept : optional HTML accept attribute, e.g. ".hex,.s37" or ".py"
function mylabPickFile(kind, accept) {
    return new Promise((resolve) => {
        const input = document.createElement("input");
        input.type = "file";
        if (accept) input.accept = accept;
        input.style.display = "none";
        document.body.appendChild(input);

        input.onchange = async () => {
            const file = input.files && input.files[0];
            document.body.removeChild(input);
            if (!file) { resolve(null); return; }
            try {
                const fd = new FormData();
                fd.append("file", file);
                fd.append("kind", kind || "misc");
                const res  = await fetch("/api/upload", { method: "POST", body: fd });
                const data = await res.json();
                if (!data.ok) { alert("Upload failed: " + (data.error || "")); resolve(null); return; }
                resolve(data.path);
            } catch (e) {
                alert("Upload error: " + e);
                resolve(null);
            }
        };
        // Some browsers need the click triggered right after appendChild,
        // in the same user-gesture tick.
        input.click();
    });
}
