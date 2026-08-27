#define WIN32_LEAN_AND_MEAN
#include <windows.h>

#include <algorithm>
#include <cstdint>
#include <cstdio>
#include <cstring>
#include <string>
#include <vector>

static const wchar_t kMarker[] = L"\u5bf9\u6728\u4eba\u6869\u9020\u6210\u4e86";

static bool Readable(const MEMORY_BASIC_INFORMATION& mbi, bool writable_only) {
  if (mbi.State != MEM_COMMIT || (mbi.Protect & (PAGE_GUARD | PAGE_NOACCESS))) {
    return false;
  }
  const DWORD access = mbi.Protect & 0xFFu;
  if (writable_only) {
    return access == PAGE_READWRITE || access == PAGE_WRITECOPY ||
           access == PAGE_EXECUTE_READWRITE ||
           access == PAGE_EXECUTE_WRITECOPY;
  }
  return access == PAGE_READONLY || access == PAGE_READWRITE ||
         access == PAGE_WRITECOPY || access == PAGE_EXECUTE_READ ||
         access == PAGE_EXECUTE_READWRITE || access == PAGE_EXECUTE_WRITECOPY;
}

static std::vector<uint32_t> FindMarkerAddresses(HANDLE process,
                                                 uintptr_t first_address,
                                                 bool writable_only) {
  std::vector<uint32_t> found;
  const uint8_t* marker = reinterpret_cast<const uint8_t*>(kMarker);
  const size_t marker_bytes = (wcslen(kMarker) * sizeof(wchar_t));
  const size_t chunk_size = 1u << 20;
  std::vector<uint8_t> chunk(chunk_size + marker_bytes);
  uintptr_t cursor = first_address;
  while (cursor < 0xFFF00000u) {
    MEMORY_BASIC_INFORMATION mbi = {};
    if (!VirtualQueryEx(process, reinterpret_cast<LPCVOID>(cursor), &mbi,
                        sizeof(mbi))) {
      break;
    }
    const uintptr_t base = reinterpret_cast<uintptr_t>(mbi.BaseAddress);
    const size_t region_size = static_cast<size_t>(mbi.RegionSize);
    const uintptr_t next = base + region_size;
    if (next <= cursor) break;
    cursor = next;
    if (!Readable(mbi, writable_only) || base >= 0xFFF00000u) continue;

    size_t offset = 0;
    while (offset < region_size) {
      const size_t requested =
          (std::min)(chunk_size, static_cast<size_t>(region_size - offset));
      SIZE_T got = 0;
      if (!ReadProcessMemory(process, reinterpret_cast<LPCVOID>(base + offset),
                             chunk.data(), requested, &got) || got < marker_bytes) {
        offset += requested;
        continue;
      }
      for (size_t i = 0; i + marker_bytes <= got; i += 2) {
        if (chunk[i] == marker[0] && chunk[i + 1] == marker[1] &&
            memcmp(chunk.data() + i, marker, marker_bytes) == 0) {
          found.push_back(static_cast<uint32_t>(base + offset + i));
        }
      }
      if (requested == 0) break;
      offset += requested;
    }
  }
  std::sort(found.begin(), found.end());
  found.erase(std::unique(found.begin(), found.end()), found.end());
  return found;
}

static uint32_t ReadDamage(HANDLE process, uint32_t address) {
  wchar_t text[96] = {};
  SIZE_T got = 0;
  if (!ReadProcessMemory(process, reinterpret_cast<LPCVOID>(address), text,
                         sizeof(text) - sizeof(wchar_t), &got)) {
    return 0;
  }
  text[got / sizeof(wchar_t)] = L'\0';
  const size_t marker_chars = wcslen(kMarker);
  if (wcsncmp(text, kMarker, marker_chars) != 0) return 0;
  const wchar_t* cursor = text + marker_chars;
  uint64_t damage = 0;
  int digits = 0;
  while (*cursor >= L'0' && *cursor <= L'9' && digits < 18) {
    damage = damage * 10u + static_cast<uint32_t>(*cursor - L'0');
    ++cursor;
    ++digits;
  }
  if (!digits || wcsncmp(cursor, L"\u70b9\u4f24\u5bb3", 3) != 0 ||
      damage > 0xFFFFFFFFu) {
    return 0;
  }
  return static_cast<uint32_t>(damage);
}

int wmain(int argc, wchar_t** argv) {
  if (argc != 2) {
    fwprintf(stderr, L"usage: dummy_damage_reader.exe <pid>\n");
    return 2;
  }
  const DWORD pid = wcstoul(argv[1], nullptr, 10);
  HANDLE process = OpenProcess(PROCESS_QUERY_INFORMATION | PROCESS_VM_READ,
                               FALSE, pid);
  if (!process) {
    fwprintf(stderr, L"OpenProcess failed error=%lu\n", GetLastError());
    return 3;
  }
  // Live AUI histories are allocated in the upper writable heap (observed
  // 0x84..-0x8D..). Scan it first, then widen to the remaining high heap. This
  // avoids rereading executable images and residual UI pools every refresh.
  std::vector<uint32_t> addresses =
      FindMarkerAddresses(process, 0x80000000u, true);
  if (addresses.empty()) {
    addresses = FindMarkerAddresses(process, 0x40000000u, true);
  }
  if (addresses.empty()) {
    addresses = FindMarkerAddresses(process, 0x04000000u, false);
  }
  std::vector<std::vector<uint32_t>> runs;
  std::vector<uint32_t> current;
  for (uint32_t address : addresses) {
    if (!current.empty() && address - current.back() > 0x300u) {
      runs.push_back(current);
      current.clear();
    }
    current.push_back(address);
  }
  if (!current.empty()) runs.push_back(current);
  std::sort(runs.begin(), runs.end(), [](const auto& left, const auto& right) {
    return left.size() > right.size();
  });

  printf("RAW %zu RUNS %zu\n", addresses.size(), runs.size());
  for (const auto& run : runs) {
    std::vector<uint32_t> values;
    values.reserve(run.size());
    for (uint32_t address : run) {
      const uint32_t damage = ReadDamage(process, address);
      if (damage) values.push_back(damage);
    }
    if (values.empty()) continue;
    printf("SEQ %08X %zu", run.front(), values.size());
    for (uint32_t value : values) printf(" %u", value);
    printf("\n");
  }
  CloseHandle(process);
  return 0;
}
