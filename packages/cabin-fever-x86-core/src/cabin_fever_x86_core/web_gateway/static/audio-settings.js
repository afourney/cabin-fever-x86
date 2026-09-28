// Separate Web Audio contexts let the radio and the cabin use different speakers.
const STORAGE_KEY = "cf86-audio";
const CHANNELS = ["radio", "ambience"];

export class AudioSettings {
  constructor({ document, mediaDevices = globalThis.navigator?.mediaDevices,
    createContext = () => new (globalThis.AudioContext || globalThis.webkitAudioContext)(),
    storage = () => globalThis.localStorage, onOpen = () => {}, onInputChange = () => {} }) {
    this.document = document;
    this.mediaDevices = mediaDevices;
    this.createContext = createContext;
    this.storage = storage;
    this.onOpen = onOpen;
    this.onInputChange = onInputChange;
    this.channels = {};
    this.preferences = { microphone: "", radio: "", ambience: "", radioVolume: 1, ambienceVolume: 1 };
    this.preview = null;
    this.previewGeneration = 0;
    this.devices = [];
    this.refreshGeneration = 0;
    try {
      const saved = JSON.parse(this.storage().getItem(STORAGE_KEY));
      for (const key of ["microphone", ...CHANNELS]) {
        if (typeof saved?.[key] === "string") this.preferences[key] = saved[key];
      }
      for (const channel of CHANNELS) {
        const value = saved?.[`${channel}Volume`];
        if (Number.isFinite(value)) this.preferences[`${channel}Volume`] = Math.max(0, Math.min(1, value));
      }
    } catch { /* Storage can be blocked; audio still works for this visit. */ }
    const $ = id => document.getElementById(id);
    this.$ = $;
    $("audio-settings-open").onclick = () => this.open();
    $("audio-settings-close").onclick = () => $("audio-settings").close();
    $("audio-settings").addEventListener("close", () => this.stopPreview());
    $("audio-refresh").onclick = () => this.refreshDevices();
    $("microphone-test").onclick = () => this.togglePreview();
    $("microphone-device").onchange = () => {
      const select = $("microphone-device");
      try {
        this.onInputChange(); // Refuse to replace a recorder whose stop event is still queued.
        this.stopPreview();
        this.preferences.microphone = select.value;
        this.message("Microphone updated. Your next transmission will use this device.");
        this.save();
      } catch (error) {
        select.value = this.preferences.microphone;
        this.message(error.message);
      }
    };
    for (const name of CHANNELS) {
      const select = $(`${name}-device`);
      select.onchange = () => this.changeOutput(name, select.value);
      const slider = $(`${name}-volume`);
      slider.value = this.preferences[`${name}Volume`] * 100;
      $(`${name}-volume-value`).textContent = `${slider.value}%`;
      slider.oninput = () => this.setVolume(name, Number(slider.value) / 100);
      $(`${name}-test`).onclick = () => this.testOutput(name);
    }
    this.mediaDevices?.addEventListener?.("devicechange", () => this.refreshDevices(true));
  }

  message(text) { this.$("audio-settings-status").textContent = text; }

  save() {
    try { this.storage().setItem(STORAGE_KEY, JSON.stringify(this.preferences)); }
    catch { this.message("These settings work for this visit, but your browser could not save them."); }
  }

  initialize() {
    if (this.ready) return this.ready;
    for (const name of CHANNELS) {
      const context = this.createContext();
      const gain = context.createGain();
      gain.gain.value = this.preferences[`${name}Volume`];
      gain.connect(context.destination);
      this.channels[name] = { context, gain, pending: false };
    }
    this.ready = Promise.all(CHANNELS.map(async name => {
      const { context } = this.channels[name];
      const deviceId = this.preferences[name];
      if (!deviceId) return;
      try {
        if (!context.setSinkId) throw new Error("Output selection unavailable");
        await context.setSinkId(deviceId);
      } catch {
        this.preferences[name] = "";
        this.save();
        this.message("A saved speaker could not be restored. Using system default; choose it again in Audio settings.");
      }
    }));
    return this.ready;
  }

  async resume() {
    // Call resume before any await so both contexts receive the user's gesture.
    const ready = this.initialize();
    await Promise.all([ready, ...CHANNELS.map(name => this.channels[name].context.resume())]);
  }

  async open() {
    this.onOpen();
    this.$("audio-settings").showModal();
    try { await this.resume(); }
    catch { this.message("Audio could not start. Check your browser's audio permissions."); }
    await this.refreshDevices();
  }

  async refreshDevices(checkDisconnected = false) {
    const generation = ++this.refreshGeneration;
    if (!this.mediaDevices?.enumerateDevices) {
      this.message("Device selection requires HTTPS or localhost and a browser with audio-device support.");
      for (const name of ["microphone", ...CHANNELS]) this.$(`${name}-device`).disabled = true;
      return;
    }
    try {
      const devices = await this.mediaDevices.enumerateDevices();
      if (generation !== this.refreshGeneration) return;
      const previous = this.devices;
      this.devices = devices;
      for (const name of ["microphone", ...CHANNELS]) {
        const select = this.$(`${name}-device`);
        const output = name !== "microphone";
        const supported = !output || Boolean(this.channels[name]?.context.setSinkId);
        const selected = this.preferences[name];
        // Only declare a disconnect for a device we actually saw before. An
        // initial, permission-filtered enumeration does not prove it is gone.
        if (checkDisconnected && output && !this.channels[name]?.pending && selected &&
            previous.some(device => device.deviceId === selected) &&
            !devices.some(device => device.deviceId === selected)) {
          if (await this.changeOutput(name, "")) {
            this.message(`${name === "radio" ? "Radio" : "Ambience"} speaker disconnected. Using system default.`);
          }
        }
        select.replaceChildren();
        const option = (value, label) => {
          const item = this.document.createElement("option");
          item.value = value; item.textContent = label; select.append(item);
        };
        option("", "System default");
        const kind = output ? "audiooutput" : "audioinput";
        const available = devices.filter(device => device.kind === kind && device.deviceId && device.deviceId !== "default");
        available.forEach((device, index) => option(device.deviceId, device.label || `${output ? "Speaker" : "Microphone"} ${index + 1}`));
        if (this.preferences[name] && !available.some(device => device.deviceId === this.preferences[name])) {
          option(this.preferences[name], "Saved device (permission or connection needed)");
        }
        if (output && supported && this.mediaDevices.selectAudioOutput) option("__choose__", "Choose another speaker…");
        select.value = this.preferences[name];
        select.disabled = !supported || Boolean(this.channels[name]?.pending);
      }
      this.$("audio-output-help").hidden = Boolean(this.channels.radio?.context.setSinkId);
    } catch { this.message("Could not list audio devices. Check browser permissions and try Refresh devices."); }
  }

  async changeOutput(name, deviceId) {
    const channel = this.channels[name];
    if (!channel || channel.pending) return;
    const select = this.$(`${name}-device`);
    channel.pending = true;
    select.disabled = true;
    try {
      // The browser picker must be invoked directly from the user's gesture.
      if (deviceId === "__choose__") deviceId = (await this.mediaDevices.selectAudioOutput()).deviceId;
      await channel.context.setSinkId(deviceId);
      this.preferences[name] = deviceId;
      this.message(`${name === "radio" ? "Radio" : "Ambience"} output updated.`);
      this.save();
      return true;
    } catch {
      this.message("Could not switch speakers. The previous output is still selected. Check the connection and browser permissions.");
    } finally {
      channel.pending = false;
      select.value = this.preferences[name];
      // Re-enumerate in case the browser picker granted access to a new device.
      await this.refreshDevices();
    }
  }

  setVolume(name, volume) {
    volume = Math.max(0, Math.min(1, volume));
    this.preferences[`${name}Volume`] = volume;
    const channel = this.channels[name];
    channel?.gain.gain.setTargetAtTime(volume, channel.context.currentTime, 0.03);
    this.$(`${name}-volume-value`).textContent = `${Math.round(volume * 100)}%`;
    this.save();
  }

  async getMicrophoneStream() {
    if (!this.mediaDevices?.getUserMedia) throw new Error("Microphone access requires HTTPS or localhost and a supported browser");
    const deviceId = this.preferences.microphone;
    try {
      return await this.mediaDevices.getUserMedia({ audio: deviceId ? { deviceId: { exact: deviceId } } : true });
    } catch (error) {
      if (!deviceId || !["NotFoundError", "OverconstrainedError"].includes(error.name)) throw error;
      // USB headsets disappear and saved device IDs can expire between visits.
      const stream = await this.mediaDevices.getUserMedia({ audio: true });
      this.preferences.microphone = "";
      this.save();
      this.message("The saved microphone is unavailable. Using your system-default microphone.");
      return stream;
    }
  }

  async togglePreview() {
    if (this.preview || this.previewPending) { this.stopPreview(); return; }
    const generation = ++this.previewGeneration;
    this.previewPending = true;
    this.$("microphone-test").textContent = "Cancel test";
    this.message("Allow microphone access if prompted, then speak to check the level. Nothing is sent.");
    let stream;
    try {
      await this.resume();
      if (generation !== this.previewGeneration) return;
      stream = await this.getMicrophoneStream();
      if (generation !== this.previewGeneration) { stream.getTracks().forEach(track => track.stop()); return; }
      const context = this.channels.radio.context;
      const source = context.createMediaStreamSource(stream);
      const analyser = context.createAnalyser();
      this.preview = { stream, source, analyser };
      analyser.fftSize = 2048;
      source.connect(analyser); // Deliberately unconnected to either output.
      const samples = new Float32Array(analyser.fftSize);
      this.preview.timer = setInterval(() => {
        analyser.getFloatTimeDomainData(samples);
        const rms = Math.sqrt(samples.reduce((sum, value) => sum + value * value, 0) / samples.length);
        this.$("microphone-level").value = Math.max(0, Math.min(1, (20 * Math.log10(Math.max(rms, 0.000001)) + 60) / 48));
      }, 100);
      this.$("microphone-test").textContent = "Stop test";
      this.message("Microphone test is running locally. Nothing is recorded or sent.");
      await this.refreshDevices();
    } catch {
      stream?.getTracks().forEach(track => track.stop());
      if (generation === this.previewGeneration) {
        this.stopPreview();
        this.message("Could not test the microphone. Allow microphone access in your browser's site settings, then try again.");
      }
    } finally {
      if (generation === this.previewGeneration) this.previewPending = false;
    }
  }

  stopPreview() {
    this.previewGeneration++;
    this.previewPending = false;
    if (this.preview) {
      clearInterval(this.preview.timer);
      this.preview.source.disconnect();
      this.preview.analyser.disconnect();
      this.preview.stream.getTracks().forEach(track => track.stop());
      this.preview = null;
    }
    this.$("microphone-test").textContent = "Test microphone";
    this.$("microphone-level").value = 0;
  }

  async testOutput(name) {
    try {
      await this.resume();
      const { context, gain } = this.channels[name];
      const tone = context.createOscillator(), envelope = context.createGain();
      const now = context.currentTime;
      tone.frequency.value = name === "radio" ? 660 : 440;
      envelope.gain.setValueAtTime(0, now);
      envelope.gain.linearRampToValueAtTime(0.12, now + 0.03);
      envelope.gain.linearRampToValueAtTime(0, now + 0.35);
      tone.connect(envelope); envelope.connect(gain);
      tone.onended = () => { tone.disconnect(); envelope.disconnect(); };
      tone.start(now); tone.stop(now + 0.4);
      this.message(`Playing a test tone through the ${name} output at its selected volume.`);
    } catch { this.message("Could not play the test tone. Check your speaker connection and browser permissions."); }
  }
}
