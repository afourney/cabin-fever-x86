/** Callsign sign-in UI and HTTP renewal for the long-lived radio socket. */
export class BrowserAuthController {
  constructor({ document, fetch = globalThis.fetch.bind(globalThis), onExpired = () => {},
    onSignedIn = () => {},
    setInterval = globalThis.setInterval.bind(globalThis),
    clearInterval = globalThis.clearInterval.bind(globalThis) }) {
    this.document = document;
    this.fetch = fetch;
    this.onExpired = onExpired;
    this.onSignedIn = onSignedIn;
    this.setInterval = setInterval;
    this.clearInterval = clearInterval;
    this.state = null;
    this.busy = false;
    this.timer = null;
    this.element("login-form").addEventListener("submit", event => {
      event.preventDefault();
      this.login();
    });
    this.element("guest-login").addEventListener("click", () => this.guest());
    this.element("logout").addEventListener("click", () => this.logout());
    this.element("auth-retry").addEventListener("click", () => this.initialize());
  }

  element(id) { return this.document.getElementById(id); }
  get authenticated() { return this.state?.authenticated === true; }

  render() {
    const signedIn = this.authenticated;
    this.element("radio-call").hidden = !signedIn;
    this.element("auth-panel").hidden = signedIn;
    this.element("login-form").hidden = signedIn || !this.state?.login_available;
    this.element("guest-login").hidden = signedIn || !this.state?.guest_available;
    this.element("logout").hidden = !signedIn || !!this.state?.implicit_guest;
    this.element("splash").setAttribute("role", signedIn ? "button" : "group");
    this.element("splash").setAttribute("tabindex", signedIn ? "0" : "-1");
    this.element("splash").setAttribute("aria-label", signedIn ? "Turn on your radio" : "Radio sign in");
    this.element("auth-message").textContent =
      !signedIn && !this.state?.login_available && !this.state?.guest_available
        ? "No browser access is configured. Ask the station operator for a callsign."
        : "Identify yourself to open the channel.";
  }

  async initialize() {
    this.element("auth-retry").hidden = true;
    try {
      const response = await this.fetch("/auth", { cache: "no-store" });
      if (!response.ok) throw new Error("Could not reach the station. Check the address and connection.");
      this.state = await response.json();
      this.element("auth-error").textContent = "";
      this.render();
      this.clearInterval(this.timer);
      this.timer = null;
      if (this.authenticated) {
        this.timer = this.setInterval(() => this.refresh(), this.state.refresh_seconds * 1000);
      }
    } catch (error) {
      this.element("auth-error").textContent = error.message || "Could not reach the station.";
      this.element("auth-retry").hidden = false;
    }
  }

  async action(path, body) {
    if (this.busy) return false;
    this.busy = true;
    this.element("login-submit").disabled = true;
    this.element("guest-login").disabled = true;
    this.element("auth-error").textContent = "";
    try {
      const response = await this.fetch(path, {
        method: "POST",
        headers: { "Content-Type": "application/json" },
        body: JSON.stringify(body ?? {}),
      });
      if (!response.ok) {
        const result = await response.json();
        throw new Error(typeof result.detail === "string" ? result.detail : "Sign-in failed.");
      }
      await this.initialize();
      if (this.authenticated && path !== "/auth/logout") await this.onSignedIn();
      return true;
    } catch (error) {
      this.element("auth-error").textContent = error.message || "Could not reach the station.";
      return false;
    } finally {
      this.busy = false;
      this.element("login-submit").disabled = false;
      this.element("guest-login").disabled = false;
    }
  }

  async login() {
    const username = this.element("callsign").value.trim();
    const password = this.element("password").value;
    this.element("password").value = "";
    return this.action("/auth/login", { username, password });
  }

  async guest() { return this.action("/auth/guest"); }

  async logout() {
    if (await this.action("/auth/logout")) {
      this.clearInterval(this.timer);
      this.onExpired();
    } else {
      this.element("logout").textContent = "Sign out failed — retry";
    }
  }

  async refresh() {
    try {
      const response = await this.fetch("/auth/refresh", { method: "POST" });
      if (response.status === 401 || response.status === 403) {
        this.clearInterval(this.timer);
        this.state = { ...this.state, authenticated: false };
        this.onExpired();
      }
    } catch { /* A transient outage is not a logout; the server still enforces expiry. */ }
  }
}
