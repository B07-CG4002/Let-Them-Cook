# Operates exclusively on the prepared snapshot; never opens the old project.
proc exactly_one {objects description} {
    if {[llength $objects] != 1} {error "Expected one $description; found: $objects"}
    return [lindex $objects 0]
}
proc main {} {
    global argv
    if {[llength $argv] != 2} {error "Expected experiment directory and job count"}
    set root [file normalize [lindex $argv 0]]
    if {![string match "2026.1*" [version -short]]} {error "Use reviewed Vivado 2026.1"}
    set project_dir [file join $root project]
    if {[file exists $project_dir]} {error "Project already exists. Use a new experiment."}
    create_project cg4002_hand12 $project_dir -part xazu3eg-sbva484-1-i
    set_property target_language Verilog [current_project]
    set_property ip_repo_paths [list [file join $root ip]] [current_project]
    update_ip_catalog
    exactly_one [get_ipdefs -all xilinx.com:hls:gesture_cnn_hand_u2_cache:1.0] "packaged hand CNN IP definition"
    set bd [file join $root sources multiply_accumulate multiply_accumulate.bd]
    read_bd $bd
    open_bd_design $bd
    set ps [exactly_one [get_bd_cells zynq_ultra_ps_e_0] "processing system"]
    if {[llength [get_bd_cells -quiet gesture_cnn_full_0]] != 0} {error "Old accelerator unexpectedly remains"}
    set mhz [get_property CONFIG.PSU__CRL_APB__PL0_REF_CTRL__FREQMHZ $ps]
    if {$mhz < 99.0 || $mhz > 101.0} {error "Unexpected PS PL0 clock target: $mhz MHz"}
    set ip gesture_cnn_hand_u2_cache_0
    create_bd_cell -type ip -vlnv xilinx.com:hls:gesture_cnn_hand_u2_cache:1.0 $ip
    connect_bd_intf_net [get_bd_intf_pins axi_smc/M00_AXI] [get_bd_intf_pins $ip/s_axi_control]
    connect_bd_intf_net [get_bd_intf_pins $ip/m_axi_gmem0] [get_bd_intf_pins axi_smc_1/S00_AXI]
    connect_bd_intf_net [get_bd_intf_pins $ip/m_axi_gmem1] [get_bd_intf_pins axi_smc_1/S01_AXI]
    connect_bd_net [get_bd_pins zynq_ultra_ps_e_0/pl_clk0] [get_bd_pins $ip/ap_clk]
    connect_bd_net [get_bd_pins rst_ps8_0_99M/peripheral_aresetn] [get_bd_pins $ip/ap_rst_n]
    assign_bd_address -offset 0x80000000 -range 64K \
        -target_address_space [get_bd_addr_spaces zynq_ultra_ps_e_0/Data] \
        [get_bd_addr_segs $ip/s_axi_control/Reg]
    foreach master {Data_m_axi_gmem0 Data_m_axi_gmem1} {
        assign_bd_address -offset 0x00000000 -range 2G \
            -target_address_space [get_bd_addr_spaces $ip/$master] \
            [get_bd_addr_segs zynq_ultra_ps_e_0/SAXIGP1/HPC1_DDR_LOW]
        exclude_bd_addr_seg -target_address_space [get_bd_addr_spaces $ip/$master] \
            [get_bd_addr_segs zynq_ultra_ps_e_0/SAXIGP1/HPC1_LPS_OCM]
    }
    # Polling driver: interrupt output deliberately has no interrupt-controller route.
    validate_bd_design
    save_bd_design
    file mkdir [file join $root reports]
    report_ip_status -file [file join $root reports ip_status.rpt]
    write_bd_tcl -force [file join $root reports validated_design.tcl]
    close_project
    set f [open [file join $root setup_complete.txt] w]
    puts $f "Vivado block design validation completed; no physical board test."
    close $f
    puts "SETUP FINISHED: independent-hand block design validated."
}
if {[catch {main} message options]} {
    puts stderr "HAND VIVADO SETUP FAILED: $message"
    if {[dict exists $options -errorinfo]} {puts stderr [dict get $options -errorinfo]}
    exit 1
}
exit 0
