
__copyright__ = "Copyright 2016, http://radical.rutgers.edu"
__license__   = "MIT"


import time
import threading

import radical.utils as ru

from .. import utils     as rpu
from .. import states    as rps
from .. import constants as rpc


# ==============================================================================
#
DEFAULT_BULK_COLLECTION_TIME = 1.0 # seconds


# ==============================================================================
#
class Update(rpu.Worker):
    """
    An UpdateWorker pushes CU and Pilot state updates to mongodb.  Its instances
    compete for update requests on the update_queue.  Those requests will be
    triplets of collection name, query dict, and update dict.  Update requests
    will be collected into bulks over some time (BULK_COLLECTION_TIME), to
    reduce number of roundtrips.
    """

    # --------------------------------------------------------------------------
    #
    def __init__(self, cfg, session=None):

        rpu.Worker.__init__(self, 'UpdateWorker', cfg, session)


    # --------------------------------------------------------------------------
    #
    @classmethod
    def create(cls, cfg, session=None):

        return cls(cfg, session)


    # --------------------------------------------------------------------------
    #
    def initialize_child(self):

        self._session_id    = self._cfg['session_id']
        self._mongodb_url   = self._cfg['mongodb_url']
        self._owner         = self._cfg['owner']

        _, db, _, _, _      = ru.mongodb_connect(self._mongodb_url)
        self._mongo_db      = db
        self._cinfo         = dict()            # collection cache
        self._lock          = threading.RLock() # protect _cinfo
        self._state_cache   = dict()            # used to preserve state ordering

        self.declare_subscriber('state', 'state_pubsub', self.state_cb)
        self.declare_idle_cb(self.idle_cb, timeout=self._cfg.get('bulk_collection_time'))

        # all components use the command channel for control messages
        self.declare_publisher ('command', rpc.COMMAND_PUBSUB)

        # communicate successful startup
        self.publish('command', {'cmd' : 'alive',
                                 'arg' : self.cname})


    # --------------------------------------------------------------------------
    #
    def finalize_child(self):

        # communicate finalization
        self.publish('command', {'cmd' : 'final',
                                 'arg' : self.cname})


    # --------------------------------------------------------------------------
    #
    def _ordered_unit_update(self, unit, state, timestamp=None):
        """
        The update worker can receive states for a specific unit in any order.
        If states are pushed straight to theh DB, the state attribute of a unit
        may not reflect the actual state.  This should be avoided by re-ordering
        on the client side DB consumption -- but until that is implemented we
        enforce ordered state pushes to MongoDB.  We do it like this:

          - for each unit arriving in the update worker
            - check if new state is final
              - yes: push update, but never push any update again (only update
                hist)
              - no:
                check if all expected earlier states are pushed already
                - yes: push this state also
                - no:  only update state history
        """
        # FIXME: this is specific to agent side updates, but needs to be
        #        generalized for the full state model

        s2i = {rps.NEW                          :  0,

               rps.PENDING                      :  1,
               rps.PENDING_LAUNCH               :  2,
               rps.LAUNCHING                    :  3,
               rps.PENDING_ACTIVE               :  4,
               rps.ACTIVE                       :  5,

               rps.UNSCHEDULED                  :  6,
               rps.SCHEDULING                   :  7,
               rps.PENDING_INPUT_STAGING        :  8,
               rps.STAGING_INPUT                :  9,
               rps.AGENT_STAGING_INPUT_PENDING  : 10,
               rps.AGENT_STAGING_INPUT          : 11,
               rps.ALLOCATING_PENDING           : 12,
               rps.ALLOCATING                   : 13,
               rps.EXECUTING_PENDING            : 14,
               rps.EXECUTING                    : 15,
               rps.AGENT_STAGING_OUTPUT_PENDING : 16,
               rps.AGENT_STAGING_OUTPUT         : 17,
               rps.PENDING_OUTPUT_STAGING       : 18,
               rps.STAGING_OUTPUT               : 19,

               rps.DONE                         : 20,
               rps.CANCELING                    : 21,
               rps.CANCELED                     : 22,
               rps.FAILED                       : 23
               }
        i2s = {v:k for k,v in s2i.items()}
        s_max = rps.FAILED

        if not timestamp:
            timestamp = rpu.timestamp()

        # we always push state history
        update_dict = {'$push': {
                           'statehistory': {
                               'state'    : state,
                               'timestamp': timestamp}}}
        uid = unit['uid']

      # self._log.debug(" === inp %s: %s" % (uid, state))

        if uid not in self._state_cache:
            self._state_cache[uid] = {'unsent' : list(),
                                      'final'  : False,
                                      'last'   : rps.AGENT_STAGING_INPUT_PENDING}
                                      # we get the unit in this state
        cache = self._state_cache[uid]

        # if unit is already final, we don't push state
        if cache['final']:
          # self._log.debug(" === fin %s: %s" % (uid, state))
            return update_dict

        # if unit becomes final, push state and remember it
        if state in [rps.DONE, rps.FAILED, rps.CANCELED]:
            cache['final'] = True
            cache['last']  = state
            update_dict['$set'] = {'state': state}
          # self._log.debug(" === Fin %s: %s" % (uid, state))
            return update_dict

        # check if we have any consecutive list beyond 'last' in unsent
        cache['unsent'].append(state)
      # self._log.debug(" === lst %s: %s %s" % (uid, cache['last'], cache['unsent']))
        new_state = None
        for i in range(s2i[cache['last']]+1, s2i[s_max]):
          # self._log.debug(" === chk %s: %s in %s" % (uid, i2s[i], cache['unsent']))
            if i2s[i] in cache['unsent']:
                new_state = i2s[i]
                cache['unsent'].remove(i2s[i])
              # self._log.debug(" === uns %s: %s" % (uid, new_state))
            else:
              # self._log.debug(" === brk %s: %s" % (uid, new_state))
                break

        if new_state:
          # self._log.debug(" === new %s: %s" % (uid, new_state))
            state = new_state

        # the max of the consecutive list is set in te update dict...
        if state:
          # self._log.debug(" === set %s: %s" % (uid, state))
            cache['last'] = state
            update_dict['$set'] = {'state': state}

        # record if final state is sent
        if state in [rps.DONE, rps.FAILED, rps.CANCELED]:
          # self._log.debug(" === FIN %s: %s" % (uid, state))
            cache['final'] = True

        return update_dict


    # --------------------------------------------------------------------------
    #
    def _timed_bulk_execute(self, cinfo, flush=False):

        # is there any bulk to look at?
        if not cinfo['bulk']:
            return False

        now = time.time()
        age = now - cinfo['last']

        # only push if collection time has been exceeded
        if not age > self._cfg.get('bulk_collection_time', DEFAULT_BULK_COLLECTION_TIME):
            return False

        res = cinfo['bulk'].execute()
        self._log.debug("bulk update result: %s", res)

        self._prof.prof('unit update bulk pushed (%d)' % len(cinfo['uids']),
                        uid=self._owner)
        for entry in cinfo['uids']:
            uid   = entry[0]
            state = entry[1]
            if state:
                self._prof.prof('update', msg='unit update pushed (%s)' % state, uid=uid)
            else:
                self._prof.prof('update', msg='unit update pushed', uid=uid)

        cinfo['last'] = now
        cinfo['bulk'] = None
        cinfo['uids'] = list()

        return True


    # --------------------------------------------------------------------------
    #
    def idle_cb(self):

        action = 0
        with self._lock:
            for cname in self._cinfo:
                action += self._timed_bulk_execute(self._cinfo[cname])

        return bool(action)


    # --------------------------------------------------------------------------
    #
    def state_cb(self, topic, msg):
        """
        'msg' is expected to be of the form ['cmd', 'thing'], where 'thing' is
        an entity to update in the DB, and 'cmd' specifies the mode of update.

        'things' are expected to be dicts with a 'type' and 'uid' field.  If
        either one does not exist, an exception is raised.

        Supported types are:

          - session
          - umgr
          - unit
          - pmgr
          - pilot

        supported 'cmds':

          - insert      : insert can be delayed until bulk is collected/flushed
          - delete      : delete can be delayed until bulk is collected/flushed
          - update      : update can be delayed until bulk is collected/flushed
          - state       : update can be delayed until bulk is collected/flushed
                          only state and state history are updated
          - insert_flush: insert is send immediately (possibly in a bulk)
          - delete_flush: delete is send immediately (possibly in a bulk)
          - update_flush: update is send immediately (possibly in a bulk)
          - state_flush : update is send immediately (possibly in a bulk)
                          only state and state history are updated

        The 'thing' can contains '$set' and '$push' fields, which will then be
        used as given.  For all other fields, we use the following convention:

          - scalar values: use '$set'
          - dict   values: use '$set'
          - list   values: use '$push'

        That implies that all potential 'list' types should be defined in the
        initial 'thing' insert as such, as (potentially empty) lists.

        For 'cmd' in ['state', 'state_flush'], only the 'uid' and 'state' fields
        of the given 'thing' are used, all other fields are ignored.  If 'state'
        does not exist, an exception is raised.
        """

        try:
            cmd, thing = msg
        except Exception as e:
            import pprint
            raise RuntimeError('protocol error %s: \n%s\n' % (type(msg), pprint.pformat(msg)))

        # FIXME: we don't have any error recovery -- any failure to update unit
        #        state in the DB will thus result in an exception here and tear
        #        down the pilot.
        #
        # FIXME: at the moment, the update worker only operates on units.
        #        it should also accept other updates, eg. for pilot states.
        #
        # got a new request.  Add to bulk (create as needed),
        # and push bulk if time is up.
        uid       = thing['uid']
        state     = thing.get('state')
        timestamp = thing.get('state_timestamp', rpu.timestamp())

        if 'clone' in uid:
            return

        self._prof.prof('get', msg="update unit state to %s" % state, uid=uid)

        query_dict  = thing.get('query')
        update_dict = thing.get('update')

        if not query_dict:
            query_dict  = {'uid' : uid} # make sure unit is not final?
        if not update_dict:
            update_dict = self._ordered_unit_update (thing, state, timestamp)

        # when the unit is about to leave the agent, we also update stdout,
        # stderr exit code etc
        # FIXME: this probably should be a parameter ('FULL') on 'msg'
        # FIXME: this should only be done by the agent, not the UMGR
        if state in [rps.DONE, rps.FAILED, rps.CANCELED, rps.PENDING_OUTPUT_STAGING]:
            if not '$set' in update_dict:
                update_dict['$set'] = dict()
            update_dict['$set']['stdout'   ] = thing.get('stdout')
            update_dict['$set']['stderr'   ] = thing.get('stderr')
            update_dict['$set']['exit_code'] = thing.get('exit_code')

        # check if we handled the collection before.  If not, initialize
        cname = self._session_id

        with self._lock:
            if not cname in self._cinfo:
                self._cinfo[cname] = {
                        'coll' : self._mongo_db[cname],
                        'bulk' : None,
                        'last' : time.time(),  # time of last push
                        'uids' : list()
                        }


            # check if we have an active bulk for the collection.  If not,
            # create one.
            cinfo = self._cinfo[cname]

            if not cinfo['bulk']:
                cinfo['bulk'] = cinfo['coll'].initialize_ordered_bulk_op()


            # push the update request onto the bulk
            cinfo['uids'].append([uid, state])
            cinfo['bulk'].find  (query_dict) \
                         .update(update_dict)
            self._prof.prof('bulk', msg='bulked (%s)' % state, uid=uid)

            # attempt a timed update
            self._timed_bulk_execute(cinfo)


# ------------------------------------------------------------------------------
