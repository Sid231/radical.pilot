
__copyright__ = "Copyright 2013-2016, http://radical.rutgers.edu"
__license__   = "MIT"

import threading

import radical.utils as ru

from ... import utils     as rpu
from ... import states    as rps
from ... import constants as rpc

from .base import UMGRSchedulingComponent, ROLE, ADDED


# ==============================================================================
#
class RoundRobin(UMGRSchedulingComponent):

    # --------------------------------------------------------------------------
    #
    def __init__(self, cfg, session):

        self.pilots = None

        UMGRSchedulingComponent.__init__(self, cfg, session)


    # --------------------------------------------------------------------------
    #
    def _configure(self):

        self._wait_pool = list()             # set of unscheduled units
        self._wait_lock = threading.RLock()  # look on the above set

        self._idx = 0


    # --------------------------------------------------------------------------
    #
    def add_pilot(self, pid):

        # a pilot just got added.  If we did not have any pilot before, we might
        # have units in the wait queue waiting -- now is a good time to take
        # care of those!
        with self._wait_lock:

            for unit in self._wait_pool:

                # FIXME: we know the pilot is 'ADDED', but are we shure we have
                #        the 'thing' dict?
                self._schedule_unit(unit, pid)

            # all units are scheduled -- empty the wait pool
            self._wait_pool = list()


    # --------------------------------------------------------------------------
    #
    def work(self, unit):

        self.advance(unit, rps.UMGR_SCHEDULING, publish=True, push=False)

        uid = unit['uid']

        with self._pilots_lock:

            # collect all pilots we know about
            pids = list()
            for pid in self._pilots.keys():

                # we need not only an added pilot, we also need one which we 
                # can inspect
                if self._pilots[pid]['thing'] and \
                   self._pilots[pid][ROLE] == ADDED:
                    pids.append(pid)

            if not len(pids):

                # no pilot is active, yet -- we add to the wait queue
                with self._wait_lock:
                    self._prof.prof('wait', uid=uid)
                    self._wait_pool.append(unit)
                    return

            # we have active pilots: use them!

            if  self._idx >= len(pids) : 
                self._idx = 0

            self._schedule_unit(unit, pids[self._idx])


    # --------------------------------------------------------------------------
    #
    def _schedule_unit(self, unit, pid):

        with self._pilots_lock:

            pilot = self._pilots[pid]

            # we assign the unit to the pilot.
            unit['pilot'] = pid

            # this is also a good opportunity to determine the unit sndboxes
            unit['pilot_sandbox'] = self._session._get_pilot_sandbox(pilot['thing'])
            unit['sandbox']       = self._session._get_unit_sandbox(unit, pilot['thing'])

            # we need to push 'pilot' to the db, otherwise the agent will never
            # pick up the unit
            unit['$set'] = ['pilot', 'sandbox', 'pilot_sandbox']
            self.advance(unit, rps.UMGR_STAGING_INPUT_PENDING, 
                    publish=True, push=True)
        

    # --------------------------------------------------------------------------
    #
    def _release_slot(self, opaque_slots):

        pass


# ------------------------------------------------------------------------------

