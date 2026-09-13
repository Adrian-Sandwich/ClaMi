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
  function tone({f, to, at = 0, dur = .2, type = 'sine', level = 1, attack = .01}) {
    const t0 = context.currentTime + at;
    const osc = context.createOscillator(), gain = context.createGain();
    osc.type = type;
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
  const cues = {
    // Envío: dos blips secos de consola, como teclear en la terminal MAGI.
    send() {
      tone({f: 880, dur: .06, type: 'square', level: .5, attack: .004});
      tone({f: 1318.5, at: .07, dur: .06, type: 'square', level: .4, attack: .004});
    },
    // Voto: una campana medida, una sola nota con coro.
    vote() { pad(523.25, {dur: .55, level: .8, attack: .008}); },
    // Veredicto: raíz — tritono — octava. El sello de la decisión.
    result() {
      pad(164.81, {dur: .38, level: .9});
      pad(233.08, {at: .16, dur: .38, level: .85});
      pad(329.63, {at: .32, dur: .6, level: .9});
    },
    // Alerta: drone grave sostenido y llamado de trompa en segunda menor,
    // dos veces. Es el "algo se trabó / falló la ejecución" de NERV.
    attention() {
      tone({f: 55, dur: 1.5, type: 'sawtooth', level: .8, attack: .25});
      tone({f: 56.7, dur: 1.5, type: 'sawtooth', level: .55, attack: .3});
      pad(220, {to: 233.08, at: .1, dur: 1.0, type: 'triangle', level: .85, attack: .18});
      pad(220, {to: 233.08, at: 1.15, dur: 1.1, type: 'triangle', level: .85, attack: .18});
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
