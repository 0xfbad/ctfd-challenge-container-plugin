async function loadContexts(dockerContextElement, csrfToken) {
	console.log("loadContexts called");
	const controller = new AbortController();
	const timeoutId = setTimeout(() => controller.abort(), 10000);

	try {
		console.log("Fetching contexts from /containers/api/contexts");
		const response = await fetch("/containers/api/contexts", {
			method: "GET",
			headers: {
				"Accept": "application/json",
				"CSRF-Token": csrfToken
			},
			signal: controller.signal
		});

		clearTimeout(timeoutId);
		console.log("Response received:", response.status);

		if (!response.ok) throw new Error("Error fetching contexts");

		const data = await response.json();
		console.log("Contexts data:", data);

		dockerContextElement.innerHTML = '<option value="" disabled selected>Choose a context...</option>';

		if (!data.contexts || data.contexts.length === 0) {
			console.log("No contexts available");
			dockerContextElement.innerHTML = '<option value="" disabled selected>No contexts available</option>';
			return false;
		}

		console.log("Loading", data.contexts.length, "contexts");
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
		console.error("Fetch error:", error);
		if (error.name === 'AbortError') {
			containerImageElement.innerHTML = '<option value="" disabled selected>Timeout loading images - check docker connection</option>';
		} else {
			containerImageElement.innerHTML = '<option value="" disabled selected>Failed to load images</option>';
		}
		return false;
	}
}
