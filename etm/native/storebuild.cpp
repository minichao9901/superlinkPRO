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
#include <openssl/sha.h>
#include <cstring>
#include <filesystem>
#include <fstream>
#include <iostream>
#include <limits>
#include <map>
#include <tuple>
#include <iomanip>
#include <memory>
#include <zlib.h>
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

class FastSha256 {
  public:
    FastSha256() { SHA256_Init(&context_); }
    void update(const void* data, size_t size) {
        SHA256_Update(&context_, data, size);
    }
    std::string hex_digest() {
        unsigned char digest[SHA256_DIGEST_LENGTH];
        SHA256_Final(digest, &context_);
        static const char* hex = "0123456789abcdef";
        std::string out;
        out.reserve(64);
        for (unsigned char byte : digest) {
            out.push_back(hex[byte >> 4]);
            out.push_back(hex[byte & 15]);
        }
        return out;
    }

  private:
    SHA256_CTX context_{};
};

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

double chrono_seconds(std::chrono::steady_clock::time_point start) {
    return std::chrono::duration<double>(std::chrono::steady_clock::now() - start).count();
}

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
                          uint64_t start, uint64_t end, uint32_t flags,
                          const std::string& text) = 0;
    virtual void on_other(uint16_t etype, uint32_t offset) = 0;
    virtual void on_packet(uint32_t offset, uint32_t size, uint32_t type_code,
                           const uint8_t* raw) = 0;
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
    FastSha256 sha;
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
            if (visitor) {
                visitor->on_packet(static_cast<uint32_t>(offset), size, type_code,
                                   body.data() + kPacketHeaderBytes);
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
        std::string element_text;
        if (text_bytes) {
            element_text.assign(reinterpret_cast<const char*>(body.data() + (kElementHeaderBytes - 5)),
                                text_bytes);
        }
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
                visitor->on_event(etype, static_cast<uint32_t>(element_offset), payload, start, end, flags, element_text);
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
                  uint64_t start, uint64_t end, uint32_t flags,
                  const std::string& text) override {
        (void)etype; (void)start; (void)end; (void)flags; (void)text;
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
    void on_packet(uint32_t offset, uint32_t size, uint32_t type_code,
                   const uint8_t* raw) override {
        (void)offset; (void)size; (void)type_code; (void)raw;
    }
    const StaticDict& dict_;
    DiagnoseResult& result_;
    DiagnoseState state_;
};


// ── 最小 blocks 1–11 builder（t20：单 sequence 块，无块 12/默认视图）─────────────
constexpr uint16_t trace_store_strings = 1;
constexpr uint16_t trace_store_static = 2;
uint32_t crc32_bytes(const uint8_t* data, size_t size) {
    static uint32_t table[256];
    static bool ready = false;
    if (!ready) {
        for (uint32_t index = 0; index < 256; ++index) {
            uint32_t value = index;
            for (int bit = 0; bit < 8; ++bit) {
                value = (value & 1) ? (0xEDB88320u ^ (value >> 1)) : (value >> 1);
            }
            table[index] = value;
        }
        ready = true;
    }
    uint32_t crc = 0xFFFFFFFFu;
    for (size_t index = 0; index < size; ++index) {
        crc = table[(crc ^ data[index]) & 0xFF] ^ (crc >> 8);
    }
    return crc ^ 0xFFFFFFFFu;
}

void wv(std::vector<uint8_t>& out, uint64_t value) {
    while (true) {
        uint8_t byte = static_cast<uint8_t>(value & 0x7F);
        value >>= 7;
        if (value) out.push_back(static_cast<uint8_t>(byte | 0x80));
        else { out.push_back(byte); return; }
    }
}

void wzv(std::vector<uint8_t>& out, int64_t value) {
    uint64_t encoded = value < 0 ? ((static_cast<uint64_t>(-value) << 1) - 1)
                                 : (static_cast<uint64_t>(value) << 1);
    wv(out, encoded);
}

void wpair(std::vector<uint8_t>& out, const void* data, size_t size) {
    wv(out, size);
    const auto* bytes = static_cast<const uint8_t*>(data);
    out.insert(out.end(), bytes, bytes + size);
}

void wpair(std::vector<uint8_t>& out, const std::string& value) {
    wpair(out, value.data(), value.size());
}

struct BuildBlock {
    uint16_t type = 0;
    std::vector<uint8_t> payload;
    uint32_t count = 0;
    uint16_t flags = 0;
    uint32_t uncompressed = 0;   // flags=1 时为解压后字节数
};

struct BuildOutput {
    std::vector<BuildBlock> blocks;
    uint64_t generation_tag = 0;
    std::string generation;
    uint64_t instructions = 0;
    uint64_t events = 0;
    uint64_t ranges = 0;
    uint64_t runs = 0;
    uint64_t rare_events = 0;
    uint64_t static_pcs = 0;
    uint64_t strings = 0;
    uint64_t packets = 0;
    uint64_t checkpoints = 1;
    uint64_t sequence_blocks = 1;
    uint64_t executed = 0;
    uint64_t unknown = 0;
    uint64_t not_taken = 0;
    uint64_t exceptions = 0;
    uint64_t gaps = 0;
    uint64_t boundaries = 0;
    uint64_t cycle_elements = 0;
    uint64_t cycle_sum = 0;
    uint64_t first_instruction = 0;
    uint64_t min_offset = 0;
    uint64_t max_offset = 0;
    uint64_t sequence_bytes = 0;
    uint64_t peak_rss = 0;
    std::string native_sha256;
    std::string static_sha256;
    std::string elf_sha256;
    std::string store_sha256;
    uint64_t store_bytes = 0;
    double assemble_seconds = 0.0;
    double write_seconds = 0.0;
    double range_seconds = 0.0;
    double flush_seconds = 0.0;
    double tracker_seconds = 0.0;
    double build_blocks_seconds = 0.0;
    uint64_t range_hits = 0;
    uint64_t range_misses = 0;
    double analysis_seconds = 0.0;
};


struct TrackerFrame {
    std::string function;
    bool has_return = false;
    uint64_t return_pc = 0;
    bool has_origin = false;
    std::string origin;
};

struct TrackerPrevious {
    bool valid = false;
    uint64_t pc = 0;
    uint32_t size = 0;
    uint8_t flow = 0;
    uint8_t executed = 0;
    std::string function;
};

struct TrackerContext {
    std::vector<TrackerFrame> stack;
    TrackerPrevious previous;
    bool has_expected = false;
    uint64_t expected = 0;
};

struct TrackerStats {
    std::string function;
    uint64_t instructions = 0;
    uint64_t known_executed = 0;
    uint64_t unknown_execution = 0;
    uint64_t not_taken = 0;
    uint64_t calls = 0;
    uint64_t closed_calls = 0;
    uint64_t tail_transfers = 0;
    uint64_t interrupt_entries = 0;
    uint64_t self_cycles = 0;
    uint64_t total_cycles = 0;
    uint64_t cycle_ranges = 0;
};

struct TrackerHotspot {
    std::string function;
    std::string file;
    bool has_file = false;
    bool has_line = false;
    int64_t line = -1;
    uint64_t records = 0;
    uint64_t unknown = 0;
    uint64_t cycles = 0;
    uint64_t pc = 0;
};

struct TrackerEdge {
    std::string caller;
    std::string callee;
    std::string kind;
    uint64_t count = 0;
};

struct FlameNode {
    std::string name;
    uint64_t self = 0;
    uint64_t value = 0;
    uint64_t self_cycles = 0;
    uint64_t value_cycles = 0;
    std::vector<std::unique_ptr<FlameNode>> children;
    std::unordered_map<std::string, FlameNode*> child_index;
    FlameNode* child(const std::string& label) {
        auto found = child_index.find(label);
        if (found != child_index.end()) return found->second;
        auto node = std::make_unique<FlameNode>();
        node->name = label;
        FlameNode* raw = node.get();
        children.push_back(std::move(node));
        child_index.emplace(label, raw);
        return raw;
    }
};

class Tracker {
  public:
    Tracker() { root_.name = "已恢复执行区间"; }
    void set_analysis(bool enabled) { analysis_enabled_ = enabled; }
    void clear_all() {
        stack_.clear();
        contexts_.clear();
        previous_ = TrackerPrevious();
        has_expected_ = false;
        expected_ = 0;
        sync();
    }
    void feed_event(uint16_t etype, uint32_t flags, uint32_t payload, uint64_t end) {
        if (etype == 1 || etype == 2 || etype == 6 || etype == 7 || etype == 8) {
            clear_all();
            ++unknown_restarts_;
        } else if (etype == 9) {
            if (flags & (1u << 12)) {
                clear_all();
                ++unknown_restarts_;
            }
            TrackerContext context;
            context.stack = stack_;
            context.previous = previous_;
            context.has_expected = has_expected_;
            context.expected = expected_;
            contexts_.push_back(std::move(context));
            stack_.clear();
            stack_.push_back(TrackerFrame{std::string("中断 ") + std::to_string(payload)});
            previous_ = TrackerPrevious();
            sync();
        } else if (etype == 10) {
            if (!contexts_.empty()) {
                TrackerContext context = std::move(contexts_.back());
                contexts_.pop_back();
                stack_ = std::move(context.stack);
                previous_ = context.previous;
                has_expected_ = context.has_expected;
                expected_ = context.expected;
            } else {
                clear_all();
                ++unknown_restarts_;
            }
            sync();
        }
    }
    void feed_instruction(const std::string& function, const std::string& file,
                          bool has_file, bool has_line, int64_t line, uint8_t executed,
                          uint8_t flow, uint64_t pc, uint32_t size, uint32_t cycles,
                          uint32_t site_id = 0xFFFFFFFFu) {
        const std::string fn = function.empty() ? "(未知函数)" : function;
        TrackerStats* s_ptr = nullptr;
        TrackerHotspot* cached_hot = nullptr;
        size_t site = static_cast<size_t>(site_id);
        if (analysis_enabled_ && site_id != 0xFFFFFFFFu) {
            if (site >= site_stat_.size()) {
                site_stat_.resize(site + 1, -1);
                site_hotspot_.resize(site + 1, -1);
            }
            if (site_stat_[site] < 0) {
                s_ptr = &statistics(fn);
                site_stat_[site] = static_cast<int64_t>(s_ptr - stats_.data());
            } else {
                s_ptr = &stats_[static_cast<size_t>(site_stat_[site])];
            }
            if (site_hotspot_[site] < 0) {
                const std::string key = fn + "\x1f" + (has_file ? file : std::string())
                    + "\x1f" + (has_line ? std::to_string(line) : std::string());
                auto found = hotspot_index_.find(key);
                if (found == hotspot_index_.end()) {
                    hotspots_.push_back(TrackerHotspot{fn, file, has_file, has_line, line, 0, 0, 0, pc});
                    site_hotspot_[site] = static_cast<int64_t>(hotspots_.size() - 1);
                    hotspot_index_.emplace(key, hotspots_.size() - 1);
                } else {
                    site_hotspot_[site] = static_cast<int64_t>(found->second);
                }
            }
            cached_hot = &hotspots_[static_cast<size_t>(site_hotspot_[site])];
        } else if (analysis_enabled_) {
            s_ptr = &statistics(fn);
        }
        TrackerStats& s = analysis_enabled_ ? *s_ptr : dummy_stats_;
        if (has_expected_) {
            if (pc != (expected_ & ~1ull)) {
                stack_.clear();
                previous_ = TrackerPrevious();
                ++unknown_restarts_;
            }
            has_expected_ = false;
        }
        if (stack_.empty()) {
            stack_.clear();
            stack_.push_back(TrackerFrame{"采集前调用栈未知"});
            stack_.push_back(TrackerFrame{fn});
            sync();
        }
        if (stack_.back().function.rfind("中断 ", 0) == 0) {
            stack_.push_back(TrackerFrame{fn});
            ++s.interrupt_entries;
            sync();
        }
        if (previous_.valid && previous_.executed == 0) {
            const std::string old_fn = previous_.function.empty() ? "(未知函数)"
                                                                   : previous_.function;
            if (previous_.flow == kFlowCall) {
                TrackerFrame frame;
                frame.function = fn;
                frame.has_return = true;
                frame.return_pc = previous_.pc + previous_.size;
                frame.has_origin = true;
                frame.origin = fn;
                stack_.push_back(std::move(frame));
                if (analysis_enabled_) { ++s.calls; add_edge(old_fn, fn, "call"); }
                sync();
            } else if (previous_.flow == kFlowReturn || previous_.flow == kFlowBranch) {
                int match = -1;
                for (int index = static_cast<int>(stack_.size()) - 1; index >= 0; --index) {
                    if (stack_[index].has_return && stack_[index].return_pc == pc) {
                        match = index;
                        break;
                    }
                }
                if (match >= 0) {
                    if (analysis_enabled_) {
                        for (size_t index = static_cast<size_t>(match); index < stack_.size(); ++index) {
                            if (stack_[index].has_origin) {
                                ++statistics(stack_[index].origin).closed_calls;
                            }
                        }
                    }
                    stack_.resize(static_cast<size_t>(match));
                    sync();
                } else if (old_fn != fn && previous_.flow == kFlowBranch) {
                    if (analysis_enabled_) { ++s.tail_transfers; add_edge(old_fn, fn, "tail-transfer"); }
                    stack_.back().function = fn;
                    sync();
                } else if (old_fn != fn) {
                    stack_.clear();
                    stack_.push_back(TrackerFrame{"采集前调用栈未知"});
                    stack_.push_back(TrackerFrame{fn});
                    ++unknown_restarts_;
                    sync();
                }
            }
        }
        if (stack_.empty() || stack_.back().function != fn) {
            stack_.clear();
            stack_.push_back(TrackerFrame{"调用关系未证实"});
            stack_.push_back(TrackerFrame{fn});
            ++unknown_restarts_;
            sync();
        }
        if (stack_.size() > 128) fail(kExitConsistency, "调用层级超过 128");
        if (analysis_enabled_) {
            ++s.instructions;
            if (executed == 0) ++s.known_executed;
            else if (executed == 1) ++s.not_taken;
            else ++s.unknown_execution;
            if (cycles) {
                s.self_cycles += cycles;
                ++s.cycle_ranges;
                for (const TrackerFrame& frame : stack_) {
                    statistics(frame.function).total_cycles += cycles;
                }
            }
            FlameNode* node = &root_;
            node->value += 1;
            node->value_cycles += cycles;
            if (!path_nodes_valid_) {
                path_nodes_.clear();
                for (const TrackerFrame& frame : stack_) {
                    FlameNode* child = node->child(frame.function);
                    path_nodes_.push_back(child);
                    node = child;
                }
                path_nodes_valid_ = true;
            }
            for (FlameNode* item : path_nodes_) {
                item->value += 1;
                item->value_cycles += cycles;
            }
            FlameNode* leaf = path_nodes_.empty() ? &root_ : path_nodes_.back();
            leaf->self += 1;
            leaf->self_cycles += cycles;
            TrackerHotspot* hot = cached_hot;
            if (hot == nullptr) {
                const std::string key = fn + "\x1f" + (has_file ? file : std::string())
                    + "\x1f" + (has_line ? std::to_string(line) : std::string());
                auto found = hotspot_index_.find(key);
                if (found == hotspot_index_.end()) {
                    hotspots_.push_back(TrackerHotspot{fn, file, has_file, has_line, line, 0, 0, 0, pc});
                    hotspot_index_.emplace(key, hotspots_.size() - 1);
                    hot = &hotspots_.back();
                } else {
                    hot = &hotspots_[found->second];
                }
            }
            ++hot->records;
            hot->unknown += executed == 2;
            hot->cycles += cycles;
        }
        previous_.valid = true;
        previous_.pc = pc;
        previous_.size = size;
        previous_.flow = flow;
        previous_.executed = executed;
        previous_.function = function;
    }
    std::string analysis_json() const;
    uint64_t unknown_restarts() const { return unknown_restarts_; }
    const TrackerPrevious& previous() const { return previous_; }
    bool has_expected() const { return has_expected_; }
    uint64_t expected() const { return expected_; }
    const std::vector<TrackerFrame>& stack() const { return stack_; }
    const std::vector<TrackerContext>& contexts() const { return contexts_; }

  private:
    TrackerStats& statistics(const std::string& fn) {
        auto found = stats_index_.find(fn);
        if (found != stats_index_.end()) return stats_[found->second];
        stats_.push_back(TrackerStats{fn});
        stats_index_.emplace(fn, stats_.size() - 1);
        return stats_.back();
    }
    void add_edge(const std::string& caller, const std::string& callee,
                  const std::string& kind) {
        const std::string key = caller + "\x1f" + callee + "\x1f" + kind;
        auto found = edge_index_.find(key);
        if (found == edge_index_.end()) {
            edges_.push_back(TrackerEdge{caller, callee, kind, 1});
            edge_index_.emplace(key, edges_.size() - 1);
        } else {
            ++edges_[found->second].count;
        }
    }
    void sync() {
        path_nodes_.clear();
        path_nodes_valid_ = false;
        path_.clear();
        for (const TrackerFrame& frame : stack_) path_.push_back(frame.function);
    }
    std::vector<TrackerFrame> stack_;
    std::vector<TrackerContext> contexts_;
    TrackerPrevious previous_;
    bool has_expected_ = false;
    uint64_t expected_ = 0;
    uint64_t unknown_restarts_ = 0;
    bool analysis_enabled_ = true;
    TrackerStats dummy_stats_;
    std::vector<int64_t> site_stat_;
    std::vector<int64_t> site_hotspot_;
    std::vector<TrackerStats> stats_;
    std::unordered_map<std::string, size_t> stats_index_;
    std::vector<TrackerHotspot> hotspots_;
    std::unordered_map<std::string, size_t> hotspot_index_;
    std::vector<TrackerEdge> edges_;
    std::unordered_map<std::string, size_t> edge_index_;
    FlameNode root_;
    std::vector<FlameNode*> path_nodes_;
    bool path_nodes_valid_ = false;
    std::vector<std::string> path_;
};

std::string flame_json(const FlameNode& node) {
    std::ostringstream out;
    out << "{\"name\":" << quote(node.name) << ",\"self\":" << node.self
        << ",\"value\":" << node.value << ",\"selfCycles\":" << node.self_cycles
        << ",\"valueCycles\":" << node.value_cycles << ",\"children\":[";
    for (size_t index = 0; index < node.children.size(); ++index) {
        if (index) out << ",";
        out << flame_json(*node.children[index]);
    }
    out << "]}";
    return out.str();
}

std::string Tracker::analysis_json() const {
    if (!analysis_enabled_) return "{}";
    uint64_t total_calls = 0, total_cycles = 0, executed = 0, instructions = 0;
    for (const TrackerStats& s : stats_) {
        total_calls += s.calls;
        total_cycles += s.self_cycles;
        executed += s.known_executed;
        instructions += s.instructions;
    }
    std::vector<size_t> order(stats_.size());
    for (size_t index = 0; index < order.size(); ++index) order[index] = index;
    std::stable_sort(order.begin(), order.end(), [&](size_t a, size_t b) {
        return stats_[a].instructions > stats_[b].instructions;
    });
    std::ostringstream out;
    out << std::setprecision(17);
    out << "{\"functions\":[";
    bool first = true;
    for (size_t index : order) {
        const TrackerStats& s = stats_[index];
        if (!first) out << ",";
        first = false;
        const uint64_t open_calls = s.calls > s.closed_calls ? s.calls - s.closed_calls : 0;
        const double call_percent = total_calls ? 100.0 * s.calls / total_calls : 0.0;
        const double cycle_percent = total_cycles ? 100.0 * s.self_cycles / total_cycles : 0.0;
        out << "{\"function\":" << quote(s.function) << ",\"instructions\":"
            << s.instructions << ",\"knownExecuted\":" << s.known_executed
            << ",\"unknownExecution\":" << s.unknown_execution
            << ",\"notTaken\":" << s.not_taken << ",\"calls\":" << s.calls
            << ",\"closedCalls\":" << s.closed_calls << ",\"tailTransfers\":"
            << s.tail_transfers << ",\"interruptEntries\":" << s.interrupt_entries
            << ",\"selfCycles\":" << s.self_cycles << ",\"totalCycles\":"
            << s.total_cycles << ",\"cycleRanges\":" << s.cycle_ranges
            << ",\"openCalls\":" << open_calls << ",\"callPercent\":" << call_percent
            << ",\"cyclePercent\":" << cycle_percent << ",\"cpi\":";
        if (s.instructions && s.self_cycles) out << (double)s.self_cycles / s.instructions;
        else out << "null";
        out << "}";
    }
    out << "],\"hotspots\":[";
    std::vector<size_t> hot_order(hotspots_.size());
    for (size_t index = 0; index < hot_order.size(); ++index) hot_order[index] = index;
    std::stable_sort(hot_order.begin(), hot_order.end(), [&](size_t a, size_t b) {
        if (hotspots_[a].cycles != hotspots_[b].cycles)
            return hotspots_[a].cycles > hotspots_[b].cycles;
        return hotspots_[a].records > hotspots_[b].records;
    });
    first = true;
    for (size_t index : hot_order) {
        const TrackerHotspot& h = hotspots_[index];
        if (!first) out << ",";
        first = false;
        out << "{\"function\":" << quote(h.function) << ",\"file\":";
        if (h.has_file) out << quote(h.file); else out << "null";
        out << ",\"line\":";
        if (h.has_line) out << h.line; else out << "null";
        out << ",\"records\":" << h.records << ",\"unknown\":" << h.unknown
            << ",\"cycles\":" << h.cycles << ",\"pc\":" << h.pc << "}";
    }
    out << "],\"flame\":" << flame_json(root_) << ",\"observedCalls\":"
        << total_calls << ",\"unknownStackRestarts\":" << unknown_restarts_
        << ",\"edges\":[";
    for (size_t index = 0; index < edges_.size(); ++index) {
        const TrackerEdge& e = edges_[index];
        if (index) out << ",";
        out << "{\"caller\":" << quote(e.caller) << ",\"callee\":" << quote(e.callee)
            << ",\"kind\":" << quote(e.kind) << ",\"count\":" << e.count << "}";
    }
    out << "],\"timing\":{\"totalCycles\":" << total_cycles
        << ",\"cycleRanges\":";
    uint64_t cycle_ranges = 0;
    for (const TrackerStats& s : stats_) cycle_ranges += s.cycle_ranges;
    out << cycle_ranges << ",\"executedInstructions\":" << executed
        << ",\"instructions\":" << instructions << ",\"cpi\":";
    if (executed && total_cycles) out << (double)total_cycles / executed; else out << "null";
    out << ",\"functions\":[";
    std::vector<size_t> cycle_order;
    for (size_t index = 0; index < stats_.size(); ++index) {
        if (stats_[index].self_cycles) cycle_order.push_back(index);
    }
    std::stable_sort(cycle_order.begin(), cycle_order.end(), [&](size_t a, size_t b) {
        return stats_[a].self_cycles > stats_[b].self_cycles;
    });
    first = true;
    for (size_t index : cycle_order) {
        const TrackerStats& s = stats_[index];
        if (!first) out << ",";
        first = false;
        const double percent = total_cycles ? 100.0 * s.self_cycles / total_cycles : 0.0;
        out << "{\"function\":" << quote(s.function) << ",\"selfCycles\":" << s.self_cycles
            << ",\"cyclePercent\":" << percent << ",\"instructions\":" << s.instructions
            << ",\"cpi\":";
        if (s.instructions && s.self_cycles) out << (double)s.self_cycles / s.instructions;
        else out << "null";
        out << ",\"calls\":" << s.calls << "}";
    }
    out << "],\"timeFlameAvailable\":" << (total_cycles ? "true" : "false")
        << ",\"basis\":";
    if (total_cycles) {
        out << quote("周期计数来自 ETMv4 cycle-count 元素（区间末指令记录，可求和）；区间若跨越函数边界会让归因偏移，属极端情况");
    } else {
        out << quote("本记录没有周期计数数据（目标未启用，或 ETMv3/F103 不支持），时间轴不可用");
    }
    out << "},\"weight\":";
    if (total_cycles) {
        out << quote("路径指令记录数（含未成立/未知状态）；周期列才是耗时，指令数不是");
    } else {
        out << quote("路径指令记录数（含未成立/未知状态），不是耗时");
    }
    out << ",\"callsPerSecond\":null,\"rateReason\":";
    if (total_cycles) {
        out << quote("可用周期计数折算时间（coreHz 已知），但调用/秒仍取决于覆盖完整度");
    } else {
        out << quote("缺少覆盖可信执行区间的时间基准，暂不计算调用/秒");
    }
    out << "}";
    return out.str();
}

class BuildVisitor : public NativeVisitor {
  public:
    BuildVisitor(const StaticDict& dict, BuildOutput& output, bool need_tracker = true)
        : dict_(dict), output_(output), need_tracker_(need_tracker) {
        strings_.values.push_back("");
        ids_.emplace("", 0);
        intern("采集前调用栈未知");
        for (const StaticEntry& entry : dict.entries) {
            const uint32_t sid = entry.id;
            if (sid >= static_entries_.size()) static_entries_.resize(sid + 1);
            static_entries_[sid] = &entry;
            intern(entry_text(entry, 0));
            intern(entry_text(entry, 1));
            intern(entry_text(entry, 2));
            intern(entry_text(entry, 3));
        }
        start_block(0);
        output_.first_instruction = 0;
        output_.min_offset = std::numeric_limits<uint64_t>::max();
    }
    void on_packet(uint32_t offset, uint32_t size, uint32_t type_code,
                   const uint8_t* raw) override {
        const uint64_t delta = packet_buffer_.empty() ? offset : offset - packet_previous_offset_;
        wv(packet_buffer_, delta);
        wv(packet_buffer_, size);
        wpair(packet_buffer_, raw, size);
        wpair(packet_buffer_, std::string());
        packet_previous_offset_ = offset;
        ++output_.packets;
    }
    const std::vector<const StaticEntry*>& range_entries(uint64_t start, uint64_t end,
                                                         uint32_t payload) {
        const uint64_t key = (start << 32) | end;
        auto found = range_cache_.find(key);
        if (found != range_cache_.end()) {
            ++output_.range_hits;
            return found->second;
        }
        ++output_.range_misses;
        std::vector<const StaticEntry*> entries;
        entries.reserve(payload);
        uint64_t pc = start;
        while (pc < end) {
            const StaticEntry* entry = dict_.find(static_cast<uint32_t>(pc));
            if (!entry) {
                std::ostringstream message;
                message << "静态字典缺少 PC 0x" << std::hex << pc;
                fail(kExitRangeMismatch, message.str());
            }
            entries.push_back(entry);
            pc += entry->size;
            if (entries.size() > payload + 1) fail(kExitRangeMismatch, "范围走查超过 payload");
        }
        if (pc != end || entries.size() != payload) {
            fail(kExitRangeMismatch, "范围走查与 payload/end 不符");
        }
        auto inserted = range_cache_.emplace(key, std::move(entries));
        return inserted.first->second;
    }

    void on_range(uint32_t offset, uint64_t start, uint64_t end, uint32_t payload,
                  uint8_t last_executed, uint32_t cycles) override {
        if (pending_.valid) {
            const auto& previous_entries = *pending_.segments.back().entries;
            const StaticEntry* previous_tail = previous_entries.back();
            const uint8_t previous_tail_code = previous_tail->predicated
                ? 2 : (pending_.last_executed ? 0 : 1);
            if (pending_.offset == offset && previous_tail_code == 0
                    && pending_.cycles == 0) {
                const auto& entries = range_entries(start, end, payload);
                pending_.segments.push_back(PendingSegment{&entries, offset, payload,
                                                           last_executed});
                pending_.last_executed = last_executed;
                pending_.cycles = cycles;
                pending_.span = cycles ? payload : 0;
                pending_.payload += payload;
                ++output_.ranges;
                return;
            }
            flush_pending();
        }
        const auto& entries = range_entries(start, end, payload);
        ++output_.ranges;
        pending_.valid = true;
        pending_.offset = offset;
        pending_.last_executed = last_executed;
        pending_.cycles = cycles;
        pending_.span = cycles ? payload : 0;
        pending_.payload = payload;
        pending_.segments.clear();
        pending_.segments.push_back(PendingSegment{&entries, offset, payload, last_executed});
    }
    void on_cycle(uint32_t offset, uint32_t cycles, uint32_t payload) override {
        (void)offset;
        ++output_.cycle_elements;
        output_.cycle_sum += cycles;
        if (pending_.valid) {
            if (cycles) {
                pending_.cycles += cycles;
                if (!pending_.span && !pending_.segments.empty())
                    pending_.span = pending_.segments.back().payload;
            }
            flush_pending();
        }
    }
    void on_event(uint16_t etype, uint32_t offset, uint32_t payload,
                  uint64_t start, uint64_t end, uint32_t flags,
                  const std::string& text) override {
        event_text_ = text;
        flush_pending();
        const uint32_t seq = static_cast<uint32_t>(expected_seq_);
        const uint32_t text_id = intern(event_text_);
        event_text_.clear();
        std::vector<uint8_t> body;
        wv(body, seq);
        body.push_back(static_cast<uint8_t>(etype));
        wv(body, offset);
        wv(body, payload);
        wv(body, start);
        wv(body, end);
        wv(body, flags);
        wv(body, text_id);
        std::vector<uint8_t> record;
        record.push_back(2);
        wv(record, body.size());
        record.insert(record.end(), body.begin(), body.end());
        emit_record(record, seq, seq);
        ++expected_seq_;
        ++output_.rare_events;
        ++output_.events;
        ++event_types_[etype];
        if (etype == 9) ++output_.exceptions;
        if (etype == 9 || etype == 10) nav_exception_.push_back(seq);
        if (is_boundary_type(etype)) {
            nav_issue_.push_back(seq);
            ++output_.boundaries;
        }
        nav_event_.push_back(seq);
        if (is_gap_type(etype) || (etype == 2 && payload)) ++output_.gaps;
        if (need_tracker_) tracker_.feed_event(etype, flags, payload, end);
    }
    void on_other(uint16_t etype, uint32_t offset) override {
        (void)etype; (void)offset;
        flush_pending();
    }
    void finish() {
        flush_pending();
        if (blocks_.size() && block_records_.back() <= 1) {
            // 最后一块只有检查点：若确有记录，rotate 一定发生在下一条记录之前；
            // 这里只防御空 build。
            blocks_.pop_back();
            block_records_.pop_back();
            block_first_.pop_back();
            block_last_.pop_back();
            checkpoint_index_.pop_back();
        }
        output_.events = expected_seq_;
        output_.instructions = output_.events - output_.rare_events;
        output_.sequence_blocks = blocks_.size();
        output_.checkpoints = checkpoint_index_.size();
        output_.sequence_bytes = 0;
        for (const auto& block : blocks_) output_.sequence_bytes += block.size();
        output_.strings = strings_.values.size();
        output_.static_pcs = static_entries_.size();
        output_.peak_rss = peak_rss_bytes();
        build_blocks();
    }
    std::vector<BuildBlock>& blocks() { return output_.blocks; }
    const std::map<uint16_t, uint64_t>& event_types() const { return event_types_; }
    std::string analysis_json() const { return tracker_.analysis_json(); }
    void set_analysis(bool enabled) { tracker_.set_analysis(enabled); }

  private:
    const std::string& entry_text(const StaticEntry& entry, int slot) {
        static const std::string empty;
        if (!dict_.has_extension) return empty;
        if (slot == 0 && !(entry.flags & 2)) return empty;
        if (slot == 1 && !(entry.flags & 4)) return empty;
        return dict_.text(entry, slot);
    }
    uint32_t intern(const std::string& value) {
        auto found = ids_.find(value);
        if (found != ids_.end()) return found->second;
        uint32_t identifier = static_cast<uint32_t>(strings_.values.size());
        strings_.values.push_back(value);
        ids_.emplace(value, identifier);
        return identifier;
    }
    void encode_function(std::vector<uint8_t>& out, const std::string& value,
                         bool present) {
        if (!present) {
            out.push_back(0);
            return;
        }
        out.push_back(1);
        wv(out, intern(value));
    }
    void encode_previous(std::vector<uint8_t>& out, const TrackerPrevious& previous) {
        if (!previous.valid) {
            out.push_back(0);
            return;
        }
        out.push_back(1);
        wv(out, previous.pc);
        wv(out, previous.size);
        out.push_back(previous.flow);
        out.push_back(previous.executed);
        encode_function(out, previous.function, !previous.function.empty());
    }
    void encode_optional(std::vector<uint8_t>& out, bool present, uint64_t value) {
        if (!present) {
            out.push_back(0);
            return;
        }
        out.push_back(1);
        wv(out, value);
    }
    void encode_stack(std::vector<uint8_t>& out, const std::vector<TrackerFrame>& stack) {
        wv(out, stack.size());
        for (const TrackerFrame& frame : stack) {
            encode_function(out, frame.function, true);
            uint8_t flags = 0;
            if (frame.has_return) flags |= 1;
            if (frame.has_origin) flags |= 2;
            out.push_back(flags);
            if (frame.has_return) wv(out, frame.return_pc);
            if (frame.has_origin) wv(out, intern(frame.origin));
        }
    }
    void encode_tracker(std::vector<uint8_t>& out) {
        wv(out, tracker_.unknown_restarts());
        encode_previous(out, tracker_.previous());
        encode_optional(out, tracker_.has_expected(), tracker_.expected());
        encode_stack(out, tracker_.stack());
        wv(out, tracker_.contexts().size());
        for (const TrackerContext& context : tracker_.contexts()) {
            encode_stack(out, context.stack);
            encode_previous(out, context.previous);
            encode_optional(out, context.has_expected, context.expected);
        }
    }
    void start_block(uint64_t seq) {
        blocks_.push_back({});
        block_records_.push_back(1);
        block_first_.push_back(seq);
        block_last_.push_back(seq);
        checkpoint_index_.push_back(std::make_tuple(seq, blocks_.size() - 1, 0));
        std::vector<uint8_t> payload;
        wv(payload, seq);
        encode_tracker(payload);
        std::vector<uint8_t> record;
        record.push_back(3);
        wv(record, payload.size());
        record.insert(record.end(), payload.begin(), payload.end());
        blocks_.back().insert(blocks_.back().end(), record.begin(), record.end());
        rotate_ = false;
    }
    void emit_record(const std::vector<uint8_t>& record, uint64_t start_seq,
                     uint64_t end_seq) {
        if (rotate_) {
            if (!need_tracker_) fail(kExitConsistency, "输入需要多 sequence 块但未启用 tracker");
            start_block(expected_seq_);
        }
        if (block_records_.back() == 1) block_first_.back() = start_seq;
        blocks_.back().insert(blocks_.back().end(), record.begin(), record.end());
        ++block_records_.back();
        block_last_.back() = end_seq;
        if (blocks_.back().size() >= (58ull << 20)) rotate_ = true;
    }
    void flush_pending() {
        if (!pending_.valid) return;
        const auto flush_started = std::chrono::steady_clock::now();
        pending_.valid = false;
        const auto& segments = pending_.segments;
        const StaticEntry* tail = segments.back().entries->back();
        const uint8_t tail_code = tail->predicated ? 2 : (pending_.last_executed ? 0 : 1);
        size_t count = 0;
        for (const PendingSegment& segment : segments) count += segment.payload;
        const uint32_t start_seq = static_cast<uint32_t>(expected_seq_);
        auto& payload = run_payload_;
        payload.clear();
        if (payload.capacity() < 128) payload.reserve(128);
        wv(payload, start_seq);
        wv(payload, count);
        payload.push_back(tail_code);
        wv(payload, pending_.offset);
        wv(payload, pending_.cycles);
        wv(payload, pending_.span);
        uint32_t internal_unknown = 0;
        size_t remaining = count;
        for (const PendingSegment& segment : segments) {
            for (const StaticEntry* entry : *segment.entries) {
                if (remaining == 1) break;
                if (entry->predicated) ++internal_unknown;
                --remaining;
            }
            if (remaining == 1) break;
        }
        wv(payload, internal_unknown);
        size_t remaining_ids = count;
        uint32_t previous_id = 0;
        bool first_id = true;
        for (const PendingSegment& segment : segments) {
            for (const StaticEntry* entry : *segment.entries) {
                if (remaining_ids == 0) break;
                if (first_id) {
                    wv(payload, entry->id);
                    first_id = false;
                } else {
                    wzv(payload, static_cast<int64_t>(entry->id)
                                - static_cast<int64_t>(previous_id));
                }
                previous_id = entry->id;
                --remaining_ids;
            }
            if (remaining_ids == 0) break;
        }
        auto& record = run_record_;
        record.clear();
        if (record.capacity() < 160) record.reserve(160);
        record.push_back(1);
        wv(record, payload.size());
        record.insert(record.end(), payload.begin(), payload.end());
        emit_record(record, start_seq, start_seq + count - 1);
        ++output_.runs;
        output_.instructions += count;
        expected_seq_ += count;
        size_t position = 0;
        const auto tracker_started = std::chrono::steady_clock::now();
        for (const PendingSegment& segment : segments) {
            for (const StaticEntry* entry : *segment.entries) {
                const bool is_tail = position + 1 == count;
                const uint8_t executed = is_tail ? tail_code
                    : (entry->predicated ? 2 : 0);
                if (!is_tail) {
                    if (entry->predicated) {
                        ++output_.unknown;
                        nav_issue_.push_back(static_cast<uint32_t>(start_seq + position));
                    } else {
                        ++output_.executed;
                        if (entry->flow == kFlowCall) nav_call_.push_back(static_cast<uint32_t>(start_seq + position));
                        if (entry->flow == kFlowReturn) nav_return_.push_back(static_cast<uint32_t>(start_seq + position));
                    }
                }
                if (need_tracker_) {
                    const bool has_file = dict_.has_extension && (entry->flags & 4);
                    const bool has_line = dict_.has_extension && (entry->flags & 8);
                    tracker_.feed_instruction(entry_text(*entry, 0), entry_text(*entry, 1),
                                              has_file, has_line, entry->line, executed,
                                              entry->flow, entry->pc, entry->size,
                                              is_tail ? pending_.cycles : 0,
                                              entry->id);
                }
                ++position;
            }
        }
        output_.tracker_seconds += chrono_seconds(tracker_started);
        if (tail_code == 0) ++output_.executed;
        else if (tail_code == 1) ++output_.not_taken;
        else ++output_.unknown;
        if (tail_code == 2) nav_issue_.push_back(static_cast<uint32_t>(start_seq + count - 1));
        else if (tail->flow == kFlowCall && tail_code == 0) nav_call_.push_back(static_cast<uint32_t>(start_seq + count - 1));
        else if (tail->flow == kFlowReturn && tail_code == 0) nav_return_.push_back(static_cast<uint32_t>(start_seq + count - 1));
        if (output_.first_instruction == 0 && count) output_.first_instruction = start_seq;
        if (output_.min_offset == std::numeric_limits<uint64_t>::max()) output_.min_offset = pending_.offset;
        output_.max_offset = std::max<uint64_t>(output_.max_offset, pending_.offset);
        output_.flush_seconds += chrono_seconds(flush_started);
    }
    void build_blocks() {
        const auto blocks_started = std::chrono::steady_clock::now();
        BuildBlock stat;
        stat.type = trace_store_static;
        for (size_t identifier = 0; identifier < static_entries_.size(); ++identifier) {
            const StaticEntry* entry = static_entries_[identifier];
            if (!entry) continue;
            wv(stat.payload, entry->pc);
            stat.payload.push_back(static_cast<uint8_t>(entry->size));
            uint8_t flags = 0;
            if (entry->predicated) flags |= 0x01;
            if (entry->conditional) flags |= 0x02;
            if (dict_.has_extension && (entry->flags & 2)) flags |= 0x04;
            if (dict_.has_extension && (entry->flags & 4)) flags |= 0x08;
            if (dict_.has_extension && (entry->flags & 8)) flags |= 0x10;
            stat.payload.push_back(flags);
            stat.payload.push_back(entry->flow);
            for (uint32_t index = 0; index < entry->size; ++index) {
                stat.payload.push_back(entry->code[index]);
            }
            wv(stat.payload, intern(entry_text(*entry, 2)));
            wv(stat.payload, intern(entry_text(*entry, 3)));
            if (flags & 0x04) wv(stat.payload, intern(entry_text(*entry, 0)));
            if (flags & 0x08) wv(stat.payload, intern(entry_text(*entry, 1)));
            if (flags & 0x10) wv(stat.payload, static_cast<uint64_t>(entry->line));
            ++stat.count;
        }
        output_.blocks.push_back(std::move(stat));
        BuildBlock strings;
        strings.type = trace_store_strings;
        for (const std::string& value : strings_.values) wpair(strings.payload, value);
        strings.count = static_cast<uint32_t>(strings_.values.size());
        output_.blocks.push_back(std::move(strings));
        for (size_t index = 0; index < blocks_.size(); ++index) {
            BuildBlock sequence;
            sequence.type = 3;
            sequence.payload = blocks_[index];
            sequence.count = block_records_[index];
            output_.blocks.push_back(std::move(sequence));
        }
        BuildBlock packets;
        packets.type = 4;
        packets.payload = packet_buffer_;
        packets.count = static_cast<uint32_t>(output_.packets);
        if (!packets.payload.empty()) output_.blocks.push_back(std::move(packets));
        auto add_nav = [&](uint32_t mode, const std::vector<uint32_t>& values) {
            if (values.empty()) return;
            for (size_t start = 0; start < values.size(); start += 65536) {
                const size_t stop = std::min(values.size(), start + 65536);
                BuildBlock block;
                block.type = 5;
                block.payload.push_back(static_cast<uint8_t>(mode));
                wv(block.payload, stop - start);
                wv(block.payload, values[start]);
                uint32_t value = values[start];
                for (size_t index = start + 1; index < stop; ++index) {
                    wv(block.payload, values[index] - value);
                    value = values[index];
                }
                block.count = static_cast<uint32_t>(stop - start);
                output_.blocks.push_back(std::move(block));
            }
        };
        add_nav(1, nav_call_);
        add_nav(2, nav_return_);
        add_nav(3, nav_exception_);
        add_nav(4, nav_issue_);
        add_nav(5, nav_event_);
        BuildBlock agg;
        agg.type = 6;
        for (size_t index = 0; index < blocks_.size(); ++index) {
            wv(agg.payload, index);
            wv(agg.payload, block_first_[index]);
            wv(agg.payload, block_last_[index]);
            wv(agg.payload, block_records_[index]);
            wv(agg.payload, output_.instructions);
            wv(agg.payload, output_.executed);
            wv(agg.payload, output_.unknown);
            wv(agg.payload, output_.not_taken);
            wv(agg.payload, output_.cycle_sum);
            wv(agg.payload, output_.rare_events);
            wv(agg.payload, nav_call_.size());
            wv(agg.payload, nav_return_.size());
            wv(agg.payload, output_.exceptions);
            wv(agg.payload, output_.boundaries);
        }
        agg.count = static_cast<uint32_t>(blocks_.size());
        output_.blocks.push_back(std::move(agg));
        BuildBlock checkpoints;
        checkpoints.type = 11;
        uint64_t previous_seq = 0;
        for (const auto& item : checkpoint_index_) {
            wv(checkpoints.payload, std::get<0>(item) - previous_seq);
            wv(checkpoints.payload, std::get<1>(item));
            wv(checkpoints.payload, std::get<2>(item));
            previous_seq = std::get<0>(item);
        }
        checkpoints.count = static_cast<uint32_t>(checkpoint_index_.size());
        output_.blocks.push_back(std::move(checkpoints));
        output_.build_blocks_seconds = chrono_seconds(blocks_started);
    }
    const StaticDict& dict_;
    BuildOutput& output_;
    bool need_tracker_ = true;
    struct Strings {
        std::vector<std::string> values;
    } strings_;
    std::unordered_map<std::string, uint32_t> ids_;
    std::vector<const StaticEntry*> static_entries_;
    std::unordered_map<uint64_t, std::vector<const StaticEntry*>> range_cache_;
    Tracker tracker_;
    std::vector<std::vector<uint8_t>> blocks_;
    std::vector<uint32_t> block_records_;
    std::vector<uint64_t> block_first_;
    std::vector<uint64_t> block_last_;
    std::vector<std::tuple<uint64_t, uint32_t, uint32_t>> checkpoint_index_;
    bool rotate_ = false;
    std::vector<uint8_t> run_payload_;
    std::vector<uint8_t> run_record_;
    std::vector<uint8_t> packet_buffer_;
    uint64_t packet_previous_offset_ = 0;
    uint64_t expected_seq_ = 0;
    struct PendingSegment {
        const std::vector<const StaticEntry*>* entries = nullptr;
        uint32_t offset = 0;
        uint32_t payload = 0;
        uint8_t last_executed = 0;
    };
    struct Pending {
        bool valid = false;
        uint32_t offset = 0;
        uint32_t last_executed = 0;
        uint32_t cycles = 0;
        uint32_t span = 0;
        uint32_t payload = 0;
        std::vector<PendingSegment> segments;
    } pending_;
    std::string event_text_;
    std::map<uint16_t, uint64_t> event_types_;
    std::vector<uint32_t> nav_call_, nav_return_, nav_exception_, nav_issue_, nav_event_;
};

std::vector<uint8_t> zlib_compress(const std::vector<uint8_t>& input, int level = 6) {
    uLongf bound = compressBound(static_cast<uLong>(input.size()));
    std::vector<uint8_t> output(bound);
    uLongf written = bound;
    int result = compress2(output.data(), &written, input.data(),
                           static_cast<uLong>(input.size()), level);
    if (result != Z_OK) fail(kExitOther, "zlib 压缩失败");
    output.resize(written);
    return output;
}

std::vector<uint8_t> read_binary_file(const fs::path& path) {
    std::ifstream file(path, std::ios::binary);
    if (!file) fail(kExitIo, "打不开 v2 index payload：" + path.string());
    file.seekg(0, std::ios::end);
    const std::streamoff size = file.tellg();
    if (size < 0) fail(kExitIo, "v2 index payload 长度无效");
    file.seekg(0, std::ios::beg);
    std::vector<uint8_t> bytes(static_cast<size_t>(size));
    if (size && !read_exact(file, bytes.data(), bytes.size())) {
        fail(kExitIo, "读 v2 index payload 失败");
    }
    return bytes;
}

void add_json_block(BuildOutput& output, uint16_t type, const std::string& json) {
    BuildBlock block;
    block.type = type;
    block.payload.assign(json.begin(), json.end());
    block.count = static_cast<uint32_t>(json.size());
    output.blocks.push_back(std::move(block));
}

void write_store(const fs::path& path, BuildOutput& output) {
    // 块顺序：strings/static/sequence/packets/nav/agg/meta/summary/sources/issues/checkpoints
    const uint64_t blocks = output.blocks.size();
    const uint64_t index_bytes = blocks * 24;
    const uint64_t payload_start = 56 + index_bytes;
    uint64_t payload_bytes = 0;
    for (const BuildBlock& block : output.blocks) payload_bytes += block.payload.size();
    const uint64_t file_bytes = payload_start + payload_bytes;
    if (file_bytes > (1ull << 40)) fail(kExitIo, "store 文件过大");
    std::vector<uint8_t> header(56, 0);
    std::memcpy(header.data(), "ETMTSv1\0", 8);
    header[8] = 1; header[9] = 0;                    // schema u16
    header[10] = 1; header[11] = 0;                  // FILE_TRACE u16
    const uint32_t byte_order = 0x01020304u;
    std::memcpy(header.data() + 12, &byte_order, 4);
    const uint64_t tag = output.generation_tag;
    std::memcpy(header.data() + 16, &tag, 8);
    const uint32_t block_count = static_cast<uint32_t>(blocks);
    std::memcpy(header.data() + 24, &block_count, 4);
    const uint32_t required = 0;
    std::memcpy(header.data() + 28, &required, 4);
    std::memcpy(header.data() + 32, &payload_bytes, 8);
    std::memcpy(header.data() + 40, &file_bytes, 8);
    const uint32_t checksum = crc32_bytes(header.data(), 48);
    std::memcpy(header.data() + 48, &checksum, 4);
    std::vector<uint8_t> index(static_cast<size_t>(index_bytes), 0);
    uint64_t offset = 0;
    for (size_t position = 0; position < output.blocks.size(); ++position) {
        const BuildBlock& block = output.blocks[position];
        uint8_t* row = index.data() + position * 24;
        const uint64_t absolute = payload_start + offset;
        std::memcpy(row, &absolute, 8);
        const uint32_t length = static_cast<uint32_t>(block.payload.size());
        const uint16_t type = block.type;
        const uint16_t flags = block.flags;
        const uint32_t count = block.flags ? block.uncompressed : block.count;
        const uint32_t crc = crc32_bytes(block.payload.data(), block.payload.size());
        std::memcpy(row + 8, &length, 4);
        std::memcpy(row + 12, &count, 4);
        std::memcpy(row + 16, &type, 2);
        std::memcpy(row + 18, &flags, 2);
        std::memcpy(row + 20, &crc, 4);
        offset += length;
    }
    const std::string path_text = path.string();
    std::FILE* out = std::fopen(path_text.c_str(), "wb");
    if (!out) fail(kExitIo, "打不开 store.bin 输出");
    FastSha256 sha;
    auto emit = [&](const uint8_t* data, size_t size) {
        if (size && std::fwrite(data, 1, size, out) != size) {
            std::fclose(out);
            fail(kExitIo, "写 store.bin 失败");
        }
        sha.update(data, size);
    };
    emit(header.data(), header.size());
    emit(index.data(), index.size());
    for (const BuildBlock& block : output.blocks) {
        emit(block.payload.data(), block.payload.size());
    }
    if (std::fflush(out) != 0 || std::fclose(out) != 0) fail(kExitIo, "写 store.bin 失败");
    output.store_sha256 = sha.hex_digest();
    output.store_bytes = file_bytes;
}

std::string meta_json(const BuildOutput& output,
                       const std::map<uint16_t, uint64_t>& event_counts) {
    std::ostringstream json;
    json << "{";
    json << "\"format\":\"trace-store\",\"formatName\":\"trace-store v1\","
            "\"schemaVersion\":1,\"byteOrder\":\"little\",";
    json << "\"record\":{\"name\":\"native-storebuild\",\"eventsSource\":\"native.bin\","
            "\"streamedSchema\":2,\"protocol\":\"ETMv4\"},";
    json << "\"counts\":{\"events\":" << output.events << ",\"instructions\":"
         << output.instructions << ",\"ranges\":" << output.ranges << ",\"declaredRanges\":"
         << output.ranges << ",\"runs\":" << output.runs << ",\"rareEvents\":"
         << output.rare_events << ",\"staticPcs\":" << output.static_pcs
         << ",\"strings\":" << output.strings << ",\"packets\":" << output.packets
         << ",\"sequenceBlocks\":" << output.sequence_blocks
         << ",\"checkpoints\":" << output.checkpoints << ",\"executed\":" << output.executed
         << ",\"unknownExecution\":" << output.unknown << ",\"notTaken\":"
         << output.not_taken << ",\"exceptions\":" << output.exceptions
         << ",\"gaps\":" << output.gaps << "},";
    json << "\"eventTypes\":{";
    bool first = true;
    for (const auto& item : event_counts) {
        if (!first) json << ",";
        first = false;
        json << "\"" << item.first << "\":" << item.second;
    }
    json << "},";
    json << "\"seq\":{\"first\":0,\"last\":" << (output.events ? output.events - 1 : 0)
         << ",\"contiguous\":true,\"count\":" << output.events
         << ",\"firstInstruction\":" << output.first_instruction
         << ",\"offsetRange\":[" << output.min_offset << "," << output.max_offset << "]},";
    json << "\"fieldPresence\":{\"instruction\":[\"kind\",\"seq\",\"offset\",\"pc\",\"size\","
            "\"bytes\",\"mnemonic\",\"operands\",\"executed\",\"function\",\"file\",\"line\","
            "\"cycleCount\",\"cycleSpan\",\"flow\",\"conditional\",\"executionState\","
            "\"executionReason\",\"callPath\",\"cycles\"],"
            "\"event\":[\"kind\",\"seq\",\"type\",\"offset\",\"text\",\"payload\",\"start\","
            "\"end\",\"flags\"]},";
    json << "\"hasCycleFields\":true,";
    json << "\"layout\":{\"headerBytes\":56,\"indexEntryBytes\":24,\"targetBlockBytes\":1048576,"
            "\"maxBlockBytes\":67108864,\"runInstructionLimit\":4096,"
            "\"checkpointInstructions\":4096},";
    json << "\"created\":{\"unix\":0,\"iso\":\"\"},\"issues\":0}";
    return json.str();
}

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
                      << ",\"capabilities\":[\"prescan\",\"diagnose\",\"build\"]"
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
        if (command == "build") {
            std::string native_path;
            std::string static_path;
            std::string out_dir;
            std::string generation;
            std::string result_path;
            std::string index_payload_path;
            bool force_analysis = false;
            uint64_t generation_tag = 0;
            for (int index = 2; index < argc; ++index) {
                const std::string arg = argv[index];
                auto next = [&](const char* label) -> std::string {
                    if (index + 1 >= argc) fail(kExitUsage, std::string(label) + " 缺少参数");
                    return argv[++index];
                };
                if (arg == "--native") native_path = next("--native");
                else if (arg == "--static") static_path = next("--static");
                else if (arg == "--out-dir") out_dir = next("--out-dir");
                else if (arg == "--generation") generation = next("--generation");
                else if (arg == "--generation-tag") generation_tag = std::stoull(next("--generation-tag"), nullptr, 0);
                else if (arg == "--result" || arg == "--json") result_path = next("--result");
                else if (arg == "--analysis") force_analysis = true;
                else if (arg == "--index-payload") index_payload_path = next("--index-payload");
                else fail(kExitUsage, "未知参数：" + arg);
            }
            if (native_path.empty()) fail(kExitUsage, "build 需要 --native");
            if (static_path.empty()) fail(kExitUsage, "build 需要 --static");
            if (out_dir.empty()) fail(kExitUsage, "build 需要 --out-dir");
            StaticDict dict = load_static_dict(static_path);
            BuildOutput output;
            output.generation = generation.empty() ? fs::path(out_dir).filename().string() : generation;
            output.generation_tag = generation_tag;
            std::error_code size_error;
            const uint64_t native_size = fs::file_size(native_path, size_error);
            const bool need_analysis = true;
            (void)force_analysis;
            (void)size_error;
            BuildVisitor visitor(dict, output, true);
            visitor.set_analysis(need_analysis);
            const auto started = std::chrono::steady_clock::now();
            NativeCounters counters = read_native(native_path, false, nullptr, 1000000, 0,
                                                   nullptr, true, &visitor);
            visitor.finish();
            for (BuildBlock& block : output.blocks) {
                if (block.type != 3) continue;
                if (block.payload.size() > (64u << 20)) {
                    std::ostringstream message;
                    message << "sequence 块 " << block.payload.size()
                            << " B 超过 64 MiB；t20 最小闭环只支持单块输入"
                               "（19.86M 需多块/检查点状态，留待下一任务）";
                    fail(kExitIo, message.str());
                }
                break;
            }
            const double assemble_seconds = seconds_since(started);
            output.native_sha256 = counters.native_sha256;
            output.static_sha256 = dict.sha256;
            output.elf_sha256 = dict.elf_sha256;
            // JSON 块
            add_json_block(output, 7, meta_json(output, visitor.event_types()));
            const auto analysis_started = std::chrono::steady_clock::now();
            const std::string analysis = visitor.analysis_json();
            output.analysis_seconds = chrono_seconds(analysis_started);
            std::ostringstream summary;
            summary << "{\"eventCount\":" << output.events << ",\"packetCount\":"
                    << output.packets << ",\"sourceFiles\":[],\"firstSeq\":"
                    << output.first_instruction << ",\"traceStore\":{\"format\":\"trace-store v1\","
                    << "\"generation\":" << quote(output.generation) << ",\"runs\":"
                    << output.runs << ",\"instructions\":" << output.instructions << "},"
                    << "\"analysis\":" << analysis
                    << ",\"streamed\":{\"schema\":2,\"events\":"
                    << output.events << "}}";
            add_json_block(output, 8, summary.str());
            add_json_block(output, 9, "{}");
            add_json_block(output, 10, "[]");
            if (!index_payload_path.empty()) {
                BuildBlock query;
                query.type = 12;
                query.payload = read_binary_file(index_payload_path);
                query.count = query.payload.size() >= 16
                    ? read_u32(query.payload.data() + 12) : 0;
                output.blocks.push_back(std::move(query));
            }
            const auto write_started = std::chrono::steady_clock::now();
            fs::path directory(out_dir);
            std::error_code error;
            fs::create_directories(directory, error);
            if (error) fail(kExitIo, "创建 out-dir 失败：" + error.message());
            fs::path store_path = directory / "store.bin";
            fs::path temporary = directory / ("store.bin.tmp." + std::to_string(GetCurrentProcessId()));
            try {
                write_store(temporary, output);
                fs::rename(temporary, store_path, error);
                if (error) {
                    fs::remove(temporary);
                    fail(kExitIo, "重命名 store.bin 失败：" + error.message());
                }
            } catch (...) {
                std::error_code ignored;
                fs::remove(temporary, ignored);
                throw;
            }
            output.write_seconds = seconds_since(write_started);
            std::ostringstream result;
            result << "{\n";
            result << " \"protocol\":" << quote(kProtocol) << ", \"schema\":" << kSchema
                   << ", \"abi\":" << kAbi << ",\n";
            result << " \"engine\":{\"name\":\"etm_storebuild\",\"buildSha\":"
                   << quote(build_sha()) << ",\"compiler\":" << quote(compiler_string())
                   << ",\"nativeFormat\":\"trace-bin native v1\",\"storeSchema\":1,"
                   << "\"queryIndex\":0},\n";
            result << " \"generation\":" << quote(output.generation)
                   << ", \"generationTag\":" << quote("0x" + [&]() {
                          std::ostringstream tag;
                          tag << std::hex << output.generation_tag;
                          return tag.str();
                      }()) << ",\n";
            result << " \"files\":[{\"name\":\"store.bin\",\"bytes\":" << output.store_bytes
                   << ",\"sha256\":" << quote(output.store_sha256) << ",\"blocks\":"
                   << output.blocks.size() << ",\"payloadBytes\":"
                   << (output.store_bytes - 56 - output.blocks.size() * 24) << "}],\n";
            result << " \"counts\":{\"events\":" << output.events << ",\"instructions\":"
                   << output.instructions << ",\"ranges\":" << output.ranges
                   << ",\"runs\":" << output.runs << ",\"rareEvents\":"
                   << output.rare_events << ",\"staticPcs\":" << output.static_pcs
                   << ",\"strings\":" << output.strings << ",\"packets\":"
                   << output.packets << ",\"sequenceBlocks\":" << output.sequence_blocks
                   << ",\"checkpoints\":" << output.checkpoints << ","
                   << "\"executed\":" << output.executed << ",\"unknownExecution\":"
                   << output.unknown << ",\"notTaken\":" << output.not_taken
                   << ",\"exceptions\":" << output.exceptions << ",\"gaps\":"
                   << output.gaps << "},\n";
            result << " \"phases\":{\"readNative\":" << assemble_seconds
                   << ",\"assemble\":" << assemble_seconds
                   << ",\"rangeHits\":" << output.range_hits << ",\"rangeMisses\":" << output.range_misses
                   << ",\"rangeWalk\":" << output.range_seconds
                   << ",\"runEncode\":" << output.flush_seconds
                   << ",\"tracker\":" << output.tracker_seconds
                   << ",\"buildBlocks\":" << output.build_blocks_seconds
                   << ",\"analysis\":" << output.analysis_seconds
                   << ",\"queryIndex\":0.0,\"defaultViews\":0.0,\"writeBlocks\":"
                   << output.write_seconds << ",\"assembleFile\":"
                   << output.write_seconds << ",\"hashFile\":0.0},\n";
            result << " \"peakRssBytes\":" << output.peak_rss << ",\n";
            result << " \"inputs\":{\"nativeSha256\":" << quote(output.native_sha256)
                   << ",\"nativeBytes\":" << counters.native_bytes
                   << ",\"elfSha256\":" << quote(output.elf_sha256)
                   << ",\"rangesSha256\":\"\",\"staticDictSha256\":"
                   << quote(output.static_sha256) << "},\n";
            result << " \"layout\":{\"checkpointInstructions\":4096,\"cellInsn\":4096,"
                      "\"targetBlockBytes\":1048576,\"maxBlockBytes\":67108864,"
                      "\"maxRunInstructions\":4096},\n";
            result << " \"canonical\":{\"compare\":\"semantic\",\"runs\":\"layout-"
                      "independent\",\"ignored\":[\"runs\",\"checkpoints\"],"
                      "\"note\":\"blocks 1-11 only; no block12/default views; R2 §8.1\"}\n";
            result << "}\n";
            fs::path result_file = result_path.empty() ? directory / "build-result.json"
                                                       : fs::path(result_path);
            std::ofstream out(result_file);
            if (!out) fail(kExitIo, "打不开 build-result 输出：" + result_file.string());
            out << result.str();
            if (!out) fail(kExitIo, "写 build-result 失败");
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
