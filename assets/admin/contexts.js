document.addEventListener('DOMContentLoaded', function() {
	const addContextBtn = document.getElementById('add-context-btn');
	const saveContextBtn = document.getElementById('save-context-btn');
	const updateContextBtn = document.getElementById('update-context-btn');

	if (!addContextBtn) {
		console.error('add-context-btn not found');
		return;
	}

	addContextBtn.addEventListener('click', function(e) {
		e.preventDefault();
		console.log('Add context button clicked');
		document.getElementById('add-context-form').reset();
		$('#addContextModal').modal('show');
	});

	saveContextBtn.addEventListener('click', async function() {
		const contextName = document.getElementById('context-name').value;
		const hostname = document.getElementById('context-hostname').value;
		const enabled = document.getElementById('context-enabled').checked;

		if (!contextName) {
			alert('Context name is required');
			return;
		}

		if (!hostname) {
			alert('Hostname is required');
			return;
		}

		try {
			const response = await fetch('/containers/api/contexts/add', {
				method: 'POST',
				headers: {
					'Content-Type': 'application/json',
					'Accept': 'application/json',
					'CSRF-Token': init.csrfNonce
				},
				body: JSON.stringify({
					context_name: contextName,
					hostname: hostname,
					weight: 1,
					enabled: enabled
				})
			});

			const data = await response.json();

			if (response.ok) {
				alert('Context added successfully');
				$('#addContextModal').modal('hide');
				location.reload();
			} else {
				alert('Error: ' + (data.error || 'Unknown error'));
			}
		} catch (error) {
			alert('Error: ' + error.message);
		}
	});

	document.querySelectorAll('.edit-context-btn').forEach(btn => {
		btn.addEventListener('click', function() {
			const contextId = this.dataset.contextId;
			const hostname = this.dataset.hostname;
			const enabled = this.dataset.enabled === 'True';

			document.getElementById('edit-context-id').value = contextId;
			document.getElementById('edit-context-hostname').value = hostname;
			document.getElementById('edit-context-enabled').checked = enabled;

			$('#editContextModal').modal('show');
		});
	});

	updateContextBtn.addEventListener('click', async function() {
		const contextId = document.getElementById('edit-context-id').value;
		const hostname = document.getElementById('edit-context-hostname').value;
		const enabled = document.getElementById('edit-context-enabled').checked;

		if (!hostname) {
			alert('Hostname is required');
			return;
		}

		try {
			const response = await fetch(`/containers/api/contexts/update/${contextId}`, {
				method: 'PUT',
				headers: {
					'Content-Type': 'application/json',
					'Accept': 'application/json',
					'CSRF-Token': init.csrfNonce
				},
				body: JSON.stringify({
					hostname: hostname,
					weight: 1,
					enabled: enabled
				})
			});

			const data = await response.json();

			if (response.ok) {
				alert('Context updated successfully');
				$('#editContextModal').modal('hide');
				location.reload();
			} else {
				alert('Error: ' + (data.error || 'Unknown error'));
			}
		} catch (error) {
			alert('Error: ' + error.message);
		}
	});

	document.querySelectorAll('.delete-context-btn').forEach(btn => {
		btn.addEventListener('click', async function() {
			const contextId = this.dataset.contextId;

			if (!confirm('Are you sure you want to delete this context?')) {
				return;
			}

			try {
				const response = await fetch(`/containers/api/contexts/delete/${contextId}`, {
					method: 'DELETE',
					headers: {
						'Accept': 'application/json',
						'CSRF-Token': init.csrfNonce
					}
				});

				const data = await response.json();

				if (response.ok) {
					alert('Context deleted successfully');
					location.reload();
				} else {
					alert('Error: ' + (data.error || 'Unknown error'));
				}
			} catch (error) {
				alert('Error: ' + error.message);
			}
		});
	});

	document.querySelectorAll('.test-context-btn').forEach(btn => {
		btn.addEventListener('click', async function() {
			const contextId = this.dataset.contextId;
			const originalText = this.textContent;
			this.textContent = 'Testing...';
			this.disabled = true;

			try {
				const response = await fetch(`/containers/api/contexts/test/${contextId}`);
				const data = await response.json();

				if (response.ok) {
					alert('Success: Context is reachable');
				} else {
					alert('Error: ' + (data.error || 'Unknown error'));
				}
			} catch (error) {
				alert('Error: ' + error.message);
			} finally {
				this.textContent = originalText;
				this.disabled = false;
			}
		});
	});
});
