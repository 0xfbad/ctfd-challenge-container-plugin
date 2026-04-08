CTFd.plugin.run(async (_CTFd) => {
	const { loadContexts, initSSHToggle, initAdvancedSection } = ContainerFormUtils;

	const dockerContext = document.getElementById("docker-context");
	const imageInput = document.querySelector('input[name="image"]');
	const csrf = init.csrfNonce;

	await loadContexts(dockerContext, csrf);

	initSSHToggle();
	initAdvancedSection();

	async function checkImageStatus() {
		var el = document.getElementById("image-status");
		if (!el || !imageInput) return;
		var img = imageInput.value.trim();
		if (!img) { el.innerHTML = ""; return; }

		try {
			var data = await ContainerFormUtils.fetchWithTimeout(
				"/containers/api/images/status?image=" + encodeURIComponent(img), csrf
			);
			el.innerHTML = renderImageStatus(data, dockerContext.value);
		} catch (_) {
			el.innerHTML = "";
		}
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
				html = '<small class="text-success"><i class="fas fa-check-circle"></i> Available on ' + pinnedCtx + '</small>';
			} else if (ctxs[pinnedCtx]) {
				html = '<small class="text-danger"><i class="fas fa-times-circle"></i> Not found on ' + pinnedCtx + '</small>';
			} else {
				html = '<small class="text-muted"><i class="fas fa-question-circle"></i> Context ' + pinnedCtx + ' not in last scan</small>';
			}
		} else if (missing.length === 0 && available.length > 0) {
			html = '<small class="text-success"><i class="fas fa-check-circle"></i> Available on all contexts</small>';
		} else if (available.length === 0) {
			html = '<small class="text-danger"><i class="fas fa-times-circle"></i> Not found on any context</small>';
		} else {
			html = '<small class="text-warning"><i class="fas fa-exclamation-circle"></i> Missing on ' + missing.join(", ") + '</small>';
		}

		html += ' <small class="text-muted">(' + new Date(data.scanned_at * 1000).toLocaleString(undefined, {year:'numeric',month:'2-digit',day:'2-digit',hour:'2-digit',minute:'2-digit',hour12:false}) + ')</small>';
		return html;
	}

	if (imageInput) {
		imageInput.addEventListener("change", checkImageStatus);
		dockerContext.addEventListener("change", checkImageStatus);
	}
});
