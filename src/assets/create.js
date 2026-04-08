CTFd.plugin.run(async (_CTFd) => {
	const { loadContexts, initSSHToggle, initAdvancedSection, checkImageStatus } = ContainerFormUtils;

	const dockerContext = document.getElementById("docker-context");
	const imageInput = document.querySelector('input[name="image"]');
	const csrf = init.csrfNonce;

	await loadContexts(dockerContext, csrf);

	initSSHToggle();
	initAdvancedSection();

	if (imageInput) {
		imageInput.addEventListener("change", () => checkImageStatus(imageInput, dockerContext, csrf));
		dockerContext.addEventListener("change", () => checkImageStatus(imageInput, dockerContext, csrf));
	}
});
