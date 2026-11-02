// Runs on the browser's audio thread. It takes the microphone's float samples, collects about 20 ms of them,
// converts them to 16-bit integers and posts them to the page. It never touches the network.
class CaptureProcessor extends AudioWorkletProcessor {
  constructor(options) {
    super();
    const chunkMs = (options.processorOptions && options.processorOptions.chunkMs) || 20;
    this.chunkSamples = Math.max(128, Math.round(sampleRate * chunkMs / 1000));
    this.buffer = new Int16Array(this.chunkSamples);
    this.filled = 0;
  }

  process(inputs) {
    const channel = inputs[0] && inputs[0][0];
    if (!channel) return true;                       // no microphone data this round; keep running
    for (let i = 0; i < channel.length; i++) {
      const s = Math.max(-1, Math.min(1, channel[i]));
      this.buffer[this.filled++] = s < 0 ? s * 32768 : s * 32767;
      if (this.filled === this.chunkSamples) {
        const out = this.buffer.buffer;               // hand the memory over; start a fresh buffer
        this.port.postMessage(out, [out]);
        this.buffer = new Int16Array(this.chunkSamples);
        this.filled = 0;
      }
    }
    return true;
  }
}

registerProcessor("capture-processor", CaptureProcessor);
