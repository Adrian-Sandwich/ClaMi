// Señales originales 100% sintetizadas, con estética MAGI/Evangelion:
// blip de consola, campana de voto, motivo de veredicto y alerta grave.
// Sin grabaciones ni audio de terceros.
const MagiSound = (() => {
  let context, enabled = false, volume = .25, last = -Infinity;
  try {
    const saved = JSON.parse(localStorage.getItem('clami.sound') || '{}');
    enabled = saved.enabled === true;
    if (Number.isFinite(saved.volume)) volume = Math.max(0, Math.min(1, saved.volume));
  } catch (_) { /* Storage is optional. */ }
  const voices = new Set();
  function save() {
    try { localStorage.setItem('clami.sound', JSON.stringify({enabled, volume})); } catch (_) {}
  }
  function silence() { for (const voice of voices) voice.stop(); voices.clear(); }
  async function unlock() {
    if (!enabled) return;
    try {
      context ||= new (window.AudioContext || window.webkitAudioContext)();
      if (context.state === 'suspended') await context.resume();
    } catch (_) { /* Unsupported audio must never interrupt a message. */ }
  }
  // Una voz con envolvente: frecuencia base, glide opcional, ataque y caída.
  // Frequency glides and inharmonic partials give the console a sci-fi hardware character.
  function tone({f, to, at = 0, dur = .2, type = 'sine', level = 1, attack = .01, detune = 0}) {
    const t0 = context.currentTime + at;
    const osc = context.createOscillator(), gain = context.createGain();
    osc.type = type;
    osc.detune.value = detune;
    osc.frequency.setValueAtTime(f, t0);
    if (to) osc.frequency.exponentialRampToValueAtTime(to, t0 + dur);
    gain.gain.setValueAtTime(0, t0);
    gain.gain.linearRampToValueAtTime(volume * .14 * level, t0 + attack);
    gain.gain.exponentialRampToValueAtTime(.0001, t0 + dur);
    osc.connect(gain); gain.connect(context.destination);
    voices.add(osc);
    osc.onended = () => { voices.delete(osc); osc.disconnect(); gain.disconnect(); };
    osc.start(t0); osc.stop(t0 + dur + .05);
  }
  // Pareja desafinada leve: el "coro" que da grosor cinematográfico.
  function pad(f, opts = {}) {
    tone({f, ...opts});
    tone({f: f * 1.004, to: opts.to ? opts.to * 1.004 : undefined,
          at: opts.at ?? 0, dur: opts.dur, type: opts.type,
          level: (opts.level ?? 1) * .7, attack: opts.attack});
  }
  function metallic({f, at = 0, dur = .18, level = .5}) {
    tone({f, at, dur, type: 'square', level, attack: .003, detune: -11});
    tone({f: f * 1.618, at, dur: dur * .72, type: 'triangle', level: level * .55, attack: .002, detune: 7});
  }
  function radio({at = 0, dur = .08, level = .28}) {
    const frames = Math.max(1, Math.floor(context.sampleRate * dur));
    const buffer = context.createBuffer(1, frames, context.sampleRate);
    const data = buffer.getChannelData(0);
    for (let i = 0; i < frames; i++) data[i] = (Math.random() * 2 - 1) * (1 - i / frames);
    const source = context.createBufferSource();
    const filter = context.createBiquadFilter(), gain = context.createGain();
    const t0 = context.currentTime + at;
    filter.type = 'bandpass'; filter.frequency.value = 2400; filter.Q.value = .8;
    gain.gain.setValueAtTime(volume * level, t0);
    gain.gain.exponentialRampToValueAtTime(.0001, t0 + dur);
    source.buffer = buffer; source.connect(filter); filter.connect(gain); gain.connect(context.destination);
    voices.add(source);
    source.onended = () => { voices.delete(source); source.disconnect(); filter.disconnect(); gain.disconnect(); };
    source.start(t0); source.stop(t0 + dur + .01);
  }
  const cues = {
    boot() {
      radio({dur: .12, level: .18});
      tone({f: 180, to: 540, dur: .24, type: 'sawtooth', level: .38, attack: .015});
      tone({f: 720, to: 360, at: .18, dur: .22, type: 'square', level: .28, attack: .006});
    },
    // Envío: dos blips secos de consola, como teclear en la terminal MAGI.
    send() {
      radio({dur: .045, level: .18});
      metallic({f: 740, dur: .07, level: .42});
      tone({f: 1480, to: 620, at: .075, dur: .12, type: 'square', level: .34, attack: .004});
    },
    // Voto: una campana medida, una sola nota con coro.
    vote() {
      radio({dur: .035, level: .14});
      metallic({f: 392, dur: .1, level: .45});
      pad(587.33, {at: .04, dur: .42, type: 'triangle', level: .62, attack: .006});
    },
    // Veredicto: raíz — tritono — octava. El sello de la decisión.
    result() {
      tone({f: 82.41, to: 123.47, dur: .72, type: 'sawtooth', level: .55, attack: .12});
      pad(164.81, {dur: .38, level: .7});
      pad(233.08, {at: .16, dur: .38, level: .68});
      pad(329.63, {at: .32, dur: .6, level: .76});
    },
    // Alerta: drone grave sostenido y llamado de trompa en segunda menor,
    // dos veces. Es el "algo se trabó / falló la ejecución" de NERV.
    attention() {
      radio({dur: .16, level: .24});
      tone({f: 49, to: 62, dur: 2.1, type: 'sawtooth', level: .7, attack: .22});
      tone({f: 50.5, to: 63.5, dur: 2.1, type: 'sawtooth', level: .45, attack: .25});
      pad(220, {to: 185, at: .12, dur: .78, type: 'triangle', level: .8, attack: .08});
      pad(220, {to: 185, at: 1.02, dur: .78, type: 'triangle', level: .8, attack: .08});
      radio({at: .18, dur: .08, level: .18}); radio({at: 1.08, dur: .08, level: .18});
    },
  };
  function play(kind) {
    if (!enabled || !context || context.state !== 'running' || document.hidden || !volume) return;
    const now = context.currentTime;
    if (now - last < .18) return;
    last = now;
    (cues[kind] || cues.send)();
  }
  return {unlock, play, get enabled() {return enabled;}, get volume() {return volume;},
    setEnabled(value) {enabled = value; if (!value) silence(); save();},
    setVolume(value) {volume = Math.max(0, Math.min(1, value)); silence(); save();}};
})();
