// Read only the execution fields needed by exports. This is not a decoder or
// a checkpoint restorer. The Python host supplies verified static text once.
#include <algorithm>
#include <array>
#include <cstdint>
#include <cstring>
#include <filesystem>
#include <fstream>
#include <iostream>
#include <limits>
#include <openssl/sha.h>
#include <stdexcept>
#include <string>
#include <vector>
#include <zlib.h>
#ifdef _WIN32
#include <windows.h>
#endif
namespace fs = std::filesystem;
using Bytes = std::vector<unsigned char>;
static fs::path filePath(const std::string &name) {
    auto path = fs::u8path(name);
#ifdef _WIN32
    auto wide = fs::absolute(path).lexically_normal().make_preferred().native();
    if (wide.size() >= MAX_PATH && wide.compare(0, 4, L"\\\\?\\") != 0) {
        wide =
            wide.compare(0, 2, L"\\\\") == 0 ? L"\\\\?\\UNC\\" + wide.substr(2) : L"\\\\?\\" + wide;
        return fs::path(wide);
    }
#endif
    return path;
}
static void require(bool value, const char *message) {
    if (!value)
        throw std::runtime_error(message);
}
static std::string hex(const unsigned char *p, size_t n) {
    static const char digits[] = "0123456789abcdef";
    std::string out;
    out.reserve(n * 2);
    for (size_t i = 0; i < n; ++i) {
        out += digits[p[i] >> 4];
        out += digits[p[i] & 15];
    }
    return out;
}
static std::string hashFile(const fs::path &path) {
    std::ifstream in(path, std::ios::binary);
    require(bool(in), "cannot open hash input");
    std::array<char, 1 << 20> buffer;
    SHA256_CTX hash;
    SHA256_Init(&hash);
    while (in) {
        in.read(buffer.data(), buffer.size());
        if (in.gcount())
            SHA256_Update(&hash, buffer.data(), size_t(in.gcount()));
    }
    require(in.eof(), "hash input read failed");
    unsigned char result[32];
    SHA256_Final(result, &hash);
    return hex(result, 32);
}
struct Reader {
    const unsigned char *data;
    size_t pos = 0, end;
    explicit Reader(const Bytes &bytes) : data(bytes.data()), end(bytes.size()) {}
    Reader(const unsigned char *p, size_t n) : data(p), end(n) {}
    void need(size_t n) {
        require(pos <= end && n <= end - pos, "truncated export input");
    }
    uint64_t le(size_t n) {
        need(n);
        uint64_t v = 0;
        for (size_t i = 0; i < n; ++i)
            v |= uint64_t(data[pos++]) << (i * 8);
        return v;
    }
    uint64_t uv() {
        uint64_t v = 0;
        for (unsigned shift = 0; shift <= 63; shift += 7) {
            auto b = le(1);
            require(shift < 63 || b <= 1, "varint exceeds 64 bits");
            v |= (b & 127) << shift;
            if (!(b & 128))
                return v;
        }
        throw std::runtime_error("bad varint");
    }
    std::string str() {
        auto n = le(8);
        require(n <= end - pos, "invalid string length");
        std::string s(reinterpret_cast<const char *>(data + pos), size_t(n));
        pos += size_t(n);
        return s;
    }
    size_t count(size_t minimum) {
        auto n = le(8);
        require(n <= (1u << 22) && n <= (end - pos) / minimum, "invalid export table count");
        return size_t(n);
    }
};
static Bytes readBytes(std::ifstream &in, uint64_t offset, size_t n) {
    in.clear();
    in.seekg(std::streamoff(offset));
    require(bool(in), "seek failed");
    Bytes data(n);
    if (n)
        in.read(reinterpret_cast<char *>(data.data()), std::streamsize(n));
    require(bool(in), "truncated store");
    return data;
}
struct Entry {
    uint64_t pc, predicated, valid, group, context;
    // code, context, full suffix, unknown/full, unknown/simple, missing/simple,
    // JSON code/unknown/missing suffixes, CSV false/true/unknown suffixes.
    std::array<size_t, 12> text;
};
struct Plan {
    uint64_t kind, start, end, tag, storeBytes, first, last, events, instructions;
    std::string sha;
    std::vector<std::string> pool;
    size_t header, footer;
    std::vector<Entry> entries;
    std::vector<std::array<size_t, 4>> eventsText;
    explicit Plan(const fs::path &path) {
        auto size = fs::file_size(path);
        require(size >= 8 + 80 + 32 && size <= (1ull << 30), "invalid export plan size");
        std::ifstream file(path, std::ios::binary);
        require(bool(file), "cannot open plan");
        auto bytes = readBytes(file, 0, size_t(size));
        unsigned char digest[32];
        SHA256_CTX hash;
        SHA256_Init(&hash);
        SHA256_Update(&hash, bytes.data(), bytes.size() - 32);
        SHA256_Final(digest, &hash);
        require(std::memcmp(digest, bytes.data() + bytes.size() - 32, 32) == 0,
                "export plan SHA256 mismatch");
        Reader r(bytes.data(), bytes.size() - 32);
        require(std::memcmp(bytes.data(), "ETMEXP1\0", 8) == 0, "unsupported export plan");
        r.pos = 8;
        require(r.le(8) == 1, "unsupported export ABI");
        kind = r.le(8);
        require(kind < 4, "unknown export format");
        start = r.le(8);
        end = r.le(8);
        tag = r.le(8);
        storeBytes = r.le(8);
        first = r.le(8);
        last = r.le(8);
        events = r.le(8);
        instructions = r.le(8);
        require(first <= last && last < std::numeric_limits<uint64_t>::max() &&
                    events == last - first + 1,
                "unsupported event sequence");
        sha = r.str();
        require(sha.size() == 64, "missing store SHA256");
        auto count = r.count(8);
        pool.reserve(count);
        for (size_t i = 0; i < count; ++i)
            pool.push_back(r.str());
        auto id = [&]() {
            auto value = r.le(8);
            require(value < pool.size(), "text ID out of bounds");
            return size_t(value);
        };
        header = id();
        footer = id();
        count = r.count(17 * 8);
        entries.reserve(count);
        for (size_t i = 0; i < count; ++i) {
            Entry e;
            e.pc = r.le(8);
            e.predicated = r.le(8);
            e.valid = r.le(8);
            e.group = r.le(8);
            e.context = r.le(8);
            require(e.predicated <= 1 && e.valid <= 1, "invalid static flags");
            for (auto &t : e.text)
                t = id();
            entries.push_back(e);
        }
        count = r.count(4 * 8);
        eventsText.reserve(count);
        for (size_t i = 0; i < count; ++i) {
            std::array<size_t, 4> ids;
            for (auto &t : ids)
                t = id();
            eventsText.push_back(ids);
        }
        require(r.pos == r.end, "trailing export plan data");
    }
};
class Output {
    std::ofstream file;
    std::string buffer;
    SHA256_CTX hash;
    uint64_t size = 0;

  public:
    explicit Output(const fs::path &path) : file(path, std::ios::binary | std::ios::trunc) {
        require(bool(file), "cannot open export output");
        buffer.reserve((1 << 20) + 65536);
        SHA256_Init(&hash);
    }
    void flush() {
        if (buffer.empty())
            return;
        file.write(buffer.data(), std::streamsize(buffer.size()));
        require(bool(file), "export output write failed");
        SHA256_Update(&hash, buffer.data(), buffer.size());
        size += buffer.size();
        buffer.clear();
    }
    void add(const std::string &text) {
        buffer += text;
        if (buffer.size() >= (1 << 20))
            flush();
    }
    void number(uint64_t value) {
        add(std::to_string(value));
    }
    std::string finish() {
        flush();
        file.flush();
        require(bool(file), "export output flush failed");
        file.close();
        require(bool(file), "export output close failed");
        unsigned char digest[32];
        SHA256_Final(digest, &hash);
        return hex(digest, 32);
    }
    uint64_t bytes() const {
        return size;
    }
};
class Export {
    const Plan &plan;
    Output &out;
    bool firstRow = true, hasPending = false, hasContext = false;
    uint64_t context = 0, pendingEntry = 0, pendingFirst = 0, pendingLast = 0, pendingCount = 0;
    const std::string &text(size_t id) const {
        return plan.pool[id];
    }
    void comma() {
        if (!firstRow)
            out.add(",");
        firstRow = false;
    }
    void jsonRow(const char *kind, uint64_t seq, uint64_t end, const std::string &suffix) {
        comma();
        out.add("{\"kind\":\"");
        out.add(kind);
        out.add("\",\"seq\":");
        out.number(seq);
        out.add(",\"endSeq\":");
        out.number(end);
        out.add(suffix);
    }

  public:
    uint64_t rows = 0, matched = 0;
    Export(const Plan &p, Output &o) : plan(p), out(o) {
        out.add(text(plan.header));
    }
    void flush() {
        if (!hasPending)
            return;
        if (plan.kind != 2) {
            hasPending = false;
            return;
        }
        const auto &e = plan.entries[size_t(pendingEntry)];
        jsonRow("code", pendingFirst, pendingLast, text(e.text[6]));
        out.number(pendingCount);
        out.add("}");
        ++rows;
        hasPending = false;
    }
    void instruction(uint64_t seq, size_t identifier, unsigned state) {
        ++matched;
        const auto &e = plan.entries[identifier];
        if (plan.kind == 3) {
            out.number(seq);
            out.add(text(e.text[9 + state]));
            ++rows;
            return;
        }
        if (state == 0)
            return;
        if (state == 2 || !e.valid) {
            flush();
            if (plan.kind == 2)
                jsonRow(state == 2 ? "unknown" : "missing", seq, seq,
                        text(e.text[state == 2 ? 7 : 8]));
            else {
                out.add("/* #");
                out.number(seq);
                out.add(text(e.text[state == 2 ? (plan.kind == 1 ? 3 : 4) : 5]));
            }
            ++rows;
            hasContext = false;
            hasPending = false;
            return;
        }
        if (plan.kind == 1) {
            out.add("/* #");
            out.number(seq);
            out.add(text(e.text[2]));
            out.add(text(e.text[0]));
            ++rows;
            return;
        }
        if (hasPending && plan.entries[size_t(pendingEntry)].group == e.group) {
            pendingLast = seq;
            ++pendingCount;
            return;
        }
        flush();
        if (plan.kind == 0) {
            if (!hasContext || context != e.context) {
                out.add(text(e.text[1]));
                context = e.context;
                hasContext = true;
            }
            out.add(text(e.text[0]));
            ++rows;
        }
        pendingEntry = identifier;
        pendingFirst = pendingLast = seq;
        pendingCount = 1;
        hasPending = true;
    }
    void event(uint64_t seq, size_t stringId) {
        ++matched;
        flush();
        hasContext = false;
        const auto &t = plan.eventsText[stringId];
        if (plan.kind == 3) {
            out.number(seq);
            out.add(text(t[3]));
        } else if (plan.kind == 2)
            jsonRow("event", seq, seq, text(t[2]));
        else if (plan.kind == 1)
            out.add(text(t[0]));
        else {
            out.add("/* #");
            out.number(seq);
            out.add(text(t[1]));
        }
        ++rows;
    }
    void finish() {
        flush();
        out.add(text(plan.footer));
    }
};
struct Block {
    uint64_t offset;
    uint32_t length, count;
    uint16_t type, flags;
    uint32_t crc;
};
static bool eventType(uint64_t type) {
    return type == 1 || type == 2 || type == 3 || (type >= 6 && type <= 10);
}
static void readStore(const fs::path &path, const Plan &plan, Export &output) {
    require(fs::file_size(path) == plan.storeBytes, "store size changed");
    require(hashFile(path) == plan.sha, "store SHA256 mismatch");
    std::ifstream in(path, std::ios::binary);
    require(bool(in), "cannot open store");
    auto header = readBytes(in, 0, 56);
    Reader h(header);
    require(std::memcmp(header.data(), "ETMTSv1\0", 8) == 0, "store magic mismatch");
    h.pos = 8;
    require(h.le(2) == 1 && h.le(2) == 1 && h.le(4) == 0x01020304, "unsupported store format");
    require(h.le(8) == plan.tag, "store generation mismatch");
    auto count = h.le(4);
    require(count > 0 && count <= (1u << 20), "invalid block count");
    require(h.le(4) == 0, "unsupported required store feature");
    auto payloadBytes = h.le(8);
    require(h.le(8) == plan.storeBytes, "invalid store header size");
    require(h.le(4) == crc32(0, header.data(), 48), "store header CRC mismatch");
    (void)h.le(4); // Optional features do not change the required sequence encoding.
    uint64_t next = 56 + count * 24;
    require(next <= plan.storeBytes, "block directory exceeds file");
    auto directory = readBytes(in, 56, size_t(count) * 24);
    Reader d(directory);
    std::vector<Block> blocks;
    uint64_t total = 0;
    for (uint64_t i = 0; i < count; ++i) {
        Block b;
        b.offset = d.le(8);
        b.length = uint32_t(d.le(4));
        b.count = uint32_t(d.le(4));
        b.type = uint16_t(d.le(2));
        b.flags = uint16_t(d.le(2));
        b.crc = uint32_t(d.le(4));
        require(b.type >= 1 && b.type <= 12 && (b.flags & ~1u) == 0, "unsupported store block");
        require(b.offset == next && b.length <= (64u << 20) && b.length <= plan.storeBytes - next,
                "invalid block extent");
        next += b.length;
        total += b.length;
        blocks.push_back(b);
    }
    require(next == plan.storeBytes && total == payloadBytes, "store block lengths mismatch");
    uint64_t expected = plan.first, instructions = 0, events = 0;
    bool sawCheckpoint = false;
    for (const auto &b : blocks) {
        if (b.type != 3)
            continue;
        auto bytes = readBytes(in, b.offset, b.length);
        require(crc32(0, bytes.data(), uInt(bytes.size())) == b.crc, "sequence CRC mismatch");
        if (b.flags & 1) {
            require(b.count <= (64u << 20), "inflated sequence exceeds limit");
            Bytes expanded(b.count);
            z_stream z{};
            z.next_in = bytes.data();
            z.avail_in = uInt(bytes.size());
            z.next_out = expanded.data();
            z.avail_out = b.count;
            require(inflateInit(&z) == Z_OK, "zlib initialization failed");
            int status = inflate(&z, Z_FINISH);
            auto written = z.total_out;
            auto remaining = z.avail_in;
            inflateEnd(&z);
            require(status == Z_STREAM_END && written == b.count && remaining == 0,
                    "invalid compressed sequence");
            bytes.swap(expanded);
        }
        Reader r(bytes);
        while (r.pos < r.end) {
            auto type = r.le(1), length = r.uv();
            r.need(size_t(length));
            Reader record(r.data + r.pos, size_t(length));
            r.pos += size_t(length);
            auto seq = record.uv();
            require(seq == expected, "noncontiguous execution sequence");
            if (type == 3) {
                sawCheckpoint = true;
                continue;
            }
            require(sawCheckpoint, "missing initial checkpoint");
            if (type == 1) {
                auto n = record.uv(), tail = record.le(1);
                require(n > 0 && n <= 4096 && tail <= 2, "invalid run");
                record.uv();
                auto cycles = record.uv(), span = record.uv(), unknown = record.uv(),
                     identifier = record.uv();
                require((cycles == 0 && span == 0) || (cycles > 0 && span >= 1 && span <= 4096),
                        "invalid cycle span");
                require(n <= std::numeric_limits<uint64_t>::max() - expected, "sequence overflow");
                uint64_t predicated = 0;
                for (uint64_t i = 0; i < n; ++i) {
                    if (i) {
                        auto encoded = record.uv();
                        int64_t delta = int64_t(encoded >> 1) ^ -int64_t(encoded & 1);
                        require(identifier <= uint64_t(std::numeric_limits<int64_t>::max()),
                                "invalid static ID");
                        if (delta < 0) {
                            auto magnitude = uint64_t(-(delta + 1)) + 1;
                            require(magnitude <= identifier, "static delta underflow");
                            identifier -= magnitude;
                        } else {
                            require(uint64_t(delta) <=
                                        std::numeric_limits<uint64_t>::max() - identifier,
                                    "static delta overflow");
                            identifier += uint64_t(delta);
                        }
                    }
                    require(identifier < plan.entries.size(), "static ID out of range");
                    const auto &e = plan.entries[size_t(identifier)];
                    unsigned state = e.predicated ? 2u
                                                  : (i + 1 == n ? (tail == 0   ? 1u
                                                                   : tail == 1 ? 0u
                                                                               : 2u)
                                                                : 1u);
                    if (i + 1 < n && e.predicated)
                        ++predicated;
                    if (expected >= plan.start && expected <= plan.end)
                        output.instruction(expected, size_t(identifier), state);
                    ++expected;
                    ++instructions;
                    ++events;
                }
                require(predicated == unknown, "internal predicate count mismatch");
            } else if (type == 2) {
                require(eventType(record.le(1)), "unsupported event type");
                for (int i = 0; i < 5; ++i)
                    record.uv();
                auto id = record.uv();
                require(id < plan.eventsText.size(), "event string ID out of range");
                if (seq >= plan.start && seq <= plan.end)
                    output.event(seq, size_t(id));
                ++expected;
                ++events;
            } else
                throw std::runtime_error("unsupported sequence record");
            require(record.pos == record.end, "trailing sequence record data");
        }
    }
    require(events == plan.events && instructions == plan.instructions && expected == plan.last + 1,
            "execution counts mismatch");
    require(hashFile(path) == plan.sha, "store changed during export");
}
static int run(const std::vector<std::string> &args) {
    if (args.size() == 2 && args[1] == "--version") {
        std::cout << "{\"protocol\":\"etm-export/"
                     "1\",\"abi\":1,\"storeSchema\":1,\"formats\":[\"simplified\",\"source\","
                     "\"simplified-html\",\"csv\"]}\n";
        return 0;
    }
    require(args.size() == 4, "usage: etm_export plan.bin store.bin output");
    auto planPath = filePath(args[1]), storePath = filePath(args[2]),
         outputPath = filePath(args[3]);
    require(fs::absolute(outputPath) != fs::absolute(planPath) &&
                fs::absolute(outputPath) != fs::absolute(storePath),
            "output aliases input");
    require(!fs::exists(outputPath) ||
                (!fs::equivalent(outputPath, planPath) && !fs::equivalent(outputPath, storePath)),
            "output aliases input file");
    Plan plan(planPath);
    Output out(outputPath);
    Export exporter(plan, out);
    readStore(storePath, plan, exporter);
    exporter.finish();
    auto digest = out.finish();
    uint64_t selected = plan.end < plan.start
                            ? 0
                            : std::min(plan.end, plan.last) - std::max(plan.start, plan.first) + 1;
    require(exporter.matched == selected, "export selection count mismatch");
    std::cout << "{\"protocol\":\"etm-export/1\",\"rows\":" << exporter.rows
              << ",\"matchedEvents\":" << exporter.matched << ",\"bytes\":" << out.bytes()
              << ",\"sha256\":\"" << digest << "\"}\n";
    return 0;
}
#ifdef _WIN32
int wmain(int argc, wchar_t **argv) {
    try {
        std::vector<std::string> args;
        for (int i = 0; i < argc; ++i) {
            int n = WideCharToMultiByte(CP_UTF8, WC_ERR_INVALID_CHARS, argv[i], -1, nullptr, 0,
                                        nullptr, nullptr);
            require(n > 0, "invalid command line UTF-16");
            std::string s(size_t(n), '\0');
            WideCharToMultiByte(CP_UTF8, WC_ERR_INVALID_CHARS, argv[i], -1, &s[0], n, nullptr,
                                nullptr);
            s.pop_back();
            args.push_back(s);
        }
        return run(args);
    } catch (const std::exception &error) {
        std::cerr << "native export: " << error.what() << "\n";
        return 1;
    }
}
#else
int main(int argc, char **argv) {
    try {
        return run(std::vector<std::string>(argv, argv + argc));
    } catch (const std::exception &error) {
        std::cerr << "native export: " << error.what() << "\n";
        return 1;
    }
}
#endif
