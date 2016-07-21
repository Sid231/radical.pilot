
__copyright__ = "Copyright 2013-2016, http://radical.rutgers.edu"
__license__   = "MIT"


import os
import copy
import time
import pprint
import threading

import radical.utils as ru

from .  import utils     as rpu
from .  import states    as rps
from .  import constants as rpc
from .  import types     as rpt

from .umgr import scheduler as rpus


# ------------------------------------------------------------------------------
#
class UnitManager(rpu.Component):
    """
    A UnitManager manages :class:`radical.pilot.ComputeUnit` instances which
    represent the **executable** workload in RADICAL-Pilot. A UnitManager connects
    the ComputeUnits with one or more :class:`Pilot` instances (which represent
    the workload **executors** in RADICAL-Pilot) and a **scheduler** which
    determines which :class:`ComputeUnit` gets executed on which
    :class:`Pilot`.

    **Example**::

        s = radical.pilot.Session(database_url=DBURL)

        pm = radical.pilot.PilotManager(session=s)

        pd = radical.pilot.ComputePilotDescription()
        pd.resource = "futuregrid.alamo"
        pd.cores = 16

        p1 = pm.submit_pilots(pd) # create first pilot with 16 cores
        p2 = pm.submit_pilots(pd) # create second pilot with 16 cores

        # Create a workload of 128 '/bin/sleep' compute units
        compute_units = []
        for unit_count in range(0, 128):
            cu = radical.pilot.ComputeUnitDescription()
            cu.executable = "/bin/sleep"
            cu.arguments = ['60']
            compute_units.append(cu)

        # Combine the two pilots, the workload and a scheduler via
        # a UnitManager.
        um = radical.pilot.UnitManager(session=session,
                                       scheduler=radical.pilot.SCHEDULER_ROUND_ROBIN)
        um.add_pilot(p1)
        um.submit_units(compute_units)


    The unit manager can issue notification on unit state changes.  Whenever
    state notification arrives, any callback registered for that notification is
    fired.  
    
    NOTE: State notifications can arrive out of order wrt the unit state model!
    """

    # --------------------------------------------------------------------------
    #
    def __init__(self, session, scheduler=None):
        """
        Creates a new UnitManager and attaches it to the session.

        **Arguments:**
            * session [:class:`radical.pilot.Session`]:
              The session instance to use.
            * scheduler (`string`): 
              The name of the scheduler plug-in to use.

        **Returns:**
            * A new `UnitManager` object [:class:`radical.pilot.UnitManager`].
        """

        self._components  = None
        self._pilots      = dict()
        self._pilots_lock = threading.RLock()
        self._units       = dict()
        self._units_lock  = threading.RLock()
        self._callbacks   = dict()
        self._cb_lock     = threading.RLock()
        self._terminate   = threading.Event()
        self._closed      = False
        self._rec_id      = 0       # used for session recording

        for m in rpt.UMGR_METRICS:
            self._callbacks[m] = list()

        cfg = ru.read_json("%s/configs/umgr_%s.json" \
                % (os.path.dirname(__file__),
                   os.environ.get('RADICAL_PILOT_UMGR_CFG', 'default')))

        if scheduler:
            # overwrite the scheduler from the config file
            cfg['scheduler'] = scheduler

        if not cfg.get('scheduler'):
            # set default scheduler if needed
            cfg['scheduler'] = rpus.SCHEDULER_DEFAULT

        assert(cfg['db_poll_sleeptime'])

        # before we do any further setup, we get the session's ctrl config with
        # bridge addresses, dburl and stuff.
        ru.dict_merge(cfg, session.ctrl_cfg, ru.PRESERVE)

        # initialize the base class (with no intent to fork)
        self._uid    = ru.generate_id('umgr')
        cfg['owner'] = self.uid
        rpu.Component.__init__(self, cfg, session)
        self.start(spawn=False)

        # only now we have a logger... :/
        self._log.report.info('<<create unit manager')
        self._prof.prof('create umgr', uid=self._uid)

        # we can start bridges and components, as needed
        self._controller = rpu.Controller(cfg=self._cfg, session=self.session)

        # merge controller config back into our own config
        ru.dict_merge(self._cfg, self._controller.ctrl_cfg, ru.OVERWRITE)

        # The output queue is used to forward submitted units to the
        # scheduling component.
        self.register_output(rps.UMGR_SCHEDULING_PENDING, 
                             rpc.UMGR_SCHEDULING_QUEUE)

        # the umgr will also collect units from the agent again, for output
        # staging and finalization
        self.register_output(rps.UMGR_STAGING_OUTPUT_PENDING, 
                             rpc.UMGR_STAGING_OUTPUT_QUEUE)

        # register the state notification pull cb
        # FIXME: we may want to have the frequency configurable
        # FIXME: this should be a tailing cursor in the update worker
        self.register_timed_cb(self._state_pull_cb, 
                               timer=self._cfg['db_poll_sleeptime'])

        # register callback which pulls units back from agent
        # FIXME: this should be a tailing cursor in the update worker
        # FIXME: make frequency configurable
        self.register_timed_cb(self._unit_pull_cb, 
                               timer=self._cfg['db_poll_sleeptime'])

        # also listen to the state pubsub for unit state changes
        self.register_subscriber(rpc.STATE_PUBSUB, self._state_sub_cb)

        # let session know we exist
        self._session._register_umgr(self)

        self._prof.prof('UMGR setup done', logger=self._log.debug)
        self._log.report.ok('>>ok\n')


    # --------------------------------------------------------------------------
    #
    def close(self):
        """
        Shut down the UnitManager, and all umgr components.
        """

        # we do not cancel units at this point, in case any component or pilot
        # wants to continue to progress unit states, which should indeed be
        # independent from the umgr life cycle.

        if self._closed:
            return

        self._log.debug("closing %s", self.uid)
        self._log.report.info('<<close unit manager')

        self._terminate.set()
        self._controller.stop()
        self.stop()

        self._session.prof.prof('closed umgr', uid=self._uid)
        self._log.info("Closed UnitManager %s." % self._uid)

        self._closed = True
        self._log.report.ok('>>ok\n')


    # --------------------------------------------------------------------------
    #
    def as_dict(self):
        """
        Returns a dictionary representation of the UnitManager object.
        """

        ret = {
            'uid': self.uid,
            'cfg': self.cfg
        }

        return ret


    # --------------------------------------------------------------------------
    #
    def __str__(self):

        """
        Returns a string representation of the UnitManager object.
        """

        return str(self.as_dict())


    #---------------------------------------------------------------------------
    #
    def _state_pull_cb(self):

        # pull all unit states from the DB, and compare to the states we know
        # about.  If any state changed, update the unit instance and issue
        # notification callbacks as needed
        # FIXME: we also pull for dead units.  That is not efficient...
        # FIXME: this needs to be converted into a tailed cursor in the update
        #        worker
        units  = self._session._dbs.get_units(umgr_uid=self.uid)
        action = False

        for unit in units:
            if self._update_unit(unit['uid'], unit):
                action = True

        return action


    #---------------------------------------------------------------------------
    #
    def _unit_pull_cb(self):

        self._log.info(" === units pulled: ?")

        # pull units those units from the agent which are about to get back
        # under umgr control, and push them into the respective queues
        # FIXME: this should also be based on a tailed cursor
        # FIXME: Unfortunately, 'find_and_modify' is not bulkable, so we have
        #        to use 'find'.  To avoid finding the same units over and over 
        #        again, we update the 'control' field *before* running the next
        #        find -- so we do it right here.
        tgt_states  = rps.FINAL + [rps.UMGR_STAGING_OUTPUT_PENDING]
        unit_cursor = self.session._dbs._c.find(spec={
            'type'    : 'unit',
            'umgr'    : self.uid,
            'control' : 'umgr_pending'})

        if not unit_cursor.count():
            # no units whatsoever...
            self._log.info(" === units pulled:    0")
            return False

        # update the units to avoid pulling them again next time.
        units = list(unit_cursor)
        uids  = [unit['uid'] for unit in units]

        self._session._dbs._c.update(multi    = True,
                        spec     = {'type'  : 'unit',
                                    'uid'   : {'$in'     : uids}},
                        document = {'$set'  : {'control' : 'umgr'}})

        self._log.info(" === units pulled: %4d %s", len(units), [u['uid'] for u in units])
        self._prof.prof('get', msg="bulk size: %d" % len(units), uid=self.uid)
        for unit in units:

            self._log.debug('\n\n=======================================')
            self._log.debug(' === details %s: %s', unit['uid'], pprint.pformat(unit))
            
            # we need to make sure to have the correct state:
            old = unit['state']
            new = rps._unit_state_collapse(unit['states'])
            self._log.debug(' === %s state: %s -> %s', unit['uid'], old, new)

            if new == rps.UMGR_STAGING_OUTPUT:
                self._log.debug(' === %s state: %s -> %s %s', unit['uid'], old, new, unit['states'])


            unit['state'] = new
            unit['control'] = 'umgr'
            self._prof.prof('get', msg="bulk size: %d" % len(units), uid=unit['uid'])

            self._log.debug('\n=======================================\n\n')

        # now we really own the CUs, and can start working on them (ie. push
        # them into the pipeline).  We don't publish the advance, since that
        # happened already on the agent side when the state was set.
        self.advance(units, publish=False, push=True)

        return True


    # --------------------------------------------------------------------------
    #
    def _state_sub_cb(self, topic, msg):

        if isinstance(msg, list): things =  msg
        else                    : things = [msg]

        for thing in things:

            if 'type' in thing and thing['type'] == 'unit':

                uid   = thing["uid"]
                state = thing["state"]

                self._update_unit(uid, {'state' : state})


    # --------------------------------------------------------------------------
    #
    def _update_unit(self, uid, unit_dict):

        # we don't care about units we don't know
        # otherwise get old state
        with self._units_lock:

            if uid not in self._units:
                return False

            # only update on state changes
            if self._units[uid].state != unit_dict['state']:
                return self._units[uid]._update(unit_dict)
            else:
                return False


    # --------------------------------------------------------------------------
    #
    def _call_unit_callbacks(self, unit, state):

        for cb, cb_data in self._callbacks[rpt.UNIT_STATE]:

            if cb_data: cb(unit, state, cb_data)
            else      : cb(unit, state)


    # --------------------------------------------------------------------------
    #
    # FIXME: this needs to go to the scheduler
    def _default_wait_queue_size_cb(self, umgr, wait_queue_size):
        # FIXME: this needs to come from the scheduler?

        self._log.info("[Callback]: wait_queue_size: %s.", wait_queue_size)


    # --------------------------------------------------------------------------
    #
    @property
    def uid(self):
        """
        Returns the unique id.
        """
        return self._uid


    # --------------------------------------------------------------------------
    #
    @property
    def scheduler(self):
        """
        Returns the scheduler name.
        """

        return self._cfg.get('scheduler')



    # --------------------------------------------------------------------------
    #
    def add_pilots(self, pilots):
        """
        Associates one or more pilots with the unit manager.

        **Arguments:**

            * **pilots** [:class:`radical.pilot.ComputePilot` or list of
              :class:`radical.pilot.ComputePilot`]: The pilot objects that will be
              added to the unit manager.
        """

        if self._closed:
            raise RuntimeError("instance is already closed")

        if not isinstance(pilots, list):
            pilots = [pilots]

        if len(pilots) == 0:
            raise ValueError('cannot add no pilots')

        self._log.report.info('<<add %d pilot(s)' % len(pilots))

        with self._pilots_lock:

            # sanity check, and keep pilots around for inspection
            for pilot in pilots:
                pid = pilot.uid
                if pid in self._pilots:
                    raise ValueError('pilot %s already added' % pid)
                self._pilots[pid] = pilot

        pilot_docs = [pilot.as_dict() for pilot in pilots]

        # publish to the command channel for the scheduler to pick up
        self.publish(rpc.CONTROL_PUBSUB, {'cmd' : 'add_pilots',
                                          'arg' : {'pilots': pilot_docs,
                                                   'umgr'  : self.uid}})
        self._log.report.ok('>>ok\n')


    # --------------------------------------------------------------------------
    #
    def list_pilots(self):
        """
        Lists the UIDs of the pilots currently associated with the unit manager.

        **Returns:**
              * A list of :class:`radical.pilot.ComputePilot` UIDs [`string`].
        """

        if self._closed:
            raise RuntimeError("instance is already closed")

        with self._pilots_lock:
            return self._pilots.keys()


    # --------------------------------------------------------------------------
    #
    def get_pilots(self):
        """
        Get the pilots instances currently associated with the unit manager.

        **Returns:**
              * A list of :class:`radical.pilot.ComputePilot` instances.
        """
        if self._closed:
            raise RuntimeError("instance is already closed")

        with self._pilots_lock:
            return self._pilots.values()


    # --------------------------------------------------------------------------
    #
    def remove_pilots(self, pilot_ids, drain=False):
        """
        Disassociates one or more pilots from the unit manager.

        After a pilot has been removed from a unit manager, it won't process
        any of the unit manager's units anymore. Calling `remove_pilots`
        doesn't stop the pilot itself.

        **Arguments:**

            * **drain** [`boolean`]: Drain determines what happens to the units
              which are managed by the removed pilot(s). If `True`, all units
              currently assigned to the pilot are allowed to finish execution.
              If `False` (the default), then non-final units will be canceled.
        """

        # TODO: Implement 'drain'.
        # NOTE: the actual removal of pilots from the scheduler is asynchron!

        if drain:
            raise RuntimeError("'drain' is not yet implemented")

        if self._closed:
            raise RuntimeError("instance is already closed")

        if not isinstance(pilot_ids, list):
            pilot_ids = [pilot_ids]

        if len(pilot_ids) == 0:
            raise ValueError('cannot remove no pilots')

        self._log.report.info('<<add %d pilot(s)' % len(pilot_ids))

        with self._pilots_lock:

            # sanity check, and keep pilots around for inspection
            for pid in pilot_ids:
                if pid not in self._pilots:
                    raise ValueError('pilot %s not added' % pid)
                del(self._pilots[pid])

        # publish to the command channel for the scheduler to pick up
        self.publish(rpc.CONTROL_PUBSUB, {'cmd' : 'remove_pilots',
                                          'arg' : {'pids'  : pilot_ids, 
                                                   'umgr'  : self.uid}})
        self._log.report.ok('>>ok\n')


    # --------------------------------------------------------------------------
    #
    def list_units(self):
        """
        Returns the UIDs of the :class:`radical.pilot.ComputeUnit` managed by
        this unit manager.

        **Returns:**
              * A list of :class:`radical.pilot.ComputeUnit` UIDs [`string`].
        """

        if self._closed:
            raise RuntimeError("instance is already closed")

        with self._pilots_lock:
            return self._units.keys()


    # --------------------------------------------------------------------------
    #
    def submit_units(self, descriptions):
        """
        Submits on or more :class:`radical.pilot.ComputeUnit` instances to the
        unit manager.

        **Arguments:**
            * **descriptions** [:class:`radical.pilot.ComputeUnitDescription`
              or list of :class:`radical.pilot.ComputeUnitDescription`]: The
              description of the compute unit instance(s) to create.

        **Returns:**
              * A list of :class:`radical.pilot.ComputeUnit` objects.
        """

        from .compute_unit import ComputeUnit

        if self._closed:
            raise RuntimeError("instance is already closed")

        ret_list = True
        if not isinstance(descriptions, list):
            ret_list     = False
            descriptions = [descriptions]

        if len(descriptions) == 0:
            raise ValueError('cannot submit no unit descriptions')


        self._log.report.info('<<submit %d unit(s)\n\t' % len(descriptions))

        # we return a list of compute units
        units = list()
        for descr in descriptions :
            unit = ComputeUnit.create(umgr=self, descr=descr)
            units.append(unit)

            # keep units around
            with self._units_lock:
                self._units[unit.uid] = unit

            if self._session._rec:
                import radical.utils as ru
                ru.write_json(descr.as_dict(), "%s/%s.batch.%03d.json" \
                        % (self._session._rec, unit.uid, self._rec_id))

            self._log.report.progress()

        if self._session._rec:
            self._rec_id += 1

        # insert units into the database, as a bulk.
        unit_docs = [unit.as_dict() for unit in units]
        self._session._dbs.insert_units(unit_docs)

        # Only after the insert can we hand the units over to the next
        # components (ie. advance state).
        self.advance(unit_docs, rps.UMGR_SCHEDULING_PENDING, publish=True, push=True)
        self._log.report.ok('>>ok\n')

        if ret_list: return units
        else       : return units[0]


    # --------------------------------------------------------------------------
    #
    def get_units(self, uids=None):
        """Returns one or more compute units identified by their IDs.

        **Arguments:**
            * **uids** [`string` or `list of strings`]: The IDs of the
              compute unit objects to return.

        **Returns:**
              * A list of :class:`radical.pilot.ComputeUnit` objects.
        """
        
        if self._closed:
            raise RuntimeError("instance is already closed")

        if not uids:
            with self._units_lock:
                ret = self._units.values()
            return ret


        ret_list = True
        if (not isinstance(uids, list)) and (uids is not None):
            ret_list = False
            uids = [uids]

        ret = list()
        with self._units_lock:
            for uid in uids:
                if uid not in self._units:
                    raise ValueError('unit %s not known' % uid)
                ret.append(self._units[uid])

        if ret_list: return ret
        else       : return ret[0]


    # --------------------------------------------------------------------------
    #
    def wait_units(self, uids=None, state=None, timeout=None):
        """
        Returns when one or more :class:`radical.pilot.ComputeUnits` reach a
        specific state.

        If `uids` is `None`, `wait_units` returns when **all**
        ComputeUnits reach the state defined in `state`.  This may include
        units which have previously terminated or waited upon.

        **Example**::

            # TODO -- add example

        **Arguments:**

            * **uids** [`string` or `list of strings`]
              If uids is set, only the ComputeUnits with the specified
              uids are considered. If uids is `None` (default), all
              ComputeUnits are considered.

            * **state** [`string`]
              The state that ComputeUnits have to reach in order for the call
              to return.

              By default `wait_units` waits for the ComputeUnits to
              reach a terminal state, which can be one of the following:

              * :data:`radical.pilot.rps.DONE`
              * :data:`radical.pilot.rps.FAILED`
              * :data:`radical.pilot.rps.CANCELED`

            * **timeout** [`float`]
              Timeout in seconds before the call returns regardless of Pilot
              state changes. The default value **None** waits forever.
        """

        if self._closed:
            raise RuntimeError("instance is already closed")

        if not uids:
            with self._units_lock:
                uids = list()
                for uid,unit in self._units.iteritems():
                    if unit.state not in rps.FINAL:
                        uids.append(uid)

        if not state:
            states = rps.FINAL
        elif isinstance(state, list):
            states = state
        else:
            states = [state]

        ret_list = True
        if not isinstance(uids, list):
            ret_list = False
            uids = [uids]

        self._log.report.info('<<wait for %d unit(s)\n\t' % len(uids))

        start    = time.time()
        to_check = None

        with self._units_lock:
            to_check = [self._units[uid] for uid in uids]

        # We don't want to iterate over all units again and again, as that would
        # duplicate checks on units which were found in matching states.  So we
        # create a list from which we drop the units as we find them in
        # a matching state
        self._log.report.idle(mode='start')
        while to_check and not self._terminate.is_set():

            # check timeout
            if timeout and (timeout <= (time.time() - start)):
                self._log.debug ("wait timed out")
                break

            time.sleep (0.1)

            # FIXME: print percentage...
            self._log.report.idle()
          # print 'wait units: %s' % [[u.uid, u.state] for u in to_check]

            check_again = list()
            for unit in to_check:
                if  unit.state not in states and \
                    unit.state not in rps.FINAL:
                    check_again.append(unit)
                else:
                    # stop watching this unit
                    if unit.state in [rps.FAILED]:
                        self._log.report.idle(color='error', c='-')
                    elif unit.state in [rps.CANCELED]:
                        self._log.report.idle(color='warn', c='*')
                    else:
                        self._log.report.idle(color='ok', c='+')

            to_check = check_again

        self._log.report.idle(mode='stop')

        if to_check: self._log.report.warn('>>timeout\n')
        else       : self._log.report.ok(  '>>ok\n')

        # grab the current states to return
        state = None
        with self._units_lock:
            states = [self._units[uid].state for uid in uids]

        # done waiting
        if ret_list: return states
        else       : return states[0]


    # --------------------------------------------------------------------------
    #
    def cancel_units(self, uids=None):
        """
        Cancel one or more :class:`radical.pilot.ComputeUnits`.

        Note that cancellation of units is *immediate*, i.e. their state is
        immediately set to `CANCELED`, even if some RP component may still
        operate on the units.  Specifically, other state transitions, including
        other final states (`DONE`, `FAILED`) can occur *after* cancellation.
        This is a side effect of an optimization: we consider this 
        acceptable tradeoff in the sense "Oh, that unit was DONE at point of
        cancellation -- ok, we can use the results, sure!".

        If that behavior is not wanted, set the environment variable:

            export RADICAL_PILOT_STRICT_CANCEL=True

        **Arguments:**
            * **uids** [`string` or `list of strings`]: The IDs of the
              compute units objects to cancel.
        """
        if self._closed:
            raise RuntimeError("instance is already closed")

        if not uids:
            with self._units_lock:
                uids  = self._units.keys()
        else:
            if not isinstance(uids, list):
                uids = [uids]

        # NOTE: We advance all units to cancelled, and send a cancellation
        #       control command.  If that command is picked up *after* some
        #       state progression, we'll see state transitions after cancel.
        #       For non-final states that is not a problem, as it is equivalent
        #       with a state update message race, which our state collapse
        #       mechanism accounts for.  For an eventual non-canceled final
        #       state, we do get an invalid state transition.  That is also
        #       corrected eventually in the state collapse, but the point
        #       remains, that the state model is temporarily violated.  We
        #       consider this a side effect of the fast-cancel optimization.
        #
        #       The env variable 'RADICAL_PILOT_STRICT_CANCEL == True' will
        #       disable this optimization.
        #
        # FIXME: the effect of the env var is not well tested
        if 'RADICAL_PILOT_STRICT_CANCEL' not in os.environ:
            with self._units_lock:
                units = [self._units[uid] for uid  in uids ]
            unit_docs = [unit.as_dict()   for unit in units]
            self.advance(unit_docs, state=rps.CANCELED, publish=True, push=True)

        # we *always* issue the cancellation command!
        self.publish(rpc.CONTROL_PUBSUB, {'cmd' : 'cancel_units', 
                                          'arg' : {'uids' : uids}})

        # In the default case of calling 'advance' above, we just set the state,
        # so we *know* units are canceled.  But we nevertheless wait until that
        # state progression trickled through, so that the application will see
        # the same state on unit inspection.
        self.wait_units(uids=uids)


    # --------------------------------------------------------------------------
    #
    def register_callback(self, cb, metric=rpt.UNIT_STATE, cb_data=None):
        """
        Registers a new callback function with the UnitManager.  Manager-level
        callbacks get called if the specified metric changes.  The default
        metric `UNIT_STATE` fires the callback if any of the ComputeUnits
        managed by the PilotManager change their state.

        All callback functions need to have the same signature::

            def cb(obj, value, cb_data)

        where ``object`` is a handle to the object that triggered the callback,
        ``value`` is the metric, and ``data`` is the data provided on
        callback registration..  In the example of `UNIT_STATE` above, the
        object would be the unit in question, and the value would be the new
        state of the unit.

        Available metrics are:

          * `UNIT_STATE`: fires when the state of any of the units which are
            managed by this unit manager instance is changing.  It communicates
            the unit object instance and the units new state.

          * `WAIT_QUEUE_SIZE`: fires when the number of unscheduled units (i.e.
            of units which have not been assigned to a pilot for execution)
            changes.
        """

        # FIXME: the signature should be (self, metrics, cb, cb_data)

        if  metric not in rpt.UMGR_METRICS :
            raise ValueError ("Metric '%s' is not available on the unit manager" % metric)

        with self._cb_lock:
            self._callbacks[metric].append([cb, cb_data])


# ------------------------------------------------------------------------------

