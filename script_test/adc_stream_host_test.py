"""Production shared ADC DMA stream with virtual DMA and USB; no board."""
from pathlib import Path
import os,re,subprocess,tempfile
root=Path(__file__).resolve().parents[1]
src=root/'firmware/application_5301/src/analog_bridge/adc_stream.c'
source=re.sub(r'^#include "(?!adc_stream.h)[^"]+"','',src.read_text(encoding='utf-8'),flags=re.M)
stub=r'''
#include <stdint.h>
#include <assert.h>
#include <stdio.h>
#include <string.h>
#include "adc_stream.h"
enum {ANALOG_OK,ANALOG_RANGE,ANALOG_BUSY,ANALOG_STATE,ANALOG_UNSUPPORTED};
#define BP_ADC 4
#define BP_I2C 1
#define BP_SPI 2
#define SPI_IN_EP 0x8b
static uint32_t raw[4096];static uint8_t tx[8192];
static unsigned depth,owner,supported=1,core,bp_busy,shared_busy;
static unsigned reset_on_start_lock,inject_reset,starts;
static unsigned pos,stop_pos,hw_running,hw_error,prepare_error,width;
static uint64_t clock_now;
static uint8_t *flight;static uint32_t flight_len;static unsigned transfers;
uint32_t bp_lock(void){if(reset_on_start_lock){reset_on_start_lock=0;adc_stream_reset(0);}return depth++;}void bp_unlock(uint32_t n){assert(depth==n+1);depth=n;}
uint64_t bp_now(void){return clock_now;}
uint8_t bus_periodic_owns(uint8_t b){(void)b;return bp_busy;}
int scope_sampler_is_running(void){return core;}int rtt_bridge_is_running(void){return 0;}
uint8_t spi_bridge_adc_claim(uint32_t **c,uint8_t **t){assert(depth);if(shared_busy)return 0;assert(!owner);owner=1;*c=raw;*t=tx;return 1;}
void spi_bridge_adc_release(void){assert(depth&&owner&&!hw_running);owner=0;}
uint8_t adc_hw_supported(void){return supported;}
uint8_t adc_hw_prepare(uint32_t *b,uint8_t bits,uint32_t rate,uint32_t count,uint32_t *actual){
 assert(!depth&&owner&&b==raw);memset(raw,0,sizeof(raw));pos=hw_error=0;
 stop_pos=count&&count<4096?count:4095;width=bits;*actual=rate;
 if(prepare_error){pos=123;hw_error=0;}if(inject_reset){inject_reset=0;reset_on_start_lock=1;}return prepare_error;
}
void adc_hw_begin(void){assert(depth);starts++;hw_running=1;}void adc_hw_abort(void){hw_running=0;}
void adc_hw_stop(void){assert(!depth);hw_running=0;}void adc_hw_restore(void){assert(!depth&&!hw_running);}
uint16_t adc_hw_position(void){return pos;}uint8_t adc_hw_fault(void){return hw_error;}
void adc_hw_release_until(uint16_t n){stop_pos=n;}
void adc_hw_read_barrier(uint32_t *b,uint16_t first,uint16_t n){assert(b==raw&&first<4096&&n<=2031);}
int usbd_ep_start_write(uint8_t bus,uint8_t ep,uint8_t *p,uint32_t n){
 assert(bus==0&&ep==0x8b&&!flight&&n<=4094&&(n%512)!=0&&depth);flight=p;flight_len=n;transfers++;return 0;
}
static uint32_t u32(const uint8_t *p){return p[0]|((uint32_t)p[1]<<8)|((uint32_t)p[2]<<16)|((uint32_t)p[3]<<24);}
static void produce(unsigned n){for(unsigned i=0;i<n&&hw_running;i++){
 if(pos==stop_pos){hw_error=5;break;}raw[pos]=(0x1234U<<(16-width))&65535U;pos=(pos+1)%4096;
}clock_now+=48000;}
static unsigned points,blocks;static uint32_t next;
static void complete(void){assert(flight&&!memcmp(flight,"ADS2",4));unsigned n=flight[24]|flight[25]<<8;
 assert(flight_len==32+2*n&&u32(flight+16)==next&&u32(flight+12)==blocks);next+=n;points+=n;blocks++;flight=0;adc_stream_complete();}
static void drain(void){for(unsigned i=0;i<100;i++){adc_stream_poll();if(flight)complete();else break;}}
static void close_stream(void){assert(adc_stream_close()==2);adc_stream_poll();assert(!owner&&!adc_stream_enabled());assert(!adc_stream_close());}
static void open_stream(unsigned count){uint32_t token;next=points=blocks=0;assert(!adc_stream_open(16,2000000,count,&token));assert(!flight&&!hw_running);assert(!adc_stream_start());adc_stream_poll();assert(hw_running);}
'''
test=r'''
int main(void){
 uint32_t token;assert(adc_stream_open(16,1,1,&token)==3);adc_stream_reset(1);
 shared_busy=1;assert(adc_stream_open(16,1,1,&token)==2);shared_busy=0;
 core=1;assert(adc_stream_open(16,1,1,&token)==2);core=0;
 bp_busy=1;assert(adc_stream_open(16,1,1,&token)==2);bp_busy=0;
 supported=0;assert(adc_stream_open(16,1,1,&token)==4);supported=1;
 assert(adc_stream_open(16,2000001,1,&token)==1);
 open_stream(3);produce(10);drain();assert(points==3&&blocks==2&&!hw_running);close_stream();
 open_stream(9000);for(unsigned i=0;i<20&&hw_running;i++){produce(1000);drain();}
 drain();assert(points==9000);close_stream();
 open_stream(0);produce(2031);adc_stream_poll();assert(flight&&flight_len==4094);
 uint8_t copy[4096];memcpy(copy,flight,flight_len);produce(2031);adc_stream_poll();assert(!memcmp(copy,flight,flight_len));
 produce(4096);adc_stream_poll();assert(!hw_running);drain();assert(points>4000);
 uint8_t status[24];adc_stream_status(status);assert(u32(status+16)==5);close_stream();
 next=points=blocks=0;assert(!adc_stream_open(12,1000,0,&token));adc_stream_end();drain();close_stream();
 prepare_error=3;next=points=blocks=0;assert(!adc_stream_open(16,1,1,&token));assert(!adc_stream_start());drain();
 assert(points==0&&blocks==1);close_stream();prepare_error=0;
 open_stream(0);produce(1000);adc_stream_poll();assert(flight);flight=0;
 adc_stream_reset(0);adc_stream_poll();assert(!owner&&!hw_running);adc_stream_complete();assert(!flight);
 assert(adc_stream_open(16,1,1,&token)==3);adc_stream_reset(1);
 unsigned before=starts;assert(!adc_stream_open(16,1000,1,&token));inject_reset=1;
 assert(!adc_stream_start());adc_stream_poll();assert(!owner&&!hw_running&&starts==before);adc_stream_reset(1);
 open_stream(1);produce(1);drain();assert(points==1);close_stream();
 /* Counted END retires every submitted native reader, including stop-before-START. */
 assert(adc_stream_open_pipeline(16,1000,1,0,&token)==1);
 assert(adc_stream_open_pipeline(16,1000,1,ADC_MAX_INFLIGHT+1,&token)==1);
 for(unsigned readers=2;readers<=ADC_MAX_INFLIGHT;readers++){
  next=points=blocks=0;assert(!adc_stream_open_pipeline(16,2000000,3,readers,&token));
  assert(!adc_stream_start());adc_stream_poll();produce(3);
  for(unsigned i=0;i<=readers;i++){
   adc_stream_poll();assert(flight);complete();
   adc_stream_status(status);assert(status[22]==(i==readers));
   if(i<readers)assert(adc_stream_close()==ANALOG_BUSY);
  }
  assert(points==3&&blocks==readers+1);adc_stream_poll();assert(!flight);close_stream();
  next=points=blocks=0;assert(!adc_stream_open_pipeline(16,1000,0,readers,&token));
  adc_stream_end();drain();assert(points==0&&blocks==readers);close_stream();
 }
 printf("Shared ADC DMA: finite count/ring wrap, SPI/core exclusion, two slots, overflow, END/reset/restart PASS (%u transfers)\n",transfers);
 return 0;
}
'''
with tempfile.TemporaryDirectory() as folder:
 path=Path(folder);(path/'test.c').write_text(stub+source+test)
 flags=['-fsanitize=address,undefined','-fno-omit-frame-pointer'] if os.getenv('BUS_HOST_SANITIZE') else []
 subprocess.run([os.getenv('CC','gcc'),'-std=c11','-O2','-Wall','-Wextra','-Werror',*flags,f'-I{src.parent}',str(path/'test.c'),'-o',str(path/'test')],check=True)
 subprocess.run([str(path/'test')],check=True)
