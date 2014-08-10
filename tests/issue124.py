
import sys
import radical.pilot as rp

# ##############################################################################
# #124: CUs are failing on Trestles
# ##############################################################################

#------------------------------------------------------------------------------
#
def pilot_state_cb (pilot, state) :
    """ this callback is invoked on all pilot state changes """

    print "[Callback]: ComputePilot '%s' state: %s." % (pilot.uid, state)

    if  state == rp.FAILED :
        sys.exit (1)


#------------------------------------------------------------------------------
#
def unit_state_change_cb (unit, state) :
    """ this callback is invoked on all unit state changes """

    print "[Callback]: ComputeUnit  '%s' state: %s." % (unit.uid, state)

    if  state == rp.FAILED :
        sys.exit (1)


#------------------------------------------------------------------------------
#
if __name__ == "__main__":

    session = rp.Session()

    # Add an ssh identity to the session.
    c = rp.Context('ssh')
    c.user_id = 'amerzky'
    session.add_context(c)

    pm = rp.PilotManager(session=session)
    pm.register_callback(pilot_state_cb)

    pd = rp.ComputePilotDescription()
    pd.resource = "trestles.sdsc.xsede.org"
    pd.cores    = 1
    pd.runtime  = 10
    pd.cleanup  = True

    pilot_object = pm.submit_pilots(pd)
    
    um = rp.UnitManager(session=session, scheduler=rp.SCHED_ROUND_ROBIN)

    um.add_pilots(pilot_object)

    compute_units = []
    for k in range(0, 32):
        cu = rp.ComputeUnitDescription()
        cu.cores = 1
        cu.executable = "/bin/date"
        compute_units.append(cu)

    units = um.submit_units(compute_units)

    print "Waiting for all compute units to finish..."
    um.wait_units()

    for unit in units :
        assert (unit.state == rp.DONE)

    print "  FINISHED"
    pm.cancel_pilots()

    session.close ()

    sys.exit (0)


# ------------------------------------------------------------------------------

