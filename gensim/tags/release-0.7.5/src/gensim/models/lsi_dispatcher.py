#!/usr/bin/env python
# -*- coding: utf-8 -*-
#
# Copyright (C) 2010 Radim Rehurek <radimrehurek@seznam.cz>
# Licensed under the GNU LGPL v2.1 - http://www.gnu.org/licenses/lgpl.html

"""
USAGE: %(program)s SIZE_OF_JOBS_QUEUE

    Dispatcher process which orchestrates distributed LSI computations. Run this \
script only once, on any node in your cluster.

Example: python -m gensim.models.lsi_dispatcher
"""


from __future__ import with_statement
import os, sys, logging, threading, time
from Queue import Queue

import Pyro
import Pyro.config

from gensim import utils


logger = logging.getLogger("dispatcher")
logger.setLevel(logging.INFO)


# How many jobs (=chunks of N documents) to keep "pre-fetched" in a queue?
# A small number is usually enough, unless iteration over the corpus is very very 
# slow (slower than the actual computation of LSI), in which case you can override 
# this value from command line. ie. run "python ./lsi_dispatcher.py 100" 
MAX_JOBS_QUEUE = 10

# timeout for the Queue object put/get blocking methods.
# it should really be infinity, but then keyboard interrupts don't work.
# so this is really just a hack, see http://bugs.python.org/issue1360
HUGE_TIMEOUT = 365 * 24 * 60 * 60 # one year



class Dispatcher(object):
    """
    Dispatcher object that communicates and coordinates individual workers.
    
    There should never be more than one dispatcher running at any one time.
    """
    
    def __init__(self, maxsize = 100):
        """
        Note that the constructor does not fully initialize the dispatcher;
        use the `initialize()` function to populate it with workers etc.
        """
        self.maxsize = maxsize
        self.callback = None # a pyro proxy to this object (unknown at init time, but will be set later)
    
    
    def initialize(self, **model_params):
        """
        `model_params` are parameters used to initialize individual workers (gets
        handed all the way down to worker.initialize()).
        """
        self.jobs = Queue(maxsize = self.maxsize)
        self.lock_update = threading.Lock()
        self.callback._pyroOneway.add("jobdone") # make sure workers transfer control back to dispatcher asynchronously        
        self._jobsdone = 0

        # locate all available workers and store their proxies, for subsequent RMI calls
        self.workers = {}
        with Pyro.naming.locateNS() as ns:
            for name, uri in ns.list(prefix = 'gensim.lsi_worker').iteritems():
                try:
                    worker = Pyro.core.Proxy(uri)
                    workerid = len(self.workers)
                    # make time consuming methods work asynchronously
                    worker._pyroOneway.add("requestjob")
                    worker._pyroOneway.add("exit")
                    logger.info("registering worker #%i at %s" % (workerid, uri))
                    worker.initialize(workerid, dispatcher = self.callback, **model_params)
                    self.workers[workerid] = worker
                    worker.requestjob()
                except Pyro.errors.PyroError, err:
                    logger.warning("unresponsive worker at %s, deleting it from the name server" % uri)
                    ns.remove(name)
        
        if len(self.workers) == 0:
            raise RuntimeError('no workers found; run some lsi_worker scripts on your machines first!')


    def getworkers(self):
        """
        Return pyro URIs of all registered workers.
        """
        return [worker._pyroUri for worker in self.workers.itervalues()]


    def getjob(self, worker_id):
        logger.info("worker #%i requesting a new job" % worker_id)
        return self.jobs.get(block = True, timeout = HUGE_TIMEOUT)


    def putjob(self, job):
        self.jobs.put(job, block = True, timeout = HUGE_TIMEOUT)
        logger.info("added a new job (len(queue)=%i items)" % self.jobs.qsize())

    
    def getstate(self):
        """
        Merge projections from across all workers and return the final projection.
        """
        logger.info("end of input, assigning all remaining jobs")
        while not self.jobs.empty():
            time.sleep(0.5) # check every half a second
        
        # TODO: merge in parallel, so that we're done in `log_2(workers)` merges, 
        # and not `workers - 1` merges!
        # but merging only takes place once, after all input data has been processed,
        # so the overall effect would be small... compared to the amount of coding :-)
        logger.info("merging states from %i workers" % len(self.workers))
        workers = self.workers.items()
        result = workers[0][1].getstate()
        for workerid, worker in workers[1:]:
            logger.info("pulling state from worker %s" % workerid)
            result.merge(worker.getstate())
        logger.info("sending out merged projection")
        return result

    
    @utils.synchronous('lock_update')
    def jobdone(self, workerid):
        """
        A worker has finished its job. Log this event and then asynchronously 
        transfer control back to the worker.
        
        In this way, control flow basically oscillates between dispatcher.jobdone()
        worker.requestjob().
        """
        self._jobsdone += 1
        logger.info("worker #%s finished job #%i" % (workerid, self._jobsdone))
        worker = self.workers[workerid]
        worker.requestjob() # tell the worker to ask for another job, asynchronously (one-way)

    
    def jobsdone(self):
        """Wrap self._jobsdone, needed for remote access through proxies"""
        return self._jobsdone

    
    def exit(self):
        """
        Terminate all registered workers and then the dispatcher.
        """
        for workerid, worker in self.workers.iteritems():
            logger.info("terminating worker %s" % workerid)
            worker.exit()
        logger.info("terminating dispatcher")
        os._exit(0) # exit the whole process (not just this thread ala sys.exit())
#endclass Dispatcher



def main():
    logging.basicConfig(format = '%(asctime)s : %(levelname)s : %(message)s')
    logger.info("running %s" % " ".join(sys.argv))

    program = os.path.basename(sys.argv[0])
    # make sure we have enough cmd line parameters
    if len(sys.argv) < 1:
        print globals()["__doc__"] % locals()
        sys.exit(1)
    
    if len(sys.argv) < 2:
        maxsize = MAX_JOBS_QUEUE
    else:
        maxsize = int(sys.argv[1])
    
    Pyro.config.HOST = utils.get_my_ip()
    
    with Pyro.naming.locateNS() as ns:
        with Pyro.core.Daemon() as daemon:
            dispatcher = Dispatcher(maxsize = maxsize)
            uri = daemon.register(dispatcher)
            # prepare callback object for the workers
            dispatcher.callback = Pyro.core.Proxy(uri)
            
            name = 'gensim.lsi_dispatcher'
            ns.remove(name)
            ns.register(name, uri)
            logger.info("dispatcher is ready at URI %s" % uri)
            daemon.requestLoop()

    logger.info("finished running %s" % program)
    


if __name__ == '__main__':
    main()
