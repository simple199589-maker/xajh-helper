"""Convert pcapng to classic pcap (Ethernet linktype), preserving timestamps."""
import struct
import sys

src = sys.argv[1]
dst = sys.argv[2] if len(sys.argv) > 2 else src.rsplit(".", 1)[0] + ".pcap"

data = open(src, "rb").read()
off = 0
linktype = 1
tsresol = 6  # default: 10^-6 seconds
pkts = []

while off + 12 <= len(data):
    btype, blen = struct.unpack("<II", data[off:off + 8])
    if blen < 12 or off + blen > len(data):
        break
    body = data[off + 8:off + blen - 4]
    if btype == 0x0A0D0D0A:  # SHB
        if body[0:4] != b"\x4d\x3c\x2b\x1a":  # 0x1A2B3C4D stored little-endian
            sys.exit("big-endian pcapng not supported")
    elif btype == 0x00000001:  # IDB
        linktype = struct.unpack("<H", body[0:2])[0]
        # options: after 8-byte fixed part
        o = 8
        while o + 4 <= len(body):
            code, olen = struct.unpack("<HH", body[o:o + 4])
            if code == 0:
                break
            if code == 9 and olen >= 1:  # if_tsresol
                v = body[o + 4]
                if v & 0x80:
                    tsresol = 2 ** (v & 0x7F)
                else:
                    tsresol = 10 ** v
            o += 4 + olen + (-olen % 4)
    elif btype == 0x00000006:  # EPB
        iface, th, tl, caplen, origlen = struct.unpack("<IIIII", body[0:20])
        pkt = body[20:20 + caplen]
        ts = ((th << 32) | tl) / tsresol
        pkts.append((ts, pkt))
    off += blen

with open(dst, "wb") as f:
    f.write(struct.pack("<IHHiIII", 0xA1B2C3D4, 2, 4, 0, 0, 65535, linktype))
    for ts, pkt in pkts:
        sec = int(ts)
        usec = int(round((ts - sec) * 1e6))
        if usec >= 1000000:
            sec += 1
            usec -= 1000000
        f.write(struct.pack("<IIII", sec, usec, len(pkt), len(pkt)))
        f.write(pkt)

print("%d packets, linktype=%d -> %s" % (len(pkts), linktype, dst))
