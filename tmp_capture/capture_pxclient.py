"""Passive packet capture via Npcap (ctypes), then TCP-stream reassembly.

Usage: python capture_pxclient.py <duration_seconds> <filter_host> [output_dir]
"""
import ctypes
import os
import struct
import sys
import time

NPCAP_DIR = r"C:\Windows\System32\Npcap"
DURATION = float(sys.argv[1]) if len(sys.argv) > 1 else 75.0
FILTER_HOST = sys.argv[2] if len(sys.argv) > 2 else "139.155.250.49"
OUT_DIR = sys.argv[3] if len(sys.argv) > 3 else os.path.dirname(os.path.abspath(__file__))

os.add_dll_directory(NPCAP_DIR)
wpcap = ctypes.CDLL(os.path.join(NPCAP_DIR, "wpcap.dll"))


class pcap_if_t(ctypes.Structure):
    _fields_ = [
        ("next", ctypes.c_void_p),
        ("name", ctypes.c_char_p),
        ("description", ctypes.c_char_p),
        ("addresses", ctypes.c_void_p),
        ("flags", ctypes.c_uint32),
    ]


class pcap_pkthdr_t(ctypes.Structure):
    _fields_ = [
        ("ts_sec", ctypes.c_uint32),
        ("ts_usec", ctypes.c_uint32),
        ("caplen", ctypes.c_uint32),
        ("len", ctypes.c_uint32),
    ]


class bpf_insn_t(ctypes.Structure):
    _fields_ = [
        ("code", ctypes.c_uint16),
        ("jt", ctypes.c_uint8),
        ("jf", ctypes.c_uint8),
        ("k", ctypes.c_uint32),
    ]


class bpf_program_t(ctypes.Structure):
    _fields_ = [("bf_len", ctypes.c_uint32), ("bf_insns", ctypes.POINTER(bpf_insn_t))]


ERRBUF = ctypes.create_string_buffer(256)
wpcap.pcap_findalldevs.argtypes = [ctypes.POINTER(ctypes.c_void_p), ctypes.c_char_p]
wpcap.pcap_open_live.argtypes = [ctypes.c_char_p, ctypes.c_int, ctypes.c_int, ctypes.c_int, ctypes.c_char_p]
wpcap.pcap_compile.argtypes = [ctypes.c_void_p, ctypes.POINTER(bpf_program_t), ctypes.c_char_p, ctypes.c_int, ctypes.c_uint32]
wpcap.pcap_setfilter.argtypes = [ctypes.c_void_p, ctypes.POINTER(bpf_program_t)]
wpcap.pcap_next_ex.argtypes = [ctypes.c_void_p, ctypes.POINTER(ctypes.POINTER(pcap_pkthdr_t)), ctypes.POINTER(ctypes.POINTER(ctypes.c_ubyte))]
wpcap.pcap_close.argtypes = [ctypes.c_void_p]

alldevs = ctypes.c_void_p()
if wpcap.pcap_findalldevs(ctypes.byref(alldevs), ERRBUF) != 0:
    sys.exit("pcap_findalldevs failed: %s" % ERRBUF.value.decode(errors="replace"))

devices = []
node = alldevs.value
while node:
    iface = pcap_if_t.from_address(node)
    devices.append((iface.name.decode(), iface.flags))
    node = iface.next

# PCAP_IF_LOOPBACK = 0x1, PCAP_IF_UP = 0x2, PCAP_IF_RUNNING = 0x4
targets = [n for n, f in devices if not (f & 0x1) and (f & 0x2) and (f & 0x4)]
if not targets:
    targets = [n for n, f in devices if not (f & 0x1)]
print("interfaces: %d total, %d candidates" % (len(devices), len(targets)), flush=True)

handles = []
bpf = bpf_program_t()
for name in targets:
    h = wpcap.pcap_open_live(name.encode(), 65535, 0, 1000, ERRBUF)  # promisc off = passive
    if not h:
        continue
    fstr = ("host %s" % FILTER_HOST).encode()
    if wpcap.pcap_compile(h, ctypes.byref(bpf), fstr, 1, 0xFFFFFFFF) != 0 or wpcap.pcap_setfilter(h, ctypes.byref(bpf)) != 0:
        wpcap.pcap_close(h)
        continue
    handles.append((name, h))
print("listening on %d interface(s), filter: host %s" % (len(handles), FILTER_HOST), flush=True)
if not handles:
    sys.exit("no interface could be opened")

pcap_path = os.path.join(OUT_DIR, "pxclient.pcap")
pcap_file = open(pcap_path, "wb")
pcap_file.write(struct.pack("<IHHiIII", 0xA1B2C3D4, 2, 4, 0, 0, 65535, 1))

packets = []  # (ts, raw_bytes)
started = time.time()
n_hits = 0
while time.time() - started < DURATION:
    got = False
    for _name, h in handles:
        hdr = ctypes.POINTER(pcap_pkthdr_t)()
        data = ctypes.POINTER(ctypes.c_ubyte)()
        while wpcap.pcap_next_ex(h, ctypes.byref(hdr), ctypes.byref(data)) == 1:
            got = True
            n_hits += 1
            h0 = hdr.contents
            raw = ctypes.string_at(data, h0.caplen)
            ts = h0.ts_sec + h0.ts_usec / 1e6
            packets.append((ts, raw))
            pcap_file.write(struct.pack("<IIII", h0.ts_sec, h0.ts_usec, h0.caplen, h0.len))
            pcap_file.write(raw)
    if not got:
        time.sleep(0.05)

pcap_file.close()
for _n, h in handles:
    wpcap.pcap_close(h)
print("captured %d packets -> %s" % (n_hits, pcap_path), flush=True)

# ---------- parse & reassemble ----------
def ip_str(b):
    return ".".join(str(x) for x in b)


streams = {}  # (src,sport,dst,dport) -> list[(seq, payload, ts)]
conns = {}  # canonical conn -> {dir: [(seq, payload, ts)]}
for ts, raw in packets:
    off = 12
    while len(raw) >= off + 2 and raw[off:off + 2] in (b"\x81\x00", b"\x88\xa8"):
        off += 4  # strip 802.1Q VLAN tag
    if len(raw) < off + 2 or raw[off:off + 2] != b"\x08\x00":
        continue
    eth_ip = off + 2
    if len(raw) < eth_ip + 20:
        continue
    iph_len = (raw[eth_ip] & 0x0F) * 4
    proto = raw[eth_ip + 9]
    if proto != 6:
        continue
    src = ip_str(raw[eth_ip + 12:eth_ip + 16])
    dst = ip_str(raw[eth_ip + 16:eth_ip + 20])
    tcp = raw[eth_ip + iph_len:]
    if len(tcp) < 20:
        continue
    sport, dport = struct.unpack("!HH", tcp[0:4])
    seq, _ack = struct.unpack("!II", tcp[4:12])
    doff = (tcp[12] >> 4) * 4
    payload = tcp[doff:]
    if not payload:
        continue
    streams.setdefault((src, sport, dst, dport), []).append((seq, payload, ts))

for key, segs in streams.items():
    segs.sort(key=lambda s: s[0])
    out = bytearray()
    cur = segs[0][0]
    for seq, payload, _ts in segs:
        end = seq + len(payload)
        if end <= cur:
            continue  # pure retransmission
        if seq < cur:
            payload = payload[cur - seq:]
            seq = cur
        if seq > cur:
            out.extend(b"\n<<< GAP %d bytes >>>\n" % (seq - cur))
        out.extend(payload)
        cur = end
    conns[key] = bytes(out)


def to_text(b):
    for enc in ("utf-8", "gbk"):
        try:
            return b.decode(enc)
        except UnicodeDecodeError:
            pass
    return b.decode("latin-1")


os.makedirs(os.path.join(OUT_DIR, "streams"), exist_ok=True)
print("\n===== streams =====")
for i, ((src, sport, dst, dport), data) in enumerate(sorted(conns.items())):
    fpath = os.path.join(OUT_DIR, "streams", "stream_%02d__%s_%d__to__%s_%d.txt" % (i, src, sport, dst, dport))
    with open(fpath, "wb") as f:
        f.write(data)
    print("[%d] %s:%d -> %s:%d  %d bytes  -> %s" % (i, src, sport, dst, dport, len(data), os.path.basename(fpath)))
    preview = to_text(data[:600])
    print("---- preview ----")
    print(preview)
    print("---- end ----", flush=True)
