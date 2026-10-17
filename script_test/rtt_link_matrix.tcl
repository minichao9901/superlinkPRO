# SWD read reliability while the target RUNS (the situation RTT polling is in),
# as a function of SWD clock and target HCLK.
#
# usage: openocd ... -c "init" -c "reset run" -c "source rtt_link_matrix.tcl" -c shutdown

set CB 0x2000000C

proc try_reads {n} {
    set fails 0
    for {set i 0} {$i < $n} {incr i} {
        if {[catch {mem2array t 32 $::CB 1}]} { incr fails }
    }
    return $fails
}

proc set_target_clock {mhz} {
    halt
    if {$mhz > 8} {
        mww 0x40022000 0x00000012
        mww 0x40021004 0x00380400
        mem2array cr 32 0x40021000 1
        mww 0x40021000 [expr {$cr(0) | 0x01000000}]
        for {set i 0} {$i < 200} {incr i} {
            mem2array cr 32 0x40021000 1
            if {$cr(0) & 0x02000000} { break }
        }
        mww 0x40021004 0x00380402
    } else {
        # back to the reset default: HSI, PLL off (SW=00 first)
        mww 0x40021004 0x00000000
        mem2array cr 32 0x40021000 1
        mww 0x40021000 [expr {$cr(0) & ~0x01000000}]
    }
    mem2array cfgr 32 0x40021004 1
    set sws [expr {($cfgr(0) >> 2) & 3}]
    echo [format "target clock set: RCC_CFGR=0x%08X SWS=%d" $cfgr(0) $sws]
    resume
    after 200
}

foreach hclk {8 64} {
    set_target_clock $hclk
    foreach spd {1000 10000 20000 36000 45000 60000} {
        adapter speed $spd
        set f [try_reads 20]
        echo [format "  target~%2d MHz, SWD %6d kHz : %2d/20 reads failed" $hclk $spd $f]
    }
}

# leave the target at a sane state
set_target_clock 8
adapter speed 10000
echo "matrix done"
