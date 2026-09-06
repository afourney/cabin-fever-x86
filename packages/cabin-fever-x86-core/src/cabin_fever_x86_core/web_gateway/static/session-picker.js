/** Choose a game before opening a microphone or a game-server session. */
export class SessionPickerController {
  constructor({ document, fetch = globalThis.fetch.bind(globalThis),
    onStart, onExpired = () => {}, preferredSession = null }) {
    this.document = document;
    this.fetch = fetch;
    this.onStart = onStart;
    this.onExpired = onExpired;
    this.preferredSession = preferredSession;
    this.loading = false;
    this.starting = false;
    this.ready = false;
    this.element("session-picker-form").addEventListener("submit", event => {
      event.preventDefault();
      this.start();
    });
    this.element("session-choice").addEventListener("change", () => this.render());
    this.element("sessions-retry").addEventListener("click", () => this.load());
  }

  element(id) { return this.document.getElementById(id); }

  render() {
    this.element("session-choice").disabled = !this.ready || this.starting;
    this.element("session-start").disabled = !this.ready || this.starting;
    this.element("session-start").textContent = this.starting ? "Opening the channel…"
      : this.element("session-choice").value ? "Resume session" : "Start new session";
  }

  async load() {
    if (this.loading || this.starting) return;
    this.loading = true;
    this.ready = false;
    this.element("sessions-error").textContent = "";
    this.element("sessions-retry").hidden = true;
    this.element("sessions-status").textContent = "Looking for saved sessions…";
    this.render();
    try {
      const response = await this.fetch("/sessions", { cache: "no-store" });
      if (response.status === 401 || response.status === 403) {
        this.onExpired();
        return;
      }
      if (!response.ok) throw new Error("Could not load saved sessions. Please try again.");
      const { sessions } = await response.json();
      const select = this.element("session-choice");
      select.replaceChildren();
      const addOption = (value, label) => {
        const option = this.document.createElement("option");
        option.value = value;
        option.textContent = label;
        select.append(option);
      };
      addOption("", "New Session");
      for (const session of [...sessions].sort((a, b) => Date.parse(b.modified) - Date.parse(a.modified))) {
        const date = new Date(session.modified).toLocaleString(undefined, {
          dateStyle: "medium", timeStyle: "short",
        });
        addOption(session.session_id, `${date} · ${session.session_id.slice(0, 8)}`);
      }
      select.value = sessions.some(session => session.session_id === this.preferredSession)
        ? this.preferredSession : "";
      this.element("sessions-status").textContent = sessions.length
        ? "Saved sessions are listed by when you last played, newest first."
        : "No saved sessions yet. Your first conversation starts here.";
      this.ready = true;
    } catch (error) {
      this.element("sessions-status").textContent = "";
      this.element("sessions-error").textContent = error.message || "Could not load saved sessions.";
      this.element("sessions-retry").hidden = false;
    } finally {
      this.loading = false;
      this.render();
    }
  }

  async start() {
    if (!this.ready || this.starting) return;
    this.starting = true;
    this.element("sessions-error").textContent = "";
    this.render();
    try {
      await this.onStart(this.element("session-choice").value || null);
    } catch (error) {
      this.element("sessions-error").textContent = error.message || "Could not open the channel. Please try again.";
    } finally {
      this.starting = false;
      this.render();
    }
  }
}
