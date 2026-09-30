// A transfer stays at the head of the ordered input queue until its final
// chunk is sent. ACKs, pongs and visibility controls bypass that queue.
export const INPUT_TRANSFER_CHUNK_BYTES = 16 * 1024;

export class InputTransfer {
  constructor(operation, chunkBytes) {
    this.operation = operation;
    this.chunkBytes = Math.min(INPUT_TRANSFER_CHUNK_BYTES, chunkBytes);
    this.bytes = operation.kind === 'pane-paste'
      ? new TextEncoder().encode(operation.text)
      : new Uint8Array(operation.bytes);
    this.offset = 0;
    this.started = false;
  }

  sendNext(socket, highWaterBytes) {
    if (!this.started) {
      const header = JSON.stringify({
        type: 'input-transfer',
        kind: this.operation.kind,
        stream_id: this.operation.streamId,
        extension: this.operation.extension,
        size: this.bytes.byteLength,
      });
      if (socket.bufferedAmount + new TextEncoder().encode(header).length > highWaterBytes) {
        return false;
      }
      socket.send(header);
      this.started = true;
    }
    const end = Math.min(this.offset + this.chunkBytes, this.bytes.byteLength);
    if (socket.bufferedAmount + end - this.offset > highWaterBytes) return false;
    socket.send(this.bytes.subarray(this.offset, end));
    this.offset = end;
    return this.offset === this.bytes.byteLength;
  }

  cancel(socket) {
    if (this.started && this.offset < this.bytes.byteLength) {
      socket.send(JSON.stringify({ type: 'input-transfer-cancel' }));
    }
  }
}
