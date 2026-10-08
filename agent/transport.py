"""Socket transport: RC4 framing, send/receive, packet dumps. No game state."""

import logging
import os
import socket
import struct
import threading
import time

from . import packets as pk

log = logging.getLogger("agent.session")

# Once a packet's first byte has arrived, the rest must follow within this long.
# A timeout mid-packet would desync framing and RC4 state, so it ends the session.
MID_PACKET_TIMEOUT_S = 30.0
# Handler errors are logged at most once per opcode per this many seconds.
ERROR_LOG_INTERVAL_S = 30.0


class _ErrorThrottle:
    """Rate-limits log lines per key, counting the ones it suppresses."""

    def __init__(self, interval: float, clock=time.monotonic):
        self.interval = interval
        self._clock = clock
        self._last = {}        # key -> time of last emitted line
        self._suppressed = {}  # key -> errors swallowed since then

    def check(self, key) -> tuple[bool, int]:
        """Returns (should_log, suppressed_since_last_log)."""
        now = self._clock()
        last = self._last.get(key)
        if last is not None and now - last < self.interval:
            self._suppressed[key] = self._suppressed.get(key, 0) + 1
            return False, 0
        self._last[key] = now
        return True, self._suppressed.pop(key, 0)


class Transport:
    """One world-server socket: framing, RC4 and the send lock."""

    def __init__(self, dump_packets_dir: str = ""):
        self.sock = None
        self.crypt = None
        self.dump_packets_dir = dump_packets_dir
        self._lock = threading.Lock()
        self._error_throttle = _ErrorThrottle(ERROR_LOG_INTERVAL_S)

    def send_packet(self, opcode: int, payload: bytes = b'') -> None:
        """The public name (agent.ports.PacketSink), which actions and reflexes use."""
        self._send_packet(opcode, payload)

    def _send_packet(self, opcode: int, payload: bytes = b''):
        hdr = struct.pack('>H', len(payload) + 4) + struct.pack('<I', opcode)
        if self.crypt:
            hdr = self.crypt.encrypt_send(hdr)
        with self._lock:
            self.sock.sendall(hdr + payload)

    def _recv_packet(self) -> tuple[int, bytes]:
        # Only this first read may time out: nothing has been consumed yet.
        first = self._rr(1)
        prev_timeout = self.sock.gettimeout()
        if prev_timeout is not None and prev_timeout < MID_PACKET_TIMEOUT_S:
            self.sock.settimeout(MID_PACKET_TIMEOUT_S)
        try:
            hdr = first + self._rr(3)
            if self.crypt:
                hdr = self.crypt.decrypt_recv(hdr)
            if hdr[0] & 0x80:  # large packet: one more size byte before the opcode
                extra = self._rr(1)
                if self.crypt:
                    extra = self.crypt.decrypt_recv(extra)
                hdr += extra
            size, opcode = pk.parse_server_header(hdr)
            plen = size - 2
            payload = self._rr(plen) if plen > 0 else b''
        except (socket.timeout, TimeoutError) as e:
            raise ConnectionError("timed out mid-packet, stream out of sync") from e
        finally:
            self.sock.settimeout(prev_timeout)
        return opcode, payload

    def _rr(self, n: int) -> bytes:
        buf = b''
        while len(buf) < n:
            c = self.sock.recv(n - len(buf))
            if not c:
                raise ConnectionError("disconnected")
            buf += c
        return buf

    def _dump_packet(self, opcode: int, data: bytes):
        if not self.dump_packets_dir:
            return
        # A dump failure (disk full, bad path) must not cost us the packet.
        try:
            os.makedirs(self.dump_packets_dir, exist_ok=True)
            path = os.path.join(self.dump_packets_dir, f"{time.time_ns()}_{opcode:#06x}.bin")
            with open(path, 'wb') as f:
                f.write(data)
        except OSError as e:
            if self._error_throttle.check('dump')[0]:
                log.warning("AGENT_DUMP_PACKETS: could not write dump: %s", e)
