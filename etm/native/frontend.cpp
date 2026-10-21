// 原生物理前端（P5b）：KVDAT 分块读取 → 成对毛刺滤波 → 半周期 DDR 取样 → 字节相位打包
// → TPIU 帧解复用 → tpiu.bin / ETM 流 / physical+framing 报告。
//
// 语义 oracle 是 `etm/tools/trace_frontend.py`（P5a 参考实现，已与 trace_port.extract +
// tpiu_demux.demux + decode_capture_stream 逐字节/逐字段对照通过）。本文件逐项复刻它：
//
//   * 时间轴：`time = (double(position) - (double)trigger) / (double)rate`（与 numpy 同序）；
//   * 毛刺滤波：`(后 - 前) * 1e9 <= glitch_ns + 0.001` 时成对消去，跨块只带 1 个待定边沿；
//   * 取样点：`first + (second - first) * fraction`（必须 -ffp-contract=off，否则 gcc 可能
//     合成 FMA 导致最后一位不同；CMakeLists 已对该目标显式关闭 contraction）；
//   * 取样电平：跳变表中“不晚于取样点的最后一条跳变”的电平，否则初始电平；
//   * 取样裕量：对每条滤波后跳变 t，取 `t - 前一个取样点` 与 `当前取样点 - t` 的最小值
//     （与参考实现 `min(previous.min(), following.min())` 等价，见 P5a 证据 §4.1）；
//   * 中位数：区间值→计数的精确直方图，偶数个取中间两个的 `(a + b) / 2`；
//   * clock_runs：每 256 步长、257 边沿窗口、5% 容差合并；
//   * TPIU：与 tpiu_demux.demux 相同的 FSYNC/HSYNC/帧/未知源/尾部残片规则；
//   * 相位判定：整段只能有一个有效候选，两个都有效或都无效都明确失败（不猜）。
//
// 默认链路不变：本程序是独立 CLI，只有 `etm/tools/native_frontend.py` 适配层会调用它；
// 缺失或版本不符时适配层明确报错（或由调用方显式选择参考实现回退），不会静默用旧残留。
//
// 用法：
//   etm_frontend.exe --version
//   etm_frontend.exe <capture.kvdat> --clock 0 --data 1,2,3,4 --fraction 0.25 \
//       --glitch-ns 4 --traceid 2 --chunk-bytes 1048576 --out <前缀> [--json <report.json>]

#ifdef ETM_FRONTEND_OPENSSL
#include <openssl/sha.h>
#endif
#include <algorithm>
#include <array>
#include <charconv>
#include <chrono>
#include <cmath>
#include <cctype>
#include <cstdint>
#include <cstdio>
#include <cstring>
#include <deque>
#include <filesystem>
#include <limits>
#include <map>
#include <memory>
#include <optional>
#include <stdexcept>
#include <string>
#include <unordered_map>
#include <utility>
#include <vector>

#ifndef NOMINMAX
#define NOMINMAX
#endif
#ifndef WIN32_LEAN_AND_MEAN
#define WIN32_LEAN_AND_MEAN
#endif
#include <windows.h>

namespace fs = std::filesystem;

// Windows 路径一律走宽字符 API（与项目其它原生组件的 Unicode 约定一致）：
// 命令行参数是 UTF-8，这里转成宽字符再构造 fs::path；输出 JSON 时用 u8string()。
static std::wstring utf8ToWide(const std::string& text) {
    if (text.empty()) return std::wstring();
    int size = MultiByteToWideChar(CP_UTF8, 0, text.data(), static_cast<int>(text.size()),
                                   nullptr, 0);
    std::wstring wide(static_cast<size_t>(size), L'\0');
    MultiByteToWideChar(CP_UTF8, 0, text.data(), static_cast<int>(text.size()), wide.data(), size);
    return wide;
}

static std::string wideToUtf8(const std::wstring& text) {
    if (text.empty()) return std::string();
    int size = WideCharToMultiByte(CP_UTF8, 0, text.data(), static_cast<int>(text.size()),
                                   nullptr, 0, nullptr, nullptr);
    std::string narrow(static_cast<size_t>(size), '\0');
    WideCharToMultiByte(CP_UTF8, 0, text.data(), static_cast<int>(text.size()), narrow.data(),
                        size, nullptr, nullptr);
    return narrow;
}

static fs::path pathFromUtf8(const std::string& text) {
    return fs::path(utf8ToWide(text));
}

static std::string pathToUtf8(const fs::path& path) {
    return wideToUtf8(path.wstring());
}

static std::FILE* openFile(const fs::path& path, const wchar_t* mode) {
    return _wfopen(path.c_str(), mode);
}

// ── 版本握手 ────────────────────────────────────────────────────────────────

static const char* kProtocol = "trace-frontend-native/1";
static const int kSchema = 1;
static const char* kBuildStamp = __DATE__ " " __TIME__;
#if defined(__GNUC__)
static const char* kCompiler = "gcc " __VERSION__;
#else
static const char* kCompiler = "unknown";
#endif

// ── 小工具 ──────────────────────────────────────────────────────────────────

static std::string hexBytes(const unsigned char* data, size_t size) {
    static const char* digits = "0123456789abcdef";
    std::string out;
    for (size_t index = 0; index < size; ++index) {
        if (index) out.push_back(' ');
        out.push_back(digits[data[index] >> 4]);
        out.push_back(digits[data[index] & 0x0F]);
    }
    return out;
}

static std::string jsonDouble(double value) {
    if (!std::isfinite(value)) throw std::runtime_error("JSON 不接受非有限浮点数");
    char buffer[40];
    auto result = std::to_chars(buffer, buffer + sizeof(buffer), value);
    if (result.ec != std::errc()) throw std::runtime_error("浮点数格式化失败");
    return std::string(buffer, result.ptr);
}

static std::string jsonString(const std::string& value) {
    std::string out = "\"";
    for (unsigned char ch : value) {
        switch (ch) {
            case '"': out += "\\\""; break;
            case '\\': out += "\\\\"; break;
            case '\n': out += "\\n"; break;
            case '\r': out += "\\r"; break;
            case '\t': out += "\\t"; break;
            default:
                if (ch < 0x20) {
                    char buffer[8];
                    std::snprintf(buffer, sizeof(buffer), "\\u%04x", ch);
                    out += buffer;
                } else {
                    out.push_back(static_cast<char>(ch));
                }
        }
    }
    out.push_back('"');
    return out;
}

// ── SHA-256（输入指纹；不引第三方库） ──────────────────────────────────────

#ifdef ETM_FRONTEND_OPENSSL
class Sha256 {
  public:
    Sha256() { SHA256_Init(&context_); }
    void update(const unsigned char* data, size_t size) { SHA256_Update(&context_, data, size); }
    std::string hexDigest() const {
        SHA256_CTX copy = context_;
        unsigned char digest[32];
        SHA256_Final(digest, &copy);
        const char* hex = "0123456789abcdef";
        std::string result; result.reserve(64);
        for (unsigned char value : digest) { result += hex[value >> 4]; result += hex[value & 15]; }
        return result;
    }
  private:
    SHA256_CTX context_;
};
#else
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
    std::string hexDigest() {
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

#endif
static std::string fileSha256(const fs::path& path) {
    std::FILE* file = openFile(path, L"rb");
    if (!file) throw std::runtime_error("打不开输入文件：" + pathToUtf8(path));
    Sha256 sha;
    std::vector<unsigned char> buffer(1 << 20);
    size_t got;
    while ((got = std::fread(buffer.data(), 1, buffer.size(), file)) > 0) sha.update(buffer.data(), got);
    std::fclose(file);
    return sha.hexDigest();
}

// ── 区间精确直方图（值 → 计数，复现 np.median） ──────────────────────────────

class ValueHistogram {
  public:
    explicit ValueHistogram(uint64_t max_distinct) : max_distinct_(max_distinct) {}

    void add(double value) {
        uint64_t bits = bitsOf(value);
        for (int slot = 0; slot < used_; ++slot) {
            if (cache_bits_[slot] == bits) {
                ++(*cache_counts_[slot]);
                ++total_;
                return;
            }
        }
        auto found = counts_.find(value);
        if (found == counts_.end()) {
            auto inserted = counts_.emplace(value, 1);
            found = inserted.first;
            if (counts_.size() > max_distinct_) {
                throw std::runtime_error("区间取值离散度超过有界直方图上限");
            }
        } else {
            ++found->second;
        }
        if (used_ < kCache) {
            cache_bits_[used_] = bits;
            cache_counts_[used_] = &found->second;
            ++used_;
        } else {
            // Floating interval representations change with the time exponent.
            // Retaining only the first eight keys makes long captures miss the
            // fast cache forever; refresh recent keys without changing counts.
            cache_bits_[replace_] = bits;
            cache_counts_[replace_] = &found->second;
            replace_ = (replace_ + 1) % kCache;
        }
        ++total_;
    }

    uint64_t total() const { return total_; }
    size_t distinct() const { return counts_.size(); }

    double median() const {
        if (!total_) throw std::runtime_error("没有可用于统计中位数的区间");
        std::vector<std::pair<double, int64_t>> ordered;
        ordered.reserve(counts_.size());
        for (const auto& item : counts_) ordered.emplace_back(item.first, item.second);
        std::sort(ordered.begin(), ordered.end(),
                  [](const auto& left, const auto& right) { return left.first < right.first; });
        uint64_t middle = total_ / 2;
        bool odd = (total_ % 2) != 0;
        uint64_t seen = 0;
        double lower = 0.0;
        bool have_lower = false;
        for (const auto& item : ordered) {
            if (odd) {
                if (seen + static_cast<uint64_t>(item.second) > middle) return item.first;
            } else {
                if (!have_lower && seen + static_cast<uint64_t>(item.second) > middle - 1) {
                    lower = item.first;
                    have_lower = true;
                }
                if (seen + static_cast<uint64_t>(item.second) > middle) {
                    double left = have_lower ? lower : item.first;
                    return (left + item.first) / 2.0;   // 参考实现的 (a + b) / 2
                }
            }
            seen += static_cast<uint64_t>(item.second);
        }
        throw std::runtime_error("区间直方图计数不完整");
    }

    // 遍历不同取值（用于 irregular_half_periods）
    template <typename Function>
    void forEach(Function&& function) const {
        for (const auto& item : counts_) function(item.first, item.second);
    }

  private:
    static const int kCache = 8;
    static uint64_t bitsOf(double value) {
        uint64_t bits;
        std::memcpy(&bits, &value, sizeof(bits));
        return bits;
    }
    struct Hash {
        size_t operator()(double value) const {
            return std::hash<uint64_t>()(bitsOf(value));
        }
    };
    std::unordered_map<double, int64_t, Hash> counts_;
    std::array<uint64_t, kCache> cache_bits_{};
    std::array<int64_t*, kCache> cache_counts_{};
    int used_ = 0;
    int replace_ = 0;
    uint64_t total_ = 0;
    uint64_t max_distinct_;
};

// ── KVDAT 布局（与 kingst_record.layout 同校验） ─────────────────────────────

static uint32_t readU32(const unsigned char* data) {
    return static_cast<uint32_t>(data[0]) | (static_cast<uint32_t>(data[1]) << 8)
         | (static_cast<uint32_t>(data[2]) << 16) | (static_cast<uint32_t>(data[3]) << 24);
}

static uint64_t readU64(const unsigned char* data) {
    uint64_t value = 0;
    for (int index = 7; index >= 0; --index) value = (value << 8) | data[index];
    return value;
}

struct ChannelBlock {
    int channel = 0;
    int initial = 0;
    uint64_t offset = 0;
    uint64_t count = 0;   // 含末条范围哨兵
};

struct KvdatLayout {
    uint64_t depth = 0;
    uint64_t rate = 0;
    uint64_t trigger = 0;
    std::string version;
    std::map<int, ChannelBlock> channels;
    uint64_t file_bytes = 0;

    static KvdatLayout open(const fs::path& path) {
        static const size_t kHeaderLimit = 256 * 1024;
        std::FILE* file = openFile(path, L"rb");
        if (!file) throw std::runtime_error("打不开输入文件：" + pathToUtf8(path));
        std::vector<unsigned char> header(kHeaderLimit);
        size_t got = std::fread(header.data(), 1, header.size(), file);
        header.resize(got);
        std::fclose(file);
        if (header.empty()) throw std::runtime_error("KVDAT 设置头不完整或超过上限");
        std::string text(header.begin(), header.end());
        size_t end = text.find("</settings>");
        if (end == std::string::npos) throw std::runtime_error("KVDAT 设置头不完整或超过上限");
        end += std::strlen("</settings>");
        std::string settings = text.substr(0, end);
        size_t global = settings.find("<global");
        std::string version;
        if (global != std::string::npos) {
            size_t start = settings.find("<version>", global);
            if (start != std::string::npos) {
                start += std::strlen("<version>");
                size_t stop = settings.find("</version>", start);
                if (stop != std::string::npos) version = settings.substr(start, stop - start);
            }
        }
        if (version.rfind("3.6.", 0) != 0) throw std::runtime_error("尚不支持此 KVDAT 版本：" + version);
        size_t marker = text.find(std::string("kvdat\0\0\0", 8), end);
        if (marker == std::string::npos || marker + 40 > header.size()) {
            throw std::runtime_error("KVDAT 采样头不完整");
        }
        KvdatLayout layout;
        layout.version = version;
        layout.depth = readU64(header.data() + marker + 8);
        layout.rate = readU64(header.data() + marker + 16);
        layout.trigger = readU64(header.data() + marker + 24);
        uint64_t count = readU64(header.data() + marker + 32);
        if (!(layout.depth > 0 && layout.depth < (1ull << 32)) || layout.rate == 0
            || layout.rate > 500000000ull || layout.trigger > layout.depth || count == 0 || count > 16) {
            throw std::runtime_error("不支持的 KVDAT 采样头");
        }
        layout.file_bytes = fs::file_size(path);
        // 通道块是 [通道头][边沿数据][通道头]… 交错布局，通道头可能远在 256 KiB 之外，
        // 因此与 kingst_record.layout 一样按文件偏移逐个 seek 读取。
        std::FILE* handle = openFile(path, L"rb");
        if (!handle) throw std::runtime_error("打不开输入文件：" + pathToUtf8(path));
        uint64_t position = marker + 40;
        for (uint64_t index = 0; index < count; ++index) {
            if (position + 16 > layout.file_bytes) {
                std::fclose(handle);
                throw std::runtime_error("KVDAT 通道头尚未写完");
            }
            unsigned char buffer[16];
            if (std::fseek(handle, static_cast<long>(position), SEEK_SET) != 0
                || std::fread(buffer, 1, 16, handle) != 16) {
                std::fclose(handle);
                throw std::runtime_error("KVDAT 通道头尚未写完");
            }
            const unsigned char* block = buffer;
            uint32_t magic = readU32(block);
            int channel = block[4];
            int initial = block[5];
            uint32_t reserved = static_cast<uint32_t>(block[6]) | (static_cast<uint32_t>(block[7]) << 8);
            uint64_t edges = readU64(block + 8);
            position += 16;
            if (magic != 0x442323u || reserved != 0 || (initial != 0 && initial != 1) || channel > 15
                || layout.channels.count(channel) || edges < 1 || edges > layout.depth + 1) {
                throw std::runtime_error("不支持的 KVDAT 通道块");
            }
            uint64_t stop = position + edges * 5;
            if (stop > layout.file_bytes) {
                std::fclose(handle);
                throw std::runtime_error("KVDAT 边沿数据尚未写完");
            }
            ChannelBlock info;
            info.channel = channel;
            info.initial = initial;
            info.offset = position;
            info.count = edges;
            // 末条必须是范围哨兵：position == depth 且 flag == 0
            unsigned char tail[5];
            if (std::fseek(handle, static_cast<long>(stop - 5), SEEK_SET) != 0
                || std::fread(tail, 1, 5, handle) != 5) {
                std::fclose(handle);
                throw std::runtime_error("KVDAT 边沿数据尚未写完");
            }
            if (readU32(tail) != layout.depth || tail[4] != 0) {
                std::fclose(handle);
                throw std::runtime_error("KVDAT 结束标记无效");
            }
            layout.channels[channel] = info;
            position = stop;
        }
        std::fclose(handle);
        if (position != layout.file_bytes) throw std::runtime_error("KVDAT 有未解析尾部");
        return layout;
    }
};

// ── 通道边沿记录游标（有界分块读取） ────────────────────────────────────────

class ChannelCursor {
  public:
    ChannelCursor(const fs::path& path, const ChannelBlock& block, uint64_t depth, uint64_t rate,
                  uint64_t trigger)
        : file_(nullptr), remaining_(block.count * 5), records_left_(block.count), depth_(depth),
          rate_(rate), trigger_(trigger), initial_(block.initial) {
        file_ = openFile(path, L"rb");
        if (!file_) throw std::runtime_error("打不开输入文件：" + pathToUtf8(path));
        if (std::fseek(file_, static_cast<long>(block.offset), SEEK_SET) != 0) {
            throw std::runtime_error("KVDAT 边沿数据尚未写完");
        }
    }
    ~ChannelCursor() {
        if (file_) std::fclose(file_);
    }
    ChannelCursor(const ChannelCursor&) = delete;
    ChannelCursor& operator=(const ChannelCursor&) = delete;

    bool done() const { return remaining_ == 0 && buffer_.empty(); }

    // 读入至多 chunk_bytes 字节，把新增的完整记录追加到 out_times/out_levels。
    void read(size_t chunk_bytes, std::vector<double>& out_times, std::vector<uint8_t>& out_levels) {
        if (remaining_ == 0 && buffer_.empty()) return;
        size_t want = static_cast<size_t>(std::min<uint64_t>(chunk_bytes, remaining_));
        size_t start = buffer_.size();
        buffer_.resize(start + want);
        size_t got = std::fread(buffer_.data() + start, 1, want, file_);
        if (got != want) throw std::runtime_error("KVDAT 读取时发生截断");
        remaining_ -= got;
        ++reads_;
        bytes_read_ += got;
        size_t usable = (buffer_.size() / 5) * 5;
        if (!usable) return;
        size_t records = usable / 5;
        if (records > records_left_) throw std::runtime_error("KVDAT 边沿位置或标记无效");
        records_left_ -= records;
        for (size_t index = 0; index < records; ++index) {
            const unsigned char* row = buffer_.data() + index * 5;
            uint32_t position = readU32(row);
            uint8_t flag = row[4];
            if (flag != 0 || position > depth_) throw std::runtime_error("KVDAT 边沿位置或标记无效");
            if (have_last_position_ && position <= last_position_) {
                throw std::runtime_error("KVDAT 边沿位置或标记无效");
            }
            last_position_ = position;
            have_last_position_ = true;
        }
        size_t real = records;
        if (records_left_ == 0) {
            const unsigned char* row = buffer_.data() + (records - 1) * 5;
            if (readU32(row) != depth_ || row[4] != 0) throw std::runtime_error("KVDAT 结束标记无效");
            real = records - 1;   // 末条范围哨兵不进入取样
        }
        for (size_t index = 0; index < real; ++index) {
            const unsigned char* row = buffer_.data() + index * 5;
            uint32_t position = readU32(row);
            double time = (static_cast<double>(position) - static_cast<double>(trigger_))
                        / static_cast<double>(rate_);
            out_times.push_back(time);
            ++edge_index_;
            out_levels.push_back(static_cast<uint8_t>(initial_ ^ ((edge_index_ & 1u) ? 1u : 0u)));
            last_time_ = time;
            have_last_time_ = true;
        }
        buffer_.erase(buffer_.begin(), buffer_.begin() + static_cast<std::ptrdiff_t>(usable));
    }

    bool have_last_time() const { return have_last_time_; }
    double last_time() const { return last_time_; }
    uint64_t reads() const { return reads_; }
    uint64_t bytes_read() const { return bytes_read_; }

  private:
    std::FILE* file_;
    uint64_t remaining_;
    uint64_t records_left_;
    uint64_t depth_;
    uint64_t rate_;
    uint64_t trigger_;
    int initial_;
    std::vector<unsigned char> buffer_;
    bool have_last_position_ = false;
    uint32_t last_position_ = 0;
    uint64_t edge_index_ = 0;
    double last_time_ = 0.0;
    bool have_last_time_ = false;
    uint64_t reads_ = 0;
    uint64_t bytes_read_ = 0;
};

// ── 成对毛刺滤波（跨块只带 1 个待定边沿） ────────────────────────────────────

class GlitchFilter {
  public:
    GlitchFilter(double glitch_ns, bool enabled)
        : limit_(glitch_ns + 0.001), enabled_(enabled && glitch_ns > 0) {}

    bool enabled() const { return enabled_; }
    bool has_pending() const { return has_pending_; }
    double pending_time() const { return pending_time_; }
    uint64_t removed() const { return raw_count_ - emitted_ - (has_pending_ ? 1 : 0); }
    uint64_t raw_count() const { return raw_count_; }

    // 推入一条原始边沿；返回 true 时 out_time/out_level 是一条已确认的保留边沿。
    bool push(double time, uint8_t level, double& out_time, uint8_t& out_level) {
        ++raw_count_;
        if (!enabled_) {
            ++emitted_;
            out_time = time;
            out_level = level;
            return true;
        }
        if (has_pending_) {
            if ((time - pending_time_) * 1e9 <= limit_) {
                has_pending_ = false;
                return false;
            }
            out_time = pending_time_;
            out_level = pending_level_;
            ++emitted_;
            pending_time_ = time;
            pending_level_ = level;
            return true;
        }
        pending_time_ = time;
        pending_level_ = level;
        has_pending_ = true;
        return false;
    }

    bool flush(double& out_time, uint8_t& out_level) {
        if (!has_pending_) return false;
        out_time = pending_time_;
        out_level = pending_level_;
        ++emitted_;
        has_pending_ = false;
        return true;
    }

  private:
    double limit_;
    bool enabled_;
    bool has_pending_ = false;
    double pending_time_ = 0.0;
    uint8_t pending_level_ = 0;
    uint64_t raw_count_ = 0;
    uint64_t emitted_ = 0;
};

// ── 时钟统计（区间/周期直方图、clock_runs、gaps） ───────────────────────────

struct ClockRun {
    double start_seconds = 0.0;
    double end_seconds = 0.0;
    uint64_t half_cycles = 0;
    double clock_hz_average = 0.0;
};

struct ClockGap {
    uint64_t edge = 0;
    double time_seconds = 0.0;
    double duration = 0.0;
};

class ClockStats {
  public:
    ClockStats(uint64_t max_distinct)
        : intervals_(max_distinct), periods_(max_distinct), raw_intervals_(max_distinct) {}

    void addRaw(double time) {
        ++raw_edge_count_;
        if (have_raw_tail_) raw_intervals_.add(time - raw_tail_);
        raw_tail_ = time;
        have_raw_tail_ = true;
    }

    // 与参考实现的批次语义逐条等价：区间 = 相邻滤波后时间差，周期 = 相隔一条的时间差。
    void addFiltered(double time) {
        uint64_t index = edge_count_;            // 这条边沿的全局下标
        if (!have_first_) {
            first_time_ = time;
            have_first_ = true;
        }
        if (have_prev_) {
            double interval = time - prev_;
            if (interval <= 0.0) non_increasing_ = true;
            intervals_.add(interval);
            if (interval > 1e-6) gap_candidates_.push_back({index - 1, prev_, interval});
        }
        if (have_prev2_) periods_.add(time - prev2_);
        prev2_ = have_prev_ ? prev_ : time;
        have_prev2_ = have_prev_;
        prev_ = time;
        have_prev_ = true;
        last_time_ = time;
        ++edge_count_;
        runs_window_.push_back(time);
        while (runs_window_.size() >= 257) {
            std::array<double, 257> window{};
            for (size_t index = 0; index < 257; ++index) window[index] = runs_window_[index];
            runWindow(window.data(), window.size());
            runs_window_.erase(runs_window_.begin(), runs_window_.begin() + 256);
            next_offset_ += 256;
        }
    }

    uint64_t edge_count() const { return edge_count_; }
    uint64_t raw_edge_count() const { return raw_edge_count_; }
    double first_time() const { return first_time_; }
    double last_time() const { return last_time_; }
    const ValueHistogram& intervals() const { return intervals_; }
    const ValueHistogram& periods() const { return periods_; }
    const ValueHistogram& rawIntervals() const { return raw_intervals_; }
    const std::vector<ClockRun>& runs() const { return runs_; }
    const std::vector<ClockGap>& gapCandidates() const { return gap_candidates_; }
    bool nonIncreasing() const { return non_increasing_; }

    // 收尾：处理最后一个未满 257 的窗口（参考实现 range(0, len-2, 256) 的末窗）
    void closeRuns() {
        if (next_offset_ < edge_count_ - 2 && runs_window_.size() >= 3) {
            size_t size = std::min<size_t>(257, runs_window_.size());
            std::array<double, 257> window{};
            for (size_t index = 0; index < size; ++index) window[index] = runs_window_[index];
            runWindow(window.data(), size);
            runs_window_.clear();
        }
    }

  private:
    void runWindow(const double* chunk, size_t size) {
        double duration = chunk[size - 1] - chunk[0];
        double frequency = (static_cast<double>(size - 1)) / duration / 2.0;
        if (!runs_.empty() && std::fabs(frequency / runs_.back().clock_hz_average - 1.0) < 0.05) {
            ClockRun& run = runs_.back();
            run.end_seconds = chunk[size - 1];
            run.half_cycles += size - 1;
            run.clock_hz_average = static_cast<double>(run.half_cycles)
                                 / (run.end_seconds - run.start_seconds) / 2.0;
        } else {
            ClockRun run;
            run.start_seconds = chunk[0];
            run.end_seconds = chunk[size - 1];
            run.half_cycles = size - 1;
            run.clock_hz_average = frequency;
            runs_.push_back(run);
        }
    }

    ValueHistogram intervals_;
    ValueHistogram periods_;
    ValueHistogram raw_intervals_;
    uint64_t edge_count_ = 0;
    uint64_t raw_edge_count_ = 0;
    bool have_first_ = false;
    double first_time_ = 0.0;
    double last_time_ = 0.0;
    double prev_ = 0.0;
    bool have_prev_ = false;
    double prev2_ = 0.0;
    bool have_prev2_ = false;
    double raw_tail_ = 0.0;
    bool have_raw_tail_ = false;
    std::deque<double> runs_window_;
    uint64_t next_offset_ = 0;
    std::vector<ClockRun> runs_;
    std::vector<ClockGap> gap_candidates_;
    bool non_increasing_ = false;
};

// ── TPIU 帧解复用（流式；与 tpiu_demux.demux 逐字节等价） ────────────────────

static const unsigned char kFsync[4] = {0xff, 0xff, 0xff, 0x7f};

struct FramingStats {
    uint64_t frames = 0;
    uint64_t fsyncs = 0;
    uint64_t hsyncs = 0;
    uint64_t errors = 0;
    uint64_t unknown_bytes = 0;
    uint64_t initial_skipped_bytes = 0;
    std::vector<int> id_changes;
    uint64_t trailing_sync_bytes = 0;
    uint64_t idle_resyncs = 0;
    uint64_t dropped_partial_frame_bytes = 0;
    uint64_t mid_frame_fsyncs = 0;
    uint64_t trailing_frame_bytes = 0;
    uint64_t trailing_raw_bytes = 0;
};

struct DemuxResult {
    FramingStats stats;
    std::vector<std::pair<int, std::vector<unsigned char>>> streams;   // 首次发射顺序
};

// O(1) 的 FSYNC 计数（FSYNC 无自重叠，维护最近 4 字节即可）
class SyncCounter {
  public:
    void push(unsigned char value) {
        last_[fill_ & 3u] = value;
        ++fill_;
        if (fill_ < 4) return;
        if (last_[(fill_ - 4) & 3u] == 0xff && last_[(fill_ - 3) & 3u] == 0xff
            && last_[(fill_ - 2) & 3u] == 0xff && last_[(fill_ - 1) & 3u] == 0x7f) {
            ++count_;
        }
    }
    uint64_t count() const { return count_; }

  private:
    unsigned char last_[4] = {0, 0, 0, 0};
    uint64_t fill_ = 0;
    uint64_t count_ = 0;
};

class TpuDemux {
  public:
    void feed(const unsigned char* data, size_t size) {
        if (mode_ == Mode::Failed || mode_ == Mode::Done || size == 0) return;
        if (mode_ == Mode::Search) {
            std::vector<unsigned char> window;
            window.reserve(carry_.size() + size);
            window.insert(window.end(), carry_.begin(), carry_.end());
            window.insert(window.end(), data, data + size);
            size_t found = window.size();
            for (size_t index = 0; index + 4 <= window.size(); ++index) {
                if (window[index] == kFsync[0] && window[index + 1] == kFsync[1]
                    && window[index + 2] == kFsync[2] && window[index + 3] == kFsync[3]) {
                    found = index;
                    break;
                }
            }
            if (found == window.size()) {
                size_t keep = std::min<size_t>(3, window.size());
                scanned_ += window.size() - keep;
                carry_.assign(window.end() - static_cast<std::ptrdiff_t>(keep), window.end());
                return;
            }
            stats_.initial_skipped_bytes = scanned_ + found;
            mode_ = Mode::Parse;
            carry_.clear();
            buffer_.insert(buffer_.end(), window.begin() + static_cast<std::ptrdiff_t>(found),
                           window.end());
            parse(false);
            return;
        }
        buffer_.insert(buffer_.end(), data, data + size);
        parse(false);
    }

    DemuxResult finish() {
        if (mode_ == Mode::Failed) throw std::runtime_error("TPIU 解复用已失败");
        if (mode_ == Mode::Search) throw std::runtime_error("没有 FSYNC，无法确认 TPIU 帧起点。");
        parse(true);
        mode_ = Mode::Done;
        stats_.trailing_frame_bytes = static_cast<uint64_t>(frame_len_);
        stats_.trailing_raw_bytes = static_cast<uint64_t>(buffer_.size() - pos_);
        return DemuxResult{stats_, streams_ordered_};
    }

    bool hasStream(int id) const { return stream_index_.count(id) != 0; }

  private:
    enum class Mode { Search, Parse, Failed, Done };

    void emit(int source, unsigned char value) {
        if (source < 0) {
            ++stats_.unknown_bytes;
            return;
        }
        auto found = stream_index_.find(source);
        if (found == stream_index_.end()) {
            stream_index_[source] = streams_ordered_.size();
            streams_ordered_.emplace_back(source, std::vector<unsigned char>{value});
            return;
        }
        streams_ordered_[static_cast<size_t>(found->second)].second.push_back(value);
    }

    void flushFrame() {
        unsigned char flags = frame_[15];
        for (int position = 0; position < 15; position += 2) {
            int flag = (flags >> (position / 2)) & 1;
            int value = frame_[position];
            if (value & 1) {
                int next = value >> 1;
                int previous = current_;
                if (next != previous) stats_.id_changes.push_back(next);
                current_ = next;
                if (position < 14) {
                    emit((flag && next != previous) ? previous : current_, frame_[position + 1]);
                }
            } else {
                emit(current_, static_cast<unsigned char>(value | flag));
                if (position < 14) emit(current_, frame_[position + 1]);
            }
        }
        frame_len_ = 0;
        ++stats_.frames;
    }

    void compact() {
        if (pos_ == 0) return;
        buffer_.erase(buffer_.begin(), buffer_.begin() + static_cast<std::ptrdiff_t>(pos_));
        offset_ += pos_;
        pos_ = 0;
    }

    void parse(bool eof) {
        try {
            while (true) {
                size_t remaining = buffer_.size() - pos_;
                if (remaining == 0) break;
                if (frame_len_ == 0 && remaining < 4) {
                    if (!eof) break;
                    bool prefix = true;
                    for (size_t index = 0; index < remaining; ++index) {
                        if (buffer_[pos_ + index] != kFsync[index]) {
                            prefix = false;
                            break;
                        }
                    }
                    if (prefix) {
                        stats_.trailing_sync_bytes = remaining;
                        break;
                    }
                }
                if (remaining >= 4 && frame_len_ == 0 && buffer_[pos_] == kFsync[0]
                    && buffer_[pos_ + 1] == kFsync[1] && buffer_[pos_ + 2] == kFsync[2]
                    && buffer_[pos_ + 3] == kFsync[3]) {
                    ++stats_.fsyncs;
                    pos_ += 4;
                    continue;
                }
                if (remaining < 2) break;
                if (buffer_[pos_] == 0xff && buffer_[pos_ + 1] == 0x7f) {
                    ++stats_.hsyncs;
                    pos_ += 2;
                    continue;
                }
                if (buffer_[pos_] == 0xff && buffer_[pos_ + 1] == 0xff) {
                    throw std::runtime_error(
                        "TPIU 帧内完整同步或非法源 ID 0x7f（物理偏移 " + std::to_string(offset_ + pos_)
                        + "，帧内 " + std::to_string(frame_len_) + " 字节）；拒绝拼接损坏帧");
                }
                frame_[frame_len_++] = buffer_[pos_++];
                frame_[frame_len_++] = buffer_[pos_++];
                if (frame_len_ != 16) continue;
                flushFrame();
            }
        } catch (...) {
            mode_ = Mode::Failed;
            compact();
            throw;
        }
        compact();
    }

    Mode mode_ = Mode::Search;
    std::vector<unsigned char> carry_;
    uint64_t scanned_ = 0;
    std::vector<unsigned char> buffer_;
    size_t pos_ = 0;
    uint64_t offset_ = 0;
    unsigned char frame_[16] = {0};
    int frame_len_ = 0;
    int current_ = -1;          // -1 = 未知源（unknown_bytes）
    FramingStats stats_;
    std::vector<std::pair<int, std::vector<unsigned char>>> streams_ordered_;
    std::map<int, size_t> stream_index_;
};

// ── 数据通道（跨块取样状态） ────────────────────────────────────────────────

struct DataChannel {
    DataChannel(int channel_number, int initial_level, double glitch_ns, bool glitch_enabled,
                const fs::path& path, const ChannelBlock& block, uint64_t depth, uint64_t rate,
                uint64_t trigger)
        : channel(channel_number), initial(initial_level), filter(glitch_ns, glitch_enabled),
          cursor(path, block, depth, rate, trigger) {}

    int channel;
    int initial;
    GlitchFilter filter;
    ChannelCursor cursor;
    std::vector<double> raw_times;
    std::vector<unsigned char> raw_levels;
    size_t raw_index = 0;
    std::deque<std::pair<double, unsigned char>> pending;
    bool have_prev = false;
    double prev_time = 0.0;
    unsigned char prev_level = 0;
    uint64_t filtered_count = 0;
    double margin = std::numeric_limits<double>::infinity();
    bool have_last_sample = false;
    double last_sample = 0.0;
    bool cursor_eof = false;
    bool have_raw_last = false;
    double raw_last = 0.0;
};

// ── 前端主体 ────────────────────────────────────────────────────────────────

struct FrontendConfig {
    int clock = 0;
    std::vector<int> data{1, 2, 3, 4};
    double fraction = 0.25;
    double glitch_ns = 0.0;
    int traceid = 2;
};

struct FrontendCounters {
    uint64_t reads = 0;
    uint64_t samples = 0;
    // 粗粒度计时（只在读块/喂解复用器/整个主循环边界取时，逐取样点不计时：
    // steady_clock::now() 每次 ~25 ns，2400 万次会白花 1 s 以上）。
    double read_seconds = 0.0;      // 有界读取
    double filter_seconds = 0.0;    // 时钟通道毛刺滤波 + 统计
    double demux_seconds = 0.0;     // 两个候选的 TPIU 解复用
    double loop_seconds = 0.0;      // 整个取样主循环（含数据通道滤波/电平/裕量/打包）
};

class Frontend {
  public:
    Frontend(const fs::path& path, const FrontendConfig& config, const std::vector<uint64_t>& plan,
             size_t chunk_bytes, bool demux_enabled = true)
        : path_(path), config_(config), layout_(KvdatLayout::open(path)), plan_(plan),
          demux_enabled_(demux_enabled),
          chunk_bytes_(chunk_bytes ? chunk_bytes : 1),
          clock_stats_(kMaxDistinct),
          clock_filter_(config.glitch_ns, config.glitch_ns > 0.0) {
        for (int channel : {config.clock, config.data[0], config.data[1], config.data[2],
                            config.data[3]}) {
            if (!layout_.channels.count(channel)) {
                throw std::runtime_error("KVDAT 缺少通道：" + std::to_string(channel));
            }
        }
        clock_cursor_ = std::make_unique<ChannelCursor>(
            path_, layout_.channels[config.clock], layout_.depth, layout_.rate, layout_.trigger);
        for (int index = 0; index < 4; ++index) {
            int channel = config.data[static_cast<size_t>(index)];
            channels_.emplace_back(std::make_unique<DataChannel>(
                channel, layout_.channels[channel].initial, config.glitch_ns,
                config.glitch_ns > 0.0, path_, layout_.channels[channel], layout_.depth,
                layout_.rate, layout_.trigger));
        }
    }

    void run() {
        auto started = std::chrono::steady_clock::now();
        double edge = 0.0;
        bool have_prev_edge = false;
        while (nextClockEdge(edge)) {
            if (!have_prev_edge) {
                prev_edge_ = edge;
                have_prev_edge = true;
                continue;
            }
            double sample = prev_edge_ + (edge - prev_edge_) * config_.fraction;
            ++counters_.samples;
            unsigned char nibble = 0;
            for (int bit = 0; bit < 4; ++bit) {
                DataChannel& channel = *channels_[static_cast<size_t>(bit)];
                settle(channel, sample);
                unsigned char level = sampleLevel(channel, sample);
                nibble = static_cast<unsigned char>(
                    nibble | static_cast<unsigned char>((level & 1u) << bit));
            }
            pushNibble(nibble);
            prev_edge_ = edge;
        }
        counters_.loop_seconds = secondsSince(started);
        finish();
    }

    void finish() {
        if (finished_) return;
        finished_ = true;
        for (auto& pointer : channels_) {
            DataChannel& channel = *pointer;
            settleAll(channel);
            double out_time = 0.0;
            unsigned char out_level = 0;
            if (channel.filter.flush(out_time, out_level)) {
                channel.pending.emplace_back(out_time, out_level);
                ++channel.filtered_count;
            }
            if (channel.have_last_sample && !channel.pending.empty()) {
                double distance = channel.pending.front().first - channel.last_sample;
                if (distance < 0.0) distance = 0.0;
                if (distance < channel.margin) channel.margin = distance;
            }
        }
        flushCandidates();
        // 滤波阈值校验（与参考实现同文案、同顺序：先校验阈值再看边沿数）
        if (config_.glitch_ns > 0.0) {
            if (clock_stats_.rawIntervals().total() == 0) {
                throw std::runtime_error(
                    "软件滤波阈值须小于典型时钟半周期的一半，避免滤掉正常 Trace；请降低阈值");
            }
            if (config_.glitch_ns >= clock_stats_.rawIntervals().median() * 1e9 / 2.0) {
                throw std::runtime_error(
                    "软件滤波阈值须小于典型时钟半周期的一半，避免滤掉正常 Trace；请降低阈值");
            }
        }
        if (clock_stats_.edge_count() < 4) throw std::runtime_error("TRACECLK 没有足够跳变。");
        if (clock_stats_.nonIncreasing()) throw std::runtime_error("同一时刻存在多个时钟跳变。");
        clock_stats_.closeRuns();
        if (!demux_enabled_) {          // --no-demux：只做物理层（合成 1 s 整段体检用）
            phase_ = 0;
            return;
        }
        std::vector<int> valid_phases;
        std::vector<std::pair<int, std::string>> failures;
        for (int index = 0; index < 2; ++index) {
            if (!phase_errors_[index].empty()) {
                failures.emplace_back(index, phase_errors_[index]);
                continue;
            }
            try {
                demux_result_[static_cast<size_t>(index)] =
                    demuxers_[static_cast<size_t>(index)].finish();
            } catch (const std::exception& error) {
                failures.emplace_back(index, error.what());
                continue;
            }
            const DemuxResult& result = demux_result_[static_cast<size_t>(index)];
            if (result.stats.frames > 0
                && demuxers_[static_cast<size_t>(index)].hasStream(config_.traceid)) {
                valid_phases.push_back(index);
            }
        }
        if (valid_phases.size() != 1) {
            std::string message = "不能唯一确定 TPIU 字节相位："
                                + std::to_string(valid_phases.size())
                                + " 个候选；请检查通道和取样位置。";
            if (!failures.empty()) message += " " + failures.back().second;
            throw std::runtime_error(message);
        }
        phase_ = valid_phases[0];
    }

    const FrontendConfig& config() const { return config_; }
    const FrontendCounters& counters() const { return counters_; }
    // 派生阶段：主循环里除“有界读取/时钟滤波/解复用”之外的部分（数据通道滤波、
    // 电平查询、裕量、打包）——粗粒度相减，不做逐点计时。
    double samplePackSeconds() const {
        double value = counters_.loop_seconds - counters_.read_seconds
                     - counters_.filter_seconds - counters_.demux_seconds;
        return value > 0.0 ? value : 0.0;
    }

    int phase() const { return phase_; }
    const std::vector<unsigned char>& candidate(size_t index) const { return candidates_[index]; }
    uint64_t candidateSyncs(size_t index) const { return phase_syncs_[index]; }
    const std::vector<unsigned char>& candidateHead(size_t index) const { return phase_heads_[index]; }
    const DemuxResult& demuxResult() const { return demux_result_[static_cast<size_t>(phase_)]; }
    const DataChannel& dataChannel(size_t index) const { return *channels_[index]; }
    const ClockStats& clockStats() const { return clock_stats_; }
    const KvdatLayout& layout() const { return layout_; }
    const fs::path& path() const { return path_; }

    uint64_t removedClock() const { return clock_filter_.removed(); }
    uint64_t chunkBytes() const { return chunk_bytes_; }

    uint64_t totalReads() const {
        uint64_t reads = clock_cursor_->reads();
        for (const auto& pointer : channels_) reads += pointer->cursor.reads();
        return reads;
    }
    uint64_t totalBytesRead() const {
        uint64_t bytes = clock_cursor_->bytes_read();
        for (const auto& pointer : channels_) bytes += pointer->cursor.bytes_read();
        return bytes;
    }

    double windowStart() const {
        return -static_cast<double>(layout_.trigger) / static_cast<double>(layout_.rate);
    }
    double windowEnd() const {
        double end = windowStart();
        if (clock_cursor_->have_last_time() && clock_cursor_->last_time() > end) {
            end = clock_cursor_->last_time();
        }
        for (const auto& pointer : channels_) {
            if (pointer->have_raw_last && pointer->raw_last > end) end = pointer->raw_last;
        }
        return end;
    }

  private:
    static constexpr uint64_t kMaxDistinct = 1ull << 20;
    static const size_t kDemuxBatch = 4096;

    static double secondsSince(std::chrono::steady_clock::time_point start) {
        return std::chrono::duration<double>(std::chrono::steady_clock::now() - start).count();
    }

    size_t nextChunk() {
        if (plan_.empty()) return chunk_bytes_;
        size_t index = std::min(plan_index_, plan_.size() - 1);
        ++plan_index_;
        return static_cast<size_t>(plan_[index]);
    }

    bool nextClockEdge(double& out) {
        while (true) {
            if (!clock_queue_.empty()) {
                out = clock_queue_.front();
                clock_queue_.pop_front();
                return true;
            }
            if (clock_eof_) {
                if (!clock_flushed_) {
                    clock_flushed_ = true;
                    double time = 0.0;
                    unsigned char level = 0;
                    if (clock_filter_.flush(time, level)) {
                        clock_stats_.addFiltered(time);
                        clock_queue_.push_back(time);
                        continue;
                    }
                }
                return false;
            }
            auto start = std::chrono::steady_clock::now();
            std::vector<double> times;
            std::vector<unsigned char> levels;
            clock_cursor_->read(nextChunk(), times, levels);
            clock_eof_ = clock_cursor_->done();
            counters_.read_seconds += secondsSince(start);
            start = std::chrono::steady_clock::now();
            for (double time : times) {
                clock_stats_.addRaw(time);
                double survivor_time = 0.0;
                unsigned char survivor_level = 0;
                if (clock_filter_.push(time, 0, survivor_time, survivor_level)) {
                    clock_stats_.addFiltered(survivor_time);
                    clock_queue_.push_back(survivor_time);
                }
            }
            counters_.filter_seconds += secondsSince(start);
        }
    }

    // 把一条通道的原始边沿推进滤波器，直到“待定边沿 > sample”或通道读完
    void settle(DataChannel& channel, double sample) {
        while (true) {
            if (channel.filter.enabled()) {
                if (channel.filter.has_pending() && channel.filter.pending_time() > sample) break;
            } else if (channel.raw_index < channel.raw_times.size()
                       && channel.raw_times[channel.raw_index] > sample) {
                break;
            }
            if (channel.raw_index >= channel.raw_times.size()) {
                if (channel.cursor_eof) {
                    // 通道确实读完：冲出“待定边沿”（参考实现在流结束时同样保留未成对边沿），
                    // 否则最后一段取样会读到过时电平。
                    double out_time = 0.0;
                    unsigned char out_level = 0;
                    if (channel.filter.flush(out_time, out_level)) {
                        channel.pending.emplace_back(out_time, out_level);
                        ++channel.filtered_count;
                    }
                    break;
                }
                auto start = std::chrono::steady_clock::now();
                channel.raw_times.clear();
                channel.raw_levels.clear();
                channel.raw_index = 0;
                channel.cursor.read(nextChunk(), channel.raw_times, channel.raw_levels);
                channel.cursor_eof = channel.cursor.done();
                counters_.read_seconds += secondsSince(start);
                continue;
            }
            pushRaw(channel);
        }
    }

    void settleAll(DataChannel& channel) {
        while (true) {
            if (channel.raw_index < channel.raw_times.size()) {
                pushRaw(channel);
                continue;
            }
            if (channel.cursor_eof) break;
            auto start = std::chrono::steady_clock::now();
            channel.raw_times.clear();
            channel.raw_levels.clear();
            channel.raw_index = 0;
            channel.cursor.read(nextChunk(), channel.raw_times, channel.raw_levels);
            channel.cursor_eof = channel.cursor.done();
            counters_.read_seconds += secondsSince(start);
        }
    }

    void pushRaw(DataChannel& channel) {
        // 逐边沿不计时（steady_clock::now() 每次 ~25 ns，1800 万条边沿会白花 ~1 s）；
        // 数据通道的滤波耗时计入 “sample” 阶段，时钟侧单列 “filter”。
        double time = channel.raw_times[channel.raw_index];
        unsigned char level = channel.raw_levels[channel.raw_index];
        ++channel.raw_index;
        channel.raw_last = time;
        channel.have_raw_last = true;
        double survivor_time = 0.0;
        unsigned char survivor_level = 0;
        if (channel.filter.push(time, level, survivor_time, survivor_level)) {
            channel.pending.emplace_back(survivor_time, survivor_level);
            ++channel.filtered_count;
        }
    }

    unsigned char sampleLevel(DataChannel& channel, double sample) {
        while (!channel.pending.empty() && channel.pending.front().first <= sample) {
            double time = channel.pending.front().first;
            unsigned char level = channel.pending.front().second;
            if (channel.have_last_sample) {
                double distance = time - channel.last_sample;
                if (distance < channel.margin) channel.margin = distance;
            }
            double distance = sample - time;
            if (distance < channel.margin) channel.margin = distance;
            channel.prev_time = time;
            channel.prev_level = level;
            channel.have_prev = true;
            channel.pending.pop_front();
        }
        channel.last_sample = sample;
        channel.have_last_sample = true;
        return channel.have_prev ? channel.prev_level : static_cast<unsigned char>(channel.initial);
    }

    void pushNibble(unsigned char nibble) {
        uint64_t index = nibble_count_;
        ring_[index & 3u] = nibble;
        ++nibble_count_;
        for (int phase = 0; phase < 2; ++phase) {
            uint64_t& produced = phase_counts_[static_cast<size_t>(phase)];
            while (true) {
                uint64_t first = 2 * produced + (phase == 0 ? 0 : 1);
                if (first + 1 >= nibble_count_) break;
                unsigned char low = ring_[first & 3u];
                unsigned char high = ring_[(first + 1) & 3u];
                candidates_[static_cast<size_t>(phase)].push_back(
                    static_cast<unsigned char>(low | (high << 4)));
                ++produced;
            }
        }
        for (int phase = 0; phase < 2; ++phase) {
            size_t fed = phase_fed_[static_cast<size_t>(phase)];
            if (candidates_[static_cast<size_t>(phase)].size() - fed >= kDemuxBatch) {
                flushPhase(static_cast<size_t>(phase));
            }
        }
    }

    void flushPhase(size_t phase) {
        const auto& buffer = candidates_[phase];
        size_t fed = phase_fed_[phase];
        if (buffer.size() == fed) return;
        for (size_t index = fed; index < buffer.size(); ++index) sync_[phase].push(buffer[index]);
        if (phase_heads_[phase].empty()) {
            size_t take = std::min<size_t>(32, buffer.size());
            phase_heads_[phase].assign(buffer.begin(),
                                       buffer.begin() + static_cast<std::ptrdiff_t>(take));
        }
        if (demux_enabled_) {
            auto start = std::chrono::steady_clock::now();
            try {
                demuxers_[phase].feed(buffer.data() + fed, buffer.size() - fed);
            } catch (const std::runtime_error& error) {
                // A rejected byte phase must not abort the other candidate.
                // Keep its original diagnostic for the final unique-phase check.
                phase_errors_[phase] = error.what();
            }
            counters_.demux_seconds += secondsSince(start);
        }
        phase_fed_[phase] = buffer.size();
        phase_syncs_[phase] = sync_[phase].count();
    }

    void flushCandidates() {
        flushPhase(0);
        flushPhase(1);
    }

    fs::path path_;
    FrontendConfig config_;
    KvdatLayout layout_;
    std::vector<uint64_t> plan_;
    size_t chunk_bytes_;
    size_t plan_index_ = 0;
    ClockStats clock_stats_;
    GlitchFilter clock_filter_;
    std::unique_ptr<ChannelCursor> clock_cursor_;
    std::deque<double> clock_queue_;
    bool clock_eof_ = false;
    bool clock_flushed_ = false;
    std::vector<std::unique_ptr<DataChannel>> channels_;
    double prev_edge_ = 0.0;
    unsigned char ring_[4] = {0, 0, 0, 0};
    uint64_t nibble_count_ = 0;
    uint64_t phase_counts_[2] = {0, 0};
    std::vector<unsigned char> candidates_[2];
    size_t phase_fed_[2] = {0, 0};
    uint64_t phase_syncs_[2] = {0, 0};
    std::vector<unsigned char> phase_heads_[2];
    SyncCounter sync_[2];
    TpuDemux demuxers_[2];
    std::string phase_errors_[2];
    DemuxResult demux_result_[2];
    FrontendCounters counters_;
    bool demux_enabled_ = true;
    bool finished_ = false;
    int phase_ = -1;
};

// ── 报告（JSON）与输出 ─────────────────────────────────────────────────────

static const char* kNote =
    "Byte phase must be confirmed by TPIU framing and ETM decode; "
    "sync matches alone do not prove valid capture.";
static const char* kFilterBasis =
    "complete pulses <= limit; raw CSV and KVDAT remain unchanged";

struct PhysicalReport {
    std::string json;
    double half_period = 0.0;
};

static std::string buildPhysicalJson(Frontend& frontend, const std::string& input_sha256) {
    const FrontendConfig& config = frontend.config();
    const ClockStats& clock = frontend.clockStats();
    const ValueHistogram& intervals = clock.intervals();
    double half_period = intervals.median();
    double gap_limit = std::max(half_period * 8.0, 1e-6);
    uint64_t irregular = 0;
    intervals.forEach([&](double value, int64_t count) {
        if (std::fabs(value / half_period - 1.0) > 0.25) {
            irregular += static_cast<uint64_t>(count);
        }
    });
    double average = (static_cast<double>(clock.edge_count() - 1))
                   / (clock.last_time() - clock.first_time()) / 2.0;
    double median_period = 1.0 / clock.periods().median();

    std::string out = "{\n";
    out += " \"input\": " + jsonString(pathToUtf8(frontend.path())) + ",\n";
    out += " \"input_sha256\": " + jsonString(input_sha256) + ",\n";
    out += " \"software_filter\": {\"enabled\": ";
    out += (config.glitch_ns > 0.0 ? "true" : "false");
    out += ", \"limit_ns\": " + jsonDouble(config.glitch_ns);
    out += ", \"removed_transitions\": {";
    if (config.glitch_ns > 0.0) {          // 关闭滤波时参考实现给空对象
        out += jsonString(std::to_string(config.clock)) + ": "
             + std::to_string(frontend.removedClock());
        for (size_t index = 0; index < 4; ++index) {
            out += ", " + jsonString(std::to_string(config.data[index])) + ": "
                 + std::to_string(frontend.dataChannel(index).filter.removed());
        }
    }
    out += "}, \"raw_clock_edges\": " + std::to_string(clock.raw_edge_count());
    out += ", \"basis\": " + jsonString(kFilterBasis) + "},\n";
    out += " \"stage\": \"physical DDR extraction only; no TPIU demux or ETM instruction decode\",\n";
    out += " \"clock_channel\": " + std::to_string(config.clock) + ",\n";
    out += " \"data_channels_D0_to_D3\": [" + std::to_string(config.data[0]) + ", "
         + std::to_string(config.data[1]) + ", " + std::to_string(config.data[2]) + ", "
         + std::to_string(config.data[3]) + "],\n";
    out += " \"window_seconds\": [" + jsonDouble(frontend.windowStart()) + ", "
         + jsonDouble(frontend.windowEnd()) + "],\n";
    out += " \"clock_edges\": " + std::to_string(clock.edge_count()) + ",\n";
    out += " \"clock_hz_median\": " + jsonDouble(median_period) + ",\n";
    out += " \"clock_hz_average\": " + jsonDouble(average) + ",\n";
    out += " \"half_period_ns\": " + jsonDouble(half_period * 1e9) + ",\n";
    out += " \"irregular_half_periods\": " + std::to_string(irregular) + ",\n";
    out += " \"clock_runs\": [";
    for (size_t index = 0; index < clock.runs().size(); ++index) {
        const ClockRun& run = clock.runs()[index];
        if (index) out += ", ";
        out += "{\"start_seconds\": " + jsonDouble(run.start_seconds)
             + ", \"end_seconds\": " + jsonDouble(run.end_seconds)
             + ", \"half_cycles\": " + std::to_string(run.half_cycles)
             + ", \"clock_hz_average\": " + jsonDouble(run.clock_hz_average) + "}";
    }
    out += "],\n";
    out += " \"clock_gaps\": [";
    bool first_gap = true;
    for (const ClockGap& gap : clock.gapCandidates()) {
        if (!(gap.duration > gap_limit)) continue;
        if (!first_gap) out += ", ";
        first_gap = false;
        out += "{\"edge\": " + std::to_string(gap.edge)
             + ", \"time_seconds\": " + jsonDouble(gap.time_seconds)
             + ", \"duration_ns\": " + jsonDouble(gap.duration * 1e9) + "}";
    }
    out += "],\n";
    out += " \"data_transitions\": {";
    for (size_t index = 0; index < 4; ++index) {
        if (index) out += ", ";
        out += jsonString("CH" + std::to_string(config.data[index])) + ": "
             + std::to_string(frontend.dataChannel(index).filtered_count);
    }
    out += "},\n";
    out += " \"sample_fraction_after_each_edge\": " + jsonDouble(config.fraction) + ",\n";
    double margin = std::numeric_limits<double>::infinity();
    for (size_t index = 0; index < 4; ++index) {
        const DataChannel& channel = frontend.dataChannel(index);
        if (channel.filtered_count > 0 && channel.margin < margin) margin = channel.margin;
    }
    out += " \"min_data_transition_distance_ns\": ";
    out += std::isfinite(margin) ? jsonDouble(margin * 1e9) : "null";
    out += ",\n";
    out += " \"nibbles\": " + std::to_string(frontend.counters().samples) + ",\n";
    out += " \"candidates\": [";
    for (size_t index = 0; index < 2; ++index) {
        if (index) out += ", ";
        out += "{\"phase\": " + std::to_string(index)
             + ", \"bytes\": " + std::to_string(frontend.candidate(index).size())
             + ", \"frame_sync_matches\": " + std::to_string(frontend.candidateSyncs(index))
             + ", \"first_32_bytes_hex\": "
             + jsonString(hexBytes(frontend.candidateHead(index).data(),
                                   frontend.candidateHead(index).size())) + "}";
    }
    out += "],\n";
    out += " \"note\": " + jsonString(kNote) + ",\n";
    out += " \"input_format\": \"Kingst KVDAT\",\n";
    out += " \"sample_rate\": " + std::to_string(frontend.layout().rate) + ",\n";
    out += " \"sample_depth\": " + std::to_string(frontend.layout().depth) + "\n";
    out += "}";
    return out;
}

static std::string buildFramingJson(const DemuxResult& result, int phase) {
    const FramingStats& stats = result.stats;
    std::string out = "{\n";
    out += " \"frames\": " + std::to_string(stats.frames) + ",\n";
    out += " \"fsyncs\": " + std::to_string(stats.fsyncs) + ",\n";
    out += " \"hsyncs\": " + std::to_string(stats.hsyncs) + ",\n";
    out += " \"errors\": " + std::to_string(stats.errors) + ",\n";
    out += " \"unknown_bytes\": " + std::to_string(stats.unknown_bytes) + ",\n";
    out += " \"initial_skipped_bytes\": " + std::to_string(stats.initial_skipped_bytes) + ",\n";
    out += " \"id_changes\": [";
    for (size_t index = 0; index < stats.id_changes.size(); ++index) {
        if (index) out += ", ";
        out += std::to_string(stats.id_changes[index]);
    }
    out += "],\n";
    out += " \"trailing_sync_bytes\": " + std::to_string(stats.trailing_sync_bytes) + ",\n";
    out += " \"idle_resyncs\": " + std::to_string(stats.idle_resyncs) + ",\n";
    out += " \"dropped_partial_frame_bytes\": " + std::to_string(stats.dropped_partial_frame_bytes)
         + ",\n";
    out += " \"mid_frame_fsyncs\": " + std::to_string(stats.mid_frame_fsyncs) + ",\n";
    out += " \"trailing_frame_bytes\": " + std::to_string(stats.trailing_frame_bytes) + ",\n";
    out += " \"trailing_raw_bytes\": " + std::to_string(stats.trailing_raw_bytes) + ",\n";
    out += " \"stream_bytes\": {";
    for (size_t index = 0; index < result.streams.size(); ++index) {
        if (index) out += ", ";
        out += jsonString(std::to_string(result.streams[index].first)) + ": "
             + std::to_string(result.streams[index].second.size());
    }
    out += "},\n";
    out += " \"phase\": " + std::to_string(phase) + "\n";
    out += "}";
    return out;
}

// ── CLI ────────────────────────────────────────────────────────────────────

static void usage() {
    std::printf(
        "有界分块原生物理前端（P5b）：KVDAT → 滤波/DDR → TPIU 解复用\n"
        "\n"
        "用法：etm_frontend <capture.kvdat> [选项]\n"
        "  --version            打印协议/构建信息（JSON）后退出\n"
        "  --clock N            时钟通道（默认 0）\n"
        "  --data a,b,c,d       D0..D3 通道（默认 1,2,3,4）\n"
        "  --fraction F         取样位置（默认 0.25）\n"
        "  --glitch-ns G        毛刺滤波宽度 0～100 ns（默认 0=关闭）\n"
        "  --traceid N          期望的 TPIU 数据源 ID（默认 2）\n"
        "  --chunk-bytes N      每次有界读取的字节数（默认 1048576）\n"
        "  --chunk-plan a,b,c   分块计划（逗号分隔，用尽后重复末值）\n"
        "  --out PREFIX         写出 PREFIX.tpiu.bin / PREFIX.id<N>.bin / *.json\n"
        "  --json PATH          写出完整结果 JSON（含 physical/framing/计数/耗时）\n"
        "  --no-demux           只做物理层（不解复用/不做相位判定；合成 1 s 整段体检用）\n"
        "  --quiet              不打印摘要 JSON\n");
}

static std::vector<uint64_t> parsePlan(const std::string& text) {
    std::vector<uint64_t> plan;
    size_t start = 0;
    while (start <= text.size()) {
        size_t comma = text.find(',', start);
        std::string piece = text.substr(start, comma == std::string::npos ? std::string::npos
                                                                         : comma - start);
        if (piece.empty()) throw std::runtime_error("分块计划里有空值");
        uint64_t value = 0;
        auto result = std::from_chars(piece.data(), piece.data() + piece.size(), value);
        if (result.ec != std::errc() || result.ptr != piece.data() + piece.size() || value == 0) {
            throw std::runtime_error("分块计划必须是正整数序列");
        }
        plan.push_back(value);
        if (comma == std::string::npos) break;
        start = comma + 1;
    }
    if (plan.empty()) throw std::runtime_error("分块计划不能为空");
    return plan;
}

static void writeFile(const fs::path& path, const void* data, size_t size) {
    std::FILE* file = openFile(path, L"wb");
    if (!file) throw std::runtime_error("写不出文件：" + pathToUtf8(path));
    if (size && std::fwrite(data, 1, size, file) != size) {
        std::fclose(file);
        throw std::runtime_error("写不出文件：" + pathToUtf8(path));
    }
    std::fclose(file);
}

static int realMain(const std::vector<std::string>& args) {
    int argc = static_cast<int>(args.size());
    if (argc < 2) {
        usage();
        return 2;
    }
    std::string input;
    FrontendConfig config;
    uint64_t chunk_bytes = 1ull << 20;
    std::vector<uint64_t> plan;
    std::string out_prefix;
    std::string json_path;
    bool quiet = false;
    bool no_demux = false;
    for (int index = 1; index < argc; ++index) {
        std::string arg = args[static_cast<size_t>(index)];
        auto need = [&](const char* name) -> std::string {
            if (index + 1 >= argc) throw std::runtime_error(std::string("缺少参数：") + name);
            return args[static_cast<size_t>(++index)];
        };
        try {
            if (arg == "--version") {
                std::printf("{\"protocol\": %s, \"schema\": %d, \"compiler\": %s, "
                            "\"built\": %s, \"inputFormats\": [\"kvdat\"]}\n",
                            jsonString(kProtocol).c_str(), kSchema, jsonString(kCompiler).c_str(),
                            jsonString(kBuildStamp).c_str());
                return 0;
            } else if (arg == "--help" || arg == "-h") {
                usage();
                return 0;
            } else if (arg == "--clock") {
                config.clock = std::stoi(need("--clock"));
            } else if (arg == "--data") {
                std::string text = need("--data");
                config.data.clear();
                size_t start = 0;
                while (start <= text.size()) {
                    size_t comma = text.find(',', start);
                    std::string piece = text.substr(start, comma == std::string::npos
                                                               ? std::string::npos : comma - start);
                    if (piece.empty()) throw std::runtime_error("--data 需要 4 个通道号");
                    config.data.push_back(std::stoi(piece));
                    if (comma == std::string::npos) break;
                    start = comma + 1;
                }
            } else if (arg == "--fraction") {
                config.fraction = std::stod(need("--fraction"));
            } else if (arg == "--glitch-ns") {
                config.glitch_ns = std::stod(need("--glitch-ns"));
            } else if (arg == "--traceid") {
                config.traceid = std::stoi(need("--traceid"));
            } else if (arg == "--chunk-bytes") {
                chunk_bytes = std::stoull(need("--chunk-bytes"));
                if (chunk_bytes == 0) throw std::runtime_error("分块大小必须是正整数");
            } else if (arg == "--chunk-plan") {
                plan = parsePlan(need("--chunk-plan"));
            } else if (arg == "--out") {
                out_prefix = need("--out");
            } else if (arg == "--json") {
                json_path = need("--json");
            } else if (arg == "--no-demux") {
                no_demux = true;
            } else if (arg == "--quiet") {
                quiet = true;
            } else if (arg.rfind("--", 0) == 0) {
                throw std::runtime_error("未知参数：" + arg);
            } else if (input.empty()) {
                input = arg;
            } else {
                throw std::runtime_error("多余参数：" + arg);
            }
        } catch (const std::invalid_argument&) {
            throw std::runtime_error("参数值非法：" + arg);
        } catch (const std::out_of_range&) {
            throw std::runtime_error("参数值越界：" + arg);
        }
    }
    if (input.empty()) throw std::runtime_error("缺少输入文件");
    if (!(config.fraction > 0.0 && config.fraction < 1.0)) {
        throw std::runtime_error("取样位置必须介于 0 和 1 之间。");
    }
    if (!(config.glitch_ns >= 0.0 && config.glitch_ns <= 100.0)) {
        throw std::runtime_error("软件滤波宽度须为 0～100 ns（0 为关闭）");
    }
    if (config.data.size() != 4) throw std::runtime_error("--data 需要 4 个通道号");
    {
        std::vector<int> all{config.clock};
        all.insert(all.end(), config.data.begin(), config.data.end());
        std::sort(all.begin(), all.end());
        if (std::adjacent_find(all.begin(), all.end()) != all.end()) {
            throw std::runtime_error("需要五个不同通道：时钟 + D0/D1/D2/D3。");
        }
    }
    fs::path path = pathFromUtf8(input);
    if (!fs::exists(path)) throw std::runtime_error("输入不存在：" + pathToUtf8(path));
    std::string suffix = path.extension().string();
    std::transform(suffix.begin(), suffix.end(), suffix.begin(),
                   [](unsigned char ch) { return static_cast<char>(std::tolower(ch)); });
    if (suffix != ".kvdat") {
        throw std::runtime_error("原生前端只支持 .kvdat 输入；CSV 请使用参考实现 trace_frontend.py");
    }

    auto started = std::chrono::steady_clock::now();
    std::string input_sha256 = fileSha256(path);
    double sha_seconds = std::chrono::duration<double>(
        std::chrono::steady_clock::now() - started).count();
    Frontend frontend(fs::weakly_canonical(path), config, plan, static_cast<size_t>(chunk_bytes),
                      !no_demux);
    frontend.run();
    std::string physical_json = buildPhysicalJson(frontend, input_sha256);
    DemuxResult demux_no_demux;
    const DemuxResult& demux = no_demux ? demux_no_demux : frontend.demuxResult();
    std::string framing_json = no_demux ? std::string("null")
                                       : buildFramingJson(demux, frontend.phase());
    double elapsed = std::chrono::duration<double>(std::chrono::steady_clock::now() - started).count();

    std::string tpiu_sha;
    {
        const auto& tpiu = frontend.candidate(static_cast<size_t>(frontend.phase()));
        Sha256 sha;
        sha.update(tpiu.data(), tpiu.size());
        tpiu_sha = sha.hexDigest();
    }
    std::string stream_json = "{";
    std::string stream_hashes = "{";
    for (size_t index = 0; index < demux.streams.size(); ++index) {
        Sha256 sha;
        sha.update(demux.streams[index].second.data(), demux.streams[index].second.size());
        if (index) {
            stream_json += ", ";
            stream_hashes += ", ";
        }
        stream_json += jsonString(std::to_string(demux.streams[index].first)) + ": "
                     + std::to_string(demux.streams[index].second.size());
        stream_hashes += jsonString(std::to_string(demux.streams[index].first)) + ": "
                       + jsonString(sha.hexDigest());
    }
    stream_json += "}";
    stream_hashes += "}";

    if (!out_prefix.empty()) {
        fs::path prefix = pathFromUtf8(out_prefix);
        if (prefix.has_parent_path()) fs::create_directories(prefix.parent_path());
        const auto& tpiu = frontend.candidate(static_cast<size_t>(frontend.phase()));
        writeFile(pathFromUtf8(pathToUtf8(prefix) + ".tpiu.bin"), tpiu.data(), tpiu.size());
        for (const auto& entry : demux.streams) {
            writeFile(pathFromUtf8(pathToUtf8(prefix) + ".id" + std::to_string(entry.first) + ".bin"),
                      entry.second.data(), entry.second.size());
        }
        writeFile(pathFromUtf8(pathToUtf8(prefix) + ".physical.json"), physical_json.data(),
                  physical_json.size());
        writeFile(pathFromUtf8(pathToUtf8(prefix) + ".framing.json"), framing_json.data(),
                  framing_json.size());
    }

    const FrontendCounters& counters = frontend.counters();
    std::string summary = "{\n";
    summary += " \"protocol\": " + jsonString(kProtocol) + ",\n";
    summary += " \"schema\": " + std::to_string(kSchema) + ",\n";
    summary += " \"build\": {\"compiler\": " + jsonString(kCompiler) + ", \"built\": "
             + jsonString(kBuildStamp) + "},\n";
    summary += " \"input\": " + jsonString(pathToUtf8(frontend.path())) + ",\n";
    summary += " \"inputSha256\": " + jsonString(input_sha256) + ",\n";
    summary += " \"chunkBytes\": " + std::to_string(frontend.chunkBytes()) + ",\n";
    summary += " \"chunkPlan\": [";
    for (size_t index = 0; index < plan.size(); ++index) {
        if (index) summary += ", ";
        summary += std::to_string(plan[index]);
    }
    summary += "],\n";
    summary += " \"phase\": " + std::to_string(frontend.phase()) + ",\n";
    summary += " \"tpiuBytes\": " + std::to_string(frontend.candidate(
                                    static_cast<size_t>(frontend.phase())).size()) + ",\n";
    summary += " \"tpiuSha256\": " + jsonString(tpiu_sha) + ",\n";
    summary += " \"streams\": " + stream_json + ",\n";
    summary += " \"streamSha256\": " + stream_hashes + ",\n";
    summary += " \"physical\": " + physical_json + ",\n";
    summary += " \"framing\": " + framing_json + ",\n";
    summary += " \"counters\": {\"reads\": " + std::to_string(frontend.totalReads())
             + ", \"bytesRead\": " + std::to_string(frontend.totalBytesRead())
             + ", \"samples\": " + std::to_string(counters.samples) + "},\n";
    summary += " \"timing\": {\"read\": " + jsonDouble(counters.read_seconds)
             + ", \"filter\": " + jsonDouble(counters.filter_seconds)
             + ", \"sample\": " + jsonDouble(frontend.samplePackSeconds())
             + ", \"demux\": " + jsonDouble(counters.demux_seconds)
             + ", \"loop\": " + jsonDouble(counters.loop_seconds)
             + ", \"sha256\": " + jsonDouble(sha_seconds)
             + ", \"total\": " + jsonDouble(elapsed) + "}\n";
    summary += "}";
    if (!json_path.empty()) writeFile(pathFromUtf8(json_path), summary.data(), summary.size());
    if (!quiet) std::printf("%s\n", summary.c_str());
    return 0;
}

int wmain(int argc, wchar_t** argv) {
    try {
        std::vector<std::string> args;
        args.reserve(static_cast<size_t>(argc));
        for (int index = 0; index < argc; ++index) args.push_back(wideToUtf8(argv[index]));
        return realMain(args);
    } catch (const std::exception& error) {
        std::fprintf(stderr, "原生前端失败：%s\n", error.what());
        return 1;
    }
}
