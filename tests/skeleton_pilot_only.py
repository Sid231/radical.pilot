import os
import sys
import radical.pilot as rp

#------------------------------------------------------------------------------
#
if __name__ == "__main__":

    # Create a new session. A session is the 'root' object for all other
    # SAGA-Pilot objects. It encapsualtes the MongoDB connection(s) as
    # well as security crendetials.
    session = rp.Session()

    # Add a Pilot Manager. Pilot managers manage one or more ComputePilots.
    pmgr = rp.PilotManager(session=session)

    # Register our callback with the PilotManager. This callback will get
    # called every time any of the pilots managed by the PilotManager
    # change their state.
    pmgr.register_callback(pilot_state_cb)

    # Define a 2-core local pilot that runs for 10 minutes.
    pdesc = rp.ComputePilotDescription()
    pdesc.resource = "localhost"
    pdesc.runtime = 10
    pdesc.cores = 2
    
    # Launch the pilot.
    pilot = pmgr.submit_pilots(pdesc)

    pmgr.wait_pilots()

    print "\n== PILOT STATE HISTORY==\n"

    for state in pilot.state_history:
        print " * %s: %s\n" % (state.timestamp, state.state)

    # Remove session from database
    session.close()

