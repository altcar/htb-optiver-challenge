"""
Lightweight OptiBook websocket scraper

Connects to the OptiBook websocket endpoint and captures every message passing through.
- Attempts to decompress (lz4) and unpack (msgpack) binary frames
- Falls back to utf-8 / json decoding for text frames
- Saves parsed messages to a JSONL file and raw binary frames to a folder
- Supports automatic reconnect with backoff

Usage (PowerShell):
    python .\new_scaper.py --host htb.optibook.net --username my-team --out data --modules trade_ticker,news_feed

Dependencies:
    pip install websocket-client msgpack lz4

"""

import time
import argparse
import json
import os
from pathlib import Path
import threading
import datetime as dt

import msgpack
import lz4.frame
import websocket


class OptiBookScraper:
    def __init__(self, host: str, username: str = None, out_dir: str = "data", modules: list = None):
        self.host = host
        self.username = username
        self.modules = modules or ["trade_ticker", "news_feed"]
        self.out_dir = Path(out_dir)
        self.out_dir.mkdir(parents=True, exist_ok=True)
        (self.out_dir / "raw").mkdir(exist_ok=True)

        self.session_file = self.out_dir / "messages.jsonl"
        self.lock = threading.Lock()

        self.ws = None
        self._stop = threading.Event()
        self._thread = None

    def _build_url(self):
        qs = f"?username={self.username}" if self.username else ""
        return f"wss://{self.host}/ws{qs}"

    @staticmethod
    def _try_decompress_and_unpack(data: bytes):
        # try lz4 then msgpack
        try:
            try:
                decompressed = lz4.frame.decompress(data)
            except Exception:
                # not lz4-compressed, proceed with raw
                decompressed = data
            obj = msgpack.unpackb(decompressed, raw=False)
            return obj
        except Exception:
            # not msgpack or failed
            return None

    def _on_open(self, ws):
        print(f"[+] Connected to {self.host}")
        # send select/subscriptions if requested
        try:
            select_msg = {"type": "SELECT", "team": self.username, "data": {"modules": self.modules}}
            print(f"[DEBUG] Sending SELECT: {select_msg}")
            self.ws.send(msgpack.packb(select_msg, use_bin_type=True), opcode=websocket.ABNF.OPCODE_BINARY)
        except Exception as e:
            print(f"[!] Error sending SELECT: {e}")

    def _on_close(self, ws, code, reason):
        print(f"[-] Connection closed: {code} {reason}")

    def _on_error(self, ws, err):
        print(f"[!] WebSocket error: {err}")

    def _on_message(self, ws, message):
        # message may be bytes or str
        parsed = None
        raw_saved_path = None
        if isinstance(message, (bytes, bytearray)):
            # try lz4+msgpack
            parsed = self._try_decompress_and_unpack(bytes(message))
            # save raw bytes for later
            ts = dt.datetime.utcnow().strftime('%Y%m%d_%H%M%S_%f')
            raw_saved_path = self.out_dir / "raw" / f"msg_{ts}.bin"
            try:
                with open(raw_saved_path, 'wb') as f:
                    f.write(message)
            except Exception as e:
                print(f"[!] Failed to save raw message: {e}")
        else:
            # text frame
            try:
                parsed = json.loads(message)
            except Exception:
                parsed = message

        # If parsed is still None, try text decode
        if parsed is None and isinstance(message, (bytes, bytearray)):
            try:
                text = message.decode('utf-8')
                try:
                    parsed = json.loads(text)
                except Exception:
                    parsed = text
            except Exception:
                parsed = None

        # Write a JSONL record containing: received_at, parsed (if any), raw_path (if saved)
        rec = {
            "received_at": time.time(),
            "received_at_iso": dt.datetime.utcnow().isoformat(),
            "parsed": parsed,
            "raw_path": str(raw_saved_path) if raw_saved_path else None
        }
        with self.lock:
            try:
                with open(self.session_file, 'a', encoding='utf-8') as f:
                    f.write(json.dumps(rec, default=str) + '\n')
            except Exception as e:
                print(f"[!] Failed to append JSONL: {e}")

        # Also print a short summary to console
        if isinstance(parsed, dict):
            module = parsed.get('module') or parsed.get('type')
            print(f"[MSG] module={module} ts_keys={list(parsed.keys())[:6]}")
        else:
            print(f"[MSG] {type(parsed)}")

    def start(self, blocking: bool = True):
        url = self._build_url()
        backoff = 1.0

        def run():
            nonlocal backoff
            while not self._stop.is_set():
                try:
                    self.ws = websocket.WebSocketApp(
                        url,
                        on_open=self._on_open,
                        on_message=self._on_message,
                        on_error=self._on_error,
                        on_close=self._on_close,
                    )
                    self.ws.run_forever(ping_interval=20, ping_timeout=10)
                except Exception as e:
                    print(f"[!] Connection loop error: {e}")
                if self._stop.is_set():
                    break
                print(f"[i] Reconnecting in {backoff}s...")
                time.sleep(backoff)
                backoff = min(backoff * 1.5, 30.0)

        self._thread = threading.Thread(target=run, daemon=True)
        self._thread.start()
        if blocking:
            try:
                while True:
                    time.sleep(1)
            except KeyboardInterrupt:
                print('\nStopping...')
                self.stop()

    def stop(self):
        self._stop.set()
        try:
            if self.ws:
                self.ws.close()
        except Exception:
            pass
        if self._thread:
            self._thread.join(timeout=2.0)


def main():
    parser = argparse.ArgumentParser()
    parser.add_argument('--host', default='htb.optibook.net')
    parser.add_argument('--username', default=None)
    parser.add_argument('--out', default='data')
    parser.add_argument('--modules', default='trade_ticker,news_feed')
    args = parser.parse_args()

    modules = [m.strip() for m in args.modules.split(',') if m.strip()]

    scraper = OptiBookScraper(host=args.host, username=args.username, out_dir=args.out, modules=modules)
    scraper.start(blocking=True)


if __name__ == '__main__':
    main()
