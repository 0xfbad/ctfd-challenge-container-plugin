CTFd.plugin.run(async (_CTFd) => {
	const { loadContexts, initImageSearch, initSSHToggle, initAdvancedSection } = ContainerFormUtils;

	const dockerContext = document.getElementById("docker-context");
	const csrf = init.csrfNonce;

	const imageSearch = initImageSearch({
		inputEl: document.getElementById("image-search"),
		hiddenEl: document.getElementById("image-hidden"),
		dropdownEl: document.getElementById("image-dropdown"),
		csrfToken: csrf,
	});

	await loadContexts(dockerContext, csrf);

	// empty string fetches from all contexts so images load before picking a runner
	await imageSearch.loadForContext("");

	dockerContext.addEventListener("change", async function () {
		await imageSearch.loadForContext(this.value);
	});

	initSSHToggle();
	initAdvancedSection();
});
