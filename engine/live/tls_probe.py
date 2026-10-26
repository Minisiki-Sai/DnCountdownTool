"""Bounded TLS recognition for decoding only; NEVER a capture/archive filter."""
from collections import OrderedDict


class TlsStreamProbe:
    # Two complete, consecutive records are required. A port number, size,
    # arbitrary ciphertext substring or a single TLS-looking header is NOT proof.
    def __init__(self):
        self._probes = OrderedDict()
        self._confirmed = OrderedDict()
        self.ignored_packets = 0
        self.confirmed_connections = 0

    @staticmethod
    def _header(data, offset=0):
        if len(data) - offset < 5:
            return None
        if data[offset] not in (20, 21, 22, 23) or data[offset+1] != 3 or data[offset+2] > 4:
            return False
        size = int.from_bytes(data[offset+3:offset+5], 'big')
        return size if 0 < size <= 18432 else False

    def is_tls(self, packet):
        endpoints = ((packet.source_ip, packet.source_port),
                     (packet.destination_ip, packet.destination_port))
        connection = tuple(sorted(endpoints))
        flags = int(packet.tcp_flags)
        if flags & 2 and not flags & 16:  # New connection, not SYN/ACK.
            self._confirmed.pop(connection, None)
            self._probes.pop(endpoints, None)
            self._probes.pop(endpoints[::-1], None)
        if connection in self._confirmed:
            self._confirmed.move_to_end(connection)
            self.ignored_packets += 1
            return True
        payload = packet.payload
        if not payload:
            return False
        sequence = packet.tcp_sequence
        probe = self._probes.get(endpoints)
        if probe is None:
            if self._header(payload) is False:
                return False
            # A segment beginning halfway through a record is deliberately
            # not scanned for a coincidental TLS header.
            probe = [bytearray(), sequence, float(packet.timestamp_seconds)]
            self._probes[endpoints] = probe
            while len(self._probes) > 256:
                self._probes.popitem(last=False)
        buffer, expected, first_time = probe
        if packet.timestamp_seconds - first_time > 10:
            self._probes.pop(endpoints, None)
            return False
        if sequence is not None and expected is not None:
            if sequence > expected:  # Incomplete/out-of-order proof: fail open.
                self._probes.pop(endpoints, None)
                return False
            overlap = expected - sequence
            if overlap >= len(payload):
                return False
            payload = payload[overlap:]
        buffer.extend(payload[:37000-len(buffer)])
        probe[1] = ((int(sequence) + len(packet.payload)) & 0xFFFFFFFF
                    if sequence is not None else None)
        offset = 0
        for _ in range(2):
            size = self._header(buffer, offset)
            if size is False:
                self._probes.pop(endpoints, None)
                return False
            if size is None or len(buffer) < offset + 5 + size:
                return False
            if buffer[offset] == 20 and (size != 1 or buffer[offset+5] != 1):
                self._probes.pop(endpoints, None)
                return False
            offset += 5 + size
        self._confirmed[connection] = True
        while len(self._confirmed) > 1024:
            self._confirmed.popitem(last=False)
        self._probes.pop(endpoints, None)
        self._probes.pop(endpoints[::-1], None)
        self.confirmed_connections += 1
        self.ignored_packets += 1
        return True
