# Raw JTAG scan cost as OpenOCD sees it - no Python/pyusb overhead in the loop.
#
# Usage:
#   openocd -s <sdk>/tools/openocd/tcl -s <sdk>/hpm_sdk/boards/openocd ^
#           -f script_test/openocd_hpm6800evk_dap.cfg ^
#           -c "init" -c "halt" -c "source script_test/hpm6800_jtag_bench.tcl" -c "shutdown"

proc timed {label n body} {
    set t0 [clock microseconds]
    for {set i 0} {$i < $n} {incr i} {
        uplevel 1 $body
    }
    set t1 [clock microseconds]
    set total [expr {$t1 - $t0}]
    echo [format "%-34s %8d x  ->  %9d us total, %8.3f us/op" $label $n $total [expr {$total / double($n)}]]
    return $total
}

echo "=== raw JTAG scan cost (Tcl loop, one flush per command) ==="
irscan hpm6880.cpu 0x11

timed "irscan 5 bit"          500 { irscan hpm6880.cpu 0x11 }
timed "drscan 41 bit"         500 { drscan hpm6880.cpu 0x00 41 0x0000000000 }
timed "drscan 41 bit (again)" 500 { drscan hpm6880.cpu 0x00 41 0x0000000000 }

echo ""
echo "=== OpenOCD is flushing the JTAG queue once per Tcl command, so the number"
echo "    above is one USB round trip plus the scan itself. ==="
