################################################################################
#
# This program is part of the SQLDataSource Zenpack for Zenoss.
# Copyright (C) 2010 Egor Puzanov.
#
# This program can be used under the GNU General Public License version 2
# You can find full information here: http://www.zenoss.com/oss
#
################################################################################

__doc__="""info.py

Representation of Data Source.

$Id: info.py,v 1.0 2010/05/31 18:53:58 egor Exp $"""

__version__ = "$Revision: 1.0 $"[11:-2]

from zope.interface import implements
from Products.Zuul.infos import ProxyProperty
from Products.Zuul.infos.template import InfoBase
from ZenPacks.community.SQLDataSource.interfaces import ISQLDataSourceInfo


class SQLDataSourceInfo(InfoBase):
    implements(ISQLDataSourceInfo)

    def __init__(self, dataSource):
        self._object = dataSource

    @property
    def id(self):
        return '/'.join(self._object.getPrimaryPath())

    @property
    def source(self):
        return self._object.getDescription()

    @property
    def type(self):
        return self._object.sourcetype

    enabled = ProxyProperty('enabled')
    cs = ProxyProperty('cs')
    sql = ProxyProperty('sql')

    @property
    def testable(self):
        """
        We can NOT test this datsource against a specific device
        """
        return True


