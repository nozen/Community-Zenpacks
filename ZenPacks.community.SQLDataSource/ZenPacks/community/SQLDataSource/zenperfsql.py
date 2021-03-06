################################################################################
#
# This program is part of the SQLDataSource Zenpack for Zenoss.
# Copyright (C) 2010 Egor Puzanov.
#
# This program can be used under the GNU General Public License version 2
# You can find full information here: http://www.zenoss.com/oss
#
################################################################################

__doc__="""zenperfsql

PB daemon-izable base class for creating sql collectors

$Id: zenperfsql.py,v 1.2 2010/09/02 21:47:26 egor Exp $"""

__version__ = "$Revision: 1.2 $"[11:-2]

import logging

import Globals
import zope.component
import zope.interface
from DateTime import DateTime

from twisted.internet import defer, reactor
from twisted.python.failure import Failure

from Products.ZenCollector.daemon import CollectorDaemon
from Products.ZenCollector.interfaces import ICollectorPreferences,\
                                             IDataService,\
                                             IEventService,\
                                             IScheduledTask
from Products.ZenCollector.tasks import SimpleTaskFactory,\
                                        SimpleTaskSplitter,\
                                        TaskStates
from Products.ZenEvents.ZenEventClasses import Error, Clear
from Products.ZenUtils.observable import ObservableMixin
from SQLClient import SQLClient

# We retrieve our configuration data remotely via a Twisted PerspectiveBroker
# connection. To do so, we need to import the class that will be used by the
# configuration service to send the data over, i.e. DeviceProxy.
from Products.ZenUtils.Utils import unused
from Products.ZenCollector.services.config import DeviceProxy
unused(DeviceProxy)

#
# creating a logging context for this module to use
#
log = logging.getLogger("zen.zenperfsql")

#
# RPN reverse calculation
#
import operator

OPERATORS = {
    '-': operator.add,
    '+': operator.sub,
    '/': operator.mul,
    '*': operator.truediv,
}

def rrpn(expression, value):
    oper = None
    try:
        stack = [float(value)]
        tokens = expression.split(',')
        tokens.reverse()
        for token in tokens:
            if token == 'now': token = DateTime()._t
            try:
                stack.append(float(token))
            except ValueError:
                if oper:
                    stack.append(OPERATORS[oper](stack.pop(-2), stack.pop()))
                oper = token
        val = OPERATORS[oper](stack.pop(-2), stack.pop())
        return val//1
    except:
        return value


# Create an implementation of the ICollectorPreferences interface so that the
# ZenCollector framework can configure itself from our preferences.
class ZenPerfSqlPreferences(object):
    zope.interface.implements(ICollectorPreferences)

    def __init__(self):
        """
        Construct a new ZenPerfSqlPreferences instance and provide default
        values for needed attributes.
        """
        self.collectorName = "zenperfsql"
        self.defaultRRDCreateCommand = None
        self.cycleInterval = 5 * 60 # seconds
        self.configCycleInterval = 20 # minutes
        self.options = None

        # the configurationService attribute is the fully qualified class-name
        # of our configuration service that runs within ZenHub
        self.configurationService = 'ZenPacks.community.SQLDataSource.services.SqlPerfConfig'

    def buildOptions(self, parser):
        parser.add_option('--debug', dest='debug', default=False,
                               action='store_true',
                               help='Increase logging verbosity.')

    def postStartup(self):
        # turn on debug logging if requested
        logseverity = self.options.logseverity

class ZenPerfSqlTask(ObservableMixin):
    zope.interface.implements(IScheduledTask)

    #counter to keep track of total queries sent
    QUERIES = 0

    STATE_SQLC_CONNECT = 'SQLC_CONNECT'
    STATE_SQLC_QUERY = 'SQLC_QUERY'
    STATE_SQLC_PROCESS = 'SQLC_PROCESS'

    def __init__(self,
                 deviceId,
                 taskName,
                 scheduleIntervalSeconds,
                 taskConfig):
        """
        Construct a new task instance to get SQL data.

        @param deviceId: the Zenoss deviceId to watch
        @type deviceId: string
        @param taskName: the unique identifier for this task
        @type taskName: string
        @param scheduleIntervalSeconds: the interval at which this task will be
               collected
        @type scheduleIntervalSeconds: int
        @param taskConfig: the configuration for this task
        """
        super(ZenPerfSqlTask, self).__init__()

        self.name = taskName
        self.configId = deviceId
        self.interval = scheduleIntervalSeconds
        self.state = TaskStates.STATE_IDLE

        self._taskConfig = taskConfig
        self._devId = deviceId
        self._manageIp = self._taskConfig.manageIp
        self._namespaces = self._taskConfig.queries.keys()
        self._queries = self._taskConfig.queries
        self._thresholds = self._taskConfig.thresholds
        self._datapoints = self._taskConfig.datapoints

        self._dataService = zope.component.queryUtility(IDataService)
        self._eventService = zope.component.queryUtility(IEventService)
        self._preferences = zope.component.queryUtility(ICollectorPreferences,
                                                        "zenperfsql")


    def _finished(self, result):
        """
        Callback activated when the task is complete so that final statistics
        on the collection can be displayed.
        """

        if not isinstance(result, Failure):
            log.debug("Device %s [%s] scanned successfully",
                      self._devId, self._manageIp)
        else:
            log.debug("Device %s [%s] scanned failed, %s",
                      self._devId, self._manageIp, result.getErrorMessage())

        # give the result to the rest of the callback/errchain so that the
        # ZenCollector framework can keep track of the success/failure rate
        return result

    def _failure(self, result, comp=None):
        """
        Errback for an unsuccessful asynchronous connection or collection 
        request.
        """
        err = result.getErrorMessage()
        log.error("Device %s: %s", self._devId, err)
        collectorName = self._preferences.collectorName
        summary = "Could not fetch data from source"

        self._eventService.sendEvent(dict(
            summary=summary,
            message=summary + " (%s)"%err,
            component=comp or collectorName,
            eventClass='/Status/PyDBAPI',
            device=self._devId,
            severity=Error,
            agent=collectorName,
            ))

        # give the result to the rest of the errback chain
        return result


    def _collectSuccessful(self, results):
        """
        Callback for a successful fetch of services from the remote device.
        """
        self.state = ZenPerfSqlTask.STATE_SQLC_PROCESS

        log.debug("Successful collection from %s [%s], results=%s",
                  self._devId, self._manageIp, results)

        for q in self._queries.values():
            ZenPerfSqlTask.QUERIES += len(q)

        if not results: return None
        collectorName = self._preferences.collectorName
        compstatus = {collectorName:Clear}
        for tableName, dps in self._datapoints.iteritems():
            for dpname, comp, expr, rrdPath, rrdType, rrdCreate, minmax in dps:
                values = []
                if comp not in compstatus: compstatus[comp] = Clear
                for d in results.get(tableName, []):
                    if isinstance(d, Failure):
                        compstatus[comp] = d
                        break
                    if len(d) == 0: continue
                    dpvalue = d.get(dpname, None)
                    if dpvalue == None or dpvalue == '': continue
                    elif type(dpvalue) is list: dpvalue = dpvalue[0]
                    elif isinstance(dpvalue, DateTime): dpvalue = dpvalue._t
                    if expr:
                        if expr.__contains__(':'):
                            for vmap in expr.split(','):
                                var, val = vmap.split(':')
                                if var.strip('"') != dpvalue: continue
                                dpvalue = int(val)
                                break
                        else:
                            dpvalue = rrpn(expr, dpvalue)
                    values.append(dpvalue)
                if dpname.endswith('_count'): value = len(values)
                elif not values: continue
                elif len(values) == 1: value = values[0]
                elif dpname.endswith('_avg'):value = sum(values) / len(values)
                elif dpname.endswith('_sum'): value = sum(values)
                elif dpname.endswith('_max'): value = max(values)
                elif dpname.endswith('_min'): value = min(values)
                elif dpname.endswith('_first'): value = values[0]
                elif dpname.endswith('_last'): value = values[-1]
                else: value = sum(values) / len(values)
                self._dataService.writeRRD( rrdPath,
                                            float(value),
                                            rrdType,
                                            rrdCreate,
                                            min=minmax[0],
                                            max=minmax[1])
        for comp, status in compstatus.iteritems():
            if status == Clear:
                self._eventService.sendEvent(dict(
                    summary="Could not fetch data from source",
                    component=comp,
                    eventClass='/Status/PyDBAPI',
                    device=self._devId,
                    severity=Clear,
                    agent=collectorName,
                    ))
            else: self._failure(status, comp)
        return results


    def _collectData(self):
        """
        Callback called after a connect or previous collection so that another
        collection can take place.
        """
        log.debug("Polling for SQL data from %s [%s]", 
                  self._devId, self._manageIp)

        self.state = ZenPerfSqlTask.STATE_SQLC_QUERY
        sqlc = SQLClient(self._taskConfig)
        d = sqlc.sortedQuery(self._queries)
        d.addCallbacks(self._collectSuccessful, self._failure)
        return d


    def cleanup(self):
        pass


    def doTask(self):
        log.debug("Scanning device %s [%s]", self._devId, self._manageIp)

        # try collecting events after a successful connect, or if we're
        # already connected

        d = self._collectData()

        # Add the _finished callback to be called in both success and error
        # scenarios. While we don't need final error processing in this task,
        # it is good practice to catch any final errors for diagnostic purposes.
        d.addCallback(self._finished)

        # returning a Deferred will keep the framework from assuming the task
        # is done until the Deferred actually completes
        return d


#
# Collector Daemon Main entry point
#
if __name__ == '__main__':
    myPreferences = ZenPerfSqlPreferences()
    myTaskFactory = SimpleTaskFactory(ZenPerfSqlTask)
    myTaskSplitter = SimpleTaskSplitter(myTaskFactory)
    daemon = CollectorDaemon(myPreferences, myTaskSplitter)
    daemon.run()
