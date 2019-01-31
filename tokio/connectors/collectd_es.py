#!/usr/bin/env python
"""Retrieve data generated by collectd and stored in Elasticsearch
"""

import copy
from . import es

BASE_QUERY = copy.deepcopy(es.BASE_QUERY)
es.mutate_query(BASE_QUERY, term='prefix', field='hostname', value='bb')

QUERY_DISK_DATA = copy.deepcopy(BASE_QUERY)
es.mutate_query(QUERY_DISK_DATA, term='term', field='plugin', value='disk')
es.mutate_query(QUERY_DISK_DATA, term='prefix', field='plugin_instance', value='nvme')
es.mutate_query(QUERY_DISK_DATA, term='regexp', field='collectd_type', value='disk_(octets|ops)')

QUERY_CPU_DATA = copy.deepcopy(BASE_QUERY)
es.mutate_query(QUERY_CPU_DATA, term='term', field='plugin', value='cpu')
es.mutate_query(QUERY_CPU_DATA, term='regexp', field='type_instance', value='(idle|user|system)')

QUERY_MEMORY_DATA = copy.deepcopy(BASE_QUERY)
es.mutate_query(QUERY_MEMORY_DATA, term='term', field='plugin', value='memory')

### Only return the following _source fields
SOURCE_FILTER = [
    '@timestamp',
    'hostname',
    'plugin',
    'collectd_type',
    'type_instance',
    'plugin_instance',
    'value',
    'longterm',
    'midterm',
    'shortterm',
    'majflt',
    'minflt',
    'if_octets',
    'if_packets',
    'if_errors',
    'rx',
    'tx',
    'read',
    'write',
    'io_time',
]


class CollectdEs(es.EsConnection):
    """collectd-Elasticsearch connection handler
    """
    def __init__(self, *args, **kwargs):
        super(CollectdEs, self).__init__(*args, **kwargs)
        self.filter_function = lambda x: x['hits']['hits']
        self.flush_every = 50000
        self.flush_function = lambda x: x

    def query_disk(self, start, end):
        """Query Elasticsearch for collectd disk plugin data.

        Args:
            start (datetime.datetime): lower bound for query (inclusive)
            end (datetime.datetime): upper bound for query (exclusive)
        """
        self._query_timeseries(QUERY_DISK_DATA, start, end)

    def query_memory(self, start, end):
        """Query Elasticsearch for collectd memory plugin data.

        Args:
            start (datetime.datetime): lower bound for query (inclusive)
            end (datetime.datetime): upper bound for query (exclusive)
        """
        self._query_timeseries(QUERY_MEMORY_DATA, start, end)

    def query_cpu(self, start, end):
        """Query Elasticsearch for collectd cpu plugin data.

        Args:
            start (datetime.datetime): lower bound for query (inclusive)
            end (datetime.datetime): upper bound for query (exclusive)
        """
        self._query_timeseries(QUERY_CPU_DATA, start, end)

    def _query_timeseries(self, query_template, start, end):
        """Map connection-wide attributes to self.query_timeseries arguments

        Args:
            query_template (dict): a query object containing at least one
                ``@timestamp`` field
            start (datetime.datetime): lower bound for query (inclusive)
            end (datetime.datetime): upper bound for query (exclusive)
        """
        return self.query_timeseries(query_template=query_template,
                                     start=start,
                                     end=end,
                                     source_filter=SOURCE_FILTER,
                                     filter_function=self.filter_function,
                                     flush_every=self.flush_every,
                                     flush_function=self.flush_function)

    def to_dataframe(self):
        """Converts self.scroll_pages to a DataFrame

        Returns:
            pandas.DataFrame: Contents of the last query's pages
        """
        return super(CollectdEs, self).to_dataframe(fields=SOURCE_FILTER)
