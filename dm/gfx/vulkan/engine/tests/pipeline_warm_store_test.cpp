// CPU tests for the production vk-pipeline-list durable autosave helper.
// No Vulkan device, renderer, build tree, phone, or user save data is touched.
#include "../xbg_pipeline_warm_store.h"

#include <atomic>
#include <chrono>
#include <cstdio>
#include <cstdlib>
#include <cstring>
#include <condition_variable>
#include <fstream>
#include <iostream>
#include <mutex>
#include <stdexcept>
#include <string>
#include <unistd.h>
#include <vector>

namespace {
using namespace std::chrono_literals;
using xbg_pipeline_warm_store::Autosave;
using xbg_pipeline_warm_store::AutosaveOptions;
using xbg_pipeline_warm_store::Inspect;
using xbg_pipeline_warm_store::ReplaceIo;

void require(bool ok,const char *message){if(!ok)throw std::runtime_error(message);}
void require(bool ok,const std::string &message){if(!ok)throw std::runtime_error(message);}
void put32(std::vector<uint8_t> &v,uint32_t x){for(int i=0;i<4;i++)v.push_back(uint8_t(x>>(8*i)));}

std::vector<uint8_t> record(uint32_t identity,bool truncate=false){
    std::vector<uint8_t> v;
    put32(v,xbg_pipeline_warm_store::record_magic);put32(v,1);
    put32(v,3);v.insert(v.end(),{'a','b','c'});
    put32(v,3);put32(v,identity);put32(v,identity^0xa5a5a5a5u);put32(v,0x31u);
    if(truncate)v.resize(v.size()-3);
    return v;
}

void write_file(const std::string &path,const std::vector<uint8_t> &data){
    std::ofstream out(path,std::ios::binary|std::ios::trunc);require(bool(out),"open test file");
    out.write(reinterpret_cast<const char*>(data.data()),std::streamsize(data.size()));require(bool(out),"write test file");
}

std::vector<uint8_t> read_file(const std::string &path){
    std::ifstream in(path,std::ios::binary);require(bool(in),"open test file for read");
    return std::vector<uint8_t>((std::istreambuf_iterator<char>(in)),std::istreambuf_iterator<char>());
}

std::string temp_dir(){
    char pattern[]="/tmp/xbg-pipeline-warm-XXXXXX";char *p=mkdtemp(pattern);require(p!=nullptr,"mkdtemp");return p;
}

void direct_truncated_tail_and_atomic_rename(){
    const std::string dir=temp_dir(),path=dir+"/list.bin";
    const auto one=record(1),two=record(2),three=record(3);
    std::vector<uint8_t> old=one;old.insert(old.end(),two.begin(),two.end());
    const auto partial=record(0x7777,true);old.insert(old.end(),partial.begin(),partial.end());
    write_file(path,old);
    Inspect state=Inspect::missing;std::string error;
    require(xbg_pipeline_warm_store::inspect(path,state,error) && state==Inspect::malformed,"truncated tail not detected");
    require(xbg_pipeline_warm_store::replace(path,three,ReplaceIo{},error),"repair replace failed: "+error);
    const auto got=read_file(path);std::vector<uint8_t> want=one;want.insert(want.end(),two.begin(),two.end());want.insert(want.end(),three.begin(),three.end());
    require(got==want,"repair did not preserve complete prefix and append new record exactly");
    require(xbg_pipeline_warm_store::inspect(path,state,error) && state==Inspect::valid,"repaired list is not valid");
    require(access((path+".tmp").c_str(),F_OK)!=0,"temporary file survived successful rename");
    unlink(path.c_str());rmdir(dir.c_str());
}

void rename_failure_preserves_previous(){
    const std::string dir=temp_dir(),path=dir+"/list.bin";
    const auto one=record(11),two=record(12);write_file(path,one);
    ReplaceIo io;uint64_t renames=0;
    io.rename_replace=[&](const std::string&,const std::string&,std::string &error){++renames;error="injected rename failure";return false;};
    std::string error;
    require(!xbg_pipeline_warm_store::replace(path,two,io,error),"rename failure was reported as success");
    require(read_file(path)==one,"rename failure changed the previous list");
    require(access((path+".tmp").c_str(),F_OK)!=0,"failed rename retained temp file");
    require(renames==1,"rename hook not exercised");
    unlink(path.c_str());rmdir(dir.c_str());
}

void autosave_repairs_existing_truncated_tail_without_new_record(){
    const std::string dir=temp_dir(),path=dir+"/list.bin";
    const auto one=record(13);std::vector<uint8_t> broken=one;const auto partial=record(14,true);broken.insert(broken.end(),partial.begin(),partial.end());write_file(path,broken);
    Autosave save;AutosaveOptions options;options.debounce=5ms;options.retry=50ms;
    require(save.start(path,options),"autosave start failed");require(save.wait_idle(1s),"startup tail repair did not finish");
    Inspect state=Inspect::missing;std::string error;
    require(xbg_pipeline_warm_store::inspect(path,state,error) && state==Inspect::valid && read_file(path)==one,"startup did not repair the malformed tail exactly");
    save.stop();unlink(path.c_str());rmdir(dir.c_str());
}

void fast_record_is_dirty_and_request_flushes(){
    const std::string dir=temp_dir(),path=dir+"/list.bin";
    Autosave save;AutosaveOptions options;options.debounce=500ms;options.retry=50ms;
    require(save.start(path,options),"autosave start failed");
    require(save.append(record(21)),"fast pipeline record enqueue failed");
    require(save.pending_bytes()!=0,"new record did not make the list dirty");
    require(!save.wait_idle(20ms),"debounce unexpectedly flushed immediately");
    save.request_now();
    require(save.wait_idle(1s),"requested list flush did not become idle");
    Inspect state=Inspect::missing;std::string error;
    require(xbg_pipeline_warm_store::inspect(path,state,error) && state==Inspect::valid,"flushed list is invalid");
    require(read_file(path)==record(21),"flushed list lost fast record");
    save.stop();unlink(path.c_str());rmdir(dir.c_str());
}

void write_failure_retries_without_dropping_pending(){
    const std::string dir=temp_dir(),path=dir+"/list.bin";
    Autosave save;AutosaveOptions options;options.debounce=1ms;options.retry=60ms;
    std::atomic<int> writes{0};
    options.io.write_temp=[&](const std::string &p,const std::vector<uint8_t> &d,std::string &error){
        if(++writes==1){error="injected first write failure";return false;}
        return xbg_pipeline_warm_store::default_write_temp(p,d,error);
    };
    require(save.start(path,options),"autosave start failed");
    require(save.append(record(31)),"record enqueue failed");
    save.request_now();
    require(!save.wait_idle(30ms),"failed write did not retain pending bytes");
    require(save.pending_bytes()!=0 && save.failures()!=0,"failure state did not retain record");
    require(save.wait_idle(1200ms),"retry did not flush retained record");
    Inspect state=Inspect::missing;std::string error;
    require(xbg_pipeline_warm_store::inspect(path,state,error) && state==Inspect::valid,"retried list is invalid");
    require(read_file(path)==record(31),"retry lost or duplicated pending record");
    require(save.attempts()>=2 && save.failures()>=1,"retry counters not exercised");
    save.stop();unlink(path.c_str());rmdir(dir.c_str());
}

void append_during_io_does_not_block_or_get_lost(){
    const std::string dir=temp_dir(),path=dir+"/list.bin";
    Autosave save;AutosaveOptions options;options.debounce=1ms;options.retry=5s;
    std::mutex gate_mu;std::condition_variable gate;bool writing=false,release=false;
    options.io.write_temp=[&](const std::string &p,const std::vector<uint8_t> &d,std::string &error){
        {std::unique_lock<std::mutex> lock(gate_mu);writing=true;gate.notify_all();gate.wait(lock,[&]{return release;});}
        return xbg_pipeline_warm_store::default_write_temp(p,d,error);
    };
    require(save.start(path,options),"autosave start failed");
    require(save.append(record(41)),"first record enqueue failed");save.request_now();
    {std::unique_lock<std::mutex> lock(gate_mu);require(gate.wait_for(lock,1s,[&]{return writing;}),"writer did not enter injected I/O");}
    const auto t0=std::chrono::steady_clock::now();require(save.append(record(42)),"append during I/O failed");
    require(std::chrono::steady_clock::now()-t0<100ms,"append waited for persistence I/O");
    save.request_now(); // must survive run() reacquiring its mutex after replace
    {std::lock_guard<std::mutex> lock(gate_mu);release=true;}gate.notify_all();
    require(save.wait_idle(500ms),"request during in-flight replace was overwritten by the save tail");
    std::vector<uint8_t> want=record(41),two=record(42);want.insert(want.end(),two.begin(),two.end());
    require(read_file(path)==want,"append during I/O was lost or reordered");
    save.stop();unlink(path.c_str());rmdir(dir.c_str());
}

void directory_sync_failure_is_committed_warning_not_retry(){
    const std::string dir=temp_dir(),path=dir+"/list.bin";
    Autosave save;AutosaveOptions options;options.debounce=1ms;options.retry=1s;
    std::atomic<int> syncs{0};
    options.io.sync_directory=[&](const std::string&,std::string &error){++syncs;error="injected directory fsync failure";return false;};
    require(save.start(path,options),"autosave start failed");require(save.append(record(51)),"record enqueue failed");save.request_now();
    require(save.wait_idle(500ms),"committed rename was held pending after directory fsync warning");
    require(save.pending_bytes()==0 && save.attempts()==1 && syncs==1,"dir-sync failure caused a duplicate retry instead of a warning");
    require(read_file(path)==record(51),"dir-sync warning lost the committed record");
    save.stop();unlink(path.c_str());rmdir(dir.c_str());
}

void early_onpause_request_covers_immediately_following_record(){
    const std::string dir=temp_dir(),path=dir+"/list.bin";
    Autosave save;AutosaveOptions options;options.debounce=5s;options.retry=1s;options.request_grace=500ms;
    require(save.start(path,options),"autosave start failed");
    save.request_now(); // nativeFocus ev=0/1 has no pending batch yet
    require(save.append(record(61)),"late record enqueue failed");
    require(save.wait_idle(500ms),"early lifecycle request did not cover the immediately following record");
    Inspect state=Inspect::missing;std::string error;
    require(xbg_pipeline_warm_store::inspect(path,state,error) && state==Inspect::valid && read_file(path)==record(61),"early request record was not committed");
    save.stop();unlink(path.c_str());rmdir(dir.c_str());
}

void request_and_append_during_postcommit_logger_stay_urgent(){
    const std::string dir=temp_dir(),path=dir+"/list.bin";
    Autosave save;AutosaveOptions options;options.debounce=5s;options.retry=5s;options.request_grace=500ms;
    std::atomic<bool> logged{false};
    require(save.start(path,options,[&](const std::string&){
        if(!logged.exchange(true)){save.request_now();require(save.append(record(71)),"logger append failed");}
    }),"autosave start failed");
    require(save.append(record(70)),"first record enqueue failed");save.request_now();
    require(save.wait_idle(500ms),"request/append during post-commit logger was delayed by retry interval");
    std::vector<uint8_t> want=record(70),two=record(71);want.insert(want.end(),two.begin(),two.end());
    require(read_file(path)==want,"post-commit logger append was lost");
    save.stop();unlink(path.c_str());rmdir(dir.c_str());
}

} // namespace

int main(){
    try{
        direct_truncated_tail_and_atomic_rename();
        rename_failure_preserves_previous();
        autosave_repairs_existing_truncated_tail_without_new_record();
        fast_record_is_dirty_and_request_flushes();
        write_failure_retries_without_dropping_pending();
        append_during_io_does_not_block_or_get_lost();
        directory_sync_failure_is_committed_warning_not_retry();
        early_onpause_request_covers_immediately_following_record();
        request_and_append_during_postcommit_logger_stay_urgent();
        std::cout<<"PASS pipeline warm durable autosave: fast-record dirty state, real-time debounce, early/in-flight request preservation, valid-prefix repair, file+directory sync with atomic rename, rename failure preservation, write-failure retention/retry, nonblocking append\n";
        return 0;
    }catch(const std::exception &e){
        std::cerr<<"FAIL "<<e.what()<<'\n';return 1;
    }
}
