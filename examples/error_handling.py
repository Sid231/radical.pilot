#!/usr/bin/env python

__copyright__ = "Copyright 2013-2014, http://radical.rutgers.edu"
__license__   = "MIT"

import os
import sys
import radical.pilot as rp
import time

# READ: The RADICAL-Pilot documentation: 
#   http://radicalpilot.readthedocs.org/en/latest
#
# Try running this example with RADICAL_PILOT_VERBOSE=debug set if 
# you want to see what happens behind the scences!


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


#-------------------------------------------------------------------------------
#
if __name__ == "__main__":

    """
    This example shows how simple error handling can be implemented 
    synchronously using blocking wait() calls.

    The code launches a pilot with 128 cores on 'localhost'. Unless localhost
    has 128 or more cores available, this is bound to fail. This example shows
    how this error can be caught and handled. 
    """

    # Create a new session. A session is a set of Pilot Managers
    # and Unit Managers (with associated Pilots and ComputeUnits).
    session = rp.Session()

    # Create a new pilot manager.
    pmgr = rp.PilotManager(session=session)

    # Register our callback with the PilotManager. This callback will get
    # called every time any of the pilots managed by the PilotManager
    # change their state.
    pmgr.register_callback(pilot_state_cb)

    # Create a new pilot with 128 cores. This will most definetly 
    # fail on 'localhost' because not enough cores are available. 
    pd = rp.ComputePilotDescription()
    pd.resource  = "localhost"
    pd.cores     = 128
    pd.runtime   = 10 

    pilot = pmgr.submit_pilots(pd)
    state = pilot.wait(state=[rp.ACTIVE, rp.FAILED], timeout=60)

    # If the pilot is in FAILED state it probably didn't start up properly. 
    if state == rp.FAILED:
        print pilot.log[-1] # Get the last log message
        return 0
    # The timeout was reached if the pilot state is still FAILED.
    elif state == rp.PENDING:
        print "Timeout..."
        return 1
    # If the pilot is not in FAILED or PENDING state, it is probably running.
    else:
        print "Pilot in state '%s'" % state
        # Since the pilot is running, we can cancel it now.
        # We should not hve gooten that far.
        pilot.cancel()

#-------------------------------------------------------------------------------

