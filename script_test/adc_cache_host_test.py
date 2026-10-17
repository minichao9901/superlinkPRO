"""Exercise production DMA cache spans: alignment, wrap, and no neighboring lines."""
from pathlib import Path
import os, subprocess, tempfile

root = Path(__file__).resolve().parents[1]
src = root / 'firmware/application_5301/src/analog_bridge/adc_dma_hw.c'
source = src.read_text(encoding='utf-8')
barrier = source[source.index('static void invalidate_samples'):source.index('\n#else', source.index('static void invalidate_samples'))]
test = r'''
#include <stdint.h>
#include <assert.h>
#include <stdio.h>
#define ADC_DMA_WORDS 4096U
#define HPM_L1C_CACHELINE_ALIGN_DOWN(n) ((uint32_t)(n)&~31U)
#define HPM_L1C_CACHELINE_ALIGN_UP(n) (((uint32_t)(n)+31U)&~31U)
static unsigned calls, addresses[2], lengths[2];
void l1c_dc_invalidate(uint32_t address,uint32_t length){
 assert(calls<2&&address%32==0&&length%32==0&&length);
 assert(address>=0x1000&&address+length<=0x5000);
 addresses[calls]=address;lengths[calls++]=length;
}
'''
checks = r'''
int main(void){
 for(unsigned first=0;first<4096;first++)for(unsigned n=0;n<4;n++){
  unsigned count=(unsigned[]){0,1,7,2031}[n],span=4096-first;
  if(span>count)span=count;calls=0;
  adc_hw_read_barrier((uint32_t*)(uintptr_t)0x1000,first,count);
  assert(calls==(count?(count>span?2:1):0));
  for(unsigned i=0;i<count;i++){
   unsigned a=0x1000+4*((first+i)%4096),covered=0;
   for(unsigned j=0;j<calls;j++)if(a>=addresses[j]&&a+4<=addresses[j]+lengths[j])covered++;
   assert(covered==1);
  }
  unsigned bytes=0;for(unsigned j=0;j<calls;j++)bytes+=lengths[j];
  assert(bytes<=4*count+124); /* only complete lines adjoining the two spans */
 }
 puts("ADC production cache barrier: all offsets, zero/small/full blocks, wrap, aligned spans and no neighbor invalidation PASS");
}
'''
with tempfile.TemporaryDirectory() as folder:
    path = Path(folder)
    (path/'test.c').write_text(test+barrier+checks, encoding='utf-8')
    subprocess.run([os.getenv('CC','gcc'),'-std=c11','-O2','-Wall','-Wextra','-Werror','-Wno-misleading-indentation',str(path/'test.c'),'-o',str(path/'test')],check=True)
    subprocess.run([str(path/'test')],check=True)
