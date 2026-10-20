// OpenCSD 集成。默认两参数调用写文本 JSONL（参考/诊断模式，逐字节保持旧格式）；
// `--binary <out.bin> trace.bin config-and-memory.txt` 写结构化二进制记录流：
// 显式 magic/版本/字节序/记录类型/记录长度/计数，正常路径不生成说明文字。
// 布局与校验见 etm/tools/native_binary.py 与 docs/validation/20261019-ds-etm-p2-binary-7f6c042。
#include "opencsd/c_api/opencsd_c_api.h"
#include "opencsd/etmv3/trc_pkt_types_etmv3.h"
#include "opencsd/etmv4/trc_pkt_types_etmv4.h"
#include <cstdint>
#include <cstring>
#include <fstream>
#ifdef _WIN32
#include <windows.h>
#endif
#include <iostream>
#include <sstream>
#include <string>
#include <vector>
#include <stdexcept>
#include <iomanip>

#ifdef _WIN32
static std::wstring file_path(const std::string &path) {
    const int length=MultiByteToWideChar(CP_UTF8,MB_ERR_INVALID_CHARS,path.data(),(int)path.size(),0,0);
    if(!length) throw std::runtime_error("Invalid UTF-8 path");
    std::wstring wide(length,0); MultiByteToWideChar(CP_UTF8,MB_ERR_INVALID_CHARS,path.data(),(int)path.size(),&wide[0],length); return wide;
}
#else
static const std::string &file_path(const std::string &path) { return path; }
#endif
namespace {

// ---- 二进制记录流常量（与 native_binary.py 严格一致） --------------------------------------
const char kMagic[8] = {'E', 'T', 'M', 'N', 'A', 'T', '0', '1'};
const uint16_t kSchemaVersion = 1;
const uint16_t kHeaderBytes = 64;
const uint32_t kByteOrderMark = 0x01020304u;
const uint32_t kFeatureRareElementText = 1u << 0;
const uint8_t kRecordElement = 1;
const uint8_t kRecordPacket = 2;
const uint8_t kRecordStatus = 3;
const size_t kFlushBytes = 1u << 20;   // 1 MiB 写缓冲：不逐记录 flush

static std::string quote(const char *text) {
    std::ostringstream s; s << '"';
    for (const unsigned char *p=(const unsigned char *)text; *p; ++p) {
        if (*p=='"' || *p=='\\') s << '\\' << *p;
        else if (*p=='\n') s << "\\n";
        else if (*p=='\r') s << "\\r";
        else if (*p=='\t') s << "\\t";
        else if (*p<32) s << "\\u" << std::hex << std::setw(4) << std::setfill('0') << (unsigned)*p << std::dec;
        else s << *p;
    }
    s << '"'; return s.str();
}

// ---- 元素字段视图 -------------------------------------------------------------------------
// 文本模式和二进制模式共用同一套取值规则，避免两条输出路径出现语义漂移。
// 规则与原文本输出完全一致：只有范围元素带 isa/last*，只有范围/不可访问元素带 start，
// 只有范围/带返回地址的异常元素带 end，其余字段按存在位归零。
struct ElemView {
    int etype;
    uint8_t id;
    int isa;
    uint32_t flags;
    uint32_t cycle;
    bool has_cc;
    uint64_t timestamp;
    bool has_ts;
    unsigned last_exec;
    unsigned last_cond;
    int last_type;
    int last_subtype;
    uint32_t payload;
    uint64_t offset;
    uint64_t start;
    uint64_t end;
    bool exception;
    bool excep_ret;
    bool range;
    bool needs_text;
};

// events.jsonl 的 text 字段真正需要的稀有元素类型（与 decode_stream 的展开集合一致）。
bool rare_text_type(int type) {
    switch (type) {
    case OCSD_GEN_TRC_ELEM_NO_SYNC:
    case OCSD_GEN_TRC_ELEM_TRACE_ON:
    case OCSD_GEN_TRC_ELEM_EO_TRACE:
    case OCSD_GEN_TRC_ELEM_I_RANGE_NOPATH:
    case OCSD_GEN_TRC_ELEM_ADDR_NACC:
    case OCSD_GEN_TRC_ELEM_ADDR_UNKNOWN:
    case OCSD_GEN_TRC_ELEM_EXCEPTION:
    case OCSD_GEN_TRC_ELEM_EXCEPTION_RET:
        return true;
    default:
        return false;
    }
}

ElemView make_view(const ocsd_generic_trace_elem *e, ocsd_trc_index_t index, uint8_t id) {
    ElemView v;
    v.etype = (int)e->elem_type;
    v.id = id;
    v.offset = (uint64_t)index;
    v.range = e->elem_type == OCSD_GEN_TRC_ELEM_INSTR_RANGE || e->elem_type == OCSD_GEN_TRC_ELEM_I_RANGE_NOPATH;
    v.exception = e->elem_type == OCSD_GEN_TRC_ELEM_EXCEPTION;
    v.excep_ret = e->excep_ret_addr != 0;
    v.isa = v.range ? (int)e->isa : 0;
    v.flags = e->flag_bits;
    v.has_cc = e->has_cc != 0;
    v.cycle = e->has_cc ? e->cycle_count : 0;
    v.has_ts = e->has_ts != 0;
    v.timestamp = (e->has_ts || e->elem_type == OCSD_GEN_TRC_ELEM_TIMESTAMP) ? e->timestamp : 0;
    v.last_exec = v.range ? e->last_instr_exec : 0;
    v.last_cond = v.range ? e->last_instr_cond : 0;
    v.last_type = v.range ? (int)e->last_i_type : 0;
    v.last_subtype = v.range ? (int)e->last_i_subtype : 0;
    v.start = (v.range || e->elem_type == OCSD_GEN_TRC_ELEM_ADDR_NACC) ? e->st_addr : 0;
    v.end = (v.range || (v.exception && v.excep_ret)) ? e->en_addr : 0;
    // payload 联合体的前 4 字节：num_instr_range / exception_number / unsync_eot_info /
    // trace_on_reason 都落在同一 4 字节上，文本模式也是按类型选成员后打印这个值。
    uint32_t raw = 0;
    std::memcpy(&raw, &e->num_instr_range, sizeof(raw));
    v.payload = (v.range || v.exception
                 || e->elem_type == OCSD_GEN_TRC_ELEM_NO_SYNC
                 || e->elem_type == OCSD_GEN_TRC_ELEM_EO_TRACE
                 || e->elem_type == OCSD_GEN_TRC_ELEM_TRACE_ON) ? raw : 0;
    v.needs_text = rare_text_type(v.etype);
    return v;
}

// ---- 二进制写入器 -------------------------------------------------------------------------
class BinaryWriter {
public:
    void open(const std::string &path, uint32_t protocol) {
        out_.open(file_path(path).c_str(), std::ios::binary | std::ios::trunc);
        if (!out_) throw std::runtime_error("Cannot write " + path);
        buf_.reserve(kFlushBytes + 4096);
        out_.write(kMagic, sizeof(kMagic));
        put16(kSchemaVersion);
        put16(kHeaderBytes);
        put32(kByteOrderMark);
        put32(protocol);
        put32(kFeatureRareElementText);
        put64(0);   // recordCount，收尾回填
        put64(0);   // elementCount
        put64(0);   // packetCount
        put64(0);   // bodyBytes
        put64(0);   // reserved
        flush();
        opened_ = true;
    }

    void add_element(const ElemView &v, const char *text) {
        size_t text_bytes = v.needs_text ? std::strlen(text) : 0;
        uint8_t flags = 0;
        if (v.has_cc) flags |= 1u << 0;
        if (v.has_ts) flags |= 1u << 1;
        if (v.excep_ret) flags |= 1u << 2;
        if (text_bytes) flags |= 1u << 3;
        char record[61];
        record[0] = kRecordElement;
        store_little(record + 1, (uint32_t)(61 + text_bytes));
        record[5] = (char)v.etype;
        record[6] = (char)v.isa;
        record[7] = (char)v.id;
        record[8] = v.last_exec ? 1 : 0;
        record[9] = v.last_cond ? 1 : 0;
        record[10] = (char)flags;
        store_little(record + 11, (uint32_t)v.flags);
        store_little(record + 15, (uint32_t)v.cycle);
        store_little(record + 19, (uint32_t)v.payload);
        store_little(record + 23, (uint16_t)v.last_type);
        store_little(record + 25, (uint16_t)v.last_subtype);
        store_little(record + 27, v.offset);
        store_little(record + 35, v.start);
        store_little(record + 43, v.end);
        store_little(record + 51, v.timestamp);
        store_little(record + 59, (uint16_t)text_bytes);
        put_bytes(record, sizeof(record));
        if (text_bytes) put_bytes(text, text_bytes);
        elements_++;
        maybe_flush();
    }

    void add_packet(ocsd_trc_index_t index, uint32_t type_code, uint32_t size, const uint8_t *data) {
        char record[22];
        record[0] = kRecordPacket;
        store_little(record + 1, (uint32_t)(22 + size));
        store_little(record + 5, type_code);
        record[9] = 0;        // flags：无包文字，分类由类型码 + 名称表复现。
        store_little(record + 10, (uint64_t)index);
        store_little(record + 18, size);
        put_bytes(record, sizeof(record));
        if (size) put_bytes(data, size);
        packets_++;
        maybe_flush();
    }

    void finish(uint32_t processed, int error_code, const std::string &library, const std::string &error) {
        if (!opened_) return;
        const uint32_t library_bytes = (uint32_t)library.size();
        const uint32_t error_bytes = (uint32_t)error.size();
        put8(kRecordStatus);
        put32(37 + library_bytes + error_bytes);
        put32(processed);
        put32((uint32_t)error_code);
        put32(library_bytes);
        put32(error_bytes);
        put64(elements_);
        put64(packets_);
        put_bytes(library.data(), library_bytes);
        put_bytes(error.data(), error_bytes);
        flush();
        const uint64_t record_count = elements_ + packets_;
        const std::streampos end = out_.tellp();
        const uint64_t body_bytes = (uint64_t)end - (uint64_t)kHeaderBytes;
        out_.seekp(24);
        put64(record_count);
        put64(elements_);
        put64(packets_);
        put64(body_bytes);
        flush();
        out_.close();
        opened_ = false;
    }

    uint64_t elements() const { return elements_; }
    uint64_t packets() const { return packets_; }

private:
    std::ofstream out_;
    std::string buf_;
    uint64_t elements_ = 0;
    uint64_t packets_ = 0;
    bool opened_ = false;

    void put8(uint8_t v) { buf_.push_back((char)v); }
    template <typename T> static void store_little(char* out, T value) {
#if defined(_WIN32) || (defined(__BYTE_ORDER__) && __BYTE_ORDER__ == __ORDER_LITTLE_ENDIAN__)
        std::memcpy(out, &value, sizeof(value));
#else
        for (size_t i = 0; i < sizeof(value); ++i) out[i] = (char)(value >> (8 * i));
#endif
    }
    template <typename T> void put_little(T value) {
#if defined(_WIN32) || (defined(__BYTE_ORDER__) && __BYTE_ORDER__ == __ORDER_LITTLE_ENDIAN__)
        buf_.append(reinterpret_cast<const char*>(&value), sizeof(value));
#else
        for (size_t i = 0; i < sizeof(value); ++i) put8((uint8_t)(value >> (8 * i)));
#endif
    }
    void put16(uint16_t v) { put_little(v); }
    void put32(uint32_t v) { put_little(v); }
    void put64(uint64_t v) { put_little(v); }
    void put_bytes(const void *data, size_t size) { buf_.append((const char *)data, size); }
    void maybe_flush() { if (buf_.size() >= kFlushBytes) flush(); }
    void flush() {
        if (buf_.empty()) return;
        out_.write(buf_.data(), (std::streamsize)buf_.size());
        buf_.clear();
    }
};

}  // namespace

static ocsd_trace_protocol_t packet_protocol = OCSD_PROTOCOL_ETMV4I;
static bool binary_mode = false;
static BinaryWriter binary_writer;

static ocsd_datapath_resp_t element(const void *, ocsd_trc_index_t index, uint8_t id, const ocsd_generic_trace_elem *e) {
    const ElemView view = make_view(e, index, id);
    if (binary_mode) {
        // 正常路径不调用 ocsd_gen_elem_str：只有 events.jsonl 的 text 字段确实需要的
        // 稀有元素类型（NO_SYNC/TRACE_ON/EO_TRACE/I_RANGE_NOPATH/ADDR_NACC/ADDR_UNKNOWN/
        // EXCEPTION/EXCEPTION_RET）才生成说明文字。
        char text[2048];
        text[0] = 0;
        if (view.needs_text) ocsd_gen_elem_str(e, text, sizeof(text));
        binary_writer.add_element(view, text);
        return OCSD_RESP_CONT;
    }
    char text[2048] = {0}; ocsd_gen_elem_str(e,text,sizeof(text));
    std::cout << "{\"kind\":\"element\",\"offset\":" << view.offset << ",\"id\":" << (unsigned)view.id
      << ",\"type\":" << view.etype << ",\"start\":" << view.start
      << ",\"end\":" << view.end
      << ",\"isa\":" << view.isa << ",\"flags\":" << view.flags
      << ",\"cycleCount\":" << view.cycle << ",\"timestamp\":" << view.timestamp
      << ",\"lastExecuted\":" << view.last_exec << ",\"lastConditional\":" << view.last_cond
      << ",\"lastType\":" << view.last_type << ",\"lastSubtype\":" << view.last_subtype
      << ",\"payload\":" << view.payload << ",\"text\":" << quote(text) << "}\n";
    return OCSD_RESP_CONT;
}

static uint32_t packet_type_code(const void *pkt) {
    if (packet_protocol == OCSD_PROTOCOL_ETMV3)
        return (uint32_t)((const ocsd_etmv3_pkt *)pkt)->type;
    return (uint32_t)((const ocsd_etmv4_i_pkt *)pkt)->type;
}

static void packet(const void *, ocsd_datapath_op_t op, ocsd_trc_index_t index, const void *pkt, uint32_t size, const uint8_t *data) {
    if(op!=OCSD_OP_DATA || !pkt) return;
    if (binary_mode) {
        // 正常路径不调用 ocsd_pkt_str：包记录只存偏移/长度/类型码/原始字节。
        binary_writer.add_packet(index, packet_type_code(pkt), size, data);
        return;
    }
    char text[2048]={0}; ocsd_pkt_str(packet_protocol,pkt,text,sizeof(text));
    std::ostringstream hex;
    for(uint32_t i=0;i<size;i++) hex << std::hex << std::setw(2) << std::setfill('0') << (unsigned)data[i];
    std::cout << "{\"kind\":\"packet\",\"offset\":" << index << ",\"size\":" << size
      << ",\"hex\":" << quote(hex.str().c_str()) << ",\"text\":" << quote(text) << "}\n";
}

static std::vector<uint8_t> read(const std::string &path) {
    std::ifstream f(file_path(path).c_str(),std::ios::binary);
    if(!f) throw std::runtime_error("Cannot read "+path);
    return std::vector<uint8_t>((std::istreambuf_iterator<char>(f)),std::istreambuf_iterator<char>());
}
static void check(ocsd_err_t code) { if(code!=OCSD_OK) throw std::runtime_error("OpenCSD API error "+std::to_string(code)); }

static int decode_main(int argc,char **argv) {
    std::string binary_path;
    int first = 1;
    if (argc >= 2 && std::string(argv[1]) == "--binary") {
        if (argc != 5) {
            std::cerr << "Usage: etm_decode trace.bin config-and-memory.txt\n"
                      << "       etm_decode --binary out.bin trace.bin config-and-memory.txt\n";
            return 2;
        }
        binary_path = argv[2]; first = 3;
    } else if (argc != 3) {
        std::cerr << "Usage: etm_decode trace.bin config-and-memory.txt\n"
                  << "       etm_decode --binary out.bin trace.bin config-and-memory.txt\n";
        return 2;
    }
    const std::string trace_path = argv[first];
    const std::string config_path = argv[first + 1];
    binary_mode = !binary_path.empty();
    dcd_tree_handle_t tree=0;
    try {
        std::ifstream config(file_path(config_path).c_str()); if(!config) throw std::runtime_error("Cannot read config");
        uint32_t regs[11]={0}; std::string value;
        if(!(config>>value)) throw std::runtime_error("Missing protocol/register");
        const bool v3=value=="ETMv3";
        unsigned reg_first=0;
        if(!v3) { regs[0]=std::stoul(value,0,0); reg_first=1; }
        for(unsigned i=reg_first;i<(v3?4u:11u);i++) { if(!(config>>value)) throw std::runtime_error("Missing register"); regs[i]=std::stoul(value,0,0); }
        ocsd_etmv4_cfg c={};
        c.reg_idr0=regs[0]; c.reg_idr1=regs[1]; c.reg_idr2=regs[2];
        c.reg_idr8=regs[3]; c.reg_idr9=regs[4]; c.reg_idr10=regs[5]; c.reg_idr11=regs[6]; c.reg_idr12=regs[7]; c.reg_idr13=regs[8];
        c.reg_configr=regs[9]; c.reg_traceidr=regs[10]; c.arch_ver=ARCH_V7; c.core_prof=profile_CortexM;
        tree=ocsd_create_dcd_tree(OCSD_TRC_SRC_SINGLE,0); if(!tree) throw std::runtime_error("No decode tree");
        check(ocsd_dt_set_gen_elem_outfn(tree,element,0));
        unsigned char id=0;
        if(v3) {
            ocsd_etmv3_cfg c3={};
            c3.reg_idr=regs[0]; c3.reg_ctrl=regs[1]; c3.reg_ccer=regs[2]; c3.reg_trc_id=regs[3];
            c3.arch_ver=ARCH_V7; c3.core_prof=profile_CortexM;
            packet_protocol=OCSD_PROTOCOL_ETMV3;
            check(ocsd_dt_create_decoder(tree,OCSD_BUILTIN_DCD_ETMV3,OCSD_CREATE_FLG_FULL_DECODER,&c3,&id));
        } else {
            check(ocsd_dt_create_decoder(tree,OCSD_BUILTIN_DCD_ETMV4I,OCSD_CREATE_FLG_FULL_DECODER,&c,&id));
        }
        check(ocsd_dt_attach_packet_callback(tree,id,OCSD_C_API_CB_PKT_MON,(void *)packet,0));
        std::vector<std::vector<uint8_t> > images;
        while(config>>value) {
            uint64_t address=std::stoull(value,0,0); std::string file; std::getline(config,file);
            file=file.substr(file.find_first_not_of(" \t")); if(!file.empty() && file.back()=='\r') file.pop_back();
            images.push_back(read(file));
            check(ocsd_dt_add_buffer_mem_acc(tree,address,OCSD_MEM_SPACE_ANY,images.back().data(),(uint32_t)images.back().size()));
        }
        if (binary_mode) binary_writer.open(binary_path, v3 ? 2u : 1u);
        const auto raw=read(trace_path); uint32_t done=0;
        auto response=ocsd_dt_process_data(tree,OCSD_OP_DATA,0,(uint32_t)raw.size(),raw.data(),&done);
        if(response>=OCSD_RESP_FATAL_NOT_INIT || done!=raw.size()) throw std::runtime_error("Decode stopped at "+std::to_string(done)+" / "+std::to_string(raw.size())+" response "+std::to_string((int)response));
        response=ocsd_dt_process_data(tree,OCSD_OP_EOT,done,0,0,0);
        if(response>=OCSD_RESP_FATAL_NOT_INIT) throw std::runtime_error("Decoder end-of-trace error");
        char error[4096]={0}; uint8_t chan=0; ocsd_trc_index_t index=0;
        auto code=ocsd_get_last_err(&index,&chan,error,sizeof(error));
        const char *version = ocsd_get_version_str();
        if (binary_mode) {
            binary_writer.finish(done, (int)code, version ? version : "", error);
        } else {
            std::cout << "{\"kind\":\"status\",\"processed\":" << done << ",\"library\":" << quote(version ? version : "")
              << ",\"lastErrorCode\":" << (int)code << ",\"lastError\":" << quote(error) << "}\n";
        }
        ocsd_destroy_dcd_tree(tree); return 0;
    } catch(const std::exception &e) { if(tree) ocsd_destroy_dcd_tree(tree); std::cerr << e.what() << '\n'; return 1; }
}

#ifdef _WIN32
int wmain(int argc,wchar_t **wide) {
    std::vector<std::string> utf8; std::vector<char *> argv;
    for(int i=0;i<argc;i++) {
        int size=WideCharToMultiByte(CP_UTF8,0,wide[i],-1,0,0,0,0); std::string value(size,0);
        WideCharToMultiByte(CP_UTF8,0,wide[i],-1,&value[0],size,0,0); value.resize(size-1); utf8.push_back(value);
    }
    for(auto &value:utf8) argv.push_back(&value[0]);
    return decode_main(argc,argv.data());
}
#else
int main(int argc,char **argv) { return decode_main(argc,argv); }
#endif
