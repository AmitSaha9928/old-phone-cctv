// Captures the microphone, resamples to 16 kHz mono and posts 40 ms chunks of signed 16-bit PCM.
class TalkCapture extends AudioWorkletProcessor {
  constructor() {
    super();
    this.ratio = sampleRate / 16000;
    this.pos = 0;
    this.out = new Int16Array(640);
    this.n = 0;
  }
  process(inputs) {
    const input = inputs[0] && inputs[0][0];
    if (!input) return true;
    // linear interpolation resampler, state carried across blocks
    while (this.pos < input.length - 1) {
      const i = Math.floor(this.pos), f = this.pos - i;
      const s = input[i] * (1 - f) + input[i + 1] * f;
      this.out[this.n++] = Math.max(-1, Math.min(1, s)) * 32767;
      if (this.n === this.out.length) {
        this.port.postMessage(this.out.buffer.slice(0));
        this.n = 0;
      }
      this.pos += this.ratio;
    }
    this.pos -= input.length;
    return true;
  }
}
registerProcessor('talk-capture', TalkCapture);
