(() => {
  const root = (window.init?.urlRoot || window.CTFd?.config?.urlRoot || "").replace(/\/$/, "");
  const prefix = `${root}/plugins/personalized-files/`;
  const active = new Map();
  const notices = new WeakMap();
  const downloads = new Map();

  function generatedURL(button) {
    let url;
    try {
      url = new URL(button.href, window.location.href);
    } catch {
      return null;
    }
    if (url.origin !== window.location.origin || !url.pathname.startsWith(prefix)) return null;
    if (!/^[1-9]\d*\/[A-Za-z0-9][A-Za-z0-9._-]{0,127}$/.test(url.pathname.slice(prefix.length))) return null;
    return url;
  }

  const modal = document.querySelector("#challenge-window");
  if (!window.init?.userId && modal) {
    const lockFiles = () => {
      for (const files of modal.querySelectorAll(".challenge-files")) {
        for (const button of files.querySelectorAll("a.btn-file")) {
          if (!generatedURL(button)) continue;
          button.classList.remove("btn-info");
          button.classList.add("btn-outline-secondary", "disabled");
          button.setAttribute("aria-disabled", "true");
          button.setAttribute("tabindex", "-1");
          const icon = button.querySelector("i");
          if (icon) {
            icon.className = "fas fa-lock";
            icon.style.marginInlineEnd = "6px";
            icon.setAttribute("aria-hidden", "true");
          }
          button.parentElement.title = "Sign in to download this file";
        }
      }
    };
    lockFiles();
    new MutationObserver(lockFiles).observe(modal, { childList: true, subtree: true });
  }

  document.addEventListener("click", async (event) => {
    if (event.button !== 0 || event.ctrlKey || event.metaKey || event.shiftKey || event.altKey) return;
    if (!(event.target instanceof Element)) return;
    const button = event.target.closest("#challenge-window .challenge-files a.btn-file");
    if (!button || (button.target && button.target !== "_self")) return;
    const url = generatedURL(button);
    if (!url) return;
    event.preventDefault();
    if (!window.init?.userId) return;
    const previous = active.get(url.href);
    if (previous?.isConnected && previous.closest("#challenge-window.show")) return;
    active.set(url.href, button);
    const open = () => button.isConnected && button.closest("#challenge-window.show") && button.href === url.href;

    const original = Array.from(button.childNodes);
    notices.get(button)?.remove();
    const notice = document.createElement("div");
    notice.className = "alert alert-info mt-3";
    notice.setAttribute("role", "status");
    notice.setAttribute("aria-live", "polite");
    const spinner = document.createElement("span");
    spinner.className = "spinner-border spinner-border-sm mr-2 me-2";
    spinner.setAttribute("aria-hidden", "true");
    const message = document.createElement("span");
    message.textContent = "Preparing your file for download...";
    notice.append(spinner, message);
    notices.set(button, notice);
    button.closest(".challenge-files").append(notice);
    const icon = button.querySelector("i.fa-download");
    if (icon) {
      const buttonSpinner = document.createElement("span");
      buttonSpinner.className = "spinner-border spinner-border-sm";
      buttonSpinner.setAttribute("aria-hidden", "true");
      icon.replaceWith(buttonSpinner);
    }
    button.classList.add("disabled");
    button.setAttribute("aria-disabled", "true");
    button.setAttribute("aria-busy", "true");

    try {
      while (open()) {
        const controller = new AbortController();
        const timeout = setTimeout(() => controller.abort(), 10000);
        let response;
        let data;
        try {
          response = await fetch(url, {
            headers: { Accept: "application/json" },
            credentials: "same-origin",
            cache: "no-store",
            signal: controller.signal,
          });
          if (response.redirected || !response.headers.get("Content-Type")?.includes("application/json")) {
            throw new Error("Your file is unavailable. Refresh the page and try again.");
          }
          data = await response.json();
        } finally {
          clearTimeout(timeout);
        }
        if (!open()) break;
        if (response.ok && data.state === "ready") {
          let frame = downloads.get(url.href);
          if (!frame) {
            frame = document.createElement("iframe");
            frame.hidden = true;
            downloads.set(url.href, frame);
            document.body.append(frame);
          }
          frame.src = url.href;
          notice.remove();
          break;
        }
        if (!(response.status === 202 || (response.status === 503 && data.state === "busy"))) {
          throw new Error(data.message || "Your file is unavailable. Please contact an organizer.");
        }
        message.textContent = data.message;
        await new Promise((resolve) => setTimeout(resolve, 3000));
      }
    } catch (error) {
      notice.className = "alert alert-danger mt-3";
      notice.setAttribute("role", "alert");
      spinner.remove();
      message.textContent = error.name === "AbortError"
        ? "Your file request timed out. Please try again."
        : error.message || "Your file could not be downloaded. Please try again.";
    } finally {
      if (active.get(url.href) === button) active.delete(url.href);
      if (button.href === url.href) {
        button.replaceChildren(...original);
        button.classList.remove("disabled");
        button.removeAttribute("aria-disabled");
        button.removeAttribute("aria-busy");
      }
      if (!open()) notice.remove();
    }
  });
})();
