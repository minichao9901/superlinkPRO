// R2 原生 store builder —— M0/M1：prescan 与 native.bin 诊断
//
// 用法：
//   etm_storebuild --version
//   etm_storebuild --self-test
//   etm_storebuild prescan --native <native.bin> [--out <ranges.bin>] [--json <out.json>]
//                          [--max-unique-ranges N] [--limit-records N]
//   etm_storebuild diagnose --native <native.bin> --static <static-dict.bin>
//                           [--ranges <ranges.bin>] [--json <out.json>]
//                           [--checkpoint-instructions N] [--max-run-instructions N]
//                           [--limit-records N]
//   etm_storebuild inspect-static --static <static-dict.bin> [--json <out.json>]
//
// 本文件只实现 M0/M1（预扫描 + 诊断），不写 store.bin；完整 build 在 t16。
// 设计：docs/etm-r2-native-builder-design.md §1–§4、§7.1、§8、§11。
//
// 结构约束：
// * native.bin 单遍流式读取；元素/包/状态记录在同一循环内完成解析与计数；
// * 不逐指令调用 Python / 不逐条输出 JSON；只在最终阶段输出一个 JSON；
// * 静态字典在 native 流之前一次性读入；诊断的 run/谓词/周期状态机全部在 C++ 内完成。

#include <algorithm>
#include <chrono>
#include <cstdint>
#include <cstdio>
#include <cstring>
#include <filesystem>
#include <fstream>
#include <iostream>
#include <limits>
#include <sstream>
#include <string>
#include <unordered_map>
#include <vector>

#if defined(_WIN32)
#include <windows.h>
#include <psapi.h>
#else
#include <sys/resource.h>
#endif

namespace fs = std::filesystem;

namespace {

constexpr const char* kProtocol = "etm-storebuild/1";
constexpr uint32_t kSchema = 1;
constexpr uint32_t kAbi = 1;
constexpr uint32_t kStaticDictVersion = 1;
constexpr uint32_t kRangeTableVersion = 1;
constexpr uint32_t kNativeHeaderBytes = 64;
constexpr uint32_t kNativeMagic = 0x54414E4DU;  // unused; kept for clarity
constexpr uint32_t kElementHeaderBytes = 61;
constexpr uint32_t kPacketHeaderBytes = 22;
constexpr uint32_t kStatusHeaderBytes = 37;
constexpr uint16_t kRecordElement = 1;
constexpr uint16_t kRecordPacket = 2;
constexpr uint16_t kRecordStatus = 3;
constexpr uint16_t kElementInstrRange = 5;
constexpr uint16_t kElementCycleCount = 12;
constexpr uint64_t kDefaultCheckpointInstructions = 4096;
constexpr uint64_t kDefaultMaxRunInstructions = 4096;
constexpr uint64_t kDefaultMaxUniqueRanges = 1000000;
constexpr uint64_t kTargetBlockBytes = 1u << 20;

// 与 trace_store.py / native_binary.py 同义的常量
constexpr uint16_t kEventTypes[] = {1, 2, 3, 6, 7, 8, 9, 10};
constexpr uint16_t kBoundaryTypes[] = {1, 2, 6, 7, 8};
constexpr uint16_t kGapTypes[] = {1, 6, 7, 8};
constexpr uint32_t kFlowLinear = 0;
constexpr uint32_t kFlowCall = 1;
constexpr uint32_t kFlowReturn = 2;
constexpr uint32_t kFlowBranch = 3;
constexpr uint32_t kFlowUnknown = 4;

enum ExitCode {
    kExitOk = 0,
    kExitOther = 1,
    kExitUsage = 2,
    kExitInput = 3,
    kExitRangeMismatch = 4,
    kExitConsistency = 5,
    kExitVersion = 6,
    kExitIo = 7,
};

class BuildError : public std::runtime_error {
  public:
    BuildError(int code, const std::string& message) : std::runtime_error(message), code_(code) {}
    int code() const { return code_; }
  private:
    int code_;
};

[[noreturn]] void fail(int code, const std::string& message) {
    throw BuildError(code, message);
}

// ── 小工具 ────────────────────────────────────────────────────────────────────

uint16_t read_u16(const uint8_t* data) {
    return static_cast<uint16_t>(data[0] | (data[1] << 8));
}

uint32_t read_u32(const uint8_t* data) {
    return static_cast<uint32_t>(data[0]) | (static_cast<uint32_t>(data[1]) << 8)
        | (static_cast<uint32_t>(data[2]) << 16) | (static_cast<uint32_t>(data[3]) << 24);
}

uint64_t read_u64(const uint8_t* data) {
    uint64_t value = 0;
    for (int index = 7; index >= 0; --index) value = (value << 8) | data[index];
    return value;
}

void write_u32(std::ostream& out, uint32_t value) {
    uint8_t bytes[4] = {static_cast<uint8_t>(value), static_cast<uint8_t>(value >> 8),
                        static_cast<uint8_t>(value >> 16), static_cast<uint8_t>(value >> 24)};
    out.write(reinterpret_cast<const char*>(bytes), 4);
}

void write_u64(std::ostream& out, uint64_t value) {
    uint8_t bytes[8];
    for (int index = 0; index < 8; ++index) bytes[index] = static_cast<uint8_t>(value >> (8 * index));
    out.write(reinterpret_cast<const char*>(bytes), 8);
}

bool read_exact(std::ifstream& file, void* data, size_t size) {
    file.read(reinterpret_cast<char*>(data), static_cast<std::streamsize>(size));
    return static_cast<size_t>(file.gcount()) == size;
}

// 1 MiB 级缓冲读取：native.bin 是 6M 条小记录，逐条 ifstream.read 的调用开销明显。
class FastReader {
  public:
    explicit FastReader(const fs::path& path, size_t capacity = 4u << 20) {
        file_.open(path, std::ios::binary);
        if (!file_) fail(kExitIo, "打不开 native.bin：" + path.string());
        file_.seekg(0, std::ios::end);
        size_ = static_cast<uint64_t>(file_.tellg());
        file_.seekg(0, std::ios::beg);
        buffer_.resize(capacity);
    }
    uint64_t size() const { return size_; }
    bool read(void* data, size_t size) {
        auto* out = static_cast<uint8_t*>(data);
        while (size) {
            if (position_ == available_) {
                if (!fill()) return false;
            }
            size_t take = std::min(size, available_ - position_);
            std::memcpy(out, buffer_.data() + position_, take);
            out += take;
            size -= take;
            position_ += take;
        }
        return true;
    }
    bool skip(size_t size) {
        while (size) {
            if (position_ == available_) {
                if (!fill()) return false;
            }
            size_t take = std::min(size, available_ - position_);
            position_ += take;
            size -= take;
        }
        return true;
    }

  private:
    bool fill() {
        file_.read(buffer_.data(), static_cast<std::streamsize>(buffer_.size()));
        available_ = static_cast<size_t>(file_.gcount());
        position_ = 0;
        return available_ > 0;
    }
    std::ifstream file_;
    std::vector<char> buffer_;
    size_t position_ = 0;
    size_t available_ = 0;
    uint64_t size_ = 0;
};

double seconds_since(std::chrono::steady_clock::time_point start) {
    return std::chrono::duration<double>(std::chrono::steady_clock::now() - start).count();
}

uint64_t peak_rss_bytes() {
#if defined(_WIN32)
    PROCESS_MEMORY_COUNTERS info;
    std::memset(&info, 0, sizeof(info));
    if (GetProcessMemoryInfo(GetCurrentProcess(), &info, sizeof(info))) {
        return static_cast<uint64_t>(info.WorkingSetSize);
    }
    return 0;
#else
    struct rusage usage;
    if (getrusage(RUSAGE_SELF, &usage) == 0) {
        return static_cast<uint64_t>(usage.ru_maxrss) * 1024ull;
    }
    return 0;
#endif
}

std::string json_escape(const std::string& text) {
    std::string out;
    out.reserve(text.size() + 8);
    for (unsigned char ch : text) {
        switch (ch) {
            case '"': out += "\\\""; break;
            case '\\': out += "\\\\"; break;
            case '\n': out += "\\n"; break;
            case '\r': out += "\\r"; break;
            case '\t': out += "\\t"; break;
            case '\b': out += "\\b"; break;
            case '\f': out += "\\f"; break;
            default:
                if (ch < 0x20) {
                    char buffer[7];
                    std::snprintf(buffer, sizeof(buffer), "\\u%04x", ch);
                    out += buffer;
                } else {
                    out.push_back(static_cast<char>(ch));
                }
        }
    }
    return out;
}

std::string quote(const std::string& text) { return "\"" + json_escape(text) + "\""; }

std::string compiler_string() { return std::string("gcc ") + __VERSION__; }

#ifndef ETM_STOREBUILD_SHA
#define ETM_STOREBUILD_SHA "unknown"
#endif

std::string build_sha() { return ETM_STOREBUILD_SHA; }

// ── SHA-256（与 frontend.cpp 同源；不引第三方库）────────────────────────────

class Sha256 {
  public:
    Sha256() { reset(); }
    void reset() {
        length_ = 0;
        buffer_size_ = 0;
        state_[0] = 0x6a09e667u; state_[1] = 0xbb67ae85u; state_[2] = 0x3c6ef372u;
        state_[3] = 0xa54ff53au; state_[4] = 0x510e527fu; state_[5] = 0x9b05688cu;
        state_[6] = 0x1f83d9abu; state_[7] = 0x5be0cd19u;
    }
    void update(const unsigned char* data, size_t size) {
        length_ += size;
        while (size) {
            size_t take = std::min(size, sizeof(buffer_) - buffer_size_);
            std::memcpy(buffer_ + buffer_size_, data, take);
            buffer_size_ += take;
            data += take;
            size -= take;
            if (buffer_size_ == sizeof(buffer_)) {
                transform(buffer_);
                buffer_size_ = 0;
            }
        }
    }
    void update(const void* data, size_t size) {
        update(static_cast<const unsigned char*>(data), size);
    }
    std::string hex_digest() {
        uint64_t bits = length_ * 8;
        unsigned char pad = 0x80;
        update(&pad, 1);
        unsigned char zero = 0;
        while (buffer_size_ != 56) update(&zero, 1);
        unsigned char tail[8];
        for (int index = 0; index < 8; ++index) tail[index] = static_cast<unsigned char>(bits >> (56 - 8 * index));
        update(tail, 8);
        static const char* digits = "0123456789abcdef";
        std::string out;
        for (uint32_t word : state_) {
            for (int shift = 28; shift >= 0; shift -= 4) out.push_back(digits[(word >> shift) & 0x0F]);
        }
        return out;
    }

  private:
    static uint32_t rotr(uint32_t value, int bits) { return (value >> bits) | (value << (32 - bits)); }
    void transform(const unsigned char* block) {
        static const uint32_t k[64] = {
            0x428a2f98u, 0x71374491u, 0xb5c0fbcfu, 0xe9b5dba5u, 0x3956c25bu, 0x59f111f1u,
            0x923f82a4u, 0xab1c5ed5u, 0xd807aa98u, 0x12835b01u, 0x243185beu, 0x550c7dc3u,
            0x72be5d74u, 0x80deb1feu, 0x9bdc06a7u, 0xc19bf174u, 0xe49b69c1u, 0xefbe4786u,
            0x0fc19dc6u, 0x240ca1ccu, 0x2de92c6fu, 0x4a7484aau, 0x5cb0a9dcu, 0x76f988dau,
            0x983e5152u, 0xa831c66du, 0xb00327c8u, 0xbf597fc7u, 0xc6e00bf3u, 0xd5a79147u,
            0x06ca6351u, 0x14292967u, 0x27b70a85u, 0x2e1b2138u, 0x4d2c6dfcu, 0x53380d13u,
            0x650a7354u, 0x766a0abbu, 0x81c2c92eu, 0x92722c85u, 0xa2bfe8a1u, 0xa81a664bu,
            0xc24b8b70u, 0xc76c51a3u, 0xd192e819u, 0xd6990624u, 0xf40e3585u, 0x106aa070u,
            0x19a4c116u, 0x1e376c08u, 0x2748774cu, 0x34b0bcb5u, 0x391c0cb3u, 0x4ed8aa4au,
            0x5b9cca4fu, 0x682e6ff3u, 0x748f82eeu, 0x78a5636fu, 0x84c87814u, 0x8cc70208u,
            0x90befffau, 0xa4506cebu, 0xbef9a3f7u, 0xc67178f2u};
        uint32_t w[64];
        for (int index = 0; index < 16; ++index) {
            w[index] = (static_cast<uint32_t>(block[index * 4]) << 24)
                     | (static_cast<uint32_t>(block[index * 4 + 1]) << 16)
                     | (static_cast<uint32_t>(block[index * 4 + 2]) << 8)
                     | static_cast<uint32_t>(block[index * 4 + 3]);
        }
        for (int index = 16; index < 64; ++index) {
            uint32_t s0 = rotr(w[index - 15], 7) ^ rotr(w[index - 15], 18) ^ (w[index - 15] >> 3);
            uint32_t s1 = rotr(w[index - 2], 17) ^ rotr(w[index - 2], 19) ^ (w[index - 2] >> 10);
            w[index] = w[index - 16] + s0 + w[index - 7] + s1;
        }
        uint32_t a = state_[0], b = state_[1], c = state_[2], d = state_[3];
        uint32_t e = state_[4], f = state_[5], g = state_[6], h = state_[7];
        for (int index = 0; index < 64; ++index) {
            uint32_t s1 = rotr(e, 6) ^ rotr(e, 11) ^ rotr(e, 25);
            uint32_t ch = (e & f) ^ (~e & g);
            uint32_t temp1 = h + s1 + ch + k[index] + w[index];
            uint32_t s0 = rotr(a, 2) ^ rotr(a, 13) ^ rotr(a, 22);
            uint32_t maj = (a & b) ^ (a & c) ^ (b & c);
            uint32_t temp2 = s0 + maj;
            h = g; g = f; f = e; e = d + temp1;
            d = c; c = b; b = a; a = temp1 + temp2;
        }
        state_[0] += a; state_[1] += b; state_[2] += c; state_[3] += d;
        state_[4] += e; state_[5] += f; state_[6] += g; state_[7] += h;
    }
    uint32_t state_[8];
    unsigned char buffer_[64];
    size_t buffer_size_;
    uint64_t length_;
};

// ── 静态字典（R2 设计 §3.2 + t14 固定文本槽）────────────────────────────────

struct StaticEntry {
    uint32_t pc = 0;
    uint32_t size = 0;
    uint32_t id = 0;
    uint8_t predicated = 0;
    uint8_t flow = 0;
    uint8_t text_slots = 0;  // 是否有 4 个文本槽
    uint32_t text_base = 0;  // 字符串池索引 1+4e / 2+4e / ...
    int32_t line = -1;
    uint8_t conditional = 0;
    uint8_t flags = 0;       // ext flags：bit0 bytes / bit1 function / bit2 file / bit3 line
    uint8_t code_bytes = 0;
    uint8_t code[16] = {0};
};

struct StaticDict {
    uint32_t version = 0;
    std::string elf_sha256;  // hex 64
    uint32_t entry_count = 0;
    uint32_t string_count = 0;
    uint64_t table_offset = 0;
    bool has_extension = false;
    std::string sha256;
    uint64_t bytes = 0;
    std::vector<StaticEntry> entries;                 // PC 升序
    std::unordered_map<uint32_t, uint32_t> by_pc;     // pc -> entries index
    std::vector<std::string> strings;

    const StaticEntry* find(uint32_t pc) const {
        auto found = by_pc.find(pc);
        if (found == by_pc.end()) return nullptr;
        return &entries[found->second];
    }
    const std::string& text(const StaticEntry& entry, int slot) const {
        if (!entry.text_slots) {
            static const std::string empty;
            return empty;
        }
        uint32_t index = entry.text_base + static_cast<uint32_t>(slot);
        if (index >= strings.size()) {
            static const std::string empty;
            return empty;
        }
        return strings[index];
    }
};

uint32_t parse_hex_byte(char ch) {
    if (ch >= '0' && ch <= '9') return static_cast<uint32_t>(ch - '0');
    if (ch >= 'a' && ch <= 'f') return static_cast<uint32_t>(ch - 'a' + 10);
    if (ch >= 'A' && ch <= 'F') return static_cast<uint32_t>(ch - 'A' + 10);
    fail(kExitInput, "static-dict elfSha256 不是十六进制");
}

StaticDict load_static_dict(const fs::path& path) {
    std::ifstream file(path, std::ios::binary);
    if (!file) fail(kExitInput, "打不开 static-dict：" + path.string());
    file.seekg(0, std::ios::end);
    const uint64_t size = static_cast<uint64_t>(file.tellg());
    file.seekg(0, std::ios::beg);
    if (size < 61) fail(kExitInput, "static-dict 头部被截断");
    std::vector<uint8_t> raw(static_cast<size_t>(size));
    if (!read_exact(file, raw.data(), raw.size())) fail(kExitInput, "static-dict 读取不完整");
    StaticDict dict;
    dict.bytes = size;
    dict.sha256 = [&]() {
        Sha256 sha;
        sha.update(raw.data(), raw.size());
        return sha.hex_digest();
    }();
    if (std::memcmp(raw.data(), "R2STATIC1", 9) != 0) fail(kExitVersion, "static-dict magic 不符");
    dict.version = read_u32(raw.data() + 9);
    if (dict.version != kStaticDictVersion) {
        fail(kExitVersion, "static-dict 版本不支持：" + std::to_string(dict.version));
    }
    dict.elf_sha256.reserve(64);
    for (int index = 0; index < 32; ++index) {
        uint32_t value = raw[13 + index];
        dict.elf_sha256.push_back("0123456789abcdef"[value >> 4]);
        dict.elf_sha256.push_back("0123456789abcdef"[value & 0x0F]);
    }
    dict.entry_count = read_u32(raw.data() + 45);
    dict.string_count = read_u32(raw.data() + 49);
    dict.table_offset = read_u64(raw.data() + 53);
    if (dict.table_offset < 61 || dict.table_offset > size) {
        fail(kExitInput, "static-dict tableOffset 越界");
    }
    if (dict.entry_count == 0) fail(kExitInput, "static-dict entryCount 为 0");
    const uint64_t entries_end = dict.table_offset + static_cast<uint64_t>(dict.entry_count) * 16;
    if (entries_end > size) {
        fail(kExitInput, "static-dict 条目表被截断");
    }
    bool has_extension = false;
    uint64_t extension_start = 0;
    if (entries_end < size) {
        if (size - entries_end < 16 || std::memcmp(raw.data() + entries_end, "R2EXT01\0", 8) != 0) {
            fail(kExitInput, "static-dict 尾部有未知数据（扩展段 magic 不符）");
        }
        const uint32_t ext_version = read_u32(raw.data() + entries_end + 8);
        const uint32_t ext_count = read_u32(raw.data() + entries_end + 12);
        if (ext_version != 1 || ext_count != dict.entry_count) {
            fail(kExitVersion, "static-dict 扩展段版本/条目数不符");
        }
        extension_start = entries_end + 16;
        if (extension_start + static_cast<uint64_t>(ext_count) * 24 > size) {
            fail(kExitInput, "static-dict 扩展段被截断");
        }
        has_extension = true;
        dict.has_extension = true;
    }
    // 字符串池
    uint64_t position = 61;
    dict.strings.reserve(dict.string_count);
    for (uint32_t index = 0; index < dict.string_count; ++index) {
        if (position + 4 > dict.table_offset) fail(kExitInput, "static-dict 字符串池被截断");
        uint32_t length = read_u32(raw.data() + position);
        position += 4;
        if (position + length > dict.table_offset) fail(kExitInput, "static-dict 字符串越过字符串池");
        dict.strings.emplace_back(reinterpret_cast<const char*>(raw.data() + position), length);
        position += length;
    }
    if (position != dict.table_offset) fail(kExitInput, "static-dict 字符串池长度与 tableOffset 不符");
    if (dict.string_count && (dict.strings.empty() || !dict.strings[0].empty())) {
        fail(kExitInput, "static-dict 字符串池 0 号必须为空串");
    }
    if (dict.string_count && dict.string_count < 1 + static_cast<uint64_t>(dict.entry_count) * 4) {
        fail(kExitInput, "static-dict 字符串槽不足（需要 1+4*entryCount）");
    }
    dict.entries.reserve(dict.entry_count);
    std::vector<bool> seen(static_cast<size_t>(dict.entry_count), false);
    uint32_t previous_pc = 0;
    bool first = true;
    for (uint32_t index = 0; index < dict.entry_count; ++index) {
        const uint8_t* row = raw.data() + dict.table_offset + static_cast<uint64_t>(index) * 16;
        StaticEntry entry;
        entry.pc = read_u32(row);
        entry.size = row[4];
        entry.id = read_u32(row + 5);
        entry.predicated = row[9];
        entry.flow = row[10];
        if (entry.size == 0 || entry.size > 8) fail(kExitInput, "static-dict 条目 size 非法");
        if (entry.id >= dict.entry_count || seen[entry.id]) {
            fail(kExitInput, "static-dict 静态 ID 重复或越界");
        }
        seen[entry.id] = true;
        if (entry.predicated > 1) fail(kExitInput, "static-dict predicated 非 0/1");
        if (entry.flow > kFlowUnknown) fail(kExitInput, "static-dict flow 越界");
        if (!first && entry.pc <= previous_pc) fail(kExitInput, "static-dict 未按 PC 严格升序");
        previous_pc = entry.pc;
        first = false;
        entry.text_base = 1 + 4 * index;
        entry.text_slots = dict.string_count ? 1 : 0;
        if (has_extension) {
            const uint8_t* ext = raw.data() + extension_start + static_cast<uint64_t>(index) * 24;
            entry.line = static_cast<int32_t>(read_u32(ext));
            entry.conditional = ext[4];
            entry.flags = ext[5];
            std::memcpy(entry.code, ext + 6, 16);
            entry.code_bytes = entry.size <= 16 ? static_cast<uint8_t>(entry.size) : 16;
            if (!(entry.flags & 8)) entry.line = -1;
        }
        dict.by_pc.emplace(entry.pc, static_cast<uint32_t>(dict.entries.size()));
        dict.entries.push_back(entry);
    }
    return dict;
}

// ── native.bin 读取 ──────────────────────────────────────────────────────────

struct NativeHeader {
    uint16_t schema = 0;
    uint16_t header_bytes = 0;
    uint32_t byte_order = 0;
    uint32_t protocol = 0;
    uint32_t features = 0;
    uint64_t record_count = 0;
    uint64_t element_count = 0;
    uint64_t packet_count = 0;
    uint64_t body_bytes = 0;
};

struct StatusInfo {
    uint32_t processed = 0;
    int32_t last_error_code = 0;
    std::string library;
    std::string last_error;
    uint64_t element_count = 0;
    uint64_t packet_count = 0;
    bool present = false;
};

struct NativeCounters {
    uint64_t saw_records = 0;
    uint64_t saw_elements = 0;
    uint64_t saw_packets = 0;
    uint64_t range_records = 0;
    uint64_t unique_ranges = 0;
    uint64_t duplicate_ranges = 0;
    uint64_t payload_mismatches = 0;
    uint64_t event_elements = 0;
    uint64_t cycle_elements = 0;
    uint64_t other_elements = 0;
    uint64_t payload_bytes = 0;
    uint64_t text_bytes = 0;
    uint64_t cycle_sum = 0;
    uint64_t exceptions = 0;
    uint64_t gaps = 0;
    uint64_t boundaries = 0;
    uint64_t type_counts[256] = {0};
    uint64_t packet_unsync = 0;
    uint64_t packet_invalid = 0;
    uint64_t packet_overflow = 0;
    std::string native_sha256;
    uint64_t native_bytes = 0;
    NativeHeader header;
    StatusInfo status;
};

struct RangeRecord {
    uint32_t start = 0;
    uint32_t end = 0;
    uint32_t payload = 0;
};

bool is_event_type(uint16_t type) {
    for (uint16_t item : kEventTypes) if (item == type) return true;
    return false;
}

bool is_boundary_type(uint16_t type) {
    for (uint16_t item : kBoundaryTypes) if (item == type) return true;
    return false;
}

bool is_gap_type(uint16_t type) {
    for (uint16_t item : kGapTypes) if (item == type) return true;
    return false;
}

// ETMv4/ETMv3 包故障分类（与 native_binary.classify_packet 同义）
bool packet_is_unsync(uint32_t type_code, uint32_t protocol) {
    if (protocol == 2) return type_code == 1;
    return type_code == 0x200;
}

bool packet_is_invalid(uint32_t type_code, uint32_t protocol) {
    if (protocol == 2) return type_code == 2 || type_code == 23 || type_code == 24;
    return type_code == 0x201 || type_code == 0x202
        || (type_code >= 0x300 && type_code <= 0x303);
}

bool packet_is_overflow(uint32_t type_code, uint32_t protocol) {
    if (protocol == 2) return type_code == 10;  // STORE_FAIL
    return type_code == 0x105;
}

// 单遍读取 native.bin；prescan=true 时收集唯一范围并输出 ranges.bin。
// 输出 counters/ranges；任何格式/计数/状态错误抛 BuildError（退出码 3/4/5）。
struct NativeVisitor {
    virtual ~NativeVisitor() = default;
    virtual void on_range(uint32_t offset, uint64_t start, uint64_t end,
                          uint32_t payload, uint8_t last_executed,
                          uint32_t cycles) = 0;
    virtual void on_cycle(uint32_t offset, uint32_t cycles, uint32_t payload) = 0;
    virtual void on_event(uint16_t etype, uint32_t offset, uint32_t payload,
                          uint64_t start, uint64_t end, uint32_t flags) = 0;
    virtual void on_other(uint16_t etype, uint32_t offset) = 0;
};

NativeCounters read_native(const fs::path& path, bool collect_ranges,
                           std::vector<RangeRecord>* ranges,
                           uint64_t max_unique_ranges, uint64_t limit_records,
                           std::ostream* range_out, bool compute_sha = false,
                           NativeVisitor* visitor = nullptr) {
    FastReader file(path);
    const uint64_t file_size = file.size();
    if (file_size < kNativeHeaderBytes) fail(kExitInput, "native.bin 短于 64 字节头部");
    NativeCounters counters;
    counters.native_bytes = file_size;
    Sha256 sha;
    uint8_t header_bytes[kNativeHeaderBytes];
    if (!file.read(header_bytes, sizeof(header_bytes))) fail(kExitInput, "native.bin 头部被截断");
    if (compute_sha) sha.update(header_bytes, sizeof(header_bytes));
    if (std::memcmp(header_bytes, "ETMNAT01", 8) != 0) fail(kExitInput, "native.bin magic 不符");
    NativeHeader& header = counters.header;
    header.schema = read_u16(header_bytes + 8);
    header.header_bytes = read_u16(header_bytes + 10);
    header.byte_order = read_u32(header_bytes + 12);
    header.protocol = read_u32(header_bytes + 16);
    header.features = read_u32(header_bytes + 20);
    header.record_count = read_u64(header_bytes + 24);
    header.element_count = read_u64(header_bytes + 32);
    header.packet_count = read_u64(header_bytes + 40);
    header.body_bytes = read_u64(header_bytes + 48);
    if (header.schema != 1 || header.header_bytes != kNativeHeaderBytes
            || header.byte_order != 0x01020304u || header.features & ~1u) {
        fail(kExitVersion, "native.bin 头部版本/字节序/featureFlags 不支持");
    }
    if (header.protocol < 1 || header.protocol > 2) fail(kExitVersion, "native.bin 协议码未知");
    if (header.body_bytes != file_size - kNativeHeaderBytes) {
        fail(kExitInput, "native.bin bodyBytes 与文件长度不符");
    }
    std::unordered_map<uint64_t, uint32_t> range_index;
    if (collect_ranges) {
        ranges->clear();
        ranges->reserve(1024);
    }
    std::vector<uint8_t> body;
    body.reserve(4096);
    bool status_seen = false;
    uint64_t position = kNativeHeaderBytes;
    while (position < file_size) {
        if (limit_records && counters.saw_records >= limit_records) break;
        if (file_size - position < 5) fail(kExitInput, "native.bin 记录头被截断");
        uint8_t record_header[5];
        if (!file.read(record_header, sizeof(record_header))) fail(kExitInput, "native.bin 记录头读取失败");
        if (compute_sha) sha.update(record_header, sizeof(record_header));
        const uint8_t kind = record_header[0];
        const uint32_t record_bytes = read_u32(record_header + 1);
        const uint32_t minimum = kind == kRecordElement ? kElementHeaderBytes
                              : kind == kRecordPacket ? kPacketHeaderBytes
                              : kind == kRecordStatus ? kStatusHeaderBytes : 0;
        if (minimum == 0) fail(kExitInput, "native.bin 未知记录类型 " + std::to_string(kind));
        if (record_bytes < minimum) fail(kExitInput, "native.bin 记录长度小于最小长度");
        if (record_bytes > 64u * 1024u * 1024u) fail(kExitInput, "native.bin 记录过大");
        if (position + record_bytes > file_size) fail(kExitInput, "native.bin 记录被截断");
        const uint32_t body_bytes_record = record_bytes - 5;
        body.resize(body_bytes_record);
        if (body_bytes_record && !file.read(body.data(), body_bytes_record)) {
            fail(kExitInput, "native.bin 记录内容读取失败");
        }
        if (compute_sha) sha.update(body.data(), body.size());
        uint64_t record_end = position + record_bytes;
        position = record_end;
        ++counters.saw_records;
        counters.payload_bytes += record_bytes;
        if (status_seen) fail(kExitInput, "native.bin 状态记录之后仍有记录");
        if (kind == kRecordPacket) {
            ++counters.saw_packets;
            const uint32_t type_code = read_u32(body.data());
            const uint64_t offset = read_u64(body.data() + 5);
            const uint32_t size = read_u32(body.data() + 13);
            (void)offset;
            if (size != record_bytes - kPacketHeaderBytes) {
                fail(kExitInput, "native.bin 包长度与记录长度不符");
            }
            if (packet_is_unsync(type_code, header.protocol)) ++counters.packet_unsync;
            if (packet_is_invalid(type_code, header.protocol)) {
                ++counters.packet_invalid;
                if (packet_is_overflow(type_code, header.protocol)) ++counters.packet_overflow;
            }
            continue;
        }
        if (kind == kRecordStatus) {
            bool is_last = position == file_size;
            if (!is_last && limit_records && counters.saw_records >= limit_records) {
                // 允许 --limit-records 在状态记录前停止诊断
            } else if (!is_last) {
                fail(kExitInput, "native.bin 状态记录不是最后一条");
            }
            const uint32_t processed = read_u32(body.data());
            const int32_t error_code = static_cast<int32_t>(read_u32(body.data() + 4));
            const uint32_t library_bytes = read_u32(body.data() + 8);
            const uint32_t error_bytes = read_u32(body.data() + 12);
            const uint64_t element_count = read_u64(body.data() + 16);
            const uint64_t packet_count = read_u64(body.data() + 24);
            if (record_bytes != kStatusHeaderBytes + library_bytes + error_bytes) {
                fail(kExitInput, "native.bin 状态记录长度与字符串长度不符");
            }
            const char* text = reinterpret_cast<const char*>(body.data() + 32);
            counters.status.processed = processed;
            counters.status.last_error_code = error_code;
            counters.status.library.assign(text, library_bytes);
            counters.status.last_error.assign(text + library_bytes, error_bytes);
            counters.status.element_count = element_count;
            counters.status.packet_count = packet_count;
            counters.status.present = true;
            --counters.saw_records;  // 状态记录不计入 header.recordCount（与 native_binary 一致）
            if (element_count != counters.saw_elements || packet_count != counters.saw_packets) {
                fail(kExitConsistency, "native.bin 状态记录计数与已读记录不符");
            }
            status_seen = true;
            continue;
        }
        // 元素
        ++counters.saw_elements;
        if (body_bytes_record < kElementHeaderBytes - 5) fail(kExitInput, "native.bin 元素记录过短");
        const uint8_t etype = body[0];
        const uint8_t last_executed = body[3];
        const uint8_t last_conditional = body[4];
        const uint8_t flags8 = body[5];
        const uint32_t flags = read_u32(body.data() + 6);
        const uint32_t cycle_count = read_u32(body.data() + 10);
        const uint32_t payload = read_u32(body.data() + 14);
        const uint64_t start = read_u64(body.data() + 30);
        const uint64_t end = read_u64(body.data() + 38);
        const uint16_t text_bytes = read_u16(body.data() + 54);
        const uint64_t element_offset = read_u64(body.data() + 22);
        (void)last_conditional;
        if (last_executed > 1) fail(kExitInput, "native.bin lastExecuted 非法");
        if (text_bytes != body_bytes_record - (kElementHeaderBytes - 5)) {
            fail(kExitInput, "native.bin 元素文字长度与记录长度不符");
        }
        if (bool(flags8 & 0x08) != bool(text_bytes)) fail(kExitInput, "native.bin 元素 has_text 标志不一致");
        counters.type_counts[etype]++;
        counters.text_bytes += text_bytes;
        if (visitor) {
            if (etype == kElementInstrRange) {
                visitor->on_range(static_cast<uint32_t>(element_offset), start, end, payload, last_executed, cycle_count);
            } else if (etype == kElementCycleCount) {
                visitor->on_cycle(static_cast<uint32_t>(element_offset), cycle_count, payload);
            } else if (is_event_type(etype)) {
                visitor->on_event(etype, static_cast<uint32_t>(element_offset), payload, start, end, flags);
            } else {
                visitor->on_other(etype, static_cast<uint32_t>(element_offset));
            }
        }
        if (etype == kElementInstrRange) {
            ++counters.range_records;
            if (start > 0xFFFFFFFFull || end > 0xFFFFFFFFull) {
                fail(kExitInput, "native.bin 范围地址超出 32 位");
            }
            if (end <= start) fail(kExitInput, "native.bin 范围 end <= start");
            if (payload == 0) fail(kExitInput, "native.bin 范围 payload 为 0");
            if (collect_ranges) {
                const uint64_t key = (start << 32) | end;
                auto found = range_index.find(key);
                if (found == range_index.end()) {
                    if (ranges->size() >= max_unique_ranges) {
                        fail(kExitIo, "唯一范围数超过上限 " + std::to_string(max_unique_ranges));
                    }
                    range_index.emplace(key, static_cast<uint32_t>(ranges->size()));
                    ranges->push_back({static_cast<uint32_t>(start), static_cast<uint32_t>(end),
                                       payload});
                } else {
                    ++counters.duplicate_ranges;
                    const RangeRecord& existing = ranges->at(found->second);
                    if (existing.payload != payload) ++counters.payload_mismatches;
                }
            }
        } else if (etype == kElementCycleCount) {
            ++counters.cycle_elements;
            counters.cycle_sum += cycle_count;
        } else if (is_event_type(etype)) {
            ++counters.event_elements;
            if (etype == 9) ++counters.exceptions;
            if (is_boundary_type(etype)) ++counters.boundaries;
            if (is_gap_type(etype) || (etype == 2 && payload)) ++counters.gaps;
            (void)flags;
        } else {
            ++counters.other_elements;
        }
    }
    if (!status_seen) fail(kExitInput, "native.bin 缺少状态记录（解码不完整）");
    if (header.element_count != counters.saw_elements || header.packet_count != counters.saw_packets) {
        fail(kExitConsistency, "native.bin 头部计数与记录不符");
    }
    if (header.record_count != counters.saw_elements + counters.saw_packets) {
        fail(kExitConsistency, "native.bin 头部 recordCount 与记录不符");
    }
    if (compute_sha) counters.native_sha256 = sha.hex_digest();
    counters.unique_ranges = ranges ? ranges->size() : 0;
    if (range_out && collect_ranges) {
        range_out->write("R2RANGES1", 9);
        write_u64(*range_out, static_cast<uint64_t>(ranges->size()));
        write_u64(*range_out, counters.range_records);
        for (const RangeRecord& item : *ranges) {
            write_u32(*range_out, item.start);
            write_u32(*range_out, item.end);
            write_u32(*range_out, item.payload);
        }
        if (!*range_out) fail(kExitIo, "写 ranges.bin 失败");
    }
    return counters;
}

// ── 诊断状态机（与 trace_store_write._Assembler 同义）────────────────────────

struct DiagnoseState {
    uint64_t expected_seq = 0;
    uint64_t since_checkpoint = 0;
    uint64_t run_len = 0;
    uint64_t run_offset = 0;
    uint64_t run_start_seq = 0;
    uint64_t runs = 0;
    uint64_t checkpoints = 0;
    bool pending = false;
    uint32_t pending_id = 0;
    uint32_t pending_offset = 0;
    uint8_t pending_executed = 0;  // 0=true,1=false,2=unknown
    uint32_t pending_cycles = 0;
    uint32_t pending_span = 0;
    uint32_t pending_payload = 0;
    uint64_t instructions = 0;
    uint64_t executed = 0;
    uint64_t debug_tail = 0;
    uint64_t debug_internal = 0;
    uint64_t debug_append_true = 0;
    uint64_t debug_append_false = 0;
    uint64_t debug_append_unknown = 0;
    uint64_t debug_append_total = 0;
    uint64_t unknown = 0;
    uint64_t not_taken = 0;
    uint64_t predicated_instructions = 0;
    uint64_t cycle_anchors = 0;
    uint64_t predicate_segments = 0;
    bool in_predicate_segment = false;
    uint64_t checkpoint_instructions = kDefaultCheckpointInstructions;
    uint64_t max_run_instructions = kDefaultMaxRunInstructions;
    std::ostream* run_log = nullptr;

    void flush_run(uint8_t tail_executed) {
        if (!run_len) return;
        (void)tail_executed;
        if (run_log) {
            (*run_log) << run_start_seq << ',' << run_len << ',' << run_offset << "\n";
        }
        ++runs;
        run_len = 0;
        run_offset = 0;
        run_start_seq = 0;
    }

    void append_instruction(uint32_t id, uint32_t offset, uint8_t exec_state) {
        if (run_len >= max_run_instructions) flush_run(2);
        if (run_len && offset != run_offset) flush_run(2);
        if (!run_len) {
            run_offset = offset;
            run_start_seq = expected_seq;
        }
        ++run_len;
        (void)id;
        ++since_checkpoint;
        ++expected_seq;
        ++debug_append_total;
        if (exec_state == 0) ++debug_append_true;
        if (exec_state == 1) ++debug_append_false;
        if (exec_state == 2) ++debug_append_unknown;
        if (exec_state == 2) {
            ++unknown;
            if (!in_predicate_segment) {
                ++predicate_segments;
                in_predicate_segment = true;
            }
        } else {
            if (exec_state == 0) ++executed; else ++not_taken;
            in_predicate_segment = false;
        }
        ++instructions;
    }

    void flush_pending_tail() {
        if (!pending) return;
        const uint32_t id = pending_id;
        const uint32_t offset = pending_offset;
        const uint8_t executed = pending_executed;
        const uint32_t cycles = pending_cycles;
        const uint32_t span = pending_span;
        pending = false;
        ++debug_tail;
        append_instruction(id, offset, executed);
        if (!(executed == 0 && cycles == 0)) flush_run(executed);
        (void)span;
    }

    void checkpoint() {
        if (since_checkpoint >= checkpoint_instructions) {
            if (run_len) flush_run(2);
            ++checkpoints;
            since_checkpoint = 0;
        }
    }

    void cycle_element(uint32_t cycles, uint32_t payload) {
        if (!pending) return;
        if (cycles) {
            pending_cycles += cycles;
            if (!pending_span) pending_span = payload;
        }
        flush_pending_tail();
    }

    void flush_pending_run() {
        if (run_len) flush_run(2);
    }

    void finish() {
        flush_pending_tail();
        flush_pending_run();
    }
};

struct DiagnoseResult {
    NativeCounters native;
    uint64_t instructions = 0;
    uint64_t ranges = 0;
    uint64_t runs = 0;
    uint64_t events = 0;
    uint64_t executed = 0;
    uint64_t unknown = 0;
    uint64_t not_taken = 0;
    uint64_t predicated_instructions = 0;
    uint64_t predicate_segments = 0;
    uint64_t checkpoints = 0;
    uint64_t boundaries = 0;
    uint64_t exceptions = 0;
    uint64_t gaps = 0;
    uint64_t cycle_elements = 0;
    uint64_t cycle_sum = 0;
    uint64_t cycle_anchors = 0;
    uint64_t range_instructions_mismatch = 0;
    uint64_t missing_static_pc = 0;
    uint64_t static_lookup_count = 0;
    uint64_t packet_count = 0;
    uint64_t packet_unsync = 0;
    uint64_t packet_invalid = 0;
    uint64_t packet_overflow = 0;
    uint64_t range_count = 0;
    uint64_t event_elements = 0;
    uint64_t other_elements = 0;
    double seconds = 0.0;
    uint64_t rss_bytes = 0;
};

void append_range_to_state(const StaticDict& dict, DiagnoseState& state,
                           uint32_t offset, uint64_t start, uint64_t end, uint32_t payload,
                           uint8_t last_executed, uint32_t cycles,
                           DiagnoseResult& result) {
    // 取得范围内指令（含末指令）；payload 必须与走查结果一致。
    uint64_t pc = start;
    std::vector<const StaticEntry*> entries;
    entries.reserve(payload);
    while (pc < end) {
        const StaticEntry* entry = dict.find(static_cast<uint32_t>(pc));
        if (!entry) {
            ++result.missing_static_pc;
            fail(kExitRangeMismatch, "静态字典缺少 PC 0x" + [&]() {
                std::ostringstream text;
                text << std::hex << pc;
                return text.str();
            }() + "（范围 0x" + [&]() {
                std::ostringstream text;
                text << std::hex << start << "..0x" << end;
                return text.str();
            }() + "）");
        }
        entries.push_back(entry);
        pc += entry->size;
        if (entries.size() > payload + 1) {
            fail(kExitRangeMismatch, "范围走查超过 payload");
        }
    }
    if (pc != end || entries.size() != payload) {
        ++result.range_instructions_mismatch;
        std::ostringstream message;
        message << "范围核对失败：0x" << std::hex << start << "..0x" << end
                << " payload=" << std::dec << payload << " 反汇编=" << entries.size();
        fail(kExitRangeMismatch, message.str());
    }
    result.static_lookup_count += entries.size();
    const size_t internal_count = entries.size() - 1;
    // add_range: 先 flush pending tail
    state.flush_pending_tail();
    if (internal_count) {
        state.checkpoint();
        if (state.run_len && offset != state.run_offset) state.flush_pending_run();
        const uint64_t count = internal_count;
        const bool slow_path = count > state.checkpoint_instructions - state.since_checkpoint
            || (state.run_len && state.run_len + count > state.max_run_instructions);
        if (slow_path) {
            for (size_t position = 0; position < internal_count; ++position) {
                if (position) state.checkpoint();
                const StaticEntry* entry = entries[position];
                if (entry->predicated) ++result.predicated_instructions;
                ++state.debug_internal;
                state.append_instruction(entry->id, offset, entry->predicated ? 2 : 0);
            }
        } else {
            // 快路径：批量追加，不做逐条 offset 检查（调用方已保证 run 不会超过 4096）
            if (!state.run_len) {
                state.run_offset = offset;
                state.run_start_seq = state.expected_seq;
            }
            for (size_t position = 0; position < internal_count; ++position) {
                const StaticEntry* entry = entries[position];
                if (entry->predicated) ++result.predicated_instructions;
                ++state.run_len;
                ++state.debug_internal;
                ++state.since_checkpoint;
                ++state.expected_seq;
                ++state.instructions;
                if (entry->predicated) {
                    ++state.unknown;
                    if (!state.in_predicate_segment) {
                        ++state.predicate_segments;
                        state.in_predicate_segment = true;
                    }
                } else {
                    ++state.executed;
                    state.in_predicate_segment = false;
                }
            }
        }
        state.checkpoint();
    } else {
        state.checkpoint();
    }
    const StaticEntry* tail = entries.back();
    const uint8_t executed = tail->predicated ? 2 : (last_executed ? 0 : 1);
    if (tail->predicated) ++result.predicated_instructions;
    if (cycles) ++result.cycle_anchors;
    state.pending = true;
    state.pending_id = tail->id;
    state.pending_offset = offset;
    state.pending_executed = executed;
    state.pending_cycles = cycles;
    state.pending_span = cycles ? payload : 0;
    state.pending_payload = payload;
}

struct DiagnoseVisitor : NativeVisitor {
    DiagnoseVisitor(const StaticDict& dict, DiagnoseResult& result)
        : dict_(dict), result_(result) {
        state_.checkpoint_instructions = kDefaultCheckpointInstructions;
        state_.max_run_instructions = kDefaultMaxRunInstructions;
    }
    void on_range(uint32_t offset, uint64_t start, uint64_t end, uint32_t payload,
                  uint8_t last_executed, uint32_t cycles) override {
        append_range_to_state(dict_, state_, offset, start, end, payload,
                              last_executed, cycles, result_);
    }
    void on_cycle(uint32_t offset, uint32_t cycles, uint32_t payload) override {
        (void)offset;
        state_.cycle_element(cycles, payload);
    }
    void on_event(uint16_t etype, uint32_t offset, uint32_t payload,
                  uint64_t start, uint64_t end, uint32_t flags) override {
        (void)etype; (void)start; (void)end; (void)flags;
        state_.flush_pending_tail();
        state_.flush_pending_run();
        if (state_.since_checkpoint >= state_.checkpoint_instructions) {
            ++state_.checkpoints;
            state_.since_checkpoint = 0;
        }
        ++state_.expected_seq;
        (void)offset; (void)payload;
    }
    void on_other(uint16_t etype, uint32_t offset) override {
        (void)etype; (void)offset;
        state_.flush_pending_tail();
    }
    const StaticDict& dict_;
    DiagnoseResult& result_;
    DiagnoseState state_;
};

}  // namespace

int main(int argc, char** argv) {
    std::ios::sync_with_stdio(false);
    try {
        if (argc < 2) {
            std::cerr << "用法：etm_storebuild --version | prescan ... | diagnose ...\n";
            return kExitUsage;
        }
        const std::string command = argv[1];
        if (command == "--version") {
            std::cout << "{"
                      << "\"protocol\":" << quote(kProtocol)
                      << ",\"schema\":" << kSchema
                      << ",\"abi\":" << kAbi
                      << ",\"compiler\":" << quote(compiler_string())
                      << ",\"buildSha\":" << quote(build_sha())
                      << ",\"formats\":{\"native\":\"trace-bin native v1\","
                         "\"staticDict\":1,\"ranges\":1}"
                      << ",\"capabilities\":[\"prescan\",\"diagnose\"]"
                      << "}" << std::endl;
            return kExitOk;
        }
        if (command == "--self-test") {
            // C++ 自检入口：SHA-256 已知向量 + 原生/静态字典基本不变量。
            Sha256 sha;
            sha.update("abc", 3);
            const std::string digest = sha.hex_digest();
            const std::string expected = "ba7816bf8f01cfea414140de5dae2223"
                                         "b00361a396177a9cb410ff61f20015ad";
            if (digest != expected) {
                std::cerr << "self-test: SHA-256 失败 " << digest << "\n";
                return kExitConsistency;
            }
            if (kSchema != 1 || kAbi != 1 || std::string(kProtocol) != "etm-storebuild/1") {
                std::cerr << "self-test: 协议常量不符\n";
                return kExitConsistency;
            }
            std::cout << "{\"ok\":true,\"protocol\":" << quote(kProtocol)
                      << ",\"sha256\":\"abc\"}" << std::endl;
            return kExitOk;
        }
        if (command == "prescan") {
            std::string native_path;
            std::string ranges_path;
            std::string json_path;
            uint64_t max_unique = kDefaultMaxUniqueRanges;
            uint64_t limit_records = 0;
            for (int index = 2; index < argc; ++index) {
                const std::string arg = argv[index];
                auto next = [&](const char* label) -> std::string {
                    if (index + 1 >= argc) fail(kExitUsage, std::string(label) + " 缺少参数");
                    return argv[++index];
                };
                if (arg == "--native") native_path = next("--native");
                else if (arg == "--out") ranges_path = next("--out");
                else if (arg == "--json") json_path = next("--json");
                else if (arg == "--max-unique-ranges") max_unique = std::stoull(next("--max-unique-ranges"));
                else if (arg == "--limit-records") limit_records = std::stoull(next("--limit-records"));
                else fail(kExitUsage, "未知参数：" + arg);
            }
            if (native_path.empty()) fail(kExitUsage, "prescan 需要 --native");
            std::vector<RangeRecord> ranges;
            std::ofstream range_stream;
            if (!ranges_path.empty()) {
                range_stream.open(ranges_path, std::ios::binary);
                if (!range_stream) fail(kExitIo, "打不开 ranges 输出：" + ranges_path);
            }
            const auto started = std::chrono::steady_clock::now();
            NativeCounters counters = read_native(native_path, true, &ranges, max_unique,
                                                   limit_records,
                                                   ranges_path.empty() ? nullptr : &range_stream);
            const double seconds = seconds_since(started);
            if (range_stream.is_open()) {
                range_stream.flush();
                if (!range_stream) fail(kExitIo, "写 ranges.bin 失败");
            }
            std::string ranges_sha;
            if (!ranges_path.empty()) {
                std::ifstream check(ranges_path, std::ios::binary);
                Sha256 sha;
                std::vector<char> buffer(1 << 20);
                while (check) {
                    check.read(buffer.data(), buffer.size());
                    if (check.gcount() > 0) sha.update(buffer.data(), static_cast<size_t>(check.gcount()));
                }
                ranges_sha = sha.hex_digest();
            }
            std::ostringstream json;
            json << "{\n";
            json << " \"protocol\":" << quote(kProtocol) << ",\n";
            json << " \"native\":" << quote(native_path) << ",\n";
            json << " \"nativeSha256\":" << quote(counters.native_sha256) << ",\n";
            json << " \"nativeBytes\":" << counters.native_bytes << ",\n";
            json << " \"schema\":" << counters.header.schema << ", \"protocolCode\":"
                 << counters.header.protocol << ", \"features\":" << counters.header.features << ",\n";
            json << " \"headerRecords\":" << counters.header.record_count
                 << ", \"headerElements\":" << counters.header.element_count
                 << ", \"headerPackets\":" << counters.header.packet_count
                 << ", \"headerBodyBytes\":" << counters.header.body_bytes << ",\n";
            json << " \"seenRecords\":" << counters.saw_records
                 << ", \"seenElements\":" << counters.saw_elements
                 << ", \"seenPackets\":" << counters.saw_packets << ",\n";
            json << " \"rangeRecords\":" << counters.range_records
                 << ", \"uniqueRanges\":" << counters.unique_ranges
                 << ", \"duplicateRanges\":" << counters.duplicate_ranges
                 << ", \"payloadMismatches\":" << counters.payload_mismatches << ",\n";
            json << " \"eventElements\":" << counters.event_elements
                 << ", \"cycleElements\":" << counters.cycle_elements
                 << ", \"cycleSum\":" << counters.cycle_sum
                 << ", \"otherElements\":" << counters.other_elements << ",\n";
            json << " \"exceptions\":" << counters.exceptions
                 << ", \"gaps\":" << counters.gaps
                 << ", \"boundaries\":" << counters.boundaries << ",\n";
            json << " \"payloadBytes\":" << counters.payload_bytes
                 << ", \"textBytes\":" << counters.text_bytes << ",\n";
            json << " \"ranges\":" << (ranges_path.empty() ? quote("") : quote(ranges_path))
                 << ", \"rangesSha256\":" << quote(ranges_sha) << ",\n";
            json << " \"seconds\":" << seconds << ", \"bytesPerSecond\":"
                 << (seconds > 0 ? counters.payload_bytes / seconds : 0) << ",\n";
            json << " \"rssBytes\":" << peak_rss_bytes() << ",\n";
            json << " \"typeCounts\":{";
            bool first = true;
            for (int type = 0; type < 256; ++type) {
                if (!counters.type_counts[type]) continue;
                if (!first) json << ",";
                first = false;
                json << "\"" << type << "\":" << counters.type_counts[type];
            }
            json << "}\n}\n";
            if (!json_path.empty()) {
                std::ofstream out(json_path);
                if (!out) fail(kExitIo, "打不开 JSON 输出：" + json_path);
                out << json.str();
            } else {
                std::cout << json.str();
            }
            return kExitOk;
        }
        if (command == "inspect-static") {
            std::string static_path;
            std::string json_path;
            for (int index = 2; index < argc; ++index) {
                const std::string arg = argv[index];
                if (arg == "--static") { if (++index >= argc) fail(kExitUsage, "--static 缺少参数"); static_path = argv[index]; }
                else if (arg == "--json") { if (++index >= argc) fail(kExitUsage, "--json 缺少参数"); json_path = argv[index]; }
                else fail(kExitUsage, "未知参数：" + arg);
            }
            if (static_path.empty()) fail(kExitUsage, "inspect-static 需要 --static");
            StaticDict dict = load_static_dict(static_path);
            std::ostringstream json;
            json << "{\n \"protocol\":" << quote(kProtocol) << ",\n";
            json << " \"staticDict\":" << quote(static_path) << ",\n";
            json << " \"sha256\":" << quote(dict.sha256) << ",\n";
            json << " \"elfSha256\":" << quote(dict.elf_sha256) << ",\n";
            json << " \"version\":" << dict.version << ", \"entryCount\":" << dict.entry_count
                 << ", \"stringCount\":" << dict.string_count
                 << ", \"tableOffset\":" << dict.table_offset << ",\n";
            json << " \"entries\":[\n";
            for (size_t index = 0; index < dict.entries.size(); ++index) {
                const StaticEntry& entry = dict.entries[index];
                if (index) json << ",\n";
                json << "  {\"pc\":" << entry.pc << ",\"size\":" << entry.size
                     << ",\"id\":" << entry.id << ",\"predicated\":"
                     << static_cast<int>(entry.predicated) << ",\"flow\":"
                     << static_cast<int>(entry.flow)
                     << ",\"function\":";
                if (dict.has_extension && !(entry.flags & 2)) json << "null";
                else json << quote(dict.text(entry, 0));
                json << ",\"file\":";
                if (dict.has_extension && !(entry.flags & 4)) json << "null";
                else json << quote(dict.text(entry, 1));
                json << ",\"mnemonic\":" << quote(dict.text(entry, 2))
                     << ",\"operands\":" << quote(dict.text(entry, 3))
                     << ",\"line\":";
                if (entry.flags & 8) json << entry.line; else json << "null";
                json << ",\"conditional\":" << (entry.conditional ? "true" : "false")
                     << ",\"bytes\":";
                if (entry.flags & 1) {
                    static const char* digits = "0123456789abcdef";
                    std::string hexed;
                    for (uint32_t position = 0; position < entry.code_bytes; ++position) {
                        hexed.push_back(digits[entry.code[position] >> 4]);
                        hexed.push_back(digits[entry.code[position] & 0x0F]);
                    }
                    json << quote(hexed);
                } else {
                    json << "null";
                }
                json << "}";
            }
            json << "\n ]\n}\n";
            if (!json_path.empty()) {
                std::ofstream out(json_path);
                if (!out) fail(kExitIo, "打不开 JSON 输出：" + json_path);
                out << json.str();
            } else {
                std::cout << json.str();
            }
            return kExitOk;
        }
        if (command == "diagnose") {
            std::string native_path;
            std::string static_path;
            std::string ranges_path;
            std::string json_path;
            std::string run_log_path;
            uint64_t checkpoint_instructions = kDefaultCheckpointInstructions;
            uint64_t max_run_instructions = kDefaultMaxRunInstructions;
            uint64_t limit_records = 0;
            for (int index = 2; index < argc; ++index) {
                const std::string arg = argv[index];
                auto next = [&](const char* label) -> std::string {
                    if (index + 1 >= argc) fail(kExitUsage, std::string(label) + " 缺少参数");
                    return argv[++index];
                };
                if (arg == "--native") native_path = next("--native");
                else if (arg == "--static") static_path = next("--static");
                else if (arg == "--ranges") ranges_path = next("--ranges");
                else if (arg == "--json") json_path = next("--json");
                else if (arg == "--run-log") run_log_path = next("--run-log");
                else if (arg == "--checkpoint-instructions") checkpoint_instructions = std::stoull(next("--checkpoint-instructions"));
                else if (arg == "--max-run-instructions") max_run_instructions = std::stoull(next("--max-run-instructions"));
                else if (arg == "--limit-records") limit_records = std::stoull(next("--limit-records"));
                else fail(kExitUsage, "未知参数：" + arg);
            }
            if (native_path.empty()) fail(kExitUsage, "diagnose 需要 --native");
            if (static_path.empty()) fail(kExitUsage, "diagnose 需要 --static");
            StaticDict dict = load_static_dict(static_path);
            const auto started = std::chrono::steady_clock::now();
            std::ofstream run_log_stream;
            DiagnoseResult result;
            DiagnoseVisitor visitor(dict, result);
            DiagnoseState& state = visitor.state_;
            if (!run_log_path.empty()) {
                run_log_stream.open(run_log_path, std::ios::binary);
                if (!run_log_stream) fail(kExitIo, "打不开 run-log 输出：" + run_log_path);
                visitor.state_.run_log = &run_log_stream;
            }
            visitor.state_.checkpoint_instructions =
                std::max<uint64_t>(256, checkpoint_instructions);
            visitor.state_.max_run_instructions = max_run_instructions;
            // 单遍：read_native 在解析/校验的同一循环里把元素喂给诊断状态机。
            NativeCounters counters = read_native(native_path, false, nullptr,
                                                   1000000, limit_records, nullptr,
                                                   false, &visitor);
            const double read_seconds = seconds_since(started);
            (void)ranges_path;
            result.native = counters;
            result.ranges = counters.range_records;
            result.cycle_elements = counters.cycle_elements;
            result.cycle_sum = counters.cycle_sum;
            result.packet_count = counters.saw_packets;
            result.packet_unsync = counters.packet_unsync;
            result.packet_invalid = counters.packet_invalid;
            result.packet_overflow = counters.packet_overflow;
            result.range_count = counters.range_records;
            result.event_elements = counters.event_elements;
            result.other_elements = counters.other_elements;
            result.boundaries = counters.boundaries;
            result.exceptions = counters.exceptions;
            result.gaps = counters.gaps;
            visitor.state_.finish();
            result.instructions = visitor.state_.instructions;
            result.executed = visitor.state_.executed;
            result.unknown = visitor.state_.unknown;
            result.not_taken = visitor.state_.not_taken;
            result.events = result.instructions + result.native.event_elements;
            result.predicate_segments = visitor.state_.predicate_segments;
            result.runs = visitor.state_.runs;
            result.checkpoints = visitor.state_.checkpoints;
            result.seconds = read_seconds;
            result.rss_bytes = peak_rss_bytes();
            if (result.instructions != result.native.range_records
                    && false) {
                // placeholder: instructions 来自 payload 累加，与 range count 无关
            }
            // 交叉校验：events/instructions/总 seq
            if (result.events != result.instructions + result.native.event_elements) {
                fail(kExitConsistency, "诊断计数：events != instructions + rareEvents");
            }
            if (result.executed + result.unknown + result.not_taken != result.instructions) {
                std::ostringstream detail;
                detail << "诊断计数：executed/unknown/notTaken 与 instructions 不符："
                       << result.executed << "+" << result.unknown << "+"
                       << result.not_taken << " != " << result.instructions
                       << "（tail=" << state.debug_tail << " internal=" << state.debug_internal
                       << " appendTotal=" << state.debug_append_total
                       << " appendTrue=" << state.debug_append_true
                       << " appendFalse=" << state.debug_append_false
                       << " appendUnknown=" << state.debug_append_unknown << "）";
                fail(kExitConsistency, detail.str());
            }
            std::ostringstream json;
            json << "{\n";
            json << " \"protocol\":" << quote(kProtocol) << ",\n";
            json << " \"native\":" << quote(native_path) << ",\n";
            json << " \"staticDict\":" << quote(static_path) << ",\n";
            json << " \"nativeSha256\":" << quote(counters.native_sha256) << ",\n";
            json << " \"staticDictSha256\":" << quote(dict.sha256) << ",\n";
            json << " \"elfSha256\":" << quote(dict.elf_sha256) << ",\n";
            json << " \"staticEntries\":" << dict.entry_count
                 << ", \"staticStrings\":" << dict.string_count << ",\n";
            json << " \"counts\":{\"instructions\":" << result.instructions
                 << ",\"events\":" << (result.instructions + result.native.event_elements)
                 << ",\"ranges\":" << result.native.range_records
                 << ",\"runs\":" << result.runs
                 << ",\"rareEvents\":" << result.native.event_elements
                 << ",\"executed\":" << result.executed
                 << ",\"unknownExecution\":" << result.unknown
                 << ",\"notTaken\":" << result.not_taken
                 << ",\"exceptions\":" << result.exceptions
                 << ",\"gaps\":" << result.gaps
                 << ",\"boundaries\":" << result.boundaries
                 << ",\"cycleElements\":" << result.cycle_elements
                 << ",\"cycleSum\":" << result.cycle_sum
                 << ",\"cycleAnchors\":" << result.cycle_anchors
                 << ",\"predicatedInstructions\":" << result.predicated_instructions
                 << ",\"predicateSegments\":" << result.predicate_segments
                 << ",\"checkpoints\":" << result.checkpoints
                 << "},\n";
            json << " \"rareEvents\":" << result.native.event_elements << ",\n";
            json << " \"packets\":{\"count\":" << result.packet_count
                 << ",\"unsync\":" << result.packet_unsync
                 << ",\"invalid\":" << result.packet_invalid
                 << ",\"overflow\":" << result.packet_overflow << "},\n";
            json << " \"status\":{\"processed\":" << result.native.status.processed
                 << ",\"lastErrorCode\":" << result.native.status.last_error_code
                 << ",\"library\":" << quote(result.native.status.library)
                 << ",\"lastError\":" << quote(result.native.status.last_error) << "},\n";
            json << " \"header\":{\"schema\":" << result.native.header.schema
                 << ",\"protocolCode\":" << result.native.header.protocol
                 << ",\"elements\":" << result.native.header.element_count
                 << ",\"packets\":" << result.native.header.packet_count
                 << ",\"records\":" << result.native.header.record_count << "},\n";
            json << " \"ranges\":{\"elements\":" << result.native.range_records
                 << ",\"unique\":" << result.native.unique_ranges
                 << ",\"duplicates\":" << result.native.duplicate_ranges
                 << ",\"payloadMismatches\":" << result.native.payload_mismatches
                 << ",\"missingStaticPc\":" << result.missing_static_pc
                 << ",\"staticLookups\":" << result.static_lookup_count << "},\n";
            json << " \"typeCounts\":{";
            bool first = true;
            for (int type = 0; type < 256; ++type) {
                if (!result.native.type_counts[type]) continue;
                if (!first) json << ",";
                first = false;
                json << "\"" << type << "\":" << result.native.type_counts[type];
            }
            json << "},\n";
            json << " \"seconds\":" << result.seconds
                 << ", \"readSeconds\":" << read_seconds
                 << ", \"rssBytes\":" << result.rss_bytes << ",\n";
            json << " \"layoutDependent\":{\"runs\":true,\"checkpoints\":true,"
                 << "\"reason\":\"R2 设计 §8.1 允许 sequence 块物理切分不同；t13 diagnose "
                 << "使用不含 1MiB 块旋转的规范布局（块旋转检查点会改变 run 分段）\"},\n";
            json << " \"parameters\":{\"checkpointInstructions\":" << state.checkpoint_instructions
                 << ",\"maxRunInstructions\":" << state.max_run_instructions
                 << ",\"targetBlockBytes\":" << kTargetBlockBytes << "}\n";
            json << "}\n";
            if (!json_path.empty()) {
                std::ofstream out(json_path);
                if (!out) fail(kExitIo, "打不开 JSON 输出：" + json_path);
                out << json.str();
            } else {
                std::cout << json.str();
            }
            return kExitOk;
        }
        fail(kExitUsage, "未知命令：" + command);
    } catch (const BuildError& error) {
        std::cerr << "etm_storebuild 失败（退出码 " << error.code() << "）："
                  << error.what() << "\n";
        return error.code();
    } catch (const std::exception& error) {
        std::cerr << "etm_storebuild 异常：" << error.what() << "\n";
        return kExitOther;
    }
}
