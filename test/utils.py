# Copyright 2012-2014 MongoDB, Inc.
#
# Licensed under the Apache License, Version 2.0 (the "License");
# you may not use this file except in compliance with the License.
# You may obtain a copy of the License at
#
# http://www.apache.org/licenses/LICENSE-2.0
#
# Unless required by applicable law or agreed to in writing, software
# distributed under the License is distributed on an "AS IS" BASIS,
# WITHOUT WARRANTIES OR CONDITIONS OF ANY KIND, either express or implied.
# See the License for the specific language governing permissions and
# limitations under the License.

"""Utilities for testing pymongo
"""

import contextlib
import os
import struct
import sys
import threading
import time
import warnings
from functools import partial

from pymongo import MongoClient
from pymongo.errors import AutoReconnect, OperationFailure
from pymongo.pool import NO_REQUEST, NO_SOCKET_YET, SocketInfo
from pymongo.server_selectors import (any_server_selector,
                                      writable_server_selector)
from test import (client_context,
                  db_user,
                  db_pwd,
                  host,
                  port)
from test.version import Version


def get_client(*args, **kwargs):
    client = MongoClient(*args, **kwargs)
    if client_context.auth_enabled and kwargs.get("connect", True):
        client.admin.authenticate(db_user, db_pwd)
    return client


def rs_or_single_client(h=host, p=port, **kwargs):
    """Connect to the replica set if there is one, otherwise the standalone.

    Authenticates if necessary.
    """
    if client_context.setname:
        return get_client(h, p, replicaSet=client_context.setname, **kwargs)
    else:
        return get_client(h, p, **kwargs)


def rs_or_single_client_noauth(h=host, p=port, **kwargs):
    """Connect to the replica set if there is one, otherwise the standalone.

    Like rs_or_single_client, but does not authenticate.
    """
    # Just call MongoClient(), not get_client() which does auth.
    if client_context.setname:
        return MongoClient(h, p, replicaSet=client_context.setname, **kwargs)
    else:
        return MongoClient(h, p, **kwargs)

def one(s):
    """Get one element of a set"""
    return next(iter(s))

def oid_generated_on_client(oid):
    """Is this process's PID in this ObjectId?"""
    pid_from_doc = struct.unpack(">H", oid.binary[7:9])[0]
    return (os.getpid() % 0xFFFF) == pid_from_doc

def delay(sec):
    # Javascript sleep() only available in MongoDB since version ~1.9
    return '''function() {
        var d = new Date((new Date()).getTime() + %s * 1000);
        while (d > (new Date())) { }; return true;
    }''' % sec

def get_command_line(client):
    command_line = client.admin.command('getCmdLineOpts')
    assert command_line['ok'] == 1, "getCmdLineOpts() failed"
    return command_line

def server_started_with_option(client, cmdline_opt, config_opt):
    """Check if the server was started with a particular option.
    
    :Parameters:
      - `cmdline_opt`: The command line option (i.e. --nojournal)
      - `config_opt`: The config file option (i.e. nojournal)
    """
    command_line = get_command_line(client)
    if 'parsed' in command_line:
        parsed = command_line['parsed']
        if config_opt in parsed:
            return parsed[config_opt]
    argv = command_line['argv']
    return cmdline_opt in argv


def server_started_with_auth(client):
    try:
        command_line = get_command_line(client)
    except OperationFailure as e:
        msg = e.details.get('errmsg', '')
        if e.code == 13 or 'unauthorized' in msg or 'login' in msg:
            # Unauthorized.
            return True
        raise

    # MongoDB >= 2.0
    if 'parsed' in command_line:
        parsed = command_line['parsed']
        # MongoDB >= 2.6
        if 'security' in parsed:
            security = parsed['security']
            # >= rc3
            if 'authorization' in security:
                return security['authorization'] == 'enabled'
            # < rc3
            return security.get('auth', False) or bool(security.get('keyFile'))
        return parsed.get('auth', False) or bool(parsed.get('keyFile'))
    # Legacy
    argv = command_line['argv']
    return '--auth' in argv or '--keyFile' in argv


def server_started_with_nojournal(client):
    command_line = get_command_line(client)

    # MongoDB 2.6.
    if 'parsed' in command_line:
        parsed = command_line['parsed']
        if 'storage' in parsed:
            storage = parsed['storage']
            if 'journal' in storage:
                return not storage['journal']['enabled']

    return server_started_with_option(client, '--nojournal', 'nojournal')


def server_is_master_with_slave(client):
    command_line = get_command_line(client)
    if 'parsed' in command_line:
        return command_line['parsed'].get('master', False)
    return '--master' in command_line['argv']

def drop_collections(db):
    for coll in db.collection_names():
        if not coll.startswith('system'):
            db.drop_collection(coll)

def remove_all_users(db):
    if Version.from_client(db.connection).at_least(2, 5, 3, -1):
        db.command("dropAllUsersFromDatabase", 1,
                   writeConcern={"w": client_context.w})
    else:
        db.system.users.remove({}, w=client_context.w)


def joinall(threads):
    """Join threads with a 5-minute timeout, assert joins succeeded"""
    for t in threads:
        t.join(300)
        assert not t.isAlive(), "Thread %s hung" % t

def connected(client):
    """Convenience to wait for a newly-constructed client to connect."""
    with warnings.catch_warnings():
        # Ignore warning that "ismaster" is always routed to primary even
        # if client's read preference isn't PRIMARY.
        warnings.simplefilter("ignore", UserWarning)
        client.admin.command('ismaster')  # Force connection.

    return client

def wait_until(predicate, success_description, timeout=10):
    """Wait up to 10 seconds (by default) for predicate to be True.

    E.g.:

        wait_until(lambda: client.primary == ('a', 1),
                   'connect to the primary')

    If the lambda-expression isn't true after 10 seconds, we raise
    AssertionError("Didn't ever connect to the primary").
    """
    start = time.time()
    while not predicate():
        if time.time() - start > timeout:
            raise AssertionError("Didn't ever %s" % success_description)

def is_mongos(client):
    res = client.admin.command('ismaster')
    return res.get('msg', '') == 'isdbgrid'

def enable_text_search(client):
    client.admin.command(
        'setParameter', textSearchEnabled=True)

    for host, port in client.secondaries:
        client = MongoClient(host, port)
        if client_context.auth_enabled:
            client.admin.authenticate(db_user, db_pwd)
        client.admin.command('setParameter', textSearchEnabled=True)

def assertRaisesExactly(cls, fn, *args, **kwargs):
    """
    Unlike the standard assertRaises, this checks that a function raises a
    specific class of exception, and not a subclass. E.g., check that
    MongoClient() raises ConnectionFailure but not its subclass, AutoReconnect.
    """
    try:
        fn(*args, **kwargs)
    except Exception as e:
        assert e.__class__ == cls, "got %s, expected %s" % (
            e.__class__.__name__, cls.__name__)
    else:
        raise AssertionError("%s not raised" % cls)

@contextlib.contextmanager
def ignore_deprecations():
    with warnings.catch_warnings():
        warnings.simplefilter("ignore", DeprecationWarning)
        yield

class RendezvousThread(threading.Thread):
    """A thread that starts and pauses at a rendezvous point before resuming.
    To be used in tests that must ensure that N threads are all alive
    simultaneously, regardless of thread-scheduling's vagaries.

    1. Write a subclass of RendezvousThread and override before_rendezvous
      and / or after_rendezvous.
    2. Create a state with RendezvousThread.shared_state(N)
    3. Start N of your subclassed RendezvousThreads, passing the state to each
      one's __init__
    4. In the main thread, call RendezvousThread.wait_for_rendezvous
    5. Test whatever you need to test while threads are paused at rendezvous
      point
    6. In main thread, call RendezvousThread.resume_after_rendezvous
    7. Join all threads from main thread
    8. Assert that all threads' "passed" attribute is True
    9. Test post-conditions
    """
    class RendezvousState(object):
        def __init__(self, nthreads):
            # Number of threads total
            self.nthreads = nthreads
    
            # Number of threads that have arrived at rendezvous point
            self.arrived_threads = 0
            self.arrived_threads_lock = threading.Lock()
    
            # Set when all threads reach rendezvous
            self.ev_arrived = threading.Event()
    
            # Set by resume_after_rendezvous() so threads can continue.
            self.ev_resume = threading.Event()
            

    @classmethod
    def create_shared_state(cls, nthreads):
        return RendezvousThread.RendezvousState(nthreads)

    def before_rendezvous(self):
        """Overridable: Do this before the rendezvous"""
        pass

    def after_rendezvous(self):
        """Overridable: Do this after the rendezvous. If it throws no exception,
        `passed` is set to True
        """
        pass

    @classmethod
    def wait_for_rendezvous(cls, state):
        """Wait for all threads to reach rendezvous and pause there"""
        state.ev_arrived.wait(10)
        assert state.ev_arrived.isSet(), "Thread timeout"
        assert state.nthreads == state.arrived_threads

    @classmethod
    def resume_after_rendezvous(cls, state):
        """Tell all the paused threads to continue"""
        state.ev_resume.set()

    def __init__(self, state):
        """Params:
          `state`: A shared state object from RendezvousThread.shared_state()
        """
        super(RendezvousThread, self).__init__()
        self.state = state
        self.passed = False

        # If this thread fails to terminate, don't hang the whole program
        self.setDaemon(True)

    def _rendezvous(self):
        """Pause until all threads arrive here"""
        s = self.state
        s.arrived_threads_lock.acquire()
        s.arrived_threads += 1
        if s.arrived_threads == s.nthreads:
            s.arrived_threads_lock.release()
            s.ev_arrived.set()
        else:
            s.arrived_threads_lock.release()
            s.ev_arrived.wait()

    def run(self):
        try:
            self.before_rendezvous()
        finally:
            self._rendezvous()

        # all threads have passed the rendezvous, wait for
        # resume_after_rendezvous()
        self.state.ev_resume.wait()

        self.after_rendezvous()
        self.passed = True

def read_from_which_host(
    client,
    pref,
    tag_sets=None,
    secondary_acceptable_latency_ms=None
):
    """Read from a client with the given Read Preference.

    Return the 'host:port' which was read from.

    :Parameters:
      - `client`: A MongoClient
      - `mode`: A ReadPreference
      - `tag_sets`: List of dicts of tags for data-center-aware reads
      - `secondary_acceptable_latency_ms`: Size of latency window
    """
    db = client.pymongo_test

    if isinstance(tag_sets, dict):
        tag_sets = [tag_sets]
    if tag_sets or secondary_acceptable_latency_ms:
        latency = secondary_acceptable_latency_ms or pref.latency_threshold_ms
        tags = tag_sets or pref.tag_sets
        pref = pref.__class__(latency, tags)

    db.read_preference = pref

    cursor = db.test.find()
    try:
        try:
            next(cursor)
        except StopIteration:
            # No documents in collection, that's fine
            pass

        return cursor._Cursor__connection_id
    except AutoReconnect:
        return None

def assertReadFrom(testcase, client, member, *args, **kwargs):
    """Check that a query with the given mode and tag_sets reads from
    the expected replica-set member.

    :Parameters:
      - `testcase`: A unittest.TestCase
      - `client`: A MongoClient
      - `member`: A host:port expected to be used
      - `mode`: A ReadPreference
      - `tag_sets` (optional): List of dicts of tags for data-center-aware reads
    """
    for _ in range(10):
        testcase.assertEqual(member, read_from_which_host(client, *args, **kwargs))

def assertReadFromAll(testcase, client, members, *args, **kwargs):
    """Check that a query with the given mode and tag_sets reads from all
    members in a set, and only members in that set.

    :Parameters:
      - `testcase`: A unittest.TestCase
      - `client`: A MongoClient
      - `members`: Sequence of host:port expected to be used
      - `mode`: A ReadPreference
      - `tag_sets` (optional): List of dicts of tags for data-center-aware reads
    """
    members = set(members)
    used = set()
    for _ in range(100):
        used.add(read_from_which_host(client, *args, **kwargs))

    testcase.assertEqual(members, used)

def get_pool(client):
    """Get the standalone, primary, or mongos pool."""
    topology = client._get_topology()
    server = topology.select_server(writable_server_selector)
    return server.pool

def get_pools(client):
    """Get all pools."""
    return [
        server.pool for server in
        client._get_topology().select_servers(any_server_selector)]

class TestRequestMixin(object):
    """Inherit from this class and from unittest.TestCase to get some
    convenient methods for testing connection pools and requests
    """
    def assertSameSock(self, pool):
        sock_info0 = pool.get_socket({}, 0, 0)
        sock_info1 = pool.get_socket({}, 0, 0)
        self.assertEqual(sock_info0, sock_info1)
        pool.maybe_return_socket(sock_info0)
        pool.maybe_return_socket(sock_info1)

    def assertDifferentSock(self, pool):
        sock_info0 = pool.get_socket({}, 0, 0)
        sock_info1 = pool.get_socket({}, 0, 0)
        self.assertNotEqual(sock_info0, sock_info1)
        pool.maybe_return_socket(sock_info0)
        pool.maybe_return_socket(sock_info1)

    def assertNoRequest(self, pool):
        self.assertEqual(NO_REQUEST, pool._get_request_state())

    def assertNoSocketYet(self, pool):
        self.assertEqual(NO_SOCKET_YET, pool._get_request_state())

    def assertRequestSocket(self, pool):
        self.assertTrue(isinstance(pool._get_request_state(), SocketInfo))

    def assertInRequestAndSameSock(self, client, pools):
        self.assertTrue(client.in_request())
        if not isinstance(pools, list):
            pools = [pools]
        for pool in pools:
            self.assertTrue(pool.in_request())
            self.assertSameSock(pool)

    def assertNotInRequestAndDifferentSock(self, client, pools):
        self.assertFalse(client.in_request())
        if not isinstance(pools, list):
            pools = [pools]
        for pool in pools:
            self.assertFalse(pool.in_request())
            self.assertDifferentSock(pool)


# Constants for run_threads and lazy_client_trial.
NTRIALS = 5
NTHREADS = 10


def run_threads(collection, target):
    """Run a target function in many threads.

    target is a function taking a Collection and an integer.
    """
    threads = []
    for i in range(NTHREADS):
        bound_target = partial(target, collection, i)
        threads.append(threading.Thread(target=bound_target))

    for t in threads:
        t.start()

    for t in threads:
        t.join(30)
        assert not t.isAlive()


def lazy_client_trial(reset, target, test, get_client):
    """Test concurrent operations on a lazily-connecting client.

    `reset` takes a collection and resets it for the next trial.

    `target` takes a lazily-connecting collection and an index from
    0 to NTHREADS, and performs some operation, e.g. an insert.

    `test` takes the lazily-connecting collection and asserts a
    post-condition to prove `target` succeeded.
    """
    collection = client_context.client.pymongo_test.test

    # Make concurrency bugs more likely to manifest.
    interval = None
    if not sys.platform.startswith('java'):
        if hasattr(sys, 'getswitchinterval'):
            interval = sys.getswitchinterval()
            sys.setswitchinterval(1e-6)
        else:
            interval = sys.getcheckinterval()
            sys.setcheckinterval(1)

    try:
        for i in range(NTRIALS):
            reset(collection)
            lazy_client = get_client()
            lazy_collection = lazy_client.pymongo_test.test
            run_threads(lazy_collection, target)
            test(lazy_collection)

    finally:
        if not sys.platform.startswith('java'):
            if hasattr(sys, 'setswitchinterval'):
                sys.setswitchinterval(interval)
            else:
                sys.setcheckinterval(interval)
