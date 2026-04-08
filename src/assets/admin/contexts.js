document.addEventListener('DOMContentLoaded', function() {

	function esc(str) {
		var d = document.createElement('div');
		d.textContent = str || '';
		return d.innerHTML;
	}

	async function api(url, options) {
		var opts = Object.assign({
			headers: {
				'Content-Type': 'application/json',
				'Accept': 'application/json',
				'CSRF-Token': init.csrfNonce
			}
		}, options || {});
		var resp = await fetch(url, opts);
		return resp.json();
	}

	// ---- contexts table ----

	async function loadContexts() {
		var data = await api('/containers/api/contexts/list');
		renderContextsTable(data.contexts || []);
	}

	function renderContextsTable(contexts) {
		var tbody = document.getElementById('contexts-table-body');

		if (!contexts.length) {
			tbody.innerHTML = '<tr><td colspan="6" class="text-center text-muted">No contexts configured</td></tr>';
			return;
		}

		tbody.innerHTML = contexts.map(function(ctx) {
			var name = esc(ctx.context_name);
			if (ctx.is_local) name += ' <span class="badge badge-info">Local</span>';

			var connBadge = ctx.connected
				? '<span class="badge badge-success">Connected</span>'
				: '<span class="badge badge-danger">Disconnected</span>';

			var statusBadge = ctx.enabled
				? '<span class="badge badge-success">Enabled</span>'
				: '<span class="badge badge-secondary">Disabled</span>';

			var actions = '<button class="btn btn-sm btn-info ctx-test" data-id="' + ctx.id + '">Test</button> ' +
				'<button class="btn btn-sm btn-warning ctx-edit" data-id="' + ctx.id + '" ' +
				'data-hostname="' + esc(ctx.hostname || '') + '" ' +
				'data-pub-hostname="' + esc(ctx.pub_hostname) + '" ' +
				'data-weight="' + ctx.weight + '" ' +
				'data-enabled="' + ctx.enabled + '">Edit</button>';

			if (!ctx.is_local) {
				actions += ' <button class="btn btn-sm btn-danger ctx-delete" data-id="' + ctx.id + '">Delete</button>';
			}

			return '<tr>' +
				'<td>' + name + '</td>' +
				'<td>' + esc(ctx.pub_hostname) + '</td>' +
				'<td>' + ctx.weight + '</td>' +
				'<td>' + connBadge + '</td>' +
				'<td>' + statusBadge + '</td>' +
				'<td>' + actions + '</td></tr>';
		}).join('');

		bindContextActions();
	}

	function bindContextActions() {
		document.querySelectorAll('.ctx-test').forEach(function(btn) {
			btn.onclick = function() { testContext(this.dataset.id, this); };
		});
		document.querySelectorAll('.ctx-edit').forEach(function(btn) {
			btn.onclick = function() {
				document.getElementById('edit-context-id').value = this.dataset.id;
				document.getElementById('edit-context-hostname').value = this.dataset.hostname;
				document.getElementById('edit-context-pub-hostname').value = this.dataset.pubHostname;
				document.getElementById('edit-context-weight').value = this.dataset.weight;
				document.getElementById('edit-context-enabled').checked = this.dataset.enabled === 'true';
				$('#editContextModal').modal('show');
			};
		});
		document.querySelectorAll('.ctx-delete').forEach(function(btn) {
			btn.onclick = function() { deleteContext(this.dataset.id); };
		});
	}

	async function testContext(id, btn) {
		var orig = btn.textContent;
		btn.textContent = '...';
		btn.disabled = true;
		try {
			var data = await api('/containers/api/contexts/test/' + id);
			alert(data.success || data.error || 'Unknown result');
		} catch (e) {
			alert('Error: ' + e.message);
		}
		btn.textContent = orig;
		btn.disabled = false;
	}

	async function deleteContext(id) {
		if (!confirm('Are you sure you want to delete this context?')) return;
		try {
			var data = await api('/containers/api/contexts/delete/' + id, { method: 'DELETE' });
			if (data.success) loadContexts();
			else alert('Error: ' + (data.error || 'Unknown error'));
		} catch (e) {
			alert('Error: ' + e.message);
		}
	}

	// ---- add context ----

	document.getElementById('add-context-btn').onclick = function() {
		document.getElementById('add-context-form').reset();
		document.getElementById('context-enabled').checked = true;
		$('#addContextModal').modal('show');
	};

	document.getElementById('save-context-btn').onclick = async function() {
		var name = document.getElementById('context-name').value.trim();
		var pub = document.getElementById('context-pub-hostname').value.trim();
		if (!name) { alert('Context name is required'); return; }
		if (!pub) { alert('Public hostname is required'); return; }

		try {
			var data = await api('/containers/api/contexts/add', {
				method: 'POST',
				body: JSON.stringify({
					context_name: name,
					hostname: document.getElementById('context-hostname').value.trim() || null,
					pub_hostname: pub,
					weight: parseInt(document.getElementById('context-weight').value) || 1,
					enabled: document.getElementById('context-enabled').checked
				})
			});
			if (data.success) {
				$('#addContextModal').modal('hide');
				loadContexts();
			} else {
				alert('Error: ' + (data.error || 'Unknown error'));
			}
		} catch (e) {
			alert('Error: ' + e.message);
		}
	};

	// ---- edit context ----

	document.getElementById('update-context-btn').onclick = async function() {
		var id = document.getElementById('edit-context-id').value;
		var pub = document.getElementById('edit-context-pub-hostname').value.trim();
		if (!pub) { alert('Public hostname is required'); return; }

		try {
			var data = await api('/containers/api/contexts/update/' + id, {
				method: 'PUT',
				body: JSON.stringify({
					hostname: document.getElementById('edit-context-hostname').value.trim() || null,
					pub_hostname: pub,
					weight: parseInt(document.getElementById('edit-context-weight').value) || 1,
					enabled: document.getElementById('edit-context-enabled').checked
				})
			});
			if (data.success) {
				$('#editContextModal').modal('hide');
				loadContexts();
			} else {
				alert('Error: ' + (data.error || 'Unknown error'));
			}
		} catch (e) {
			alert('Error: ' + e.message);
		}
	};

	// ---- reload ----

	document.getElementById('reload-contexts-btn').onclick = async function() {
		this.disabled = true;
		try {
			await api('/containers/api/contexts/reload', { method: 'POST' });
			await loadContexts();
		} catch (e) {
			alert('Error: ' + e.message);
		}
		this.disabled = false;
	};

	// ---- discover / import ----

	document.getElementById('import-contexts-btn').onclick = function() {
		$('#importContextsModal').modal('show');
		discoverContexts();
	};

	async function discoverContexts() {
		var loading = document.getElementById('discover-loading');
		var list = document.getElementById('discover-list');
		var empty = document.getElementById('discover-empty');

		loading.style.display = '';
		list.style.display = 'none';
		empty.style.display = 'none';

		try {
			var data = await api('/containers/api/contexts/discover');
			loading.style.display = 'none';
			var contexts = data.contexts || [];

			if (!contexts.length) {
				empty.style.display = '';
				return;
			}

			list.style.display = '';
			list.innerHTML = contexts.map(function(ctx) {
				var badge = ctx.reachable
					? '<small class="text-success">connected</small>'
					: '<small class="text-warning">unreachable</small>';

				return '<div class="border rounded p-2 mb-2 d-flex align-items-center discover-row">' +
					'<div class="flex-grow-1 mr-3">' +
						'<strong>' + esc(ctx.name) + '</strong>' +
						'<div><small class="text-muted">' + esc(ctx.endpoint) + '</small> ' + badge + '</div>' +
					'</div>' +
					'<div class="d-flex align-items-center" style="gap:8px;">' +
						'<input type="text" class="form-control form-control-sm import-hostname" ' +
							'placeholder="Public hostname" value="' + esc(ctx.suggested_hostname) + '" ' +
							'data-name="' + esc(ctx.name) + '" style="width:180px;">' +
						'<button class="btn btn-sm btn-primary import-btn" ' +
							'data-name="' + esc(ctx.name) + '">Import</button>' +
					'</div>' +
				'</div>';
			}).join('');

			list.querySelectorAll('.import-btn').forEach(function(btn) {
				btn.onclick = function() {
					var row = this.closest('.discover-row');
					var hostname = row.querySelector('.import-hostname').value.trim();
					if (!hostname) { alert('Public hostname is required'); return; }
					importContext(this.dataset.name, hostname, this);
				};
			});
		} catch (e) {
			loading.style.display = 'none';
			empty.style.display = '';
		}
	}

	async function importContext(name, pubHostname, btn) {
		btn.disabled = true;
		btn.textContent = '...';

		try {
			var data = await api('/containers/api/contexts/add', {
				method: 'POST',
				body: JSON.stringify({
					context_name: name,
					pub_hostname: pubHostname,
					weight: 1,
					enabled: true
				})
			});
			if (data.success) {
				var row = btn.closest('.discover-row');
				row.style.opacity = '0.5';
				row.querySelector('.import-hostname').disabled = true;
				btn.textContent = 'Imported';
				btn.classList.remove('btn-primary');
				btn.classList.add('btn-success');
				loadContexts();
			} else {
				btn.disabled = false;
				btn.textContent = 'Import';
				alert('Error: ' + (data.error || 'Import failed'));
			}
		} catch (e) {
			btn.disabled = false;
			btn.textContent = 'Import';
			alert('Error: ' + e.message);
		}
	}

	// ---- image matrix ----

	document.getElementById('scan-images-btn').onclick = function() { loadImageMatrix(); };

	async function loadImageMatrix() {
		var status = document.getElementById('scan-status');
		var container = document.getElementById('image-matrix-container');

		status.textContent = 'Scanning...';

		try {
			var data = await api('/containers/api/images/matrix');
			status.textContent = '';

			var images = data.images || [];
			var contexts = data.contexts || [];
			var matrix = data.matrix || {};

			if (!images.length) {
				container.innerHTML = '<p class="text-muted">No challenge images configured yet</p>';
				return;
			}

			if (!contexts.length) {
				container.innerHTML = '<p class="text-muted">No connected contexts</p>';
				return;
			}

			renderImageMatrix(images, contexts, matrix);
		} catch (e) {
			status.textContent = 'Error: ' + e.message;
		}
	}

	function newestId(img, contexts, matrix) {
		var latest = null;
		contexts.forEach(function(ctx) {
			var e = matrix[img] && matrix[img][ctx];
			if (e && e.available && e.info) {
				if (!latest || e.info.created > latest.created) latest = e.info;
			}
		});
		return latest ? latest.id : null;
	}

	function renderImageMatrix(images, contexts, matrix) {
		var container = document.getElementById('image-matrix-container');

		var header = '<thead><tr><th>Image</th>' +
			contexts.map(function(c) { return '<th class="text-center">' + esc(c) + '</th>'; }).join('') +
			'</tr></thead>';

		var rows = images.map(function(img) {
			var newest = newestId(img, contexts, matrix);
			var anyMissing = false;
			var anyOutdated = false;

			var cells = contexts.map(function(ctx) {
				var entry = matrix[img] && matrix[img][ctx];
				if (!entry || !entry.available) {
					anyMissing = true;
					return '<td class="text-center" style="background:rgba(220,53,69,0.06)">' +
						'<i class="fas fa-times-circle text-danger" title="Not found. Build or load this image on this host."></i></td>';
				}

				var info = entry.info;
				if (!info) {
					return '<td class="text-center">' +
						'<i class="fas fa-check-circle text-success" title="Available"></i></td>';
				}

				var isStale = newest && info.id !== newest;
				if (isStale) anyOutdated = true;

				if (isStale) {
					return '<td class="text-center" style="background:rgba(255,193,7,0.08)">' +
						'<i class="fas fa-exclamation-circle text-warning" title="Outdated. SHA ' + esc(info.id) + ' (' + info.size_mb + 'MB, built ' + esc(info.created) + ') does not match the newest build. Rebuild on this host."></i></td>';
				}

				return '<td class="text-center">' +
					'<i class="fas fa-check-circle text-success" title="Up to date. SHA ' + esc(info.id) + ' (' + info.size_mb + 'MB, built ' + esc(info.created) + ')"></i></td>';
			}).join('');

			var badge = '';
			if (anyMissing) badge = ' <span class="badge bg-danger">missing</span>';
			else if (anyOutdated) badge = ' <span class="badge bg-warning text-dark">outdated</span>';

			return '<tr><td>' + esc(img) + badge + '</td>' + cells + '</tr>';
		}).join('');

		container.innerHTML = '<div class="table-responsive"><table class="table table-sm table-bordered">' +
			header + '<tbody>' + rows + '</tbody></table></div>';
	}

	async function pullImage(image, ctx, btn) {
		btn.disabled = true;
		btn.innerHTML = '<i class="fas fa-spinner fa-spin"></i>';

		try {
			var data = await api('/containers/api/pull', {
				method: 'POST',
				body: JSON.stringify({ image: image, context_name: ctx })
			});

			var result = data.results && data.results[ctx];
			if (result === 'ok') {
				var cell = btn.closest('td');
				cell.innerHTML = '<span class="text-success font-weight-bold">&#10003;</span>';
			} else {
				alert('Pull failed on ' + ctx + ': ' + (result || data.error || 'unknown error'));
				btn.disabled = false;
				btn.innerHTML = 'Pull';
			}
		} catch (e) {
			alert('Pull error: ' + e.message);
			btn.disabled = false;
			btn.innerHTML = 'Pull';
		}
	}

	// ---- init ----

	loadContexts();

});
