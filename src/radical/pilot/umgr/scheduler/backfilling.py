
__copyright__ = "Copyright 2013-2016, http://radical.rutgers.edu"
__license__   = "MIT"

import os
import pprint
import threading

import radical.utils as ru

from ... import utils     as rpu
from ... import states    as rps
from ... import constants as rpc

from .base import UMGRSchedulingComponent, ROLE, ADDED


# the high water mark determines the percentage of unit oversubscription for the
# pilots, in terms of numbers of cores
_HWM = int(os.environ.get('RADICAL_PILOT_BACKFILLING_HWM', 200))

# we consider pilots eligible for unit scheduling beyond a certain start state,
# which defaults to 'ACTIVE'.
_BF_START = os.environ.get('RADICAL_PILOT_BACKFILLING_START', rps.PMGR_ACTIVE)
_BF_STOP  = os.environ.get('RADICAL_PILOT_BACKFILLING_STOP',  rps.PMGR_ACTIVE)

_BF_START_VAL = rps._pilot_state_value(_BF_START)
_BF_STOP_VAL  = rps._pilot_state_value(_BF_STOP)

# ==============================================================================
#
class Backfilling(UMGRSchedulingComponent):

    # --------------------------------------------------------------------------
    #
    def __init__(self, cfg, session):

        UMGRSchedulingComponent.__init__(self, cfg, session)


    # --------------------------------------------------------------------------
    #
    def _configure(self):

        self._wait_pool = dict()             # set of unscheduled units
        self._wait_lock = threading.RLock()  # look on the above set

        self._pids = list()
        self._idx  = 0


    # --------------------------------------------------------------------------
    #
    def add_pilots(self, pids):

      # print ' === add pilots %s' % pids

        # pilots just got added.  If we did not have any pilot before, we might
        # have units in the wait queue waiting -- now is a good time to take
        # care of those!
        with self._wait_lock:

            # initialize custom data for the pilot
            for pid in pids:
                pilot = self._pilots[pid]['pilot']
                cores = pilot['description']['cores']
                hwm   = int(cores * _HWM/100)
                self._pilots[pid]['info'] = {
                        'cores' : cores,
                        'hwm'   : hwm,
                        'used'  : 0, 
                        'units' : list(), # list of assigned unit IDs
                        'done'  : list(), # list of executed unit IDs
                        }

            # now we can use the pilot
            self._pids += pids
            self._schedule_units()


    # --------------------------------------------------------------------------
    #
    def remove_pilots(self, pids):

      # print ' === rem pilots %s' % pids

        with self._pilots_lock:

            for pid in pids:

                if not pid in self._pids:
                    raise ValueError('no such pilot %s' % pid)

                self._pids.remove(pid)
                # FIXME: cancel units


    # --------------------------------------------------------------------------
    #
    def update_pilots(self, pids):

        # FIXME: if ACTIVE: schedule
        # FIXME: if FINAL:  un/re-schedule
        action = False
        with self._pilots_lock:

            for pid in pids:

                state = self._pilots[pid]['state']

              # print ' === upd pilot  %s: %s' % (pid, state)

                if  rps._pilot_state_value(state) < _BF_START_VAL:
                    # not eligible, yet
                    continue

                if  rps._pilot_state_value(state) > _BF_STOP_VAL:
                    # not ligible anymore
                    continue

                # this pilot is eligible.  Stop checking the others, and attempt
                # reschedule
                action = True
                break

        if action:
          # print ' === upd pilot  -> schedule'
            self._schedule_units()


    # --------------------------------------------------------------------------
    #
    def update_units(self, units):

        reschedule = False

        with self._pilots_lock, self._wait_lock:

            for unit in units:

                uid = unit['uid']
                pid = unit.get('pilot')
                if not pid:
                  # print ' === upd unit  %s no pilot' % uid
                    # we are not interested in state updates for unscheduled
                    # units
                    continue

                if not pid in self._pilots:
                  # print ' === upd unit  %s not handled' % uid
                    # we don't handle the pilot of this unit
                    continue

                info = self._pilots[pid]['info']

                if uid in info['done']:
                  # print ' === upd unit  %s in done' % uid
                    # we don't need further state udates
                    continue

                if not uid in info['units']:
                  # print ' === upd unit  %s not in units' % uid
                    # this contradicts the unit's assignment
                    self._log.error('bf: unit %s on %s inconsistent', uid, pid)
                    raise RuntimeError('inconsistent scheduler state')


                state = unit['state']
              # print ' === upd unit  %s [%s]' % (uid, state)
                if  rps._unit_state_value(state) > \
                    rps._unit_state_value(rps.AGENT_EXECUTING):
                    # this unit is now considered done
                    info['done'].append(uid)
                    info['used'] -= unit['description']['cores']
                    reschedule = True
                  # print ' === upd unit  %s -> schedule' % uid

                    if info['used'] < 0:
                        self._log.error('bf: pilot %s inconsistent', pid)
                        raise RuntimeError('inconsistent scheduler state')

        # if any pilot state was changed, consider new units for scheduling
        if reschedule:
          # print ' === upd units -> schedule'
            self._schedule_units()


    # --------------------------------------------------------------------------
    #
    def work(self, units):

        if not isinstance(units, list): 
            units = [units]

        self.advance(units, rps.UMGR_SCHEDULING, publish=True, push=False)

        with self._wait_lock:
            for unit in units:
                self._prof.prof('wait', uid=unit['uid'])
                self._wait_pool[unit['uid']] = unit
                        
        self._schedule_units()


    # --------------------------------------------------------------------------
    #
    def _schedule_units(self):
        """
        We have a set of units which we can place over a set of pilots.  
        
        The overall objective is to keep pilots busy while load balancing across
        all pilots, even those which might yet to get added.  We achieve that
        via the following algorithm:

          - for each pilot which is being added, no matter the state:
            - assign sufficient units to the pilot that it can run 'n'
              generations of them, 'n' being a tunable parameter called
              'RADICAL_PILOT_BACKFILLING_HWM'.  

          - for each unit being completed (goes out of EXECUTING state)
            - determine the pilot which executed it
            - backfill units from the wait queue until the backfilling HWM is
              reached again.

        The HWM is interpreted as percent of pilot size.  For example, a pilot
        of size 10 cores and a HWM of 200 can get units with a total of 20 cores
        assigned.  It can get assigned more than that, if the last unit
        assigned to it surpasses the HWM.  We will not schedule any unit larger
        than pilot size however.
        """

      # print '\n################################### schedule\n'

        with self._pilots_lock, self._wait_lock:

            # units to advance beyond scheduling
            to_advance = list()

            # check if we have pilots to schedule over
            if not self._pids:
                return

            # we ignore pilots which are not yet added, are not yet in
            # BF_START_STATE, and are beyond ACTIVE state
            pids = list()
            for pid in self._pids:

                info  = self._pilots[pid]['info']
                state = self._pilots[pid]['state']
                role  = self._pilots[pid]['role']

                if role != ADDED:
                    continue

                if  rps._pilot_state_value(state) < _BF_START_VAL:
                    # not eligible, yet
                    continue

                if  rps._pilot_state_value(state) > _BF_STOP_VAL:
                    # not ligible anymore
                    continue

                if info['used'] >= info['hwm']:
                    # pilot is full
                    continue

                pids.append(pid)


            # cycle over available pids and add units until we either ran
            # out of units to schedule, or out of pids to schedule over

            self._log.debug(' === schedule %s units over %s pilots',
                    len(self._wait_pool), len(pids))

            scheduled   = list()   # units we want to advance
            unscheduled = dict()   # this will be the new wait pool
            for uid, unit in self._wait_pool.iteritems():

                if not pids:
                    # no more useful pilots -- move remaining units into
                    # unscheduled pool
                  # print ' =!= sch unit  %s' % uid
                    unscheduled[uid] = unit
                    continue

                cores   = unit['description']['cores']
                success = False
                for pid in pids:

                    info = self._pilots[pid]['info']

                    if info['used'] <= info['hwm']:

                      # print ' === sch unit  %s -> %s' % (uid, pid)

                        pilot = self._pilots[pid]['pilot']
                        info['units'].append(unit['uid'])
                        info['used']   += cores
                        unit['pilot']   = pid
                        unit['sandbox'] = self._session._get_unit_sandbox(unit, pilot)
                        scheduled.append(unit)
                        success = True

                        # this pilot might now be full.  If so, remove it from
                        # list of eligible pids
                        if info['used'] >= info['hwm']:
                            pids.remove(pid)

                        break  # stop looking through pilot list

                if not success:
                    # we did not find a useable pilot for this unit -- keep it
                  # print ' ==! sch unit  %s' % uid
                    unscheduled[uid] = unit


            self._log.debug(' === retain   %s units and  %s pilots',
                    len(unscheduled), len(pids))

            # all unscheduled units *are* the new wait pool
          # print ' 1 > waits: %s' % self._wait_pool.keys()
            self._wait_pool = unscheduled
          # print ' 2 > waits: %s' % self._wait_pool.keys()

        # advance scheduled units
        if scheduled:
            self.advance(scheduled, rps.UMGR_STAGING_INPUT_PENDING, 
                         publish=True, push=True)


      # print '\nafter schedule:'
      # print 'waits:    %s' % self._wait_pool.keys()
      # for pid in self._pilots:
      #     print 'pilot %s' % pid
      #     pprint.pprint(self._pilots[pid]['info'])
      # print

        

# ------------------------------------------------------------------------------
