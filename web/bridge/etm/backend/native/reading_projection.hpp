// Bounded, reversible reading projection. Raw store events are never changed.
struct ReadingPlan {
    struct Static { uint64_t pc,size,predicated,valid,group,context,function,flow,it; };
    uint64_t start,end,tag,storeBytes,first,last,events,instructions,threshold,keepFirst,keepLast,maxBlock,flags;
    std::string sha;
    std::vector<Static> entries;
    Bytes eventsText;
    explicit ReadingPlan(const fs::path &path) {
        std::ifstream file(path,std::ios::binary);
        auto n=fs::file_size(path);require(n>=160&&n<(1ull<<30),"invalid reading plan size");
        auto data=readBytes(file,0,size_t(n));
        unsigned char digest[32];SHA256_CTX hash;SHA256_Init(&hash);SHA256_Update(&hash,data.data(),data.size()-32);SHA256_Final(digest,&hash);
        require(!std::memcmp(digest,data.data()+data.size()-32,32),"reading plan SHA256 mismatch");
        Reader r(data.data(),data.size()-32);
        require(!std::memcmp(data.data(),"ETMPRJ1\0",8),"unsupported reading plan");r.pos=8;
        require(r.le(8)==1,"unsupported reading schema");
        start=r.le(8);end=r.le(8);tag=r.le(8);storeBytes=r.le(8);first=r.le(8);last=r.le(8);
        events=r.le(8);instructions=r.le(8);threshold=r.le(8);keepFirst=r.le(8);keepLast=r.le(8);maxBlock=r.le(8);flags=r.le(8);
        require(first<=start&&start<=end&&end<=last&&last<std::numeric_limits<uint64_t>::max()&&events==last-first+1,"invalid reading range");
        require(threshold>=3&&threshold<=1000&&keepFirst+keepLast<=threshold&&keepFirst&&keepLast&&maxBlock&&maxBlock<=512&&(flags&~7u)==0,"invalid reading options");
        sha=r.str();require(sha.size()==64,"invalid reading store hash");
        auto count=r.count(72);entries.reserve(count);
        for(size_t i=0;i<count;++i){Static e;e.pc=r.le(8);e.size=r.le(8);e.predicated=r.le(8);e.valid=r.le(8);e.group=r.le(8);e.context=r.le(8);e.function=r.le(8);e.flow=r.le(8);e.it=r.le(8);
            require(e.predicated<=1&&e.valid<=1&&e.flow<=4&&e.size>0&&e.size<=4,"invalid reading static entry");entries.push_back(e);}
        count=r.le(8);require(count<(1u<<22),"reading event table too large");eventsText.resize(count);require(r.pos==r.end,"trailing reading plan data");
    }
};
struct ReadingNode {
    std::string kind="code";
    uint64_t seq=0,end=0,id=0,instructions=0,unknown=0,notTaken=0,frame=0,context=0,depth=0;
    bool partial=false,returned=false,safe=true;
    std::vector<uint64_t> path,functions;
    std::vector<std::array<uint64_t,2>> unknownPositions;
    bool same(const ReadingNode &b)const {
        return kind==b.kind&&id==b.id&&context==b.context&&depth==b.depth&&end-seq==b.end-b.seq&&instructions==b.instructions&&unknown==b.unknown&&notTaken==b.notTaken&&path==b.path&&partial==b.partial&&returned==b.returned;
    }
};
static std::string readingJSON(const ReadingNode &n) {
    std::string s="{\"kind\":\""+n.kind+"\",\"seq\":"+std::to_string(n.seq)+",\"endSeq\":"+std::to_string(n.end)+",\"entry\":"+std::to_string(n.id)+",\"instructions\":"+std::to_string(n.instructions)+",\"unknown\":"+std::to_string(n.unknown)+",\"notTaken\":"+std::to_string(n.notTaken)+",\"context\":"+std::to_string(n.context)+",\"depth\":"+std::to_string(n.depth)+",\"partial\":"+(n.partial?"true":"false")+",\"returnObserved\":"+(n.returned?"true":"false")+",\"path\":[";
    for(size_t i=0;i<n.path.size();++i){if(i)s+=',';s+=std::to_string(n.path[i]);}s+="],\"functions\":[";
    for(size_t i=0;i<n.functions.size();++i){if(i)s+=',';s+=std::to_string(n.functions[i]);}s+="],\"unknownPositions\":[";
    for(size_t i=0;i<n.unknownPositions.size();++i){if(i)s+=',';s+='['+std::to_string(n.unknownPositions[i][0])+','+std::to_string(n.unknownPositions[i][1])+']';}return s+"]}";
}
class ReadingRepeats {
    const ReadingPlan &p;Output &out;
    std::deque<ReadingNode> buffer;
    std::vector<ReadingNode> pattern,partial;
    uint64_t iterations=0,repeatEnd=0,bufferTokens=0;
    uint64_t weight(const ReadingNode &n)const{return n.path.size()+2*n.unknownPositions.size()+n.functions.size();}
    void pop(){bufferTokens-=weight(buffer.front());emit(buffer.front());buffer.pop_front();}
    void emit(const ReadingNode &n){out.add(readingJSON(n)+"\n");++rows;}
    void endRepeat(){
        if(pattern.empty())return;
        if(iterations<=p.threshold){
            auto stride=pattern.back().end-pattern.front().seq+1;
            for(uint64_t i=0;i<iterations;++i)for(auto n:pattern){n.seq+=i*stride;n.end+=i*stride;emit(n);}
        }
        else {
            uint64_t count=0,skipped=0;for(const auto &n:pattern){count+=n.instructions;skipped+=n.notTaken;}
            auto hidden=iterations-p.keepFirst-p.keepLast;
            out.add("{\"kind\":\"repeat\",\"seq\":"+std::to_string(pattern.front().seq)+",\"endSeq\":"+std::to_string(repeatEnd)+",\"totalIterations\":"+std::to_string(iterations)+",\"shownIterations\":"+std::to_string(p.keepFirst+p.keepLast)+",\"keepFirst\":"+std::to_string(p.keepFirst)+",\"keepLast\":"+std::to_string(p.keepLast)+",\"hiddenIterations\":"+std::to_string(hidden)+",\"hiddenInstructionCount\":"+std::to_string(hidden*count)+",\"instructions\":"+std::to_string(iterations*count)+",\"unknown\":0,\"notTaken\":"+std::to_string(iterations*skipped)+",\"stride\":"+std::to_string(pattern.back().end-pattern.front().seq+1)+",\"pattern\":[");
            for(size_t i=0;i<pattern.size();++i){if(i)out.add(",");out.add(readingJSON(pattern[i]));}out.add("]}\n");++rows;++repeats;hiddenInstructions+=hidden*count;
        }
        pattern.clear();iterations=0;
    }
  public:
    uint64_t rows=0,repeats=0,hiddenInstructions=0;
    ReadingRepeats(const ReadingPlan &plan,Output &output):p(plan),out(output){}
    void add(ReadingNode n){
        bool closedLibrary=n.kind=="library"&&!n.partial&&n.returned&&!n.unknown&&n.path.size()==n.instructions;
        if(!(p.flags&1)||!n.safe||n.kind!="code"&&n.kind!="not-taken"&&!closedLibrary) {finish();emit(n);return;}
        if(!pattern.empty()){
            const auto &expected=pattern[partial.size()];
            bool closed= !partial.empty() || n.frame==pattern.front().frame;
            if(closed&&n.same(expected)){
                partial.push_back(std::move(n));
                if(partial.size()==pattern.size()){
                    ++iterations;repeatEnd=partial.back().end;partial.clear();
                }return;
            }
            auto extra=std::move(partial);partial.clear();endRepeat();
            for(auto &v:extra)add(std::move(v));add(std::move(n));return;
        }
        bufferTokens+=weight(n);buffer.push_back(std::move(n));
        size_t maximum=std::min<size_t>(p.maxBlock,buffer.size()/2);
        for(size_t length=1;length<=maximum;++length){
            size_t required=length*(p.threshold+1);
            if(buffer.size()<required)continue;
            size_t lo=buffer.size()-required,mid=buffer.size()-length;
            if(buffer[lo].frame!=buffer[mid].frame||!buffer.back().same(buffer[lo+length-1]))continue;
            bool same=true;auto depth=buffer[lo].depth;
            for(size_t j=length;j<required;++j){
                if(buffer[lo+j].depth<depth||j%length==0&&buffer[lo+j].frame!=buffer[lo].frame||!buffer[lo+j].same(buffer[lo+j%length])){same=false;break;}
            }
            if(!same)continue;
            while(lo--)pop();
            pattern.assign(buffer.begin(),buffer.begin()+length);
            iterations=p.threshold+1;repeatEnd=buffer.back().end;buffer.clear();bufferTokens=0;return;
        }
        // Keep a finite observation window. A failed short inner repetition
        // must not discard history needed to recognise an enclosing loop.
        while(buffer.size()>(p.threshold+1)*p.maxBlock||buffer.size()>65536||bufferTokens>1048576)pop();
    }
    void finish(){
        while(!pattern.empty()){auto extra=std::move(partial);partial.clear();endRepeat();for(auto &n:extra)add(std::move(n));}
        while(!buffer.empty())pop();
    }
};
class ReadingProjection {
    const ReadingPlan &p;ReadingRepeats &repeats;
    struct Frame {uint64_t function,returnPC,context,id;bool known;};
    std::vector<Frame> stack;
    std::map<std::array<uint64_t,3>,uint64_t> contexts;
    uint64_t nextFrame=0,nextContext=0,previous=0,previousState=0;
    bool hasPrevious=false,hasPending=false;
    ReadingNode pending;
    uint64_t ctx(uint64_t parent,uint64_t site,uint64_t fn){auto key=std::array<uint64_t,3>{parent,site,fn};auto found=contexts.find(key);if(found!=contexts.end())return found->second;return contexts.emplace(key,++nextContext).first->second;}
    void flush(){if(hasPending){repeats.add(std::move(pending));pending=ReadingNode();hasPending=false;}}
    void reset(uint64_t fn){flush();repeats.finish();stack.clear();stack.push_back({fn,~uint64_t(0),ctx(0,0,fn),++nextFrame,false});}
  public:
    uint64_t matched=0,instructions=0,unknown=0,notTaken=0,libraries=0,unknownGroups=0;
    ReadingProjection(const ReadingPlan &plan,ReadingRepeats &r):p(plan),repeats(r){}
    void instruction(uint64_t seq,size_t id,unsigned state){
        ++matched;++instructions;unknown+=state==2;notTaken+=state==0;
        const auto &e=p.entries[id];bool call=false,returned=false,flowBoundary=false;
        if(stack.empty())reset(e.function);
        if(hasPrevious){const auto &old=p.entries[previous];flowBoundary=previousState==1&&old.flow!=0;
            if(previousState==1&&old.flow==1){
                if(!hasPending||pending.kind!="library")flush();require(stack.size()<128,"reading call depth exceeds limit");
                auto context=ctx(stack.back().context,old.pc,e.function);stack.push_back({e.function,old.pc+old.size,context,++nextFrame,true});call=true;
            }else if(previousState==1&&(old.flow==2||old.flow==3)){
                size_t found=stack.size();for(size_t j=stack.size();j>1;--j)if(stack[j-1].returnPC==e.pc){found=j-1;break;}
                if(found<stack.size()){
                    if(hasPending&&(pending.kind=="library"||pending.kind=="missing")&&pending.depth>=found+1){pending.returned=true;flush();}
                    stack.resize(found);returned=true;
                }else if(old.function!=e.function){
                    if(old.flow==3&&old.function&&e.function){
                        // An observed inter-function branch is a tail transfer,
                        // not a fresh call. Preserve this frame's return address.
                        if(!hasPending||pending.kind!="library")flush();
                        stack.back().function=e.function;
                        auto parent=stack.size()>1?stack[stack.size()-2].context:0;
                        stack.back().context=ctx(parent,old.pc,e.function);
                    }else reset(e.function);
                }
            }else if(old.function!=e.function)reset(e.function);
        }
        if(stack.back().function!=e.function)reset(e.function);
        auto &frame=stack.back();
        bool library=(p.flags&2)&&!e.valid;
        if(library){
            if(hasPending&&pending.unknownPositions.size()>=4096){pending.partial=true;flush();}
            bool canExtend=hasPending&&(pending.kind=="library"||pending.kind=="missing")&&stack.size()>=pending.depth;
            if(!canExtend){flush();hasPending=true;pending.kind="library";pending.id=id;pending.seq=seq;pending.frame=frame.id;pending.depth=stack.size();pending.context=frame.context;pending.partial=!call||!frame.known;++libraries;}
            pending.end=seq;++pending.instructions;pending.unknown+=state==2;pending.notTaken+=state==0;
            if(pending.path.size()<4096)pending.path.push_back(id*3+state);
            if(e.flow==4)pending.safe=false;
            if(state==2)pending.unknownPositions.push_back({seq,id});
            if(std::find(pending.functions.begin(),pending.functions.end(),e.function)==pending.functions.end())pending.functions.push_back(e.function);
        }else{
            if(hasPending&&pending.kind=="library"){pending.partial=true;flush();}
            std::string kind=state==2?"unknown":state==0?"not-taken":e.valid?"code":"missing";
            bool same=hasPending&&pending.kind==kind&&pending.frame==frame.id&&pending.path.size()<4096;
            if(same){const auto &first=p.entries[pending.id];
                if(kind=="unknown")same=(p.flags&4)&&e.it&&e.it==first.it;
                else same=e.group==first.group&&e.context==first.context&&!flowBoundary&&e.pc>p.entries[previous].pc;
            }
            if(!same){flush();hasPending=true;pending.kind=kind;pending.id=id;pending.seq=seq;pending.frame=frame.id;pending.depth=stack.size();pending.context=frame.context;if(kind=="unknown")++unknownGroups;}
            pending.end=seq;++pending.instructions;pending.unknown+=state==2;pending.notTaken+=state==0;pending.path.push_back(id*3+state);
            if(e.flow==4)pending.safe=false;
        }
        previous=id;previousState=state;hasPrevious=true;
    }
    void event(uint64_t seq,size_t text,unsigned type){
        ++matched;flush();repeats.finish();ReadingNode n;n.kind="event";n.seq=n.end=seq;n.id=text;n.path={type};repeats.add(n);stack.clear();hasPrevious=false;
    }
    void finish(){flush();repeats.finish();}
};
