"""Minimal reader for WoW client DBC files (WDBC, as shipped with 3.3.5a build 12340).

Layout of a WDBC file:

    header (20 bytes):  magic b"WDBC", record_count, field_count, record_size,
                        string_block_size   (all little-endian uint32)
    records:            record_count * record_size bytes; every field is 4 bytes
    string block:       string_block_size bytes of NUL-terminated UTF-8 strings;
                        string fields hold a byte offset into it (0 = empty)

Fields are untyped on disk, so callers say how to read them (`uint`, `int`,
`float`, `string`). The reader keeps the file bytes and decodes on access, so
pulling three columns out of the 49 MB Spell.dbc never materialises the other
231. Drop the `WdbcFile` once the tables you need are built.

Localised strings are 16 consecutive string fields (one per client locale)
followed by a flags field. A client only fills the slot of its own locale,
so `detect_locale` picks the most populated slot and `loc_string` falls back
to slot 0 (enUS) when the chosen slot is empty for a record.
"""
import struct

HEADER = struct.Struct("<4sIIII")
MAGIC = b"WDBC"
LOCALE_SLOTS = 16


class WdbcError(ValueError):
    """The file is not a well-formed WDBC file."""


class WdbcFile:
    def __init__(self, data, name="<bytes>"):
        if len(data) < HEADER.size:
            raise WdbcError(f"{name}: too short for a WDBC header")
        magic, n_rec, n_field, rec_size, str_size = HEADER.unpack_from(data)
        if magic != MAGIC:
            raise WdbcError(f"{name}: not a WDBC file (magic={magic!r})")
        if rec_size != n_field * 4:
            raise WdbcError(f"{name}: record_size {rec_size} != 4 * field_count {n_field}")
        if HEADER.size + n_rec * rec_size + str_size > len(data):
            raise WdbcError(f"{name}: truncated ({len(data)} bytes)")
        self.name = name
        self.record_count = n_rec
        self.field_count = n_field
        self.record_size = rec_size
        self._data = data
        self._strings_at = HEADER.size + n_rec * rec_size
        self._strings_end = self._strings_at + str_size

    @classmethod
    def open(cls, path):
        with open(path, "rb") as f:
            return cls(f.read(), name=str(path))

    # ---- typed field access ------------------------------------------------
    def _offset(self, record, field):
        if not 0 <= record < self.record_count:
            raise IndexError(f"{self.name}: record {record} out of range")
        if not 0 <= field < self.field_count:
            raise IndexError(f"{self.name}: field {field} out of range")
        return HEADER.size + record * self.record_size + field * 4

    def uint(self, record, field):
        return struct.unpack_from("<I", self._data, self._offset(record, field))[0]

    def int(self, record, field):
        return struct.unpack_from("<i", self._data, self._offset(record, field))[0]

    def float(self, record, field):
        return struct.unpack_from("<f", self._data, self._offset(record, field))[0]

    def string(self, record, field):
        """The string a string field points at; "" for offset 0 or a bad offset."""
        off = self.uint(record, field)
        if not off:
            return ""
        start = self._strings_at + off
        if start >= self._strings_end:
            return ""
        end = self._data.find(b"\x00", start, self._strings_end)
        if end < 0:
            end = self._strings_end
        return self._data[start:end].decode("utf-8", "replace")

    def records(self):
        return range(self.record_count)

    # ---- localised strings -------------------------------------------------
    def detect_locale(self, first_field):
        """Index (0..15) of the most populated locale slot of a localised column."""
        counts = [0] * LOCALE_SLOTS
        for r in self.records():
            for slot in range(LOCALE_SLOTS):
                if self.uint(r, first_field + slot):
                    counts[slot] += 1
        best = max(range(LOCALE_SLOTS), key=lambda s: (counts[s], -s))
        return best if counts[best] else 0

    def loc_string(self, record, first_field, slot=0):
        """String in locale `slot`, falling back to slot 0 (enUS) when empty."""
        s = self.string(record, first_field + slot)
        if not s and slot:
            s = self.string(record, first_field)
        return s
