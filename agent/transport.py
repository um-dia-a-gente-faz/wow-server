"""Socket transport: RC4 framing, send/receive, packet dumps. No game state."""

import logging
import os
import socket
import struct
import threading
import time

from . import packets as pk
from .opcodes import NUM_MSG_TYPES

log = logging.getLogger("agent.session")

# Once a packet's first byte has arrived, the rest must follow within this long.
# A timeout mid-packet would desync framing and RC4 state, so it ends the session.
MID_PACKET_TIMEOUT_S = 30.0
# Handler errors are logged at most once per opcode per this many seconds.
ERROR_LOG_INTERVAL_S = 30.0
# Unwanted packet bodies are read and dropped in chunks of this size (#570).
DISCARD_CHUNK_B = 0x10000
# #793: desync bounds, checked on the in-world path (the one that passes keep).
# MAX_SANE_PACKET: TrinityCore has no cap on a send (WorldSocket.cpp WorldSocket::Update sends a
# packet larger than _sendBufferSize whole), and ServerPktHeader.h (3.3.5 @ 92127cd) allows up to
# 0x7FFFFF. UpdateData.cpp BuildPacket compresses every update above 100 B into
# SMSG_COMPRESSED_UPDATE_OBJECT, so the large packets (achievements, spells, guild roster) are
# tens to a few hundred KB. 1 MiB is above that, and far under the 8 MiB a desynced RC4 header hits.
MAX_SANE_PACKET = 0x100000
# Opcodes.h (3.3.5 @ 92127cd) enum Opcodes ends at NUM_MSG_TYPES = 0x51F, and ServerPktHeader.h writes
# the opcode unchanged, so a healthy server never sends an opcode >= NUM_MSG_TYPES. A desynced header
# is uniform 16-bit: out of range 98 % of the time. An in-range opcode, known or not, resets the run.
DESYNC_OUT_OF_RANGE_LIMIT = 16


class StreamDesync(ConnectionError):
    """The world stream no longer parses as packets (#793). The session ends with `reason`."""

    def __init__(self, reason: str):
        super().__init__(reason)
        self.reason = reason


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
        self._out_of_range_run = 0  # consecutive opcodes >= NUM_MSG_TYPES on the in-world path (#793)
        self.desync_reason = ""   # set when the in-world stream is declared desynced (#793)

    def send_packet(self, opcode: int, payload: bytes = b'') -> None:
        """The public name (agent.ports.PacketSink), which actions and reflexes use."""
        self._send_packet(opcode, payload)

    def _send_packet(self, opcode: int, payload: bytes = b''):
        hdr = struct.pack('>H', len(payload) + 4) + struct.pack('<I', opcode)
        with self._lock:
            # The RC4 header stream must advance in the order bytes hit the wire.
            if self.crypt:
                hdr = self.crypt.encrypt_send(hdr)
            self.sock.sendall(hdr + payload)

    def _recv_packet(self, keep=None) -> tuple[int, bytes]:
        """keep(opcode) False: the body is read through and dropped, not returned (b'').
        A desynced stream declares multi-MB sizes; buffering those for packets nobody
        handles is what grew the soak's RSS (#570)."""
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
            if keep is not None:
                self._check_stream(size, opcode)  # raises before any body read
            if keep is not None and not keep(opcode):
                self._discard(plen)
                payload = b''
            else:
                payload = self._rr(plen) if plen > 0 else b''
        except (socket.timeout, TimeoutError) as e:
            raise ConnectionError("timed out mid-packet, stream out of sync") from e
        finally:
            self.sock.settimeout(prev_timeout)
        return opcode, payload

    def _check_stream(self, size: int, opcode: int):
        """#793: a declared size over MAX_SANE_PACKET, or DESYNC_OUT_OF_RANGE_LIMIT opcodes >=
        NUM_MSG_TYPES in a row, means the RC4 header stream is out of step. Raises StreamDesync."""
        if size > MAX_SANE_PACKET:
            raise StreamDesync(f"declared size {size} B above {MAX_SANE_PACKET} B, opcode {opcode:#06x}")
        if opcode < NUM_MSG_TYPES:
            self._out_of_range_run = 0
            return
        self._out_of_range_run += 1
        if self._out_of_range_run >= DESYNC_OUT_OF_RANGE_LIMIT:
            raise StreamDesync(f"{self._out_of_range_run} opcodes >= {NUM_MSG_TYPES:#x} in a row, "
                               f"last {opcode:#06x}")

    def _discard(self, n: int):
        while n > 0:
            n -= len(self._rr(min(n, DISCARD_CHUNK_B)))

    def _rr(self, n: int) -> bytes:
        buf = bytearray()  # += on bytes copies the whole buffer per chunk: quadratic (#793)
        while len(buf) < n:
            c = self.sock.recv(n - len(buf))
            if not c:
                raise ConnectionError("disconnected")
            buf += c
        return bytes(buf)

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
