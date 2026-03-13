CTFd.plugin.run(async (_CTFd) => {
	const { loadContexts, initImageSearch, initSSHToggle, initAdvancedSection } = ContainerFormUtils;

	const dockerContext = document.getElementById("docker-context");
	const connectType = document.getElementById("connect-type");
	const csrf = init.csrfNonce;

	const imageSearch = initImageSearch({
		inputEl: document.getElementById("image-search"),
		hiddenEl: document.getElementById("image-hidden"),
		dropdownEl: document.getElementById("image-dropdown"),
		csrfToken: csrf,
	});

	await loadContexts(dockerContext, csrf);

	if (container_context_selected) {
		dockerContext.value = container_context_selected;
	}

	const ctx = dockerContext.value;
	const loaded = await imageSearch.loadForContext(ctx);

	if (loaded && container_image_selected) {
		imageSearch.setValue(container_image_selected);
	}

	dockerContext.addEventListener("change", async function () {
		await imageSearch.loadForContext(this.value);
	});

	function getChallengeIdFromURL() {
		const match = window.location.href.match(/\/challenges\/(\d+)/);
		return match ? parseInt(match[1]) : null;
	}

	const challengeId = getChallengeIdFromURL();
	if (challengeId) {
		try {
			const data = await ContainerFormUtils.fetchWithTimeout(
				`/containers/api/get_connect_type/${challengeId}`, csrf
			);
			if (data && data.connect) {
				connectType.value = data.connect;
			}
		} catch (_) {}
	}

	initSSHToggle();
	initAdvancedSection(container_advanced_values);
});
