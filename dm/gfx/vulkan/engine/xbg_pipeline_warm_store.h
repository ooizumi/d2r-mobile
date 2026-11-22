// Durable, asynchronous persistence for vk-pipeline-list.bin.
// The render thread only appends completed records to memory.  A single helper
// thread owns all list I/O, coalesces records for a short real-time interval,
// retries failures without dropping pending bytes, and replaces the list only
// after the complete old valid prefix plus pending records is synced.
#pragma once

#include <algorithm>
#include <atomic>
#include <cerrno>
#include <chrono>
#include <condition_variable>
#include <cstddef>
#include <cstdint>
#include <cstdio>
#include <cstring>
#include <fcntl.h>
#include <functional>
#include <limits>
#include <mutex>
#include <string>
#include <thread>
#include <sys/stat.h>
#include <sys/resource.h>
#include <unistd.h>
#include <utility>
#include <vector>

namespace xbg_pipeline_warm_store {

constexpr uint32_t record_magic=0x57504c58u;
constexpr uint32_t max_stages=5u;
constexpr uint32_t max_shader_key=256u;
constexpr uint32_t max_pipeline_words=4096u;
constexpr size_t max_list_bytes=64u<<20;

inline uint32_t load_u32(const uint8_t *p){
    return uint32_t(p[0])|uint32_t(p[1])<<8|uint32_t(p[2])<<16|uint32_t(p[3])<<24;
}

// Return the first malformed record start.  A zero-length file has prefix 0.
// Concatenated records are the only accepted representation; bytes after the
// first bad record are deliberately ignored, matching merge_warm_lists.py and
// warm::start.  The durable writer drops those bytes during its next replace.
inline bool valid_prefix(const uint8_t *data,size_t bytes,size_t &prefix,std::string *why=nullptr){
    size_t i=0;
    auto fail=[&](const char *text){if(why)*why=std::string(text)+" at byte "+std::to_string(i);return false;};
    while(i<bytes){
        if(bytes-i<8)return fail("truncated record header");
        if(load_u32(data+i)!=record_magic)return fail("bad record magic");
        const uint32_t stages=load_u32(data+i+4);i+=8;
        if(stages>max_stages)return fail("too many shader stages");
        for(uint32_t stage=0;stage<stages;stage++){
            if(bytes-i<4)return fail("truncated shader-key length");
            const uint32_t length=load_u32(data+i);i+=4;
            if(length>max_shader_key)return fail("oversize shader key");
            if(size_t(length)>bytes-i)return fail("truncated shader key");i+=length;
        }
        if(bytes-i<4)return fail("truncated pipeline-key length");
        const uint32_t words=load_u32(data+i);i+=4;
        if(words>max_pipeline_words)return fail("oversize pipeline key");
        const size_t payload=size_t(words)*4;
        if(payload>bytes-i)return fail("truncated pipeline key");i+=payload;
        prefix=i;
    }
    return true;
}

enum class Inspect { missing, valid, malformed, unreadable, too_large };

inline bool read_all(const std::string &path,std::vector<uint8_t> &data,Inspect &state,std::string &error){
    data.clear();state=Inspect::unreadable;error.clear();
    int fd=open(path.c_str(),O_RDONLY|O_CLOEXEC);
    if(fd<0){if(errno==ENOENT){state=Inspect::missing;return true;}error=std::strerror(errno);return false;}
    struct stat st{};
    if(fstat(fd,&st)){error=std::strerror(errno);close(fd);return false;}
    if(st.st_size<0 || size_t(st.st_size)>max_list_bytes){error="pipeline warm list exceeds 64 MiB";close(fd);state=Inspect::too_large;return false;}
    data.resize(size_t(st.st_size));
    size_t done=0;
    while(done<data.size()){
        ssize_t n=read(fd,data.data()+done,data.size()-done);
        if(n>0){done+=size_t(n);continue;}
        if(n<0 && errno==EINTR)continue;
        error=n==0?"short read":std::strerror(errno);close(fd);data.clear();return false;
    }
    const int close_error=close(fd);
    if(close_error){error=std::strerror(errno);data.clear();return false;}
    size_t prefix=0;std::string reason;
    if(!valid_prefix(data.data(),data.size(),prefix,&reason)){state=Inspect::malformed;error=std::move(reason);return true;}
    state=Inspect::valid;return true;
}

inline bool inspect(const std::string &path,Inspect &state,std::string &error){
    std::vector<uint8_t> data;return read_all(path,data,state,error);
}

inline bool default_write_temp(const std::string &path,const std::vector<uint8_t> &data,std::string &error){
    int fd=open(path.c_str(),O_WRONLY|O_CREAT|O_TRUNC|O_CLOEXEC,0644);
    if(fd<0){error=std::strerror(errno);return false;}
    size_t done=0;bool ok=true;
    while(done<data.size()){
        ssize_t n=write(fd,data.data()+done,data.size()-done);
        if(n>0){done+=size_t(n);continue;}
        if(n<0 && errno==EINTR)continue;
        error=n==0?"short write":std::strerror(errno);ok=false;break;
    }
    if(ok && fsync(fd)){error=std::strerror(errno);ok=false;}
    if(close(fd) && ok){error=std::strerror(errno);ok=false;}
    return ok;
}

inline bool default_rename_replace(const std::string &from,const std::string &to,std::string &error){
    if(rename(from.c_str(),to.c_str())){error=std::strerror(errno);return false;}
    return true;
}

inline std::string parent_directory(const std::string &path){
    const size_t slash=path.find_last_of('/');
    if(slash==std::string::npos)return ".";
    if(slash==0)return "/";
    return path.substr(0,slash);
}

inline bool default_sync_directory(const std::string &path,std::string &error){
    const std::string parent=parent_directory(path);
    int flags=O_RDONLY|O_CLOEXEC;
#ifdef O_DIRECTORY
    flags|=O_DIRECTORY;
#endif
    int fd=open(parent.c_str(),flags);
    if(fd<0){error=std::strerror(errno);return false;}
    bool ok=fsync(fd)==0;
    if(!ok)error=std::strerror(errno);
    if(close(fd) && ok){error=std::strerror(errno);ok=false;}
    return ok;
}

struct ReplaceIo {
    std::function<bool(const std::string&,const std::vector<uint8_t>&,std::string&)> write_temp;
    std::function<bool(const std::string&,const std::string&,std::string&)> rename_replace;
    std::function<bool(const std::string&,std::string&)> sync_directory;
};

// Atomically replace path with its valid old prefix followed by pending.
// The old file is untouched until the complete temporary file is fsynced and
// rename succeeds.  A malformed old tail is ignored and removed.  Concurrent
// appends are not allowed here; the caller passes one immutable snapshot.
inline bool replace(const std::string &path,const std::vector<uint8_t> &pending,const ReplaceIo &io,std::string &error,std::string *durability_warning=nullptr){
    error.clear();if(durability_warning)durability_warning->clear();
    std::vector<uint8_t> old;Inspect state=Inspect::missing;
    if(!read_all(path,old,state,error))return false;
    size_t prefix=0;std::string reason;
    const bool old_well_formed=valid_prefix(old.data(),old.size(),prefix,&reason);
    if(!old_well_formed && state!=Inspect::malformed)return false;
    size_t pending_prefix=0;
    if(!valid_prefix(pending.data(),pending.size(),pending_prefix,&reason)){
        error="invalid pending pipeline list: "+reason;return false;
    }
    if(pending_prefix!=pending.size()){error="pending pipeline list has a malformed tail";return false;}
    const bool malformed=state==Inspect::malformed;
    if(!malformed && pending.empty())return true;
    if(pending.size()>max_list_bytes-prefix){error="replacement pipeline warm list exceeds 64 MiB";return false;}
    std::vector<uint8_t> replacement;
    replacement.reserve(prefix+pending.size());
    replacement.insert(replacement.end(),old.begin(),old.begin()+std::ptrdiff_t(prefix));
    replacement.insert(replacement.end(),pending.begin(),pending.end());
    const std::string tmp=path+".tmp";
    unlink(tmp.c_str());
    ReplaceIo hooks=io;
    if(!hooks.write_temp)hooks.write_temp=[](const std::string &p,const std::vector<uint8_t> &d,std::string &e){return default_write_temp(p,d,e);};
    if(!hooks.rename_replace)hooks.rename_replace=[](const std::string &a,const std::string &b,std::string &e){return default_rename_replace(a,b,e);};
    if(!hooks.sync_directory)hooks.sync_directory=[](const std::string &p,std::string &e){return default_sync_directory(p,e);};
    if(!hooks.write_temp(tmp,replacement,error)){unlink(tmp.c_str());return false;}
    if(!hooks.rename_replace(tmp,path,error)){unlink(tmp.c_str());return false;}
    // rename is the commit point.  A failed directory sync is reported for
    // power-loss durability but must not retain pending bytes and append the
    // same snapshot again on retry.
    std::string sync_error;
    if(!hooks.sync_directory(path,sync_error) && durability_warning)*durability_warning=sync_error;
    return true;
}

struct AutosaveOptions {
    std::chrono::milliseconds debounce{2000};
    std::chrono::milliseconds retry{2000};
    std::chrono::milliseconds request_grace{500};
    ReplaceIo io;
};

// Owns list pending bytes and one low-priority persistence thread.  append()
// is the only render-thread operation and never performs I/O.  A successful
// write removes exactly the snapshot that was written; bytes appended during
// that write remain pending.  Failed writes leave the entire pending buffer in
// place and retry by real time.
class Autosave {
public:
    Autosave()=default;
    Autosave(const Autosave&)=delete;
    Autosave& operator=(const Autosave&)=delete;
    ~Autosave(){stop();}

    bool start(const std::string &path,AutosaveOptions options={},std::function<void(const std::string&)> log={}){
        if(worker_.joinable())stop();
        {
            std::lock_guard<std::mutex> lock(mu_);
            path_=path;debounce_=options.debounce;retry_=options.retry;request_grace_=options.request_grace;io_=std::move(options.io);log_=std::move(log);
            pending_.clear();stopping_=false;scheduled_=false;urgent_=false;repair_=false;urgent_sequence_=0;urgent_until_={};
            attempts_=0;failures_=0;error_.clear();started_=true;
        }
        // A malformed local tail is repaired by the first durable replace even
        // when no new pipeline arrives.  Unreadable/oversize files are not
        // replaced: doing so could discard the only copy.
        Inspect state=Inspect::missing;std::string reason;
        if(inspect(path,state,reason) && state==Inspect::malformed){
            std::lock_guard<std::mutex> lock(mu_);
            repair_=true;scheduled_=true;deadline_=clock::now()+debounce_;
            log_locked("NVN warm list repair scheduled: "+reason);
        }else if(state==Inspect::too_large || (state==Inspect::unreadable)){
            std::lock_guard<std::mutex> lock(mu_);
            error_=reason;log_locked("NVN warm list unavailable; keeping it unchanged: "+reason);
        }
        try{worker_=std::thread([this]{run();});}
        catch(const std::exception &e){
            std::lock_guard<std::mutex> lock(mu_);
            started_=false;error_=std::string("worker start: ")+e.what();return false;
        }
        return true;
    }

    bool append(std::vector<uint8_t> record){
        if(record.empty())return true;
        size_t prefix=0;std::string reason;
        if(!valid_prefix(record.data(),record.size(),prefix,&reason) || prefix!=record.size()){
            std::lock_guard<std::mutex> lock(mu_);
            error_="refused malformed pending record: "+reason;return false;
        }
        {
            std::lock_guard<std::mutex> lock(mu_);
            if(!started_ || stopping_){error_="pipeline warm autosave is not running";return false;}
            pending_.insert(pending_.end(),record.begin(),record.end());
            publish_pending_locked();
            const bool requested=clock::now()<=urgent_until_;
            if(!scheduled_){scheduled_=true;deadline_=requested?clock::now():clock::now()+debounce_;}
            if(requested)urgent_=true;
        }
        cv_.notify_one();return true;
    }

    // Android onPause calls this through the public xbg API.  It makes the
    // current pending batch due immediately but never performs file I/O on the
    // caller or waits for the driver-cache writer.
    void request_now(){
        {
            std::lock_guard<std::mutex> lock(mu_);
            if(!started_ || stopping_){idle_.notify_all();return;}
            urgent_until_=clock::now()+request_grace_;
            if(pending_.empty() && !repair_){idle_.notify_all();return;}
            scheduled_=true;urgent_=true;deadline_=clock::now();++urgent_sequence_;
        }
        cv_.notify_one();idle_.notify_all();
    }

    // Wait only for this list worker.  Returns false on timeout or a retained
    // pending batch, never by serializing the Vulkan pipeline cache.
    bool wait_idle(std::chrono::milliseconds timeout){
        std::unique_lock<std::mutex> lock(mu_);
        if(!started_)return false;
        return idle_.wait_for(lock,timeout,[this]{return pending_.empty() && !repair_ && !scheduled_;});
    }

    void stop(){
        if(!worker_.joinable())return;
        {
            std::lock_guard<std::mutex> lock(mu_);
            stopping_=true;
            if(!pending_.empty() || repair_){scheduled_=true;urgent_=true;deadline_=clock::now();}else scheduled_=false;
        }
        cv_.notify_all();
        worker_.join();
        std::lock_guard<std::mutex> lock(mu_);
        started_=false;
    }

    size_t pending_bytes()const{return pending_bytes_.load();}
    uint64_t attempts()const{return attempts_.load();}
    uint64_t failures()const{return failures_.load();}
    std::string last_error()const{std::lock_guard<std::mutex> lock(mu_);return error_;}

private:
    using clock=std::chrono::steady_clock;
    static std::chrono::milliseconds nonzero(std::chrono::milliseconds value,std::chrono::milliseconds fallback){return value.count()>0?value:fallback;}

    void log_locked(const std::string &line){if(log_)log_(line);}
    void publish_pending_locked(){pending_bytes_.store(pending_.size(),std::memory_order_relaxed);}

    void run(){
        setpriority(PRIO_PROCESS,0,10);
        std::unique_lock<std::mutex> lock(mu_);
        for(;;){
            cv_.wait(lock,[this]{return scheduled_ || stopping_;});
            if(!scheduled_)break;
            if(!stopping_ && !urgent_){
                if(deadline_>clock::now()){
                    cv_.wait_until(lock,deadline_,[this]{return stopping_ || urgent_;});
                    continue;
                }
            }
            const std::vector<uint8_t> snapshot=pending_;
            const bool final_attempt=stopping_;
            const uint64_t urgent_seen=urgent_sequence_;
            urgent_=false;
            lock.unlock();
            ++attempts_;
            std::string error;
            std::string durability_warning;
            const bool ok=replace(path_,snapshot,io_,error,&durability_warning);
            lock.lock();
            std::string line;
            if(ok){
                if(snapshot.size()<=pending_.size())pending_.erase(pending_.begin(),pending_.begin()+std::ptrdiff_t(snapshot.size()));
                repair_=false;error_.clear();
                line="NVN warm list save bytes="+std::to_string(snapshot.size())+" ok=1 pending="+std::to_string(pending_.size());
                if(!durability_warning.empty())line+=" durability=0 fsync-dir: "+durability_warning;
            }else{
                ++failures_;error_=std::move(error);
                // Snapshot bytes and any newer bytes are all still in pending_.
                line="NVN warm list save ok=0 pending="+std::to_string(pending_.size())+": "+error_;
            }
            publish_pending_locked();idle_.notify_all();
            auto logger=log_;lock.unlock();if(logger)logger(line);lock.lock();
            if(final_attempt)break;
            if(!pending_.empty() || repair_){
                // request_now() may have raced the replace.  Preserve that
                // request by sequence rather than reading/overwriting urgent_.
                urgent_=urgent_ || urgent_sequence_!=urgent_seen;
                scheduled_=true;deadline_=urgent_?clock::now():clock::now()+nonzero(retry_,std::chrono::milliseconds(1));
            }else scheduled_=false;
            // The idle predicate includes scheduled_; wake waiters after its
            // final update as well as after publishing the committed bytes.
            idle_.notify_all();
        }
    }

    mutable std::mutex mu_;
    std::condition_variable cv_,idle_;
    std::thread worker_;
    std::string path_,error_;
    std::vector<uint8_t> pending_;
    ReplaceIo io_;
    std::function<void(const std::string&)> log_;
    std::chrono::milliseconds debounce_{2000},retry_{2000},request_grace_{500};
    clock::time_point deadline_{},urgent_until_{};
    uint64_t urgent_sequence_=0;
    bool started_=false,stopping_=false,scheduled_=false,urgent_=false,repair_=false;
    std::atomic<size_t> pending_bytes_{0};
    std::atomic<uint64_t> attempts_{0},failures_{0};
};

} // namespace xbg_pipeline_warm_store
