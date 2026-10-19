// Native bulk import of the verified browse sidecar. No decoding or hardware IO.
#include <sqlite3.h>
#include <algorithm>
#include <array>
#include <cstdint>
#include <filesystem>
#include <fstream>
#include <iostream>
#include <map>
#include <stdexcept>
#include <string>
#include <unordered_map>
#include <vector>

using I = sqlite3_int64;
static std::string quote(const std::string &s) {
    std::string out="\"";
    for (unsigned char c:s) {
        if(c=='"'||c=='\\'){out+='\\';out+=char(c);}
        else if(c<32){const char *hex="0123456789abcdef";out+="\\u00";out+=hex[c>>4];out+=hex[c&15];}
        else out+=char(c);
    }
    return out+'"';
}
static I number(const std::string &s) {
    size_t end=0;I value=std::stoll(s,&end);
    if(end!=s.size()||value<0)throw std::runtime_error("invalid integer in sidecar");
    return value;
}
static std::string array(const std::vector<I> &v) {
    std::string out="[";bool first=true;
    for(I n:v){if(!first)out+=',';first=false;out+=std::to_string(n);}return out+"]";
}
struct DB {
    sqlite3 *db=nullptr;
    explicit DB(const std::filesystem::path &p) {
        if(sqlite3_open(p.u8string().c_str(),&db)!=SQLITE_OK)throw std::runtime_error("cannot open index");
    }
    ~DB(){if(db)sqlite3_close(db);}
    void exec(const char *sql){char *error=nullptr;if(sqlite3_exec(db,sql,nullptr,nullptr,&error)!=SQLITE_OK){std::string text=error?error:"SQLite error";sqlite3_free(error);throw std::runtime_error(text);}}
};
struct Statement {
    sqlite3_stmt *p=nullptr;sqlite3 *db;
    Statement(sqlite3 *d,const char *sql):db(d){if(sqlite3_prepare_v2(d,sql,-1,&p,nullptr)!=SQLITE_OK)throw std::runtime_error(sqlite3_errmsg(d));}
    ~Statement(){if(p)sqlite3_finalize(p);}
    void integer(int i,I n){sqlite3_bind_int64(p,i,n);}
    void nullable_number(int i,const std::string &s){if(s.empty())sqlite3_bind_null(p,i);else integer(i,number(s));}
    void text(int i,const std::string &s){sqlite3_bind_text(p,i,s.data(),int(s.size()),SQLITE_TRANSIENT);}
    void optional_text(int i,const std::string &s){if(s.empty())sqlite3_bind_null(p,i);else text(i,s);}
    void run(){if(sqlite3_step(p)!=SQLITE_DONE)throw std::runtime_error(sqlite3_errmsg(db));sqlite3_reset(p);sqlite3_clear_bindings(p);}
};
struct Lane {I count=0;std::vector<I> bins=std::vector<I>(1600);};
struct Marker {I start=0,end=0;std::map<std::string,I> types;};
#pragma pack(push,1)
struct Metric {uint32_t instruction,path;uint64_t cycles;uint8_t state,call;uint16_t reserved;};
#pragma pack(pop)
static_assert(sizeof(Metric)==20,"metric layout");
struct MetricsUnavailable:std::runtime_error {using std::runtime_error::runtime_error;};
static std::string path_json(const std::string &body) {
    size_t pos=body.find("\"callPath\":");if(pos==std::string::npos)throw MetricsUnavailable("missing call path");
    pos=body.find('[',pos);if(pos==std::string::npos)throw MetricsUnavailable("missing path array");
    size_t start=pos;bool quoted=false,escaped=false;
    for(++pos;pos<body.size();++pos){char c=body[pos];if(escaped){escaped=false;continue;}if(c=='\\'&&quoted){escaped=true;continue;}if(c=='"'){quoted=!quoted;continue;}if(c==']'&&!quoted)return body.substr(start,pos-start+1);}
    throw MetricsUnavailable("incomplete call path");
}
static uint64_t cycles(const std::string &body) {
    size_t pos=body.find("\"cycleCount\":");if(pos==std::string::npos)return 0;pos+=13;
    while(pos<body.size()&&body[pos]==' ')++pos;
    size_t end=pos;while(end<body.size()&&body[end]>='0'&&body[end]<='9')++end;
    if(end==pos)throw MetricsUnavailable("invalid cycles");
    uint64_t value;
    try{value=std::stoull(body.substr(pos,end-pos));}catch(const std::exception&){throw MetricsUnavailable("invalid cycles");}
    if(value>UINT32_MAX)throw MetricsUnavailable("wide cycle count");return value;
}
struct Timeline {
    I total,first=-1,seen=0,markers_count=0;bool valid=true;
    std::unordered_map<std::string,Lane> lanes;
    std::vector<I> unknown=std::vector<I>(1600),skipped=std::vector<I>(1600);
    std::map<int,Marker> markers;
    explicit Timeline(I n):total(n){}
    void feed(I seq,const std::array<std::string,10>&f) {
        if(!valid)return;if(first<0)first=seq;
        if(seq!=first+seen||total<=0){valid=false;return;}++seen;
        int bucket=int(std::min<I>(1599,(seq-first)*1600/total));
        if(f[1]!="instruction"){
            I type=f[8].empty()?0:number(f[8]);
            if(type==1||type==2||type==3||type==6||type==7||type==8||type==9||type==10){
                auto it=markers.find(bucket);if(it==markers.end()){Marker m;m.start=m.end=seq;it=markers.emplace(bucket,m).first;}
                it->second.end=seq;++it->second.types[std::to_string(type)];++markers_count;
            }return;
        }
        if(f[7]=="executed"){
            std::string fn=f[3].empty()?"(未知函数)":f[3];
            auto it=lanes.find(fn);if(it==lanes.end()){if(lanes.size()>=256){valid=false;return;}it=lanes.emplace(fn,Lane()).first;}
            ++it->second.count;++it->second.bins[bucket];
        }else if(f[7]=="not-taken")++skipped[bucket];else ++unknown[bucket];
    }
    std::string json() const {
        if(!valid||seen!=total||first<0)return "";
        std::vector<std::string> order;I executed=0,u=0,s=0,other=0;std::vector<I> others(1600);
        for(const auto &item:lanes){order.push_back(item.first);executed+=item.second.count;}
        std::sort(order.begin(),order.end(),[&](const std::string&a,const std::string&b){I x=lanes.at(a).count,y=lanes.at(b).count;return x==y?a<b:x>y;});
        for(I n:unknown)u+=n;for(I n:skipped)s+=n;
        std::string ls="[";bool comma=false;
        auto lane=[&](const std::string&key,const std::string&name,const std::string&kind,I count,const std::vector<I>&bins){
            if(comma)ls+=',';comma=true;ls+="{\"key\":"+quote(key)+",\"name\":"+quote(name)+",\"kind\":"+quote(kind)+",\"records\":"+std::to_string(count)+",\"bins\":"+array(bins)+"}";
        };
        for(size_t i=0;i<order.size();++i){const auto &name=order[i];const auto &v=lanes.at(name);if(i<6)lane("fn:"+name,name,"function",v.count,v.bins);else{other+=v.count;for(int j=0;j<1600;++j)others[j]+=v.bins[j];}}
        if(other)lane("other","其他函数","other",other,others);
        if(u)lane("unknown","执行状态未知","unknown",u,unknown);
        if(s)lane("not-taken","未成立路径（未证实执行）","not-taken",s,skipped);
        ls+=']';std::string ms="[";comma=false;
        for(const auto &item:markers){const auto &m=item.second;if(comma)ms+=',';comma=true;I count=0;std::string types="{";bool tc=false;
            for(const auto &t:m.types){if(tc)types+=',';tc=true;types+=quote(t.first)+":"+std::to_string(t.second);count+=t.second;}
            ms+="{\"startSeq\":"+std::to_string(m.start)+",\"endSeq\":"+std::to_string(m.end)+",\"seq\":"+std::to_string(m.start)+",\"types\":"+types+"},\"count\":"+std::to_string(count)+"}";
        }
        I last=first+total-1;
        return "{\"range\":{\"start\":"+std::to_string(first)+",\"end\":"+std::to_string(last)+",\"totalStart\":"+std::to_string(first)+",\"totalEnd\":"+std::to_string(last)+"},\"lanes\":"+ls+",\"binCount\":1600,\"markers\":"+ms+"],\"markerCount\":"+std::to_string(markers_count)+",\"summary\":{\"executed\":"+std::to_string(executed)+",\"unknown\":"+std::to_string(u)+",\"notTaken\":"+std::to_string(s)+",\"filteredRecords\":"+std::to_string(total)+",\"markers\":"+std::to_string(markers_count)+"},\"timing\":{\"axis\":\"execution-order\",\"verifiedTime\":false,\"reason\":\"当前记录没有已验收的逐指令时间戳，横轴只表示执行序号\"}}";
    }
};
static int build(const std::filesystem::path &database,const std::filesystem::path &events,const std::filesystem::path &columns,I expected) {
    DB db(database);db.exec("PRAGMA journal_mode=OFF;PRAGMA synchronous=OFF;PRAGMA cache_size=-262144;BEGIN");
    Statement templ(db.db,"INSERT INTO event_templates(id,kind,fn,file,line,type,search,pc,flow) VALUES (?,?,?,?,?,?,?,?,?)");
    Statement row(db.db,"INSERT INTO event_rows(seq,pc,flow,state,template,src,data) VALUES (?,?,?,?,?,?,NULL)");
    Statement nav(db.db,"INSERT INTO event_navigation(mode,seq) VALUES (?,?)");
    Statement meta(db.db,"INSERT INTO meta(key,value) VALUES (?,?)");
    Statement path_insert(db.db,"INSERT INTO metric_paths(id,path) VALUES (?,?)");
    Statement block(db.db,"INSERT INTO metric_blocks(first,last,data) VALUES (?,?,?)");
    std::ifstream raw(events,std::ios::binary),side(columns,std::ios::binary);
    if(!raw||!side)throw std::runtime_error("missing event source or sidecar");
    std::string header,line,body;std::getline(side,header);
    if(header.rfind("#etm-browse-columns 2 ",0)!=0)throw std::runtime_error("unsupported sidecar");
    std::unordered_map<std::string,I> templates,paths;I offset=0,count=0,first=-1,first_record=-1,block_first=0;Timeline timeline(expected);
    uint16_t endian=1;bool metrics_ok=*reinterpret_cast<char*>(&endian)==1&&expected<=UINT32_MAX;
    std::vector<Metric> metrics;metrics.reserve(8192);
    auto flush=[&](){if(metrics.empty())return;block.integer(1,block_first);block.integer(2,block_first+I(metrics.size())-1);sqlite3_bind_blob(block.p,3,metrics.data(),int(metrics.size()*sizeof(Metric)),SQLITE_TRANSIENT);block.run();metrics.clear();};
    while(std::getline(raw,body)){
        if(!std::getline(side,line))throw std::runtime_error("sidecar shorter than events");
        if(!line.empty()&&line.back()=='\r')line.pop_back();
        std::array<std::string,10> f;size_t begin=0;
        for(int i=0;i<9;++i){size_t end=line.find('\t',begin);if(end==std::string::npos)throw std::runtime_error("invalid sidecar columns");f[i]=line.substr(begin,end-begin);begin=end+1;}f[9]=line.substr(begin);
        I seq=number(f[0]);if(first<0&&f[1]=="instruction")first=seq;
        if(first_record<0)first_record=seq;
        if(seq!=first_record+count)metrics_ok=false;
        std::string key=f[1]+'\t'+f[3]+'\t'+f[4]+'\t'+f[5]+'\t'+f[8]+'\t'+f[9];
        auto it=templates.find(key);I id;
        if(it==templates.end()){
            id=I(templates.size());templates.emplace(std::move(key),id);
            templ.integer(1,id);templ.text(2,f[1]);templ.optional_text(3,f[3]);templ.optional_text(4,f[4]);templ.nullable_number(5,f[5]);templ.nullable_number(6,f[8]);templ.text(7,f[9]);templ.nullable_number(8,f[2]);templ.optional_text(9,f[6]);templ.run();
        }else id=it->second;
        row.integer(1,seq);row.nullable_number(2,f[2]);row.optional_text(3,f[6]);row.optional_text(4,f[7]);row.integer(5,id);row.integer(6,offset);row.run();
        auto navigation=[&](const char *mode){nav.text(1,mode);nav.integer(2,seq);nav.run();};
        I type=f[8].empty()?-1:number(f[8]);
        if(f[7]=="executed"&&(f[6]=="call"||f[6]=="return"))navigation(f[6].c_str());
        if(type==9||type==10)navigation("exception");
        if(f[7]=="unknown"||type==1||type==2||type==6||type==7||type==8)navigation("issue");
        if(f[1]!="instruction")navigation("event");
        if(metrics_ok){
            try{
                Metric metric{uint32_t(id),UINT32_MAX,0,3,0,0};
                if(f[1]=="instruction"){
                    std::string path=path_json(body);if(path=="[]")throw MetricsUnavailable("empty path");
                    auto found=paths.find(path);I path_id;
                    if(found==paths.end()){
                        if(paths.size()>=8192)throw MetricsUnavailable("too many paths");
                        path_id=I(paths.size());paths.emplace(path,path_id);path_insert.integer(1,path_id);path_insert.text(2,path);path_insert.run();
                    }else path_id=found->second;
                    metric.path=uint32_t(path_id);metric.cycles=cycles(body);
                    metric.state=f[7]=="executed"?1:(f[7]=="not-taken"?0:2);metric.call=f[6]=="call";
                }
                if(metrics.empty())block_first=seq;metrics.push_back(metric);if(metrics.size()>=8192)flush();
            }catch(const MetricsUnavailable&){metrics_ok=false;metrics.clear();}
        }
        timeline.feed(seq,f);offset+=I(body.size())+(raw.eof()?0:1);++count;
    }
    if(count!=expected||std::getline(side,line))throw std::runtime_error("event/sidecar count mismatch");
    if(metrics_ok){flush();meta.text(1,"metrics-v1");meta.text(2,"{\"first\":"+std::to_string(first_record)+",\"count\":"+std::to_string(count)+"}");meta.run();}
    std::string chart=timeline.json();if(!chart.empty()){meta.text(1,"function-timeline");meta.text(2,chart);meta.run();}
    db.exec("COMMIT");std::cout<<"{\"indexFormat\":7,\"rows\":"<<count<<",\"firstSeq\":"<<(first<0?"null":std::to_string(first))<<",\"metrics\":"<<(metrics_ok?"true":"false")<<"}\n";return 0;
}
#ifdef _WIN32
int wmain(int argc,wchar_t **argv) {
#else
int main(int argc,char **argv) {
#endif
    try{
        if(argc!=5)throw std::runtime_error("usage: etm_index database events sidecar expected_count");
        return build(std::filesystem::path(argv[1]),std::filesystem::path(argv[2]),std::filesystem::path(argv[3]),number(std::filesystem::path(argv[4]).u8string()));
    }catch(const std::exception &e){std::cerr<<e.what()<<'\n';return 1;}
}
