proc finished_run {name} {
    set run [get_runs $name]
    set progress [get_property PROGRESS $run]
    set status [get_property STATUS $run]
    puts "RUN $name: progress=$progress status=$status"
    if {$progress ne "100%" || [regexp -nocase {error|failed|cancel} $status]} {
        error "$name did not complete: $status ($progress)"
    }
}
proc main {} {
    global argv
    if {[llength $argv] != 2} {error "Expected experiment directory and job count"}
    set root [file normalize [lindex $argv 0]]
    set jobs [lindex $argv 1]
    if {![string is integer -strict $jobs] || $jobs < 1 || $jobs > 16} {error "Jobs must be 1..16"}
    if {![string match "2026.1*" [version -short]]} {error "Use reviewed Vivado 2026.1"}
    if {![file exists [file join $root setup_complete.txt]]} {error "Run validation first"}
    set results [file join $root results]
    if {[file exists $results]} {error "Build was already attempted; preserve it and prepare a new experiment"}
    file mkdir $results
    open_project [file join $root project cg4002_hand12.xpr]
    set bd [get_files [file join $root sources multiply_accumulate multiply_accumulate.bd]]
    if {[llength $bd] != 1} {error "Prepared block design missing"}
    open_bd_design $bd
    validate_bd_design
    save_bd_design
    generate_target all $bd
    set wrappers [make_wrapper -files $bd -top]
    add_files -norecurse $wrappers
    set_property top multiply_accumulate_wrapper [current_fileset]
    update_compile_order -fileset sources_1
    launch_runs synth_1 -jobs $jobs
    wait_on_run synth_1
    finished_run synth_1
    launch_runs impl_1 -to_step route_design -jobs $jobs
    wait_on_run impl_1
    finished_run impl_1
    open_run impl_1
    report_timing_summary -report_unconstrained -file [file join $results timing_summary.rpt]
    report_utilization -file [file join $results utilization.rpt]
    report_clocks -file [file join $results clocks.rpt]
    check_timing -verbose -file [file join $results check_timing.rpt]
    report_drc -file [file join $results drc.rpt]
    foreach delay {max min} {
        set paths [get_timing_paths -delay_type $delay -max_paths 1]
        if {[llength $paths] != 1} {error "Missing $delay timing path; review constraints"}
        set slack [get_property SLACK [lindex $paths 0]]
        if {$slack < 0} {error "Routed $delay timing slack is $slack ns. Bitstream not written."}
    }
    write_bitstream [file join $results cnn_hand12_v1.bit]
    close_project
    set f [open [file join $root build_complete.txt] w]
    puts $f "Local implementation and bitstream generation completed. Board testing still pending."
    close $f
    puts "BUILD FINISHED: run artifact validation/export next."
}
if {[catch {main} message options]} {
    puts stderr "HAND VIVADO BUILD FAILED: $message"
    if {[dict exists $options -errorinfo]} {puts stderr [dict get $options -errorinfo]}
    exit 1
}
exit 0
