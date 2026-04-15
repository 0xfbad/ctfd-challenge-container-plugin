window.ContainerFormUtils = (function () {

	async function fetchWithTimeout(url, csrfToken, timeoutMs = 10000) {
		const controller = new AbortController();
		const timeoutId = setTimeout(() => controller.abort(), timeoutMs);

		try {
			const response = await fetch(url, {
				method: "GET",
				headers: { "Accept": "application/json", "CSRF-Token": csrfToken },
				signal: controller.signal,
			});
			clearTimeout(timeoutId);
			if (!response.ok) throw new Error(`HTTP ${response.status}`);
			return await response.json();
		} catch (error) {
			clearTimeout(timeoutId);
			throw error;
		}
	}

	async function loadContexts(element, csrfToken) {
		try {
			const data = await fetchWithTimeout("/containers/api/contexts", csrfToken);

			element.innerHTML = "";

			const auto = document.createElement("option");
			auto.value = "";
			auto.textContent = "Auto (load-balanced)";
			element.appendChild(auto);

			if (data.contexts && data.contexts.length > 0) {
				data.contexts.forEach(ctx => {
					const opt = document.createElement("option");
					opt.value = ctx;
					opt.textContent = ctx;
					element.appendChild(opt);
				});
			}

			return true;
		} catch (error) {
			const msg = error.name === "AbortError"
				? "Timeout loading contexts - check docker connection"
				: "Failed to load contexts";
			element.innerHTML = `<option value="" disabled selected>${msg}</option>`;
			return false;
		}
	}

	function formatImageName(raw) {
		const colonIdx = raw.lastIndexOf(":");
		if (colonIdx === -1) return capitalize(raw);

		const name = raw.substring(0, colonIdx);
		const tag = raw.substring(colonIdx + 1);

		// only capitalize bare names (no slash = no registry prefix)
		const display = name.includes("/") ? name : capitalize(name);
		return `${display} (${tag})`;
	}

	function capitalize(s) {
		if (!s) return s;
		return s.charAt(0).toUpperCase() + s.slice(1);
	}

	function scoreMatch(query, raw, display) {
		const q = query.toLowerCase();
		const r = raw.toLowerCase();
		const d = display.toLowerCase();

		if (r.startsWith(q) || d.startsWith(q)) return 3;

		const separators = ["/", ":", "-", " ("];
		for (const sep of separators) {
			const rIdx = r.indexOf(sep);
			if (rIdx !== -1 && r.substring(rIdx + sep.length).startsWith(q)) return 2;
			const dIdx = d.indexOf(sep);
			if (dIdx !== -1 && d.substring(dIdx + sep.length).startsWith(q)) return 2;
		}

		if (r.includes(q) || d.includes(q)) return 1;

		return 0;
	}

	function initImageSearch({ inputEl, hiddenEl, dropdownEl, csrfToken }) {
		let images = [];
		let filtered = [];
		let highlightIdx = -1;
		let mouseInDropdown = false;

		function render() {
			dropdownEl.innerHTML = "";
			if (filtered.length === 0 && inputEl.value.trim()) {
				const item = document.createElement("div");
				item.className = "list-group-item text-muted";
				item.textContent = "No matching images";
				dropdownEl.appendChild(item);
				dropdownEl.style.display = "block";
				return;
			}
			if (filtered.length === 0) {
				dropdownEl.style.display = "none";
				return;
			}

			filtered.slice(0, 50).forEach((entry, idx) => {
				const item = document.createElement("div");
				item.className = "list-group-item list-group-item-action";
				if (idx === highlightIdx) item.classList.add("active");
				item.textContent = entry.display;

				item.addEventListener("mousedown", (e) => {
					e.preventDefault();
					select(entry);
				});

				item.addEventListener("mouseenter", () => {
					highlightIdx = idx;
					render();
				});

				dropdownEl.appendChild(item);
			});
			dropdownEl.style.display = "block";
		}

		function select(entry) {
			inputEl.value = entry.display;
			hiddenEl.value = entry.raw;
			inputEl.classList.remove("is-invalid");
			close();
		}

		function close() {
			dropdownEl.style.display = "none";
			highlightIdx = -1;
		}

		function filter() {
			const q = inputEl.value.trim();
			if (!q) {
				filtered = images.map(raw => ({ raw, display: formatImageName(raw), score: 0 }));
			} else {
				filtered = [];
				for (const raw of images) {
					const display = formatImageName(raw);
					const s = scoreMatch(q, raw, display);
					if (s > 0) filtered.push({ raw, display, score: s });
				}
				filtered.sort((a, b) => b.score - a.score);
			}
			highlightIdx = -1;
			render();
		}

		inputEl.addEventListener("input", () => {
			hiddenEl.value = "";
			filter();
		});

		inputEl.addEventListener("focus", () => {
			if (images.length > 0) filter();
		});

		inputEl.addEventListener("blur", () => {
			if (!mouseInDropdown) close();
		});

		dropdownEl.addEventListener("mouseenter", () => { mouseInDropdown = true; });
		dropdownEl.addEventListener("mouseleave", () => { mouseInDropdown = false; });

		inputEl.addEventListener("keydown", (e) => {
			if (dropdownEl.style.display === "none") return;

			const max = Math.min(filtered.length, 50);

			if (e.key === "ArrowDown") {
				e.preventDefault();
				highlightIdx = (highlightIdx + 1) % max;
				render();
			} else if (e.key === "ArrowUp") {
				e.preventDefault();
				highlightIdx = (highlightIdx - 1 + max) % max;
				render();
			} else if (e.key === "Enter" && highlightIdx >= 0 && highlightIdx < max) {
				e.preventDefault();
				select(filtered[highlightIdx]);
			} else if (e.key === "Escape") {
				close();
			}
		});

		const form = inputEl.closest("form");
		if (form) {
			form.addEventListener("submit", (e) => {
				if (!hiddenEl.value && inputEl.value.trim()) {
					hiddenEl.value = inputEl.value.trim();
				}
				if (!hiddenEl.value) {
					e.preventDefault();
					inputEl.classList.add("is-invalid");
					inputEl.focus();
				}
			});
		}

		async function loadForContext(ctx) {
			inputEl.value = "";
			hiddenEl.value = "";
			inputEl.setAttribute("placeholder", "Loading images...");
			images = [];

			try {
				const url = ctx
					? `/containers/api/images/${ctx}`
					: "/containers/api/images";
				const data = await fetchWithTimeout(url, csrfToken, 15000);

				if (data.error) {
					inputEl.setAttribute("placeholder", data.error);
					return false;
				}

				images = data.images || [];

				if (images.length === 0) {
					inputEl.setAttribute("placeholder", "No images found");
					return false;
				}

				inputEl.setAttribute("placeholder", `Search ${images.length} images...`);
				return true;
			} catch (error) {
				const msg = error.name === "AbortError"
					? "Timeout loading images"
					: "Failed to load images";
				inputEl.setAttribute("placeholder", msg);
				return false;
			}
		}

		function setValue(img) {
			if (!img) return;
			hiddenEl.value = img;
			inputEl.value = formatImageName(img);
		}

		return { loadForContext, setValue };
	}

	function initSSHToggle() {
		const connectType = document.getElementById("connect-type");
		const sshFields = document.getElementById("ssh-fields");
		if (!connectType || !sshFields) return;

		function toggle() {
			sshFields.style.display = connectType.value === "ssh" ? "block" : "none";
		}

		connectType.addEventListener("change", toggle);
		toggle();
	}

	function initAdvancedSection(fieldValues) {
		const toggle = document.getElementById("advanced-toggle");
		const section = document.getElementById("advanced-section");
		if (!toggle || !section) return;

		if (fieldValues && Object.values(fieldValues).some(v => v !== null && v !== undefined && v !== "")) {
			section.classList.add("show");
			toggle.classList.remove("collapsed");
			toggle.setAttribute("aria-expanded", "true");
		}
	}

	function esc(s) {
		var d = document.createElement("div");
		d.textContent = s;
		return d.innerHTML;
	}

	function renderImageStatus(data, pinnedCtx) {
		if (!data.cached) {
			return '<small class="text-muted"><i class="fas fa-info-circle"></i> No scan data yet. Scan from plugin config.</small>';
		}

		var ctxs = data.contexts || {};
		var available = [], missing = [];
		for (var c in ctxs) {
			if (ctxs[c].available) available.push(c);
			else missing.push(c);
		}

		var html = "";
		if (Object.keys(ctxs).length === 0) {
			html = '<small class="text-warning"><i class="fas fa-question-circle"></i> Image not in last scan</small>';
		} else if (pinnedCtx) {
			if (ctxs[pinnedCtx] && ctxs[pinnedCtx].available) {
				html = '<small class="text-success"><i class="fas fa-check-circle"></i> Available on ' + esc(pinnedCtx) + '</small>';
			} else if (ctxs[pinnedCtx]) {
				html = '<small class="text-danger"><i class="fas fa-times-circle"></i> Not found on ' + esc(pinnedCtx) + '</small>';
			} else {
				html = '<small class="text-muted"><i class="fas fa-question-circle"></i> Context ' + esc(pinnedCtx) + ' not in last scan</small>';
			}
		} else if (missing.length === 0 && available.length > 0) {
			html = '<small class="text-success"><i class="fas fa-check-circle"></i> Available on all contexts</small>';
		} else if (available.length === 0) {
			html = '<small class="text-danger"><i class="fas fa-times-circle"></i> Not found on any context</small>';
		} else {
			html = '<small class="text-warning"><i class="fas fa-exclamation-circle"></i> Missing on ' + missing.map(esc).join(", ") + '</small>';
		}

		html += ' <small class="text-muted">(' + new Date(data.scanned_at * 1000).toLocaleString('en-US', {month:'short',day:'numeric',year:'numeric',hour:'numeric',minute:'2-digit',hour12:true}) + ')</small>';
		return html;
	}

	async function checkImageStatus(imageInput, dockerContext, csrfToken) {
		var el = document.getElementById("image-status");
		if (!el || !imageInput) return;
		var img = imageInput.value.trim();
		if (!img) { el.innerHTML = ""; return; }

		try {
			var data = await fetchWithTimeout(
				"/containers/api/images/status?image=" + encodeURIComponent(img), csrfToken
			);
			el.innerHTML = renderImageStatus(data, dockerContext.value);
		} catch (_) {
			el.innerHTML = "";
		}
	}

	return {
		fetchWithTimeout,
		loadContexts,
		initSSHToggle,
		initAdvancedSection,
		checkImageStatus,
	};

})();
