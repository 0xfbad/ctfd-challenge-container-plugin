if (typeof _containerView !== "undefined" && _containerView) _endContainerView(_containerView);

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

    return CTFd.api.post_challenge_attempt(params, body);
};

var _expiryInterval = null;
var _syncInterval = null;
var _activeChalId = null;
var _activeExpiresAt = null;
var _containerView = null;
var _retryTimer = null;

function _isCurrentContainerView(view, challengeId) {
    return view && view === _containerView &&
        (challengeId == null || view.challengeId === challengeId) &&
        view.info === document.getElementById("deployment-info");
}

function _endContainerView(view) {
    if (!view || view !== _containerView) return;
    _containerView = null;
    if (view.pending) view.pending.abort();
    if (view.modal) view.modal.removeEventListener("hide.bs.modal", view.onHide);
    if (view.jqueryModal) view.jqueryModal.off("hide.bs.modal", view.onHide);
    _stopSync();
    if (_expiryInterval) { clearInterval(_expiryInterval); _expiryInterval = null; }
    if (_retryTimer) { clearTimeout(_retryTimer); _retryTimer = null; }
    _requestInFlight = false;
    _retryPending = false;
}

function _fetchContainer(view, path, timeout, accept, failed) {
    if (_retryTimer) { clearTimeout(_retryTimer); _retryTimer = null; }
    if (view.pending) view.pending.abort();
    var controller = new AbortController();
    view.pending = controller;
    if (timeout != null) {
        _retryTimer = setTimeout(function() { controller.abort(); }, timeout);
    }
    function current() {
        return _isCurrentContainerView(view) && view.pending === controller;
    }
    function clearDeadline() {
        if (_retryTimer) clearTimeout(_retryTimer);
        _retryTimer = null;
    }
    fetch(path, {
        method: "POST",
        headers: { "Content-Type": "application/json", "Accept": "application/json", "CSRF-Token": init.csrfNonce },
        body: JSON.stringify({ chal_id: view.challengeId }),
        signal: controller.signal,
    })
    .then(function(response) {
        return response.json().catch(function(error) {
            if (response.status === 403 || response.status === 429) return null;
            throw error;
        }).then(function(data) {
            return { data: data, status: response.status, retryAfter: response.headers.get("Retry-After") };
        });
    })
    .then(function(payload) {
        if (!current()) return;
        clearDeadline();
        accept(payload);
    })
    .catch(function(error) {
        if (!current()) return;
        clearDeadline();
        failed(error);
    })
    .finally(function() {
        if (view.pending === controller) view.pending = null;
    });
}

function _startSync(challengeId) {
    _activeChalId = challengeId;
    if (_syncInterval) clearInterval(_syncInterval);
    _syncInterval = setInterval(function() { _syncNow(); }, 10000);
}

function _syncNow() {
    var view = _containerView;
    if (!_activeChalId || !_isCurrentContainerView(view, _activeChalId) || view.pending) return;
    var expiryInterval = _expiryInterval;
    _fetchContainer(view, "/containers/api/view_info", 5000, function(payload) {
        if (expiryInterval !== _expiryInterval) return;
        _applyInstanceStatus(view, payload);
    }, function() {});
}

function _stopSync() {
    _activeChalId = null;
    _activeExpiresAt = null;
    if (_syncInterval) { clearInterval(_syncInterval); _syncInterval = null; }
}

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
    var view = _containerView;
    if (_expiryInterval) clearInterval(_expiryInterval);
    _activeExpiresAt = expiresAt;

    var timer = document.getElementById("instance-timer");

    function tick() {
        if (!_isCurrentContainerView(view)) {
            clearInterval(interval);
            return;
        }
        var left = Math.max(0, Math.floor((_activeExpiresAt * 1000 - Date.now()) / 1000));
        timer.textContent = left > 0 ? formatTime(left) : "expired";
        timer.className = "bar-timer" + (left <= 0 ? " timer-expired" : left < 300 ? " timer-warning" : "");

        if (left <= 0) {
            clearInterval(_expiryInterval);
            _expiryInterval = null;
            _stopSync();
            resetAlert();
            showStart();
        }
    }

    var interval = setInterval(tick, 1000);
    _expiryInterval = interval;
    tick();
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

function showConnection(data, container, challengeId) {
    container.innerHTML = '';
    container.style.display = 'block';

    container.classList.remove('alert-danger');
    _resetInstanceActions(data);

    var hintText;
    if (data.connect === "web") {
        var url = "http://" + data.hostname + ":" + data.port;
        var link = document.createElement('a');
        link.href = url;
        link.textContent = url;
        link.target = '_blank';
        container.append(link);
        hintText = 'click to open in a new tab';
    } else if (data.connect === "ssh") {
        container.append(makeCopyField("Command", "ssh " + (data.ssh_username || '') + "@" + data.hostname + " -p " + data.port));
        if (data.ssh_password) {
            container.append(makeCopyField("Password", data.ssh_password));
        }
        hintText = 'run the command in your terminal, then enter the password';
    } else {
        container.append(makeCopyField(null, "nc " + data.hostname + " " + data.port));
        hintText = 'paste into your terminal to connect';
    }

    var hint = document.createElement('div');
    hint.className = 'connection-hint';
    hint.textContent = hintText;
    container.append(hint);

    startTimer(data.expires);
    if (challengeId) _startSync(challengeId);
    showRunning();
}


function view_container_info(challengeId) {
    _endContainerView(_containerView);
    var info = resetAlert();
    var view = { challengeId: challengeId, info: info };
    _containerView = view;
    view.modal = document.getElementById("challenge-window");
    if (view.modal) {
        view.onHide = function() { _endContainerView(view); };
        view.modal.addEventListener("hide.bs.modal", view.onHide, { once: true });
        var jquery = CTFd.lib && CTFd.lib.$;
        if (jquery && jquery.fn && jquery.fn.jquery) {
            view.jqueryModal = jquery(view.modal);
            view.jqueryModal.one("hide.bs.modal", view.onHide);
        }
    }

    _fetchContainer(view, "/containers/api/view_info", null, function(payload) {
        var data = payload.data;
        if (!data || typeof data !== "object") throw new Error("invalid instance status");
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
            showConnection(data, info, challengeId);
        } else if (data.status === "host_unavailable") {
            showConnection(data, info, challengeId);
            var warn = document.createElement('div');
            warn.className = 'connection-hint host-unavailable-warning';
            warn.style.color = '#b58105';
            warn.textContent = data.message || 'host temporarily unreachable';
            info.append(warn);
        } else if (data.message || data.error) {
            var errMsg = data.message || data.error;
            if (_errorKind(data, errMsg) === "permanent") {
                _showServerError(info);
            } else {
                info.textContent = errMsg;
                info.classList.add('alert-danger');
                info.style.display = 'block';
            }
        }
    }, function(error) { console.error("Fetch error:", error); });
}

var _requestInFlight = false;
var _retryPending = false;

function _isPermanentError(msg) {
    if (!msg) return false;
    var permanent = ["image not found", "challenge not found"]; // keep fallback patterns in src/utils.py consistent
    var lower = msg.toLowerCase();
    return permanent.some(function(p) { return lower.indexOf(p) !== -1; });
}

function _isUserError(msg) {
    if (!msg) return false;
    var userErrs = [ // keep fallback patterns in src/utils.py consistent
        "you can only spawn",
        "rate limit", "too many",
        "not a member of a team",
        "invalid",
        "no container found",
    ];
    var lower = msg.toLowerCase();
    return userErrs.some(function(p) { return lower.indexOf(p) !== -1; });
}

function _errorKind(data, msg) {
    var kind = data && data.error_kind;
    if (kind === "user" || kind === "transient" || kind === "permanent") return kind;
    if (_isPermanentError(msg)) return "permanent";
    if (_isUserError(msg)) return "user";
    return "transient";
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

function _resetStartButton(btn) {
    btn.disabled = false;
    btn.innerHTML = '<i class="fas fa-play"></i> Start Instance';
}

function _creationUnknown(view) {
    if (!_isCurrentContainerView(view)) return;
    view.info.textContent = "Could not confirm the instance. Reopen this challenge to check.";
    view.info.classList.add("alert-danger");
    view.info.style.display = "block";
    _requestInFlight = false;
    _resetStartButton(document.getElementById("create-chal").querySelector("button"));
}

function _validConnection(data) {
    return data && typeof data.hostname === "string" && data.hostname &&
        ["web", "ssh", "tcp"].indexOf(data.connect) !== -1 &&
        Number.isInteger(data.port) && data.port > 0 && data.port <= 65535 &&
        Number.isFinite(data.expires) && data.expires > 0;
}

function _reconcileContainer(view, deadline) {
    if (!_isCurrentContainerView(view)) return;
    var remaining = deadline - Date.now();
    if (remaining <= 0) {
        _creationUnknown(view);
        return;
    }
    function retry() {
        var delay = Math.min(2000, deadline - Date.now());
        _retryTimer = setTimeout(function() {
            _retryTimer = null;
            _reconcileContainer(view, deadline);
        }, Math.max(0, delay));
    }
    _fetchContainer(view, "/containers/api/view_info", Math.min(5000, remaining), function(payload) {
        var data = payload.data;
        var message = data && (data.error || data.message);
        var kind = _errorKind(data, message);
        if (payload.status === 403 || payload.status === 429 || data && data.status === "misconfigured" ||
            message && (kind === "user" || kind === "permanent")) {
            if (kind === "permanent" || data && data.status === "misconfigured") {
                _showServerError(view.info);
            } else {
                view.info.textContent = message || "Could not check the instance. Reopen this challenge to check.";
                view.info.classList.add("alert-danger");
                view.info.style.display = "block";
            }
            _requestInFlight = false;
            _resetStartButton(document.getElementById("create-chal").querySelector("button"));
            return;
        }
        if (_validConnection(data) && (data.status === "already_running" || data.status === "host_unavailable")) {
            showConnection(data, view.info, view.challengeId);
            _requestInFlight = false;
            _resetStartButton(document.getElementById("create-chal").querySelector("button"));
            return;
        }
        retry();
    }, retry);
}

function _doContainerRequest(challengeId, isRetry, retryDeadline) {
    var view = _containerView;
    if (!_isCurrentContainerView(view, challengeId)) return;
    if (_requestInFlight || _retryPending) return;
    var info = resetAlert();

    var btn = document.getElementById("create-chal").querySelector("button");
    _requestInFlight = true;
    btn.disabled = true;
    btn.innerHTML = '<span class="loading-spinner"></span> ' + (isRetry ? 'Retrying...' : 'Starting...');

    _fetchContainer(view, "/containers/api/request", Math.max(0, Math.min(10000, retryDeadline - Date.now())), function(payload) {
        var data = payload.data;
        if (payload.status === 403 || (payload.status === 429 && !data)) {
            info.textContent = data && (data.error || data.message) || "Start request was denied. Try again later.";
            info.classList.add("alert-danger");
            info.style.display = "block";
            _requestInFlight = false;
            _resetStartButton(btn);
            return;
        }
        if (!data || typeof data !== "object" || Array.isArray(data)) throw new Error("invalid start response");
        if (data.error || data.message) {
            var errMsg = data.error || data.message;
            var kind = _errorKind(data, errMsg);
            var seconds = parseInt(payload.retryAfter, 10);
            var capacityWait = payload.status === 429 && kind === "transient" && seconds >= 1;
            if (!(seconds >= 1)) seconds = 2;
            var delay = Math.min(seconds, 30) * 1000;
            delay += Math.random() * (capacityWait ? delay : 1000);
            var canRetry = Date.now() + delay < retryDeadline && (capacityWait || !isRetry);
            if (canRetry && kind === "transient" && seconds <= 30) { // cleanup waits must show the error without an automatic retry
                btn.innerHTML = '<span class="loading-spinner"></span> Retrying...';
                _requestInFlight = false;
                _retryPending = true;
                _retryTimer = setTimeout(function() {
                    if (!_isCurrentContainerView(view)) return;
                    _retryTimer = null;
                    _retryPending = false;
                    if (Date.now() >= retryDeadline) {
                        info.textContent = errMsg;
                        info.classList.add('alert-danger');
                        info.style.display = 'block';
                        _resetStartButton(btn);
                        return;
                    }
                    _doContainerRequest(challengeId, true, retryDeadline);
                }, delay);
                return;
            }
            if (kind === "permanent") {
                _showServerError(info);
            } else {
                info.textContent = errMsg;
                info.classList.add('alert-danger');
                info.style.display = 'block';
            }
        } else {
            if (!_validConnection(data)) throw new Error("invalid connection response");
            showConnection(data, info, challengeId);
        }
        _requestInFlight = false;
        _resetStartButton(btn);
    }, function() { _reconcileContainer(view, retryDeadline); });
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
    _doContainerRequest(challengeId, false, Date.now() + 30000);
}

function _resetInstanceActions(data) {
    var renew = document.getElementById("extend-chal");
    renew.innerHTML = '<i class="fas fa-redo"></i> Renew <span id="renewals-counter"></span>';
    renew.disabled = true;
    if (data && data.max_renewals != null) updateRenewButton(data.renewals_used || 0, data.max_renewals);
    var stop = document.getElementById("terminate-chal");
    stop.innerHTML = '<i class="fas fa-stop"></i> Stop';
    stop.disabled = false;
}

function _instanceAbsent() {
    if (_expiryInterval) { clearInterval(_expiryInterval); _expiryInterval = null; }
    _stopSync();
    _resetInstanceActions();
    resetAlert();
    showStart();
}

function _validRenewalCounts(data) {
    return Number.isInteger(data.renewals_used) && data.renewals_used >= 0 &&
        Number.isInteger(data.max_renewals) && data.max_renewals >= 0;
}

function _applyInstanceStatus(view, payload) {
    var data = payload.data;
    if (payload.status < 200 || payload.status >= 300 || !data || typeof data !== "object" ||
        Array.isArray(data) || data.error) return false;
    if (data.status === "instance not started" && !data.message) {
        _instanceAbsent();
        return true;
    }
    if (!_validConnection(data) || !_validRenewalCounts(data) ||
        (data.status !== "already_running" && data.status !== "host_unavailable") ||
        (data.message && data.status !== "host_unavailable")) return false;
    if (view.info.classList.contains('alert-danger') || view.info.style.display !== 'block' ||
        _activeChalId !== view.challengeId) {
        showConnection(data, view.info, view.challengeId);
    } else {
        _resetInstanceActions(data);
        if (data.expires !== _activeExpiresAt) startTimer(data.expires);
    }
    var warning = view.info.querySelector('.host-unavailable-warning');
    if (data.status === "host_unavailable") {
        if (!warning) {
            warning = document.createElement('div');
            warning.className = 'connection-hint host-unavailable-warning';
            warning.style.color = '#b58105';
            view.info.append(warning);
        }
        warning.textContent = data.message || 'host temporarily unreachable';
    } else if (warning) {
        view.info.removeChild(warning);
    }
    return true;
}

function _mutationUnknown(view, message) {
    _resetInstanceActions();
    document.getElementById("terminate-chal").disabled = true;
    view.info.textContent = (message ? message + " " : "") +
        "Could not confirm the action. Reopen this challenge to check.";
    view.info.classList.add('alert-danger');
    view.info.style.display = 'block';
}

function _reconcileMutation(view, message) {
    document.getElementById("extend-chal").disabled = true;
    document.getElementById("terminate-chal").disabled = true;
    _fetchContainer(view, "/containers/api/view_info", 5000, function(payload) {
        if (!_applyInstanceStatus(view, payload)) {
            _mutationUnknown(view, message);
            return;
        }
        if (message) {
            var error = document.createElement('div');
            error.textContent = message;
            view.info.append(error);
            view.info.classList.add('alert-danger');
            view.info.style.display = 'block';
        }
    }, function() { _mutationUnknown(view, message); });
}

function container_renew(challengeId) {
    var view = _containerView;
    if (!_isCurrentContainerView(view, challengeId)) return;
    var btn = document.getElementById("extend-chal");
    if (btn.disabled) return;
    btn.disabled = true;
    btn.innerHTML = '<span class="loading-spinner"></span>';

    _fetchContainer(view, "/containers/api/renew", 10000, function(payload) {
        var data = payload.data;
        if (payload.status >= 200 && payload.status < 300 && data && !Array.isArray(data) &&
            !data.error && !data.message && data.status === "success" && data.success === "container renewed" &&
            _validConnection(data) && _validRenewalCounts(data)) {
            showConnection(data, view.info, challengeId);
        } else {
            var message = data && (data.error || data.message);
            _reconcileMutation(view, typeof message === "string" ? message :
                (payload.status === 403 || payload.status === 429 ? "Request was denied." : null));
        }
    }, function() { _reconcileMutation(view); });
}

function container_stop(challengeId) {
    var view = _containerView;
    if (!_isCurrentContainerView(view, challengeId)) return;
    var btn = document.getElementById("terminate-chal");
    if (btn.disabled) return;
    resetAlert();
    btn.disabled = true;
    document.getElementById("extend-chal").disabled = true;
    btn.innerHTML = '<span class="loading-spinner"></span>';

    _fetchContainer(view, "/containers/api/stop", 10000, function(payload) {
        var data = payload.data;
        if (payload.status >= 200 && payload.status < 300 && data && !Array.isArray(data) &&
            !data.error && !data.message && data.success === "container killed") {
            _instanceAbsent();
        } else {
            var message = data && (data.error || data.message);
            _reconcileMutation(view, typeof message === "string" ? message :
                (payload.status === 403 || payload.status === 429 ? "Request was denied." : null));
        }
    }, function() { _reconcileMutation(view); });
}
