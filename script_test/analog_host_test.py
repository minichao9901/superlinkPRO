"""Compile production ADC adapter for both boards with mocked SDK I/O."""
from pathlib import Path
import os, subprocess, tempfile, sys
root = Path(__file__).resolve().parents[1]
src = root / 'firmware/application_5301/src/analog_bridge'
header = r'''
#ifndef TEST_HW_H
#define TEST_HW_H
#include <stdint.h>
#include <stdbool.h>
typedef int hpm_stat_t;
#define status_success 0
#define adc16_res_16_bits 21
#define adc16_conv_mode_oneshot 0
#define adc16_clock_divider_8 8
#define clock_adc0 0
#define clk_adc_src_ahb0 0
#define IOC_PAD_PB11 11
#define IOC_PAD_FUNC_CTL_ANALOG_MASK 0x10000U
typedef struct { uint32_t FUNC_CTL; } pad_t;
typedef struct { pad_t PAD[32]; } ioc_t;
extern ioc_t ioc;
#define HPM_IOC (&ioc)
#define HPM_ADC0 ((void*)1)
typedef struct { int res,conv_mode,adc_clk_div;bool sel_sync_ahb; } adc16_config_t;
typedef struct { unsigned ch,sample_cycle; } adc16_channel_config_t;
extern unsigned enabled, reads, clocks, inits, init_fail, channel_fail, read_fail;
static inline int clock_set_adc_source(int clk,int source){(void)clk;(void)source;clocks++;return 0;}
static inline void clock_add_to_group(int clk,int group){(void)clk;(void)group;}
static inline void adc16_get_default_config(adc16_config_t *c){*c=(adc16_config_t){0};}
static inline int adc16_init(void *base,adc16_config_t *c){(void)base;inits++;return init_fail || c->res!=21 || c->adc_clk_div!=8;}
static inline void adc16_get_channel_default_config(adc16_channel_config_t *c){*c=(adc16_channel_config_t){0};}
static inline int adc16_init_channel(void *base,adc16_channel_config_t *c){(void)base;return channel_fail || c->ch!=3 || c->sample_cycle!=20;}
static inline void adc16_enable_oneshot_mode(void *base){(void)base;}
int adc16_get_oneshot_result(void*,uint8_t,uint16_t*);
uint8_t spi_bridge_is_enabled(void);
uint8_t spi_bridge_adc_flags(void);
uint8_t led_state_read_vref_raw(uint16_t*);
#endif
'''
test = r'''
#include <assert.h>
#include <string.h>
#include <stdio.h>
#include "board.h"
#include "analog_bridge.h"
#include "adc_stream.h"
ioc_t ioc;
unsigned enabled,reads,clocks,inits,init_fail,channel_fail,read_fail;
uint8_t adc_stream_open(uint8_t bits,uint32_t rate,uint32_t count,uint32_t *token){assert(bits==16&&((rate==1000&&count==0)||(rate==500&&count==512)));*token=1;return 0;}
uint8_t adc_stream_open_pipeline(uint8_t bits,uint32_t rate,uint32_t count,uint8_t depth,uint32_t *token){if(!depth||depth>ADC_MAX_INFLIGHT)return ANALOG_RANGE;return adc_stream_open(bits,rate,count,token);}
uint8_t adc_stream_close(void){return 0;}
uint8_t adc_stream_start(void){return 0;}
uint8_t adc_stream_enabled(void){return 0;}
void adc_stream_status(uint8_t *p){memset(p,0,24);}
uint8_t spi_bridge_adc_flags(void){return 0;}
uint8_t adc_hw_supported(void){return 1;}
void adc_stream_end(void){}
uint8_t spi_bridge_is_enabled(void){return enabled;}
uint8_t led_state_read_vref_raw(uint16_t *p){reads++;*p=0xabcd;return read_fail;}
int adc16_get_oneshot_result(void *base,uint8_t channel,uint16_t *raw){
 (void)base;assert(channel==3);assert(ioc.PAD[11].FUNC_CTL==IOC_PAD_FUNC_CTL_ANALOG_MASK);
 reads++;*raw=0xabcd;return read_fail;
}
#ifdef ANALOG_WIRE
int main(void){
 char line[256];uint8_t req[64],res[64];
 while(fgets(line,sizeof(line),stdin)){
   assert(strlen(line)>=128);
   for(unsigned i=0;i<64;i++){unsigned n;assert(sscanf(line+2*i,"%2x",&n)==1);req[i]=(uint8_t)n;}
   memset(res,0,sizeof(res));res[0]=1;analog_bridge_hid(req,res);
   for(unsigned i=0;i<64;i++)printf("%02x",res[i]);puts("");fflush(stdout);
 }
 return 0;
}
#else
int main(void){
 uint8_t req[64]={0},res[64]={0},p[2]={BOARD_HAS_VREF_ADC?2:3,16},data[2],n;
 req[1]=2;analog_bridge_hid(req,res);assert(res[1]==20&&!memcmp(res+8,"ANA1",4));
 assert(res[12]==p[0]&&res[13]==16&&res[14]==(BOARD_HAS_VREF_ADC?2:1)&&res[15]==0);
 req[1]=1;analog_bridge_hid(req,res);assert(res[4]==1);
 req[1]=2;req[3]=ANALOG_DAC_CAPS;analog_bridge_hid(req,res);
 assert(res[1]==24&&!res[4]&&!memcmp(res+8,"DAC1",4)&&res[12]==1&&res[13]==0);
 for(unsigned a=ANALOG_DAC_CONFIG;a<=ANALOG_DAC_GET_CONFIG;a++){
  req[3]=a;req[1]=(uint8_t[]){10,5,12,11,7,3,3}[a-ANALOG_DAC_CONFIG];req[11]=1;
  analog_bridge_hid(req,res);assert(res[4]==ANALOG_UNSUPPORTED);
  req[1]=2;analog_bridge_hid(req,res);assert(res[4]==ANALOG_RANGE);
 }
 assert(!reads&&!clocks&&!inits);
 req[1]=2;req[3]=255;analog_bridge_hid(req,res);assert(res[4]==1);
 req[3]=ANALOG_STREAM_CAPS;analog_bridge_hid(req,res);assert(res[1]==28&&!memcmp(res+8,"ADB2",4)&&res[12]==0x8b&&res[15]==6);
 req[3]=ANALOG_STREAM_OPEN;analog_bridge_hid(req,res);assert(res[4]==ANALOG_RANGE);
 req[1]=12;analog_bridge_hid(req,res);assert(res[4]==ANALOG_RANGE);
 req[1]=11;req[4]=16;req[5]=0xe8;req[6]=3;memset(req+7,0,6);
 analog_bridge_hid(req,res);assert(res[1]==12&&res[8]==1);
 req[1]=12;req[13]=3;analog_bridge_hid(req,res);assert(!res[4]&&res[8]==1);
 req[13]=ADC_MAX_INFLIGHT+1;analog_bridge_hid(req,res);assert(res[4]==ANALOG_RANGE);
 req[1]=2;req[3]=ANALOG_STREAM_PIPELINE;analog_bridge_hid(req,res);
 assert(!res[4]&&res[1]==10&&res[8]==1&&res[9]==ADC_MAX_INFLIGHT);
 enabled=1;assert(analog_periodic_exec(p,2,data,&n)==2&&n==0&&!reads);enabled=0;
 assert(analog_periodic_check(p,1));p[1]=7;assert(analog_periodic_check(p,2));p[1]=16;
#if !BOARD_HAS_VREF_ADC
 init_fail=1;assert(analog_periodic_exec(p,2,data,&n)==3);init_fail=0;
 channel_fail=1;assert(analog_periodic_exec(p,2,data,&n)==3);channel_fail=0;
#endif
 ioc.PAD[11].FUNC_CTL=0x12345678;
 assert(!analog_periodic_exec(p,2,data,&n));assert(n==2&&data[0]==0xcd&&data[1]==0xab);
 assert(ioc.PAD[11].FUNC_CTL==0x12345678);
 for(unsigned i=0;i<3;i++){p[1]=(uint8_t[]){8,10,12}[i];assert(!analog_periodic_exec(p,2,data,&n));assert((data[0]|data[1]<<8)==(0xabcd>>(16-p[1])));}
 read_fail=1;assert(analog_periodic_exec(p,2,data,&n)==3&&n==0);assert(ioc.PAD[11].FUNC_CTL==0x12345678);
#if BOARD_HAS_VREF_ADC
 assert(!clocks&&!inits);
#else
 assert(clocks==3&&inits==3);
#endif
 return 0;
}
#endif
'''
with tempfile.TemporaryDirectory() as folder:
    path=Path(folder)
    for name in ['board.h','clock.h','hpm_adc16_drv.h','hpm_clock_drv.h','hpm_soc.h','spi_bridge.h','led_state.h']:
        (path/name).write_text(header)
    (path/'test.c').write_text(test)
    wire=len(sys.argv)==3 and sys.argv[1]=='--wire'
    for vref in ([0] if wire else [0,1]):
        exe=Path(sys.argv[2]) if wire else path/f'test-{vref}'
        subprocess.run([os.getenv('CC','gcc'),'-std=c11','-O2','-Wall','-Wextra','-Werror',*(['-DANALOG_WIRE=1','-Wno-misleading-indentation'] if wire else []),f'-DBOARD_HAS_VREF_ADC={vref}',f'-I{path}',f'-I{src}',str(src/'analog_bridge.c'),str(path/'test.c'),'-o',str(exe)],check=True)
        if not wire: subprocess.run([str(exe)],check=True)
print('Analog production adapter: board CAPS, input bounds, SPI exclusion, quantization, initialization errors and pad restoration PASS')
