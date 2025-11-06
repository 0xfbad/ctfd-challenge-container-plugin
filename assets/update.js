CTFd.plugin.run(async (_CTFd) => {
	async function loadContexts(dockerContextElement, csrfToken) {
		const controller = new AbortController();
		const timeoutId = setTimeout(() => controller.abort(), 10000);

		try {
			const response = await fetch("/containers/api/contexts", {
				method: "GET",
				headers: {
					"Accept": "application/json",
					"CSRF-Token": csrfToken
				},
				signal: controller.signal
			});

			clearTimeout(timeoutId);

			if (!response.ok) throw new Error("Error fetching contexts");

			const data = await response.json();

			dockerContextElement.innerHTML = '<option value="" disabled selected>Choose a context...</option>';

			if (!data.contexts || data.contexts.length === 0) {
				dockerContextElement.innerHTML = '<option value="" disabled selected>No contexts available</option>';
				return false;
			}

			data.contexts.forEach(context => {
				const option = document.createElement("option");
				option.value = context;
				option.textContent = context;
				dockerContextElement.appendChild(option);
			});

			return true;
		} catch (error) {
			clearTimeout(timeoutId);
			console.error("loadContexts error:", error);
			if (error.name === 'AbortError') {
				dockerContextElement.innerHTML = '<option value="" disabled selected>Timeout loading contexts - check docker connection</option>';
			} else {
				dockerContextElement.innerHTML = '<option value="" disabled selected>Failed to load contexts</option>';
			}
			return false;
		}
	}

	async function loadImagesForContext(selectedContext, containerImageElement, csrfToken) {
		containerImageElement.setAttribute("disabled", "disabled");
		containerImageElement.innerHTML = '<option value="" disabled selected>Loading images...</option>';

		const controller = new AbortController();
		const timeoutId = setTimeout(() => controller.abort(), 15000);

		try {
			const response = await fetch(`/containers/api/images/${selectedContext}`, {
				method: "GET",
				headers: {
					"Accept": "application/json",
					"CSRF-Token": csrfToken
				},
				signal: controller.signal
			});

			clearTimeout(timeoutId);

			if (!response.ok) throw new Error("Error fetching images");

			const data = await response.json();

			containerImageElement.innerHTML = '<option value="" disabled selected>Choose an image...</option>';

			if (data.error) {
				containerImageElement.innerHTML = `<option value="" disabled selected>${data.error}</option>`;
				return false;
			} else if (data.images.length === 0) {
				containerImageElement.innerHTML = '<option value="" disabled selected>No images found in this context</option>';
				return false;
			} else {
				data.images.forEach(image => {
					const option = document.createElement("option");
					option.value = image;
					option.textContent = image;
					containerImageElement.appendChild(option);
				});

				containerImageElement.removeAttribute("disabled");
				return true;
			}
		} catch (error) {
			clearTimeout(timeoutId);
			console.error("loadImagesForContext error:", error);
			if (error.name === 'AbortError') {
				containerImageElement.innerHTML = '<option value="" disabled selected>Timeout loading images - check docker connection</option>';
			} else {
				containerImageElement.innerHTML = '<option value="" disabled selected>Failed to load images</option>';
			}
			return false;
		}
	}

	const dockerContext = document.getElementById("docker-context");
	const containerImage = document.getElementById("container-image");
	const connectType = document.getElementById("connect-type");

	await loadContexts(dockerContext, init.csrfNonce);

	if (container_context_selected) {
		dockerContext.value = container_context_selected;
		const loaded = await loadImagesForContext(container_context_selected, containerImage, init.csrfNonce);

		if (loaded && container_image_selected) {
			containerImage.value = container_image_selected;
		}
	}

	dockerContext.addEventListener("change", async function() {
		const selectedContext = this.value;
		await loadImagesForContext(selectedContext, containerImage, init.csrfNonce);
	});

	function getChallengeIdFromURL() {
		const currentURL = window.location.href;
		const match = currentURL.match(/\/challenges\/(\d+)/);
		return match && match[1] ? parseInt(match[1]) : null;
	}

	const challengeId = getChallengeIdFromURL();
	if (challengeId) {
		try {
			const response = await fetch(`/containers/api/get_connect_type/${challengeId}`, {
				method: "GET",
				headers: {
					"Accept": "application/json",
					"CSRF-Token": init.csrfNonce
				}
			});

			if (response.ok) {
				const data = await response.json();
				if (data && data.connect) {
					connectType.value = data.connect;
				}
			}
		} catch (error) {
			console.error("Error loading connect type:", error);
		}
	}
});
