CTFd.plugin.run(async (_CTFd) => {
	const { loadContexts, initSSHToggle, initAdvancedSection, checkImageStatus } = ContainerFormUtils;

	const dockerContext = document.getElementById("docker-context");
	const connectType = document.getElementById("connect-type");
	const imageInput = document.querySelector('input[name="image"]');
	const csrf = init.csrfNonce;

	await loadContexts(dockerContext, csrf);

	if (container_context_selected) {
		dockerContext.value = container_context_selected;
	}

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

	if (imageInput) {
		checkImageStatus(imageInput, dockerContext, csrf);
		imageInput.addEventListener("change", () => checkImageStatus(imageInput, dockerContext, csrf));
		dockerContext.addEventListener("change", () => checkImageStatus(imageInput, dockerContext, csrf));
	}
});
