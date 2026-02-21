"""OptiBook WebSocket client

Connects to the same protocol implemented by the JS `WebSocketService` in
`optibook.js`: binary WebSocket frames containing LZ4-framed-compressed
MessagePack payloads.

Features:
- Connect / reconnect with exponential backoff
- Send MessagePack messages compressed with LZ4 frame wrapper (compatible with the JS implementation)
- Receive binary frames, decompress via LZ4, decode MessagePack and call a handler

Usage:
    python optibook_client.py --host example.com --username team1

Dependencies (see requirements.txt): websocket-client, msgpack, lz4
"""
import time
import threading
import urllib.parse
from typing import Optional, Callable, Any

import msgpack
import lz4.frame
import websocket


class OptiBookClient:
    """
    A simple client compatible with the JS `WebSocketService` in `optibook.js`.

    - host: host (will connect to wss://{host}/ws)
    - username: optional team/username to send as query param
    - on_message: callable(decoded_obj) called for each incoming message
    - on_open/on_close/on_error: optional callbacks

    Note: This client uses websocket-client (threaded) and is synchronous in
    the sense it runs a background thread for the socket.
    """

    def __init__(self, host: str, username: Optional[str] = None):
        self.host = host
        self.username = username
        self.ws: Optional[websocket.WebSocketApp] = None
        self._thread: Optional[threading.Thread] = None
        self._stop_event = threading.Event()
        self.on_message: Optional[Callable[[Any], None]] = None
        self.on_open: Optional[Callable[[], None]] = None
        self.on_close: Optional[Callable[[], None]] = None
        self.on_error: Optional[Callable[[Exception], None]] = None
        self.initial_backoff = 1.0
        self.max_backoff = 10.0
        self._current_backoff = self.initial_backoff

    def _build_url(self) -> str:
        params = {}
        if self.username:
            params["username"] = self.username
        qs = urllib.parse.urlencode(params)
        return f"wss://{self.host}/ws?{qs}" if qs else f"wss://{self.host}/ws"

    # Encoding/decoding helpers
    @staticmethod
    def _pack_and_compress(obj: Any) -> bytes:
        # JS used MessagePack (msgpack) and then compress() producing an LZ4 frame.
        packed = msgpack.packb(obj, use_bin_type=True)
        compressed = lz4.frame.compress(packed)
        return compressed

    @staticmethod
    def _decompress_and_unpack(data: bytes) -> Any:
        # Decompress LZ4 frame then unpack MessagePack
        decompressed = lz4.frame.decompress(data)
        return msgpack.unpackb(decompressed, raw=False)

    # High-level send
    def send_message(self, obj: Any) -> None:
        """Pack+compress and send an object as binary frame."""
        if not self.ws or not self.ws.sock or not self.ws.sock.connected:
            raise RuntimeError("WebSocket is not connected")
        b = self._pack_and_compress(obj)
        # websocket-client's send for binary data
        self.ws.send(b, opcode=websocket.ABNF.OPCODE_BINARY)

    def send_select(self, instrument: Optional[str] = None, team: Optional[str] = None) -> None:
        # JS sends { type: zo.SELECT, team: this.username, data: $p({ instrument, team }) }
        # We don't know zo.SELECT numeric code here; server likely accepts the same message structure
        # where "type" may be a string or number. To be safe, send type as "SELECT".
        msg = {
            "type": "SELECT",
            "team": self.username,
            "data": {"instrument": instrument, "team": team},
        }
        self.send_message(msg)

    # WebSocket callbacks
    def _on_open(self, ws):
        self._current_backoff = self.initial_backoff
        if self.on_open:
            self.on_open()

    def _on_close(self, ws, close_status_code, close_msg):
        if self.on_close:
            self.on_close()

    def _on_error(self, ws, err):
        if self.on_error:
            try:
                self.on_error(err)
            except Exception:
                pass

    def _on_message(self, ws, message):
        try:
            # message is bytes/bytearray when binary
            if isinstance(message, (bytes, bytearray)):
                try:
                    obj = self._decompress_and_unpack(bytes(message))
                except Exception as e:
                    # If it fails, try to interpret as raw msgpack without compression
                    try:
                        obj = msgpack.unpackb(message, raw=False)
                    except Exception:
                        # not a msgpack message
                        raise
                if self.on_message:
                    self.on_message(obj)
            else:
                # text frame - attempt JSON/MessagePack in text? we'll pass raw
                if self.on_message:
                    self.on_message(message)
        except Exception as e:
            # surface decode errors to on_error
            if self.on_error:
                self.on_error(e)

    def connect(self, run_forever_kwargs: Optional[dict] = None) -> None:
        """Start connection in background thread and auto-reconnect on close.

        run_forever_kwargs are forwarded to websocket.WebSocketApp.run_forever
        """
        if self._thread and self._thread.is_alive():
            return
        self._stop_event.clear()

        def run():
            while not self._stop_event.is_set():
                url = self._build_url()
                self.ws = websocket.WebSocketApp(
                    url,
                    on_open=self._on_open,
                    on_message=self._on_message,
                    on_close=self._on_close,
                    on_error=self._on_error,
                )
                try:
                    # run_forever will block until closed; set ping_interval to None by default
                    kwargs = run_forever_kwargs or {"ping_interval": None}
                    self.ws.run_forever(**kwargs)
                except Exception as e:
                    if self.on_error:
                        self.on_error(e)
                # if we reach here, connection closed -> wait and reconnect unless stopped
                if self._stop_event.is_set():
                    break
                time.sleep(self._current_backoff)
                self._current_backoff = min(self._current_backoff * 1.1, self.max_backoff)

        self._thread = threading.Thread(target=run, daemon=True)
        self._thread.start()

    def close(self):
        self._stop_event.set()
        try:
            if self.ws:
                self.ws.close()
        finally:
            if self._thread:
                self._thread.join(timeout=2.0)


if __name__ == "__main__":
    host = "htb.optibook.net"
    username = 'team-005'


    client = OptiBookClient(host, username)

    def _on_msg(obj):
        print("RECV:", obj)

    def _on_open():
        print("Connected")
        # example: send a SELECT as the JS client would
        try:
            client.send_select(None, None)
        except Exception as e:
            print("Send failed:", e)

    def _on_close():
        print("Closed")

    def _on_err(e):
        print("ERROR:", e)

    client.on_message = _on_msg
    client.on_open = _on_open
    client.on_close = _on_close
    client.on_error = _on_err

    print("Connecting to", client._build_url())
    client.connect({"ping_interval": 20, "ping_timeout": 10})

    try:
        while True:
            time.sleep(1)
    except KeyboardInterrupt:
        print("Stopping...")
        client.close()
