"""Ordered async libusb bulk reads for hardware tests (PyUSB's loaded backend)."""
import ctypes as C
import threading
from usb.backend.libusb1 import _libusb_transfer,_libusb_transfer_cb_fn_p

class BulkReader:
    def __init__(self,device,output,depth=6,size=8192):
        self.device=device;self.output=output;self.depth=depth;self.size=size
        self.lib=device._ctx.backend.lib;self.ctx=device._ctx.backend.ctx
        self.lib.libusb_cancel_transfer.argtypes=[C.POINTER(_libusb_transfer)]
        self.errors=[];self.active={};self.transfers=[];self.buffers=[];self.next_submit=0;self.next_emit=0;self.completed={};self.running=True;self.small=False;self.tail_ready=threading.Event()
        self.draining = False
        self.eof = threading.Event()
        self.eof_remaining = 0
        self.callback=_libusb_transfer_cb_fn_p(self._complete)
        for _ in range(depth):
            t=self.lib.libusb_alloc_transfer(0);b=C.create_string_buffer(size)
            if not t:raise MemoryError('libusb transfer allocation')
            t.contents.dev_handle=device._ctx.handle.handle;t.contents.endpoint=0x83;t.contents.type=2;t.contents.timeout=300;t.contents.buffer=C.cast(b,C.c_void_p);t.contents.callback=self.callback
            self.transfers.append(t);self.buffers.append(b);self._submit(t)
        self.thread=threading.Thread(target=self._events);self.thread.start()
    def _submit(self,t):
        key=C.addressof(t.contents);self.active[key]=(self.next_submit,512 if self.small else self.size);self.next_submit+=1;t.contents.length=self.active[key][1]
        rc=self.lib.libusb_submit_transfer(t)
        if rc<0:self.active.pop(key);raise RuntimeError('libusb_submit_transfer '+str(rc))
    def _complete(self,t):
        try:
            key=C.addressof(t.contents);seq,requested=self.active.pop(key);v=t.contents
            self.completed[seq]=(C.string_at(v.buffer,v.actual_length),v.status,requested)
            while self.next_emit in self.completed:
                data,status,size=self.completed.pop(self.next_emit)
                self.next_emit+=1
                if self.eof.is_set():
                    if data or status != 0:
                        self.errors.append('Read after Scope EOF was not empty and successful')
                    continue
                self.output.extend(data)
                if self.draining and status == 0 and len(data) < size:
                    if len(data) % 512:
                        raise RuntimeError('Scope EOF is not packet-aligned')
                    self.running = False
                    self.eof_remaining = len(self.active)
                    self.eof.set()
            if v.status not in (0,2,3):self.errors.append('libusb transfer status '+str(v.status));self.running=False
            if self.running:self._submit(t)
            if self.small and all(size==512 for _,size in self.active.values()):self.tail_ready.set()
        except Exception as error:self.errors.append(repr(error));self.running=False
    def _events(self):
        while self.active:
            rc=self.lib.libusb_handle_events(self.ctx)
            if rc<0 and rc!=-10:self.errors.append('libusb event '+str(rc));self.running=False
    def finite_tail(self):
        self.small=True
        if not self.tail_ready.wait(3):raise TimeoutError('Native read window did not switch to packet-sized tail')
    def begin_drain(self):
        self.draining = True
    def wait_eof(self):
        if not self.eof.wait(2):
            raise TimeoutError('Native Scope EOF was not received')
        return self.eof_remaining
    def wait_retired(self):
        self.thread.join(2)
        if self.thread.is_alive() or self.errors:
            raise RuntimeError(f'Native Scope reads failed to retire: {self.errors}')
    def stop(self):
        self.running=False
        for t in self.transfers:
            if C.addressof(t.contents) in self.active:self.lib.libusb_cancel_transfer(t)
        self.thread.join(3)
        if self.thread.is_alive():raise TimeoutError('Native USB requests did not retire; retain claimed device')
        for t in self.transfers:self.lib.libusb_free_transfer(t)
        self.transfers=[]
