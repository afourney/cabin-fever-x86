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
function page({ saved = null, supported = true, blockedStorage = false, onInputChange,
  ambienceMuted = false, permissions } = {}) {
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
  const settings = new AudioSettings({ document, mediaDevices, permissions, onInputChange,
    getAmbienceMuted: () => ambienceMuted,
    onAmbienceMute: value => { ambienceMuted = value; },
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

test("the settings click requests device access, shares a pending prompt, and releases a late stream", async () => {
  const p = page();
  const available = await p.mediaDevices.enumerateDevices();
  p.setDevices([{ kind: "audiooutput", deviceId: "default", label: "" }]);
  await p.settings.refreshDevices();
  assert.equal(p.requests.length, 0, "background device discovery never requests microphone access");
  assert.equal(p.$("ambience-device").children.length, 1);
  const permission = deferred(), requested = deferred(), microphone = stream();
  let calls = 0;
  p.mediaDevices.getUserMedia = async constraints => {
    calls++;
    assert.deepEqual(constraints, { audio: true });
    requested.resolve();
    await permission.promise;
    p.setDevices(available);
    return microphone;
  };
  const opened = p.$("audio-settings-open").onclick();
  assert.equal(p.$("audio-settings").open, true);
  await requested.promise;
  p.$("audio-settings").close();
  const reopened = p.$("audio-settings-open").onclick();
  await new Promise(resolve => setImmediate(resolve));
  assert.equal(calls, 1, "reopening shares the pending permission request");
  p.$("audio-settings").close();
  permission.resolve();
  await Promise.all([opened, reopened]);
  assert.equal(microphone.track.stopped, true, "late approval after closing also releases the microphone");
  assert.equal(p.settings.preview, null);
  assert.deepEqual(p.$("ambience-device").children.map(option => option.value), ["", "handset", "speakers"]);
  assert.deepEqual(p.$("radio-device").children.map(option => option.value), ["", "handset", "speakers"]);
});

test("opening settings requests permission without waiting for audio playback", async () => {
  const p = page(), playback = deferred(), requested = deferred(), microphone = stream();
  const available = await p.mediaDevices.enumerateDevices();
  p.setDevices([]);
  p.settings.resume = () => playback.promise;
  p.mediaDevices.getUserMedia = async () => {
    p.setDevices(available);
    requested.resolve();
    return microphone;
  };
  const opened = p.$("audio-settings-open").onclick();
  await requested.promise;
  await new Promise(resolve => setImmediate(resolve));
  assert.equal(microphone.track.stopped, true);
  assert.equal(p.$("audio-settings").open, true);
  playback.resolve();
  await opened;
});

test("denied device access stays retryable and preserves the chosen outputs", async () => {
  const p = page({ saved: JSON.stringify({ radio: "handset", ambience: "speakers" }) });
  p.setDevices([]);
  p.mediaDevices.getUserMedia = async () => { throw new Error("Not allowed"); };
  await p.$("audio-settings-open").onclick();
  assert.equal(p.$("audio-settings").open, true);
  assert.match(p.$("audio-settings-status").textContent, /Allow microphone access/);
  assert.equal(p.settings.preferences.radio, "handset");
  assert.equal(p.settings.preferences.ambience, "speakers");
  const microphone = stream();
  p.mediaDevices.getUserMedia = async () => microphone;
  p.$("audio-settings").close();
  await p.$("audio-settings-open").onclick();
  assert.equal(microphone.track.stopped, true);
});

test("opening settings with granted permission never opens the microphone", async () => {
  let queries = 0;
  const p = page({ permissions: { async query(descriptor) {
    assert.deepEqual(descriptor, { name: "microphone" });
    queries++;
    return { state: "granted", addEventListener() {} };
  } } });
  p.setDevices([{ kind: "audioinput", deviceId: "default", label: "" }]);
  await p.settings.open();
  assert.equal(p.$("audio-device-access").textContent, "Microphone access is allowed.");
  assert.equal(p.requests.length, 0, "checking permission never opens a microphone");
  await p.settings.open();
  assert.equal(queries, 1);
  assert.equal(p.requests.length, 0);
});

test("permission changes refresh devices and access status without overwriting operation messages", async () => {
  let onChange, listeners = 0;
  const permission = { state: "prompt", addEventListener(event, callback) {
    assert.equal(event, "change");
    listeners++;
    onChange = callback;
  } };
  const p = page({ permissions: { query: async () => permission } });
  const available = await p.mediaDevices.enumerateDevices();
  await p.settings.initialize();
  await p.settings.refreshDevices();
  assert.match(p.$("audio-device-access").textContent, /Allow microphone access/, "permission takes precedence over cached labels");
  p.setDevices([]);
  await p.settings.refreshDevices();
  assert.equal(p.$("ambience-device").children.length, 1);
  p.setDevices(available);
  permission.state = "granted";
  await onChange();
  assert.match(p.$("audio-device-access").textContent, /access is allowed/);
  assert.equal(p.$("ambience-device").children.length, 3);
  await p.settings.changeOutput("ambience", "speakers");
  const message = p.$("audio-settings-status").textContent;
  permission.state = "denied";
  await onChange();
  assert.match(p.$("audio-device-access").textContent, /blocked.*site settings/);
  assert.equal(p.settings.preferences.ambience, "speakers");
  assert.equal(p.$("audio-settings-status").textContent, message);
  permission.state = "prompt";
  await onChange();
  assert.match(p.$("audio-device-access").textContent, /Allow microphone access/);
  assert.equal(listeners, 1);
  assert.equal(p.requests.length, 0);
});

test("denied permission is not requested again until browser settings change", async () => {
  let onChange;
  const permission = { state: "prompt", addEventListener(_, callback) { onChange = callback; } };
  const p = page({ permissions: { query: async () => permission } });
  let requests = 0;
  p.mediaDevices.getUserMedia = async () => {
    requests++;
    permission.state = "denied";
    await onChange();
    throw new Error("Permission denied");
  };
  await p.$("audio-settings-open").onclick();
  p.$("audio-settings").close();
  await p.$("audio-settings-open").onclick();
  assert.equal(requests, 1);
  assert.match(p.$("audio-device-access").textContent, /blocked/);
  permission.state = "prompt";
  await onChange();
  const microphone = stream();
  p.mediaDevices.getUserMedia = async () => { requests++; permission.state = "granted"; return microphone; };
  p.$("audio-settings").close();
  await p.$("audio-settings-open").onclick();
  assert.equal(requests, 2);
  assert.equal(microphone.track.stopped, true);
  assert.match(p.$("audio-device-access").textContent, /access is allowed/);
});

test("unsupported permission queries fall back to device names without capturing audio", async () => {
  const p = page({ permissions: { query: async () => { throw new TypeError("Unsupported"); } } });
  await p.settings.open();
  assert.match(p.$("audio-device-access").textContent, /names are available/);
  p.setDevices([]);
  await p.settings.refreshDevices();
  assert.match(p.$("audio-device-access").textContent, /Allow microphone access/);
  assert.equal(p.requests.length, 0);
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

for (const latestFirst of [false, true]) {
  test(`overlapping refreshes recover unplugged speakers when the ${latestFirst ? "latest" : "older"} enumeration finishes first`, async () => {
    const p = page({ saved: JSON.stringify({ radio: "handset", ambience: "speakers" }) });
    await p.settings.initialize();
    await p.settings.refreshDevices();
    const older = deferred(), latest = deferred();
    const enumerate = p.mediaDevices.enumerateDevices;
    const pending = [older.promise, latest.promise];
    p.mediaDevices.enumerateDevices = () => pending.shift() ?? enumerate();
    p.setDevices([]);
    const disconnected = p.mediaDevices.events.devicechange();
    const refreshed = p.$("audio-refresh").onclick();
    if (latestFirst) {
      latest.resolve([]);
      await refreshed;
      older.resolve([]);
      await disconnected;
    } else {
      older.resolve([]);
      await disconnected;
      latest.resolve([]);
      await refreshed;
    }
    for (const [index, name] of ["radio", "ambience"].entries()) {
      assert.equal(p.contexts[index].sinkId, "");
      assert.equal(p.settings.preferences[name], "");
      assert.equal(p.saved()[name], "");
      assert.equal(p.$(`${name}-device`).value, "");
    }
    assert.match(p.$("audio-settings-status").textContent, /disconnected/);
  });
}

test("a failed enumeration leaves disconnect recovery pending for the next refresh", async () => {
  const p = page();
  await p.settings.initialize();
  await p.settings.changeOutput("radio", "handset");
  const enumerate = p.mediaDevices.enumerateDevices;
  p.mediaDevices.enumerateDevices = async () => { throw new Error("Device discovery failed"); };
  await p.mediaDevices.events.devicechange();
  assert.equal(p.settings.preferences.radio, "handset");
  p.mediaDevices.enumerateDevices = enumerate;
  p.setDevices([]);
  await p.$("audio-refresh").onclick();
  assert.equal(p.contexts[0].sinkId, "");
  assert.equal(p.saved().radio, "");
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
  assert.equal(p.saved().microphone, "");
});

for (const action of ["select another microphone", "close settings"]) {
  test(`a pending preview fallback cannot change preferences after ${action}`, async () => {
    const p = page({ saved: JSON.stringify({ microphone: "missing" }) });
    const pending = deferred(), requested = deferred(), microphone = stream();
    p.mediaDevices.getUserMedia = async constraints => {
      if (constraints.audio !== true) throw Object.assign(new Error(), { name: "NotFoundError" });
      requested.resolve();
      return pending.promise;
    };
    const preview = p.settings.togglePreview();
    await requested.promise;
    if (action === "select another microphone") {
      p.$("microphone-device").value = "new-mic";
      p.$("microphone-device").onchange();
    } else {
      p.$("audio-settings").close();
    }
    const expected = action === "select another microphone" ? "new-mic" : "missing";
    const message = p.$("audio-settings-status").textContent;
    pending.resolve(microphone);
    await preview;
    assert.equal(p.settings.preferences.microphone, expected);
    assert.equal(p.saved().microphone, expected);
    assert.equal(p.$("audio-settings-status").textContent, message);
    assert.equal(microphone.track.stopped, true);
    assert.equal(p.settings.preview, null);
    assert.equal(p.$("microphone-test").textContent, "Test microphone");
  });
}

test("microphone fallback outside a preview also preserves a newer selection", async () => {
  const p = page({ saved: JSON.stringify({ microphone: "missing" }) });
  const pending = deferred(), requested = deferred(), microphone = stream();
  p.mediaDevices.getUserMedia = async constraints => {
    if (constraints.audio !== true) throw Object.assign(new Error(), { name: "OverconstrainedError" });
    requested.resolve();
    return pending.promise;
  };
  const capturing = p.settings.getMicrophoneStream();
  await requested.promise;
  p.$("microphone-device").value = "new-mic";
  p.$("microphone-device").onchange();
  const message = p.$("audio-settings-status").textContent;
  pending.resolve(microphone);
  const result = await capturing;
  result.getTracks().forEach(track => track.stop());
  assert.equal(result, microphone);
  assert.equal(p.settings.preferences.microphone, "new-mic");
  assert.equal(p.saved().microphone, "new-mic");
  assert.equal(p.$("audio-settings-status").textContent, message);
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


test("ambience mute reflects and updates the existing rain setting", () => {
  const p = page({ ambienceMuted: true });
  assert.equal(p.$("ambience-mute").textContent, "Unmute");
  assert.equal(p.$("ambience-mute").events?.click, undefined);
  p.$("ambience-mute").onclick();
  assert.equal(p.$("ambience-mute").textContent, "Mute");
  p.$("ambience-mute").onclick();
  assert.equal(p.$("ambience-mute").textContent, "Unmute");
});
