"""Decode RemObjects (RO107) RPC messages from a pcap captured by capture_pxclient.py.

Framing observed on the wire (port 7798):
  [type:1][msg_id:4 LE][body_len:4 LE][body]
  type 0x01 = request, 0x02 = response; client replies with bare [0x02][msg_id] as ack
  type 0x04/0x05 = superchannel ping/pong (5 bytes)
Body: "RO107" + flag(1); flag bit 0x01 => zlib stream follows after fixed header.
"""
import struct
import sys
import zlib

PCAP = sys.argv[1] if len(sys.argv) > 1 else "run2/pxclient.pcap"
ONLY_RPC_PORT = 7798


def parse_pcap(path):
    data = open(path, "rb").read()
    off = 24
    pkts = []
    while off + 16 <= len(data):
        ts_sec, ts_usec, caplen, _wire = struct.unpack("<IIII", data[off:off + 16])
        off += 16
        pkts.append((ts_sec + ts_usec / 1e6, data[off:off + caplen]))
        off += caplen
    return pkts


def reassemble(pkts):
    """return {(src,sport,dst,dport): {reverse_key: [(seq,payload,ts)]}}"""
    flows = {}
    for ts, raw in pkts:
        o = 12
        while len(raw) >= o + 2 and raw[o:o + 2] in (b"\x81\x00", b"\x88\xa8"):
            o += 4
        if len(raw) < o + 2 or raw[o:o + 2] != b"\x08\x00":
            continue
        ip = o + 2
        if len(raw) < ip + 20 or (raw[ip] >> 4) != 4:
            continue
        ihl = (raw[ip] & 0x0F) * 4
        if raw[ip + 9] != 6:
            continue
        src = ".".join(str(b) for b in raw[ip + 12:ip + 16])
        dst = ".".join(str(b) for b in raw[ip + 16:ip + 20])
        t = raw[ip + ihl:]
        if len(t) < 20:
            continue
        sport, dport = struct.unpack("!HH", t[0:4])
        seq = struct.unpack("!I", t[4:8])[0]
        doff = (t[12] >> 4) * 4
        payload = t[doff:]
        if not payload:
            continue
        flows.setdefault((src, sport, dst, dport), []).append((seq, payload, ts))
    # merge both directions per connection, reassemble each direction
    conns = {}
    for key, segs in flows.items():
        rev = (key[2], key[3], key[0], key[1])
        if rev in conns or key in conns:
            base = conns.get(key) or conns.get(rev)
        else:
            base = {}
            conns[key] = base
            conns[rev] = base
        base[key] = segs
    out = []
    seen = set()
    for key, dirs in conns.items():
        if key in seen:
            continue
        seen.add(key)
        seen.add((key[2], key[3], key[0], key[1]))
        streams = {}
        for d, segs in dirs.items():
            segs.sort(key=lambda s: s[0])
            buf = bytearray()
            cur = segs[0][0]
            for seq, payload, _ts in segs:
                end = seq + len(payload)
                if end <= cur:
                    continue
                if seq < cur:
                    payload = payload[cur - seq:]
                buf.extend(payload)
                cur = seq + len(payload)
            streams[d] = (bytes(buf), min(s[2] for s in segs))
        out.append((key, streams))
    return out


def frame_messages(stream):
    """yield (type, msg_id, body) from one TCP direction.

    Data messages: [type][id:4][len:4][body starting with b"RO107"]
    Bare frames (5 bytes): [type][id:4] with no length field (acks, ping/pong).
    """
    i = 0
    n = len(stream)
    while i + 14 <= n:
        mtype = stream[i]
        msg_id = struct.unpack("<I", stream[i + 1:i + 5])[0]
        if stream[i + 9:i + 14] == b"RO107":
            blen = struct.unpack("<I", stream[i + 5:i + 9])[0]
            if blen > 20 * 1024 * 1024 or i + 9 + blen > n:
                i += 1  # bogus length -> resync
                continue
            yield mtype, msg_id, stream[i + 9:i + 9 + blen]
            i += 9 + blen
        elif i + 13 > n:
            break
        else:
            # bare frame (ack/ping) or misaligned start: creep 1 byte to resync
            i += 1


def zlib_scan(b):
    """decompress any zlib streams found, return (processed_bytes, had_zlib)"""
    out = bytearray()
    i = 0
    found = False
    while i < len(b):
        if b[i] == 0x78 and i + 2 < len(b):
            for wbits in (15, -15):
                try:
                    d = zlib.decompressobj(wbits)
                    chunk = d.decompress(b[i:])
                    out.extend(chunk)
                    found = True
                    i = len(b)  # rest consumed (ignore trailing garbage)
                    break
                except zlib.error:
                    continue
            else:
                out.append(b[i])
                i += 1
        else:
            out.append(b[i])
            i += 1
    return bytes(out), found


def strings_of(b, limit=24):
    """extract length-prefixed / texty strings for display"""
    res = []
    i = 0
    n = len(b)
    while i < n - 4 and len(res) < limit:
        L = struct.unpack("<I", b[i:i + 4])[0]
        if 1 <= L <= 240 and i + 4 + L <= n:
            s = b[i + 4:i + 4 + L]
            try:
                t = s.decode("gbk")
                if sum(1 for c in t if c.isprintable()) >= max(2, int(len(t) * 0.8)):
                    res.append(t)
                    i += 4 + L
                    continue
            except UnicodeDecodeError:
                pass
        i += 1
    return res


def to_text(b):
    for enc in ("gbk", "utf-8"):
        try:
            return b.decode(enc)
        except UnicodeDecodeError:
            pass
    return b.decode("latin-1")


import time

for key, streams in reassemble(parse_pcap(PCAP)):
    if ONLY_RPC_PORT not in (key[1], key[3]):
        continue
    print("=" * 100)
    print("connection: %s:%d <-> %s:%d" % key)
    for d, (stream, _t0) in sorted(streams.items()):
        c2s = d[0] == key[0]
        for mtype, msg_id, body in frame_messages(stream):
            ts = "C" if c2s else "S"
            if mtype in (0x04, 0x05) and len(body) == 0:
                print("  %s->  ping/pong (type 0x%02x id 0x%x)" % (ts, mtype, msg_id))
                continue
            if not body:
                continue  # bare ack (type 0x02) / ping-pong frames
            label = "msg1" if mtype == 1 else "type%02x" % mtype
            # try zlib content
            core = body
            comp = False
            # find zlib magic after RO header area (skip magic+len prefixes: up to ~40 bytes)
            for probe in range(9, min(len(core), 60)):
                if core[probe] == 0x78 and core[probe + 1] in (0x01, 0x9c, 0xda, 0x5e):
                    dec, ok = zlib_scan(core[probe:])
                    if ok:
                        core = core[:probe] + dec
                        comp = True
                        break
            strs = strings_of(core)
            print("  %s-> %-4s id=0x%02x len=%6d %s | %s" % (
                ts, label, msg_id, len(body), "zlib!" if comp else "     ",
                "  ".join(strs[:10])))
            if strs and len(strs) > 10:
                print("        more: %s" % "  ".join(strs[10:24]))
