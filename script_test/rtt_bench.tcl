# RTT drain benchmark executed *inside* OpenOCD.
#
# Running the whole poll loop as TCL removes every telnet/text round trip that
# rtt_fast_poll.py pays; what is left is OpenOCD's own DAP transaction cost.
#
# The STM32F103 must be boosted while HALTED (a running core at 64 MHz makes
# plain AP reads fail on this setup), then resumed, so the RTT producer side is
# not the limiter either.
#
# usage: openocd -f <cfg> -c "gdb port disabled" -c "init" -c "adapter speed 60000" \
#                -c "set BOOST 1" -c "source rtt_bench.tcl" -c shutdown

if {![info exists BOOST]} { set BOOST 1 }
if {![info exists CHUNK]} { set CHUNK 512 }
set CB 0x2000000C
set G_BYTES 0x20000000
set WINDOW_MS 3000

proc boost_clock_halted {} {
    if {[catch {halt}]} { echo "boost: halt failed"; return }
    catch {mww 0x40022000 0x00000012}
    catch {mww 0x40021004 0x00380400}
    if {[catch {mem2array cr 32 0x40021000 1}]} { echo "boost: RCC read failed"; catch {resume}; return }
    catch {mww 0x40021000 [expr {$cr(0) | 0x01000000}]}
    for {set i 0} {$i < 200} {incr i} {
        if {[catch {mem2array cr 32 0x40021000 1}]} { break }
        if {$cr(0) & 0x02000000} { break }
    }
    catch {mww 0x40021004 0x00380402}
    if {[catch {mem2array cfgr 32 0x40021004 1}]} { echo "boost: CFGR read failed"; catch {resume}; return }
    echo [format "boost: RCC_CFGR=0x%08X (SWS=%d, PLL=2)" $cfgr(0) [expr {($cfgr(0) >> 2) & 3}]]
    catch {resume}
}

proc target_clock {} {
    if {[catch {halt}]} { return }
    if {[catch {mem2array cfgr 32 0x40021004 1}]} { catch {resume}; return }
    set sws [expr {($cfgr(0) >> 2) & 3}]
    set hpre [expr {($cfgr(0) >> 4) & 0xF}]
    set div {1 2 4 8 16 64 128 256 2 4 8 16 64 128 256 512}
    set base [expr {$sws == 2 ? 64 : 8}]
    echo [format "target: SYSCLK src=%d (~%d MHz), HCLK~%.1f MHz" \
        $sws $base [expr {$base / double([lindex $div $hpre])}]]
    catch {resume}
}

proc read_words {addr nwords} {
    if {[catch {mem2array w32 32 $addr $nwords}]} { return 0 }
    return 1
}

proc rtt_bench {window_ms} {
    if {[catch {halt}]} { echo "halt failed"; return }
    if {[catch {mem2array cb 32 $::CB 12}]} { echo "CB read failed"; catch {resume}; return }
    catch {resume}
    set buf_ptr $cb(7)
    set size $cb(8)
    set wr_addr [expr {$::CB + 24 + 12}]
    set rd_addr [expr {$::CB + 24 + 16}]
    echo [format "RTT up-buffer ptr=0x%08X size=%d" $buf_ptr $size]
    if {$size == 0 || $size > 65536} { echo "bad control block - RTT fw running?"; return }

    mem2array g0 32 $::G_BYTES 3
    set total 0; set drains 0; set empties 0; set maxdrain 0; set badcb 0
    set e_cb 0; set e_data 0; set e_wr 0
    set t0 [clock milliseconds]
    while {[clock milliseconds] - $t0 < $window_ms} {
        if {[catch {mem2array off 32 $wr_addr 2}]} { incr e_cb; continue }
        set wr $off(0); set rd $off(1)
        # a torn/garbage control block would send us to a bogus address
        if {$wr >= $size || $rd >= $size} {
            incr badcb
            catch {mem2array cb 32 $::CB 12}
            set buf_ptr $cb(7); set size $cb(8)
            continue
        }
        set avail [expr {($wr - $rd + $size) % $size}]
        if {$avail == 0} { incr empties; continue }
        # Reads are issued in bounded chunks: while the target RUNS its AP
        # transactions contend with the core, and one late ACK inside a long
        # block transfer fails the whole transfer. Small chunks + retry keep it
        # reliable (and end up faster).
        set whole [expr {$avail & ~3}]
        set rem [expr {$avail - $whole}]
        set left $whole
        set pos $rd
        set bad 0
        while {$left > 0} {
            set chunk $::CHUNK
            if {$left < $chunk} { set chunk $left }
            set src [expr {$buf_ptr + $pos}]
            set nw [expr {$chunk / 4}]
            if {[read_words $src $nw] == 0} {
                incr bad
                if {$bad > 3} { break }
            } else {
                set pos [expr {($pos + $chunk) % $size}]
                set left [expr {$left - $chunk}]
            }
        }
        if {$bad > 3} { incr e_data; continue }
        if {$rem > 0} {
            set src8 [expr {$buf_ptr + (($rd + $whole) % $size)}]
            catch {mem2array data8 8 $src8 $rem}
        }
        set newrd [expr {($rd + $avail) % $size}]
        if {[catch {mww $rd_addr $newrd}]} { incr e_wr; continue }
        incr total $avail
        incr drains
        if {$avail > $maxdrain} { set maxdrain $avail }
    }
    set dt [expr {[clock milliseconds] - $t0}]
    set tps [expr {$dt > 0 ? $drains * 1000.0 / $dt : 0}]
    set kbs [expr {$dt > 0 ? $total / ($dt / 1000.0) / 1024.0 : 0}]
    echo [format "RESULT drained %d bytes in %d ms -> %.1f KB/s" $total $dt $kbs]
    echo [format "RESULT polls=%.0f/s drains=%d empties=%d maxdrain=%d avg=%.0f B" \
        $tps $drains $empties $maxdrain [expr {$drains ? $total / double($drains) : 0}]]
    echo [format "RESULT errors: badcb=%d cb_read=%d data_read=%d rd_write=%d" \
        $badcb $e_cb $e_data $e_wr]
    halt
    mem2array g1 32 $::G_BYTES 3
    echo [format "RESULT target produced %d bytes in %d ms -> %.1f KB/s" \
        [expr {$g1(0) - $g0(0)}] $dt [expr {($g1(0) - $g0(0)) / ($dt / 1000.0) / 1024.0}]]
    resume
}

if {$BOOST} { boost_clock_halted } else { echo "boost: disabled (raw reset clock)" }
target_clock
rtt_bench $WINDOW_MS
