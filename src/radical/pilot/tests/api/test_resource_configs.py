""" (Compute) Unit tests
"""
import os
import sys
import radical.pilot
import unittest

import uuid
from copy import deepcopy
from radical.pilot.db import Session
from pymongo import MongoClient

# DBURL defines the MongoDB server URL and has the format mongodb://host:port.
# For the installation of a MongoDB server, refer to the MongoDB website:
# http://docs.mongodb.org/manual/installation/
DBURL = os.getenv("RADICAL_PILOT_DBURL")
if DBURL is None:
    print "ERROR: radical.pilot_DBURL (MongoDB server URL) is not defined."
    sys.exit(1)
    
DBNAME = 'radicalpilot_unittests'


#-----------------------------------------------------------------------------
#
class TestResourceConfigs(unittest.TestCase):
    # silence deprecation warnings under py3

    def setUp(self):
        # clean up fragments from previous tests
        client = MongoClient(DBURL)
        client.drop_database(DBNAME)

    def tearDown(self):
        # clean up after ourselves 
        client = MongoClient(DBURL)
        client.drop_database(DBNAME)

    def failUnless(self, expr):
        # St00pid speling.
        return self.assertTrue(expr)

    def failIf(self, expr):
        # St00pid speling.
        return self.assertFalse(expr)

    #-------------------------------------------------------------------------
    #
    def test__add_resource_config_1(self):
        """ Test if we can wait for different pilot states.
        """
        session = radical.pilot.Session(database_url=DBURL)

        pm = radical.pilot.PilotManager(session=session)
        len1 = len(session.list_resource_configs())

        rc = radical.pilot.ResourceConfig()
        rc.name = "test"

        session.add_resource_config(rc)
        assert len1+1 == len(session.list_resource_configs())
        assert "test" in session.list_resource_configs()

        session.close()

    #-------------------------------------------------------------------------
    #
    def test__add_resource_config_2(self):
        """ Test if we can wait for different pilot states.
        """
        session = radical.pilot.Session(database_url=DBURL)

        rc = radical.pilot.ResourceConfig()
        rc.name = "mylocalhost"
        rc.remote_job_manager_endpoint = "fork://localhost"
        rc.remote_filesystem_endpoint = "file://localhost/"
        rc.bootstrapper = "default_bootstrapper.sh"
        rc.pilot_agent = "radical-pilot-agent-singlecore.py"

        pm = radical.pilot.PilotManager(session=session)
        session.add_resource_config(rc)

        pd = radical.pilot.ComputePilotDescription()
        pd.resource = "mylocalhost"
        pd.cores    = 1
        pd.runtime  = 1 # minutes
        pd.sandbox = "/tmp/radical.pilot.sandbox.unittests"
        pd.cleanup = True

        pilot = pm.submit_pilots(pd)
        pilot.wait(timeout=5*60)
        pilot.cancel()

        session.close()

