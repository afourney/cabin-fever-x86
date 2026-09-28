import assert from "node:assert/strict";
import test from "node:test";
import { AudioSettings } from "../src/cabin_fever_x86_core/web_gateway/static/audio-settings.js";

function stream() {
  const track = { stopped: false, stop() { this.stopped = true; } };
  return { track, getTracks: () => [track] };
}
function deferred() {
  let resolve, reject;
  const promise = new Promise((yes, no) => { resolve = yes; reject = no; });
  return { promise, resolve, reject };
}
function page({ saved = null, supported = true, blockedStorage = false, onInputChange } = {}) {
  const elements = new Map(), contexts = [], requests = [];
  let devices = [
    { kind: "audioinput", deviceId: "mic", label: "USB microphone" },
    { kind: "audiooutput", deviceId: "handset", label: "USB handset" },
    { kind: "audiooutput", deviceId: "speakers", label: "Computer speakers" },
  ];
  const element = () => ({ value: "", textContent: "", children: [], disabled: false, hidden: false,
    events: {}, addEventListener(name, callback) { this.events[name] = callback; },
    append(child) { this.children.push(child); }, replaceChildren() { this.children = []; },
    blur() {},
    showModal() { this.open = true; }, close() { this.open = false; this.events.close?.(); } });
  const document = { createElement: element, getElementById(id) {
    if (!elements.has(id)) elements.set(id, element());
    return elements.get(id);
  } };
  const storage = { getItem: () => saved, setItem: (_, value) => { saved = value; } };
  const node = () => ({ gain: { value: 0, setTargetAtTime(value) { this.value = value; },
    setValueAtTime() {}, linearRampToValueAtTime() {} }, frequency: {},
    connect(destination) { this.destination = destination; }, disconnect() { this.disconnected = true; },
    start() {}, stop() { this.onended?.(); }, getFloatTimeDomainData(samples) { samples.fill(0.1); } });
  const mediaDevices = { events: {}, addEventListener(name, callback) { this.events[name] = callback; },
    enumerateDevices: async () => devices,
    getUserMedia: async constraints => { requests.push(constraints); return stream(); },
  };
  const settings = new AudioSettings({ document, mediaDevices, onInputChange,
    storage: () => { if (blockedStorage) throw new Error("blocked"); return storage; },
    createContext: () => {
      const context = { sinkId: "", currentTime: 0, destination: {}, resumes: 0,
        createGain: node, createOscillator: node, createMediaStreamSource: node, createAnalyser: node,
        resume: async () => { context.resumes++; },
      };
      if (supported) context.setSinkId = async id => { context.sinkId = id; };
      contexts.push(context); return context;
    },
  });
  return { settings, contexts, requests, mediaDevices, document,
    $: document.getElementById, saved: () => JSON.parse(saved), setDevices: value => { devices = value; } };
}

test("radio and ambience use independent contexts, sinks, gains, and saved preferences", async () => {
  const p = page();
  await p.settings.resume();
  await p.settings.changeOutput("radio", "handset");
  await p.settings.changeOutput("ambience", "speakers");
  p.settings.setVolume("radio", 0.4);
  p.settings.setVolume("ambience", 0.7);
  assert.equal(p.contexts.length, 2);
  assert.deepEqual(p.contexts.map(ctx => ctx.sinkId), ["handset", "speakers"]);
  assert.equal(p.settings.channels.radio.gain.gain.value, 0.4);
  assert.equal(p.settings.channels.ambience.gain.gain.value, 0.7);
  for (const { context, gain } of Object.values(p.settings.channels)) assert.equal(gain.destination, context.destination);
  const restored = page({ saved: JSON.stringify(p.saved()) });
  await restored.settings.initialize();
  assert.deepEqual(restored.contexts.map(ctx => ctx.sinkId), ["handset", "speakers"]);
  assert.equal(restored.settings.channels.ambience.gain.gain.value, 0.7);
});

test("resumes both contexts synchronously before waiting for a saved sink", async () => {
  const p = page({ saved: JSON.stringify({ radio: "handset" }) });
  const pending = deferred();
  p.settings.initialize();
  p.settings.ready = pending.promise;
  const resumed = p.settings.resume();
  assert.deepEqual(p.contexts.map(ctx => ctx.resumes), [1, 1]);
  pending.resolve();
  await resumed;
});

test("unsupported output selection keeps independent volume controls working", async () => {
  const p = page({ supported: false });
  await p.settings.open();
  assert.equal(p.$("radio-device").disabled, true);
  assert.equal(p.$("ambience-device").disabled, true);
  assert.equal(p.$("microphone-device").disabled, false);
  assert.equal(p.$("audio-output-help").hidden, false);
  p.settings.setVolume("radio", 0);
  assert.equal(p.settings.channels.radio.gain.gain.value, 0);
  assert.equal(p.settings.channels.ambience.gain.gain.value, 1);
});

test("failed speaker switching preserves the previous route and selection", async () => {
  const p = page();
  await p.settings.initialize();
  await p.settings.changeOutput("radio", "handset");
  p.contexts[0].setSinkId = async () => { throw new Error("Permission denied"); };
  await p.settings.changeOutput("radio", "speakers");
  assert.equal(p.contexts[0].sinkId, "handset");
  assert.equal(p.$("radio-device").value, "handset");
  assert.equal(p.saved().radio, "handset");
  assert.match(p.$("audio-settings-status").textContent, /previous output/);
});

test("in-flight output changes cannot race and browser picker permissions are supported", async () => {
  const p = page();
  await p.settings.initialize();
  const pending = deferred();
  p.mediaDevices.selectAudioOutput = () => pending.promise;
  const first = p.settings.changeOutput("radio", "__choose__");
  assert.equal(p.$("radio-device").disabled, true);
  await p.settings.changeOutput("radio", "speakers");
  pending.resolve({ deviceId: "handset" });
  await first;
  assert.equal(p.contexts[0].sinkId, "handset");
  assert.equal(p.$("radio-device").disabled, false);
});

test("unplugged known output falls back without treating hidden devices as unplugged", async () => {
  const p = page({ saved: JSON.stringify({ radio: "hidden-by-permissions" }) });
  await p.settings.initialize();
  await p.settings.refreshDevices(true);
  assert.equal(p.settings.preferences.radio, "hidden-by-permissions");
  await p.settings.changeOutput("radio", "handset");
  p.setDevices([]);
  await p.settings.refreshDevices(true);
  assert.equal(p.contexts[0].sinkId, "");
  assert.equal(p.settings.preferences.radio, "");
  assert.match(p.$("audio-settings-status").textContent, /disconnected/);
});

test("microphone selection uses exact constraints and missing devices fall back", async () => {
  const p = page({ saved: JSON.stringify({ microphone: "mic" }) });
  await p.settings.getMicrophoneStream();
  assert.deepEqual(p.requests[0], { audio: { deviceId: { exact: "mic" } } });
  const fallback = stream(), calls = [];
  p.mediaDevices.getUserMedia = async constraints => {
    calls.push(constraints);
    if (calls.length === 1) throw Object.assign(new Error(), { name: "OverconstrainedError" });
    return fallback;
  };
  assert.equal(await p.settings.getMicrophoneStream(), fallback);
  assert.deepEqual(calls[1], { audio: true });
  assert.equal(p.settings.preferences.microphone, "");
});

test("permission denial is surfaced without repeatedly requesting another microphone", async () => {
  const p = page({ saved: JSON.stringify({ microphone: "mic" }) });
  let calls = 0;
  p.mediaDevices.getUserMedia = async () => { calls++; throw Object.assign(new Error(), { name: "NotAllowedError" }); };
  await assert.rejects(p.settings.getMicrophoneStream(), { name: "NotAllowedError" });
  assert.equal(calls, 1);
  assert.equal(p.settings.preferences.microphone, "mic");
});

test("busy recording blocks microphone changes and preserves selection", () => {
  const p = page({ onInputChange: () => { throw new Error("Wait for transmission"); } });
  p.$("microphone-device").value = "mic";
  p.$("microphone-device").onchange();
  assert.equal(p.settings.preferences.microphone, "");
  assert.equal(p.$("microphone-device").value, "");
  assert.match(p.$("audio-settings-status").textContent, /Wait/);
});

test("closing a microphone test releases its stream, nodes, and meter", async () => {
  const p = page();
  await p.settings.togglePreview();
  const preview = p.settings.preview;
  assert.ok(preview);
  assert.equal(preview.analyser.destination, undefined, "microphone is never sent to speakers");
  p.$("audio-settings").close();
  assert.equal(preview.stream.track.stopped, true);
  assert.equal(preview.source.disconnected, true);
  assert.equal(preview.analyser.disconnected, true);
  assert.equal(p.settings.preview, null);
  assert.equal(p.$("microphone-level").value, 0);
});

test("closing while permission is pending stops a late-approved stream", async () => {
  const p = page(), pending = deferred(), lateStream = stream();
  p.mediaDevices.getUserMedia = () => pending.promise;
  const preview = p.settings.togglePreview();
  await new Promise(resolve => setImmediate(resolve));
  p.$("audio-settings").close();
  pending.resolve(lateStream);
  await preview;
  assert.equal(lateStream.track.stopped, true);
  assert.equal(p.settings.preview, null);
  assert.equal(p.$("microphone-test").textContent, "Test microphone");
});

test("blocked storage and invalid saved preferences do not prevent audio", async () => {
  for (const options of [{ blockedStorage: true }, { saved: "broken JSON" },
    { saved: JSON.stringify({ radio: 3, radioVolume: -4, ambienceVolume: 12 }) }]) {
    const p = page(options);
    await p.settings.resume();
    p.settings.setVolume("radio", 0.3);
    assert.equal(p.settings.channels.radio.gain.gain.value, 0.3);
    assert.equal(p.settings.preferences.radio, "");
  }
});
