"""Compile production descriptor initializers against CherryUSB headers; no board."""
from pathlib import Path
import os, re, subprocess, tempfile
root = Path(__file__).resolve().parents[1]
usb = root / 'firmware/application_5301/src/usb'
sdk = Path(os.environ.get('HPM_SDK_BASE', root.parent / 'hpm-sdk-reference'))
cherry = sdk / 'middleware/cherryusb'
source = (usb / 'usb_composite.c').read_text(encoding='utf-8')
source = source[source.index('#define CMSIS_DAP_INTERFACE_SIZE'):source.index('char serial_number_dynamic')]
prefix = r'''
#include <stdint.h>
#include <assert.h>
#include <stdio.h>
#define __PACKED __attribute__((packed))
#define __ALIGN_BEGIN
#define WBVAL(x) ((x)&255), (((x)>>8)&255)
#include "usb_def.h"
#include "usb_cdc.h"
#include "usb_msc.h"
#include "usb_hid.h"
#define CONFIG_USB_HS 1
#define CONFIG_CHERRYDAP_USE_CUSTOM_HID 1
#define CONFIG_CHERRYDAP_USE_MSC 0
#define BOARD_HAS_SPI_BRIDGE 1
#define CONFIG_USBDEV_REQUEST_BUFFER_LEN 768
#define DAP_PACKET_SIZE 512
'''
# Endpoint constants and HID MPS are taken from production, not duplicated.
header = (usb / 'usb_composite.h').read_text(encoding='utf-8')
for name in ['DAP_IN_EP','DAP_OUT_EP','SWO_IN_EP','CDC_IN_EP','CDC_OUT_EP','CDC_INT_EP',
             'HID_IN_EP','HID_OUT_EP','MSC_IN_EP','MSC_OUT_EP','SPI_IN_EP','SPI_OUT_EP',
             'USBD_VID','USBD_PID','USBD_MAX_POWER','HID_PACKET_SIZE']:
    prefix += re.search(r'^#define\s+' + name + r'\s+[^\n]+', header, re.M).group(0) + '\n'
test = r'''
static unsigned u16(const uint8_t *p){return p[0]|p[1]<<8;}
int main(void){
 assert(sizeof(config_descriptor)==USB_CONFIG_SIZE);
 assert(sizeof(other_speed_config_descriptor)==USB_CONFIG_SIZE);
 assert(sizeof(USBD_WinUSBDescriptorSetDescriptor)==USBD_WINUSB_DESC_SET_LEN);
 assert(sizeof(USBD_BinaryObjectStoreDescriptor)==USBD_BOS_WTOTALLENGTH);
 assert(sizeof(hid_custom_report_desc)==53);
 const uint8_t *m=USBD_WinUSBDescriptorSetDescriptor;
 assert(u16(m+8)==sizeof(USBD_WinUSBDescriptorSetDescriptor));
 for(unsigned off=10;off<sizeof(USBD_WinUSBDescriptorSetDescriptor);off+=160){
   assert(u16(m+off)==8 && u16(m+off+6)==160);
   assert(u16(m+off+8)==20 && u16(m+off+28)==132);
 }
 unsigned interfaces=0,hid=0,adc=0,scope=0,eps=0;
 for(unsigned off=0;off<sizeof(config_descriptor);){
   const uint8_t *d=config_descriptor+off;assert(d[0]>=2 && off+d[0]<=sizeof(config_descriptor));
   if(d[1]==4)interfaces++;
   if(d[1]==5){unsigned bit=1U<<((d[2]&15)+(d[2]&128?16:0));assert(!(eps&bit));eps|=bit;
     if(d[2]==0x87 || d[2]==8){assert(u16(d+4)==64 && d[6]==6);hid++;}
     assert(d[2]!=0x8c);
     if(d[2]==0x8b){assert(d[3]==2 && u16(d+4)==512);adc++;}
     if(d[2]==0x83){assert(d[3]==2 && u16(d+4)==512);scope++;}
   }off+=d[0];
 }
 assert(interfaces==INTF_NUM && hid==2 && adc==1 && scope==1);
 puts("Production USB descriptors: shared SPI/ADC Bulk, no extra endpoint, HID 64 bytes/4 ms, lengths PASS");
}
'''
with tempfile.TemporaryDirectory() as folder:
    path = Path(folder)
    (path / 'test.c').write_text(prefix + source + test)
    command = [os.getenv('CC','gcc'), '-std=c11', '-Werror', '-Wno-unused-variable']
    for directory in ['common','class/cdc','class/msc','class/hid']:
        command.append(f'-I{cherry / directory}')
    subprocess.run(command + [str(path/'test.c'), '-o', str(path/'test')], check=True)
    subprocess.run([str(path/'test')], check=True)
