CTFd._internal.challenge.data = undefined;
CTFd._internal.challenge.renderer = null;
CTFd._internal.challenge.preRender = function () {};
CTFd._internal.challenge.render = null;
CTFd._internal.challenge.postRender = function () {};

CTFd._internal.challenge.submit = function (preview) {
    const challengeId = parseInt(CTFd.lib.$("#challenge-id").val());
    const submission = CTFd.lib.$("#challenge-input").val();
    resetAlert();

    const body = {
        challenge_id: challengeId,
        submission: submission,
    };

    const params = preview ? { preview: true } : {};

    return CTFd.api.post_challenge_attempt(params, body).then((response) => {
        if (response.status === 429 || response.status === 403) {
            return response;
        }
        // re-fetch container info to pick up post-solve expiry change
        var status = (response.data && response.data.status) || (response.data && response.data.data && response.data.data.status);
        if (status === "correct" || status === "already_solved") {
            setTimeout(function() {
                fetch("/containers/api/view_info", {
                    method: "POST",
                    headers: { "Content-Type": "application/json", "Accept": "application/json", "CSRF-Token": init.csrfNonce },
                    body: JSON.stringify({ chal_id: challengeId }),
                })
                .then(function(r) { return r.json(); })
                .then(function(data) {
                    if (data.expires) startTimer(data.expires);
                })
                .catch(function() {});
            }, 1000);
        }
        return response;
    });
};

var _expiryInterval = null;

function resetAlert() {
    var el = document.getElementById("deployment-info");
    el.innerHTML = "";
    el.classList.remove("alert-danger");
    el.style.display = "none";
    return el;
}

function showStart() {
    document.getElementById("create-chal").classList.remove("d-none");
    document.getElementById("running-bar").classList.add("d-none");
}

function showRunning() {
    document.getElementById("create-chal").classList.add("d-none");
    document.getElementById("running-bar").classList.remove("d-none");
}

function hideAll() {
    document.getElementById("create-chal").classList.add("d-none");
    document.getElementById("running-bar").classList.add("d-none");
}

function formatTime(seconds) {
    var hours = Math.floor(seconds / 3600);
    var minutes = Math.floor((seconds % 3600) / 60);
    var secs = seconds % 60;

    var h = hours > 0 ? String(hours).padStart(2, '0') + ':' : '';
    return h + String(minutes).padStart(2, '0') + ':' + String(secs).padStart(2, '0');
}

function startTimer(expiresAt) {
    if (_expiryInterval) clearInterval(_expiryInterval);

    var timer = document.getElementById("instance-timer");

    function tick() {
        var left = Math.max(0, Math.floor((expiresAt * 1000 - Date.now()) / 1000));
        timer.textContent = left > 0 ? formatTime(left) : "expired";
        timer.className = "bar-timer" + (left <= 0 ? " timer-expired" : left < 300 ? " timer-warning" : "");

        if (left <= 0) {
            clearInterval(_expiryInterval);
            _expiryInterval = null;
            resetAlert();
            showStart();
        }
    }

    tick();
    _expiryInterval = setInterval(tick, 1000);
}

function updateRenewButton(renewalsUsed, maxRenewals) {
    var btn = document.getElementById("extend-chal");
    var counter = document.getElementById("renewals-counter");
    if (!btn || !counter) return;
    var remaining = Math.max(0, maxRenewals - renewalsUsed);
    counter.textContent = '(' + remaining + ')';
    btn.disabled = remaining <= 0;
    btn.setAttribute('data-tip', remaining <= 0 ? 'All ' + maxRenewals + ' renewals used' : 'Reset the container timer');
}

function showConnection(data, container) {
    container.innerHTML = '';
    container.style.display = 'block';

    var renewBtn = document.getElementById("extend-chal");
    if (renewBtn) renewBtn.innerHTML = '<i class="fas fa-redo"></i> Renew <span id="renewals-counter"></span>';
    if (data.max_renewals != null) updateRenewButton(data.renewals_used || 0, data.max_renewals);

    if (data.connect === "web") {
        var url = "http://" + data.hostname + ":" + data.port;
        var link = document.createElement('a');
        link.href = url;
        link.textContent = url;
        link.target = '_blank';
        container.append(link);

        var hint = document.createElement('div');
        hint.className = 'connection-hint';
        hint.textContent = 'click to open in a new tab';
        container.append(hint);
    } else if (data.connect === "ssh") {
        var cmd = "ssh " + (data.ssh_username || '') + "@" + data.hostname + " -p " + data.port;
        container.append(makeCopyField("Command", cmd));
        if (data.ssh_password) {
            container.append(makeCopyField("Password", data.ssh_password));
        }
        var hint = document.createElement('div');
        hint.className = 'connection-hint';
        hint.textContent = 'run the command in your terminal, then enter the password';
        container.append(hint);
    } else {
        var cmd = "nc " + data.hostname + " " + data.port;
        container.append(makeCopyField(null, cmd));
        var hint = document.createElement('div');
        hint.className = 'connection-hint';
        hint.textContent = 'paste into your terminal to connect';
        container.append(hint);
    }

    startTimer(data.expires);
    showRunning();
}


function view_container_info(challengeId) {
    var info = resetAlert();

    fetch("/containers/api/view_info", {
        method: "POST",
        headers: { "Content-Type": "application/json", "Accept": "application/json", "CSRF-Token": init.csrfNonce },
        body: JSON.stringify({ chal_id: challengeId }),
    })
    .then(function(r) { return r.json(); })
    .then(function(data) {
        if (data.status === "misconfigured") {
            info.style.display = 'block';
            var banner = document.createElement('div');
            banner.className = 'misconfigured-banner';
            var icon = document.createElement('i');
            icon.className = 'fas fa-exclamation-triangle';
            icon.style.marginRight = '6px';
            banner.appendChild(icon);
            banner.appendChild(document.createTextNode(
                data.message || 'This challenge has a broken configuration. This is on our end, not yours.'
            ));
            info.innerHTML = '';
            info.appendChild(banner);
        } else if (data.status === "instance not started") {
            showStart();
        } else if (data.status === "already_running") {
            showConnection(data, info);
        } else if (data.status === "host_unavailable") {
            showConnection(data, info);
            var warn = document.createElement('div');
            warn.className = 'connection-hint';
            warn.style.color = '#b58105';
            warn.textContent = data.message || 'host temporarily unreachable';
            info.append(warn);
        } else if (data.message || data.error) {
            var errMsg = data.message || data.error;
            if (_isPermanentError(errMsg)) {
                _showServerError(info);
            } else {
                info.textContent = errMsg;
                info.classList.add('alert-danger');
                info.style.display = 'block';
            }
        }
    })
    .catch(function(e) { console.error("Fetch error:", e); });
}

var _requestInFlight = false;

function _isPermanentError(msg) {
    if (!msg) return false;
    var permanent = ["image not found", "max containers", "challenge not found"];
    var lower = msg.toLowerCase();
    return permanent.some(function(p) { return lower.indexOf(p) !== -1; });
}

function _showServerError(container) {
    container.innerHTML = '<div class="server-error-banner">' +
        '<i class="fas fa-exclamation-triangle banner-icon"></i>' +
        '<div class="error-title">This challenge isn\'t available right now</div>' +
        '<div class="error-detail">Something is wrong on our end, not yours. Please let an admin know so we can fix it.</div>' +
        '</div>';
    container.style.display = 'block';
    container.classList.remove('alert-danger');
    hideAll();
}

function _doContainerRequest(challengeId, isRetry) {
    var info = resetAlert();
    var startDiv = document.getElementById("create-chal");
    var btn = startDiv.querySelector("button");

    if (_requestInFlight) return;
    _requestInFlight = true;

    btn.disabled = true;
    btn.innerHTML = '<span class="loading-spinner"></span> ' + (isRetry ? 'Retrying...' : 'Starting...');

    fetch("/containers/api/request", {
        method: "POST",
        headers: { "Content-Type": "application/json", "Accept": "application/json", "CSRF-Token": init.csrfNonce },
        body: JSON.stringify({ chal_id: challengeId }),
    })
    .then(function(r) { return r.json(); })
    .then(function(data) {
        if (data.error || data.message) {
            var errMsg = data.error || data.message;
            if (!isRetry && !_isPermanentError(errMsg)) {
                btn.innerHTML = '<span class="loading-spinner"></span> Retrying...';
                _requestInFlight = false;
                setTimeout(function() { _doContainerRequest(challengeId, true); }, 2000);
                return;
            }
            if (_isPermanentError(errMsg)) {
                _showServerError(info);
            } else {
                info.textContent = errMsg;
                info.classList.add('alert-danger');
                info.style.display = 'block';
            }
            btn.disabled = false;
            btn.innerHTML = '<i class="fas fa-play"></i> Start Instance';
        } else {
            showConnection(data, info);
            btn.disabled = false;
            btn.innerHTML = '<i class="fas fa-play"></i> Start Instance';
        }
    })
    .catch(function(e) {
        console.error("Fetch error:", e);
        btn.disabled = false;
        btn.innerHTML = '<i class="fas fa-play"></i> Start Instance';
    })
    .finally(function() { _requestInFlight = false; });
}

function makeCopyField(label, value) {
    var wrapper = document.createElement('div');
    wrapper.style.marginBottom = '4px';

    if (label) {
        var lbl = document.createElement('div');
        lbl.className = 'connection-label';
        lbl.textContent = label;
        wrapper.append(lbl);
    }

    var row = document.createElement('div');
    row.className = 'connection-row';

    var code = document.createElement('code');
    code.textContent = value;
    row.append(code);

    var btn = document.createElement('button');
    btn.className = 'copy-btn';
    btn.innerHTML = '<i class="fas fa-copy"></i>';
    btn.title = 'Copy';
    btn.onclick = function() {
        navigator.clipboard.writeText(value).then(function() {
            btn.innerHTML = '<i class="fas fa-check"></i>';
            btn.classList.add('copied');
            setTimeout(function() {
                btn.innerHTML = '<i class="fas fa-copy"></i>';
                btn.classList.remove('copied');
            }, 1500);
        });
    };
    row.append(btn);
    wrapper.append(row);
    return wrapper;
}

function container_request(challengeId) {
    _doContainerRequest(challengeId, false);
}

function container_renew(challengeId) {
    var btn = document.getElementById("extend-chal");

    btn.disabled = true;
    btn.innerHTML = '<span class="loading-spinner"></span>';

    fetch("/containers/api/renew", {
        method: "POST",
        headers: { "Content-Type": "application/json", "Accept": "application/json", "CSRF-Token": init.csrfNonce },
        body: JSON.stringify({ chal_id: challengeId }),
    })
    .then(function(r) { return r.json(); })
    .then(function(data) {
        btn.innerHTML = '<i class="fas fa-redo"></i> Renew <span id="renewals-counter"></span>';
        if (data.error || data.message) {
            btn.disabled = false;
            var info = document.getElementById("deployment-info");
            info.textContent = data.error || data.message;
            info.classList.add('alert-danger');
            info.style.display = 'block';
        } else {
            startTimer(data.expires);
            if (data.max_renewals != null) updateRenewButton(data.renewals_used || 0, data.max_renewals);
        }
    })
    .catch(function(e) {
        console.error("Fetch error:", e);
        btn.disabled = false;
        btn.innerHTML = '<i class="fas fa-redo"></i> Renew <span id="renewals-counter"></span>';
    });
}

function container_stop(challengeId) {
    var info = resetAlert();
    var btn = document.getElementById("terminate-chal");
    var extBtn = document.getElementById("extend-chal");

    btn.disabled = true;
    extBtn.disabled = true;
    btn.innerHTML = '<span class="loading-spinner"></span>';

    fetch("/containers/api/stop", {
        method: "POST",
        headers: { "Content-Type": "application/json", "Accept": "application/json", "CSRF-Token": init.csrfNonce },
        body: JSON.stringify({ chal_id: challengeId }),
    })
    .then(function(r) { return r.json(); })
    .then(function(data) {
        btn.disabled = false;
        extBtn.disabled = false;
        btn.innerHTML = '<i class="fas fa-stop"></i> Stop';
        extBtn.innerHTML = '<i class="fas fa-plus"></i> Extend';

        if (data.error || data.message) {
            info.textContent = data.error || data.message;
            info.classList.add('alert-danger');
            info.style.display = 'block';
        } else {
            info.style.display = 'none';
        }

        if (_expiryInterval) { clearInterval(_expiryInterval); _expiryInterval = null; }
        showStart();
    })
    .catch(function(e) {
        console.error("Fetch error:", e);
        btn.disabled = false;
        extBtn.disabled = false;
        btn.innerHTML = '<i class="fas fa-stop"></i> Stop';
        extBtn.innerHTML = '<i class="fas fa-plus"></i> Extend';
    });
}
