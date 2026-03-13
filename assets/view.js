CTFd._internal.challenge.data = undefined;
CTFd._internal.challenge.renderer = null;
CTFd._internal.challenge.preRender = function () {};
CTFd._internal.challenge.render = null;
CTFd._internal.challenge.postRender = function () {};

CTFd._internal.challenge.submit = function (preview) {
    const challengeId = parseInt(CTFd.lib.$("#challenge-id").val());
    const submission = CTFd.lib.$("#challenge-input").val();
    const alert = resetAlert();

    const body = {
        challenge_id: challengeId,
        submission: submission,
    };

    const params = preview ? { preview: true } : {};

    return CTFd.api.post_challenge_attempt(params, body).then((response) => {
        if (response.status === 429 || response.status === 403) {
            return response;
        }
        return response;
    });
};

function mergeQueryParams(parameters, queryParameters) {
    if (parameters.$queryParameters) {
        Object.keys(parameters.$queryParameters).forEach((paramName) => {
            queryParameters[paramName] = parameters.$queryParameters[paramName];
        });
    }

    return queryParameters;
}

function resetAlert() {
    const alert = document.getElementById("deployment-info");
    alert.innerHTML = "";
    alert.classList.remove("alert-danger");
    alert.style.display = "none";

    return alert;
}

function toggleChallengeCreate() {
    const btn = document.getElementById("create-chal");
    btn.classList.toggle('d-none');
}

function toggleChallengeUpdate() {
    const btnExtend = document.getElementById("extend-chal");
    const btnTerminate = document.getElementById("terminate-chal");
    btnExtend.classList.toggle('d-none');
    btnTerminate.classList.toggle('d-none');
}

function calculateExpiry(expiresAtTimestamp) {
    const now = Date.now(); 
    const difference = Math.floor((expiresAtTimestamp * 1000 - now) / 1000);

    return difference > 0 ? difference : 0;
}

function formatTime(seconds) {
    const hours = Math.floor(seconds / 3600);
    const minutes = Math.floor((seconds % 3600) / 60);
    const secs = seconds % 60;

    const hoursStr = hours > 0 ? String(hours).padStart(2, '0') + ':' : '';
    const minutesStr = String(minutes).padStart(2, '0');
    const secondsStr = String(secs).padStart(2, '0');

    return hoursStr + `${minutesStr}:${secondsStr}`;
}

function getTimerClass(seconds) {
    if (seconds <= 0) return 'timer-expired';
    if (seconds < 300) return 'timer-warning';
    return 'timer-ok';
}


function createChallengeLinkElement(data, parent) {
    if (parent.expiryInterval) {
        clearInterval(parent.expiryInterval);
    }
    parent.innerHTML = '';
    parent.style.display = 'block';

    const timerDiv = document.createElement('div');
    timerDiv.className = 'instance-timer';
    parent.append(timerDiv);

    const connectionDetails = document.createElement('div');
    connectionDetails.style.marginTop = '10px';
    parent.append(connectionDetails);

    function updateExpiry() {
        const secondsLeft = calculateExpiry(data.expires);

        timerDiv.textContent = secondsLeft > 0
            ? `Expires in ${formatTime(secondsLeft)}`
            : "Expired";

        timerDiv.className = 'instance-timer ' + getTimerClass(secondsLeft);

        if (secondsLeft <= 0) {
            clearInterval(parent.expiryInterval);
            delete parent.expiryInterval;

            toggleChallengeCreate();
            toggleChallengeUpdate();

            connectionDetails.innerHTML = '';
        }
    }

    updateExpiry();
    parent.expiryInterval = setInterval(updateExpiry, 1000);

    if (data.connect === "tcp") {
        const codeElement = document.createElement('code');
        codeElement.textContent = `nc ${data.hostname} ${data.port}`;
        connectionDetails.append(codeElement);
    } else if (data.connect === "ssh") {
        const codeElement = document.createElement('code');
        codeElement.textContent = data.ssh_password
            ? `sshpass -p ${data.ssh_password} ssh -o StrictHostKeyChecking=no ${data.hostname} -p ${data.port}`
            : `ssh -o StrictHostKeyChecking=no ${data.hostname} -p ${data.port}`;
        connectionDetails.append(codeElement);
    } else {
        const link = document.createElement('a');
        link.href = `http://${data.hostname}:${data.port}`;
        link.textContent = link.href;
        link.target = '_blank';
        connectionDetails.append(link);
    }
}

function view_container_info(challengeId) {
    resetAlert();
    const alert = document.getElementById("deployment-info");

    fetch("/containers/api/view_info", {
        method: "POST",
        headers: {
            "Content-Type": "application/json",
            "Accept": "application/json",
            "CSRF-Token": init.csrfNonce,
        },
        body: JSON.stringify({ chal_id: challengeId }),
    })
    .then((response) => response.json())
    .then((data) => {
        if (data.status === "instance not started") {
            alert.style.display = "none";
            toggleChallengeCreate();
        } else if (data.status === "already_running") {
            createChallengeLinkElement(data, alert);
            toggleChallengeUpdate();
        } else if (data.status === "host_unavailable") {
            createChallengeLinkElement(data, alert);
            const warning = document.createElement('div');
            warning.className = 'alert alert-warning';
            warning.style.marginTop = '8px';
            warning.textContent = data.message || 'Host temporarily unreachable';
            alert.prepend(warning);
            toggleChallengeUpdate();
        } else {
            resetAlert();
            alert.textContent = data.message;
            alert.classList.add('alert-danger');
            alert.style.display = "block";
        }
    })
    .catch((error) => console.error("Fetch error:", error));
}

var _requestInFlight = false;

function _isPermanentError(msg) {
    if (!msg) return false;
    const permanent = ["image not found", "max containers", "challenge not found"];
    const lower = msg.toLowerCase();
    return permanent.some(p => lower.includes(p));
}

function _doContainerRequest(challengeId, isRetry) {
    const alert = resetAlert();
    const btn = document.getElementById("create-chal");

    if (_requestInFlight) return;
    _requestInFlight = true;

    btn.disabled = true;
    btn.innerHTML = isRetry
        ? '<span class="loading-spinner"></span> Retrying...'
        : '<span class="loading-spinner"></span> Starting...';

    fetch("/containers/api/request", {
        method: "POST",
        headers: {
            "Content-Type": "application/json",
            "Accept": "application/json",
            "CSRF-Token": init.csrfNonce,
        },
        body: JSON.stringify({ chal_id: challengeId }),
    })
    .then((response) => response.json())
    .then((data) => {
        if (data.error || data.message) {
            const errMsg = data.error || data.message;

            if (!isRetry && !_isPermanentError(errMsg)) {
                btn.innerHTML = '<span class="loading-spinner"></span> Retrying...';
                _requestInFlight = false;
                setTimeout(() => _doContainerRequest(challengeId, true), 2000);
                return;
            }

            alert.textContent = errMsg;
            alert.classList.add('alert-danger');
            alert.style.display = "block";
            btn.disabled = false;
            btn.innerHTML = '<i class="fas fa-play"></i> Start Instance';
        } else {
            createChallengeLinkElement(data, alert);
            btn.disabled = false;
            btn.innerHTML = '<i class="fas fa-play"></i> Start Instance';
            toggleChallengeCreate();
            toggleChallengeUpdate();
        }
    })
    .catch((error) => {
        console.error("Fetch error:", error);
        btn.disabled = false;
        btn.innerHTML = '<i class="fas fa-play"></i> Start Instance';
    })
    .finally(() => {
        _requestInFlight = false;
    });
}

function container_request(challengeId) {
    _doContainerRequest(challengeId, false);
}

function container_renew(challengeId) {
    const alert = resetAlert();
    const btn = document.getElementById("extend-chal");

    btn.disabled = true;
    btn.innerHTML = '<span class="loading-spinner"></span> Extending...';

    fetch("/containers/api/renew", {
        method: "POST",
        headers: {
            "Content-Type": "application/json",
            "Accept": "application/json",
            "CSRF-Token": init.csrfNonce,
        },
        body: JSON.stringify({ chal_id: challengeId }),
    })
    .then((response) => response.json())
    .then((data) => {
        if (data.error || data.message) {
            alert.textContent = data.error || data.message;
            alert.classList.add('alert-danger');
            alert.style.display = "block";
            btn.disabled = false;
            btn.innerHTML = '<i class="fas fa-clock"></i> Extend Time';
        } else {
            createChallengeLinkElement(data, alert);
            btn.disabled = false;
            btn.innerHTML = '<i class="fas fa-clock"></i> Extend Time';
        }
    })
    .catch((error) => {
        console.error("Fetch error:", error);
        btn.disabled = false;
        btn.innerHTML = '<i class="fas fa-clock"></i> Extend Time';
    });
}

function container_stop(challengeId) {
    const alert = resetAlert();
    const btn = document.getElementById("terminate-chal");
    const extendBtn = document.getElementById("extend-chal");

    btn.disabled = true;
    btn.innerHTML = '<span class="loading-spinner"></span> Terminating...';

    if (extendBtn) {
        extendBtn.disabled = true;
    }

    fetch("/containers/api/stop", {
        method: "POST",
        headers: {
            "Content-Type": "application/json",
            "Accept": "application/json",
            "CSRF-Token": init.csrfNonce,
        },
        body: JSON.stringify({ chal_id: challengeId }),
    })
    .then((response) => response.json())
    .then((data) => {
        btn.disabled = false;
        btn.innerHTML = '<i class="fas fa-power-off"></i> Terminate Instance';

        if (extendBtn) {
            extendBtn.disabled = false;
        }

        if (data.error || data.message) {
            alert.textContent = data.error || data.message;
            alert.classList.add('alert-danger');
            alert.style.display = "block";
        } else {
            alert.style.display = "none";
        }

        toggleChallengeCreate();
        toggleChallengeUpdate();
    })
    .catch((error) => {
        console.error("Fetch error:", error);
        btn.disabled = false;
        btn.innerHTML = '<i class="fas fa-power-off"></i> Terminate Instance';
        if (extendBtn) {
            extendBtn.disabled = false;
        }
    });
}
