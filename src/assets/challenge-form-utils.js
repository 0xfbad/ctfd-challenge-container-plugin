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
