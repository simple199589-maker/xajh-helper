import struct
import sys

path = sys.argv[1] if len(sys.argv) > 1 else "pxclient.pcap"
data = open(path, "rb").read()
off = 24
i = 0
while off + 16 <= len(data):
    ts_sec, ts_usec, caplen, wirelen = struct.unpack("<IIII", data[off:off + 16])
    off += 16
    raw = data[off:off + caplen]
    off += caplen
    i += 1
    if len(raw) < 34:
        print("%3d  short frame %d bytes" % (i, len(raw)))
        continue
    ethertype = raw[12:14]
    off12 = 12
    while len(raw) >= off12 + 2 and raw[off12:off12 + 2] in (b"\x81\x00", b"\x88\xa8"):
        off12 += 4
    if len(raw) < off12 + 2 or raw[off12:off12 + 2] != b"\x08\x00":
        print("%3d  ethertype=%s (vlan-stripped=%s)" % (i, ethertype.hex(), raw[off12:off12 + 2].hex() if len(raw) >= off12 + 2 else "?"))
        continue
    iph = raw[off12 + 2:]
    ihl = (iph[0] & 0x0F) * 4
    proto = iph[9]
    src = ".".join(str(x) for x in iph[12:16])
    dst = ".".join(str(x) for x in iph[16:20])
    info = ""
    if proto == 6 and len(iph) >= ihl + 20:
        tcp = iph[ihl:]
        sport, dport = struct.unpack("!HH", tcp[0:4])
        seq, ack = struct.unpack("!II", tcp[4:12])
        flags = tcp[13]
        doff = (tcp[12] >> 4) * 4
        payload = tcp[doff:]
        fl = "".join(n for b, n in [(0x02, "S"), (0x10, "A"), (0x08, "P"), (0x01, "F"), (0x04, "R")] if flags & b)
        info = "TCP %s:%d -> %s:%d seq=%u ack=%u [%s] pay=%d" % (src, sport, dst, dport, seq, ack, fl, len(payload))
        if payload:
            info += "\n      hex: " + payload[:120].hex()
            printable = "".join(chr(c) if 32 <= c < 127 else "." for c in payload[:120])
            info += "\n      txt: " + printable
    elif proto == 17:
        info = "UDP %s -> %s" % (src, dst)
    else:
        info = "IP proto=%d %s -> %s" % (proto, src, dst)
    print("%3d  %s" % (i, info))
